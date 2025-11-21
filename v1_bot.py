import asyncio
import logging
import sqlite3
import os
import time
import re
import shutil
import aiosqlite
from datetime import datetime
from collections import deque

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.client.default import DefaultBotProperties

import pandas as pd
import aiofiles

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8294936286:AAGfR-q_GGWIlxS4QlOwhAsJyFtSgFKKK_I")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "7975448643").split(',')))
DB_PATH = os.getenv("DB_PATH", "tyreterra.db")
MAX_STOCK_ITEMS = int(os.getenv("MAX_STOCK_ITEMS", "10000"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "52428800"))  # 50MB

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('tyreterra.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# =============================================================================
# ОПТИМИЗАЦИИ ДЛЯ НАГРУЗКИ
# =============================================================================

class Cache:
    def __init__(self, timeout=300):
        self.cache = {}
        self.timeout = timeout
    
    def get(self, key):
        if key in self.cache:
            data, timestamp = self.cache[key]
            if time.time() - timestamp < self.timeout:
                return data
            else:
                del self.cache[key]
        return None
    
    def set(self, key, data):
        self.cache[key] = (data, time.time())
    
    def clear(self):
        self.cache.clear()

cache = Cache()

class RateLimiter:
    def __init__(self, max_requests=10, window=60):
        self.requests = {}
        self.max_requests = max_requests
        self.window = window
    
    def is_limited(self, user_id):
        now = time.time()
        if user_id not in self.requests:
            self.requests[user_id] = []
        
        self.requests[user_id] = [req_time for req_time in self.requests[user_id] if now - req_time < self.window]
        
        if len(self.requests[user_id]) >= self.max_requests:
            return True
        
        self.requests[user_id].append(now)
        return False

rate_limiter = RateLimiter()

def cleanup_temp_files():
    """Очистка файлов старше 1 часа"""
    try:
        current_time = time.time()
        if not os.path.exists('temp_files'):
            return
            
        for filename in os.listdir('temp_files'):
            filepath = os.path.join('temp_files', filename)
            if os.path.isfile(filepath):
                if current_time - os.path.getmtime(filepath) > 3600:
                    os.remove(filepath)
    except Exception as e:
        logger.error(f"Error cleaning temp files: {e}")

# =============================================================================
# БАЗА ДАННЫХ (АСИНХРОННАЯ)
# =============================================================================

class AsyncDatabase:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
    
    async def init_db(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE,
                    name TEXT,
                    company_name TEXT,
                    inn TEXT,
                    phone TEXT,
                    email TEXT,
                    role TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS stock (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    sku TEXT,
                    tyre_size TEXT,
                    tyre_pattern TEXT,
                    brand TEXT,
                    country TEXT,
                    qty_available INTEGER,
                    retail_price REAL,
                    wholesale_price REAL,
                    warehouse_location TEXT,
                    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')
            
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_stock_sku ON stock(sku)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_stock_brand ON stock(brand)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_stock_user ON stock(user_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_stock_size ON stock(tyre_size)')
            
            await conn.commit()
    
    async def execute(self, query, params=()):
        async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
            cursor = await conn.execute(query, params)
            await conn.commit()
            return cursor.lastrowid
    
    async def fetchone(self, query, params=()):
        async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchone()
    
    async def fetchall(self, query, params=()):
        async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchall()
    
    async def get_user_stock_count(self, user_id):
        result = await self.fetchone(
            "SELECT COUNT(*) FROM stock WHERE user_id = ?", 
            (user_id,)
        )
        return result[0] if result else 0
    
    async def get_user_role(self, telegram_id):
        result = await self.fetchone(
            "SELECT role FROM users WHERE telegram_id = ?", 
            (telegram_id,)
        )
        return result[0] if result else None

db = AsyncDatabase()

# =============================================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# =============================================================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()

# =============================================================================
# СОСТОЯНИЯ FSM
# =============================================================================

class Registration(StatesGroup):
    waiting_for_role = State()
    waiting_for_company = State()
    waiting_for_inn = State()
    waiting_for_phone = State()
    waiting_for_email = State()

class AddStock(StatesGroup):
    waiting_for_sku = State()
    waiting_for_size = State()
    waiting_for_pattern = State()
    waiting_for_brand = State()
    waiting_for_country = State()
    waiting_for_qty = State()
    waiting_for_retail_price = State()
    waiting_for_wholesale_price = State()
    waiting_for_warehouse = State()

class SearchStock(StatesGroup):
    waiting_for_search_type = State()
    waiting_for_search_value = State()

class DeleteItem(StatesGroup):
    waiting_for_sku = State()
    confirmation = State()

class DeleteAllStock(StatesGroup):
    confirmation = State()

class AdminPanel(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_stock_id = State()
    waiting_for_edit_field = State()
    waiting_for_edit_value = State()
    waiting_for_delete_id = State()
    waiting_for_sql_query = State()
    confirmation = State()

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def is_admin(telegram_id):
    return telegram_id in ADMIN_IDS

async def check_rate_limit(user_id: int) -> bool:
    if rate_limiter.is_limited(user_id):
        return True
    return False

async def get_user_role(telegram_id):
    return await db.get_user_role(telegram_id)

def validate_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_inn(inn):
    return inn.isdigit() and len(inn) in [10, 12]

def validate_phone(phone):
    phone = phone.replace('+7', '8').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    return phone.isdigit() and len(phone) == 11 and phone.startswith('8')

# Клавиатуры
def get_role_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Дилер"), KeyboardButton(text="Покупатель")]],
        resize_keyboard=True
    )

async def get_main_keyboard(telegram_id):
    """Возвращает клавиатуру в зависимости от роли пользователя"""
    
    if is_admin(telegram_id):
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="/addstock"), KeyboardButton(text="/mystock")],
                [KeyboardButton(text="/search"), KeyboardButton(text="/deletestock")],
                [KeyboardButton(text="/deleteitem"), KeyboardButton(text="/admin")],
                [KeyboardButton(text="/help")]
            ],
            resize_keyboard=True
        )
    
    user_role = await get_user_role(telegram_id)
    
    if user_role == 'Дилер':
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="/addstock"), KeyboardButton(text="/mystock")],
                [KeyboardButton(text="/search"), KeyboardButton(text="/deletestock")],
                [KeyboardButton(text="/deleteitem"), KeyboardButton(text="/help")]
            ],
            resize_keyboard=True
        )
    else:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="/search"), KeyboardButton(text="/help")]
            ],
            resize_keyboard=True
        )

def get_search_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="SKU"), KeyboardButton(text="Типоразмер")],
            [KeyboardButton(text="Бренд"), KeyboardButton(text="Склад")],
            [KeyboardButton(text="Все")]
        ],
        resize_keyboard=True
    )

def get_confirmation_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]],
        resize_keyboard=True
    )

def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/admin_users"), KeyboardButton(text="/admin_stock")],
            [KeyboardButton(text="/admin_stats"), KeyboardButton(text="/admin_export")],
            [KeyboardButton(text="/admin_backup"), KeyboardButton(text="/admin_sql")],
            [KeyboardButton(text="/admin_edit_user"), KeyboardButton(text="/admin_edit_stock")],
            [KeyboardButton(text="/admin_delete_user"), KeyboardButton(text="/admin_delete_stock")],
            [KeyboardButton(text="/admin_clear_cache"), KeyboardButton(text="/help")]
        ],
        resize_keyboard=True
    )

async def create_search_excel(stock_items, user_role, search_type="результаты"):
    """Создает Excel файл с результатами поиска (скрывает оптовую цену для покупателей)"""
    if not stock_items:
        return None
    
    if not os.path.exists('temp_files'):
        os.makedirs('temp_files')
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"temp_files/search_{timestamp}.xlsx"
    
    # Для покупателей скрываем оптовую цену и контакты других пользователей
    if user_role == 'Покупатель':
        columns = ['sku', 'tyre_size', 'tyre_pattern', 'brand', 'country', 
                  'qty_available', 'retail_price', 'warehouse_location', 'company_name']
        
        processed_items = []
        for item in stock_items:
            processed_item = list(item[:6]) + [item[6]] + [item[8]] + [item[9]]  # Пропускаем wholesale_price и контакты
            processed_items.append(processed_item)
        
        df = pd.DataFrame(processed_items, columns=columns)
    else:
        # Для дилеров и админов показываем все данные
        columns = ['sku', 'tyre_size', 'tyre_pattern', 'brand', 'country', 
                  'qty_available', 'retail_price', 'wholesale_price', 'warehouse_location',
                  'company_name', 'phone', 'email']
        df = pd.DataFrame(stock_items, columns=columns)
    
    df.to_excel(filename, index=False, engine='openpyxl')
    return filename

# =============================================================================
# ОСНОВНЫЕ КОМАНДЫ
# =============================================================================

@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активных операций для отмены.")
        return
    
    await state.clear()
    await message.answer("❌ Операция отменена.", reply_markup=await get_main_keyboard(message.from_user.id))

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    
    user = await db.fetchone("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
    
    if not user:
        await message.answer(
            f"Добро пожаловать в Tyreterra, {user_name}!\n"
            "Давайте зарегистрируем вас в системе.\n"
            "Пожалуйста, выберите вашу роль:",
            reply_markup=get_role_keyboard()
        )
        await state.set_state(Registration.waiting_for_role)
    else:
        role = user[7]
        await message.answer(
            f"С возвращением, {user_name}!\n"
            f"Ваша роль: {role}\n"
            "Используйте команды для работы с системой:",
            reply_markup=await get_main_keyboard(user_id)
        )

@dp.message(Registration.waiting_for_role)
async def process_role(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text not in ["Дилер", "Покупатель"]:
        await message.answer("Пожалуйста, выберите роль из предложенных вариантов:")
        return
    
    await state.update_data(role=message.text, name=message.from_user.full_name)
    await message.answer("Введите название вашей компании:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Registration.waiting_for_company)

@dp.message(Registration.waiting_for_company)
async def process_company(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    await state.update_data(company_name=message.text)
    await message.answer("Введите ИНН вашей компании (10 или 12 цифр):")
    await state.set_state(Registration.waiting_for_inn)

@dp.message(Registration.waiting_for_inn)
async def process_inn(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if not validate_inn(message.text):
        await message.answer("❌ Неверный формат ИНН. Введите 10 или 12 цифр:")
        return
    
    await state.update_data(inn=message.text)
    await message.answer("Введите ваш контактный телефон (в формате 89991234567):")
    await state.set_state(Registration.waiting_for_phone)

@dp.message(Registration.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if not validate_phone(message.text):
        await message.answer("❌ Неверный формат телефона. Введите в формате 89991234567:")
        return
    
    await state.update_data(phone=message.text)
    await message.answer("Введите ваш email:")
    await state.set_state(Registration.waiting_for_email)

@dp.message(Registration.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if not validate_email(message.text):
        await message.answer("❌ Неверный формат email. Введите корректный email:")
        return
    
    user_data = await state.get_data()
    
    try:
        await db.execute(
            """INSERT INTO users 
            (telegram_id, name, company_name, inn, phone, email, role) 
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message.from_user.id, user_data['name'], user_data['company_name'], 
             user_data['inn'], user_data['phone'], message.text, user_data['role'])
        )
        
        role_permissions = ""
        if user_data['role'] == 'Дилер':
            role_permissions = "\n✅ Вы можете: загружать склад, скачивать свой склад, просматривать склад других пользователей"
        else:
            role_permissions = "\n✅ Вы можете: просматривать склад других пользователей"
        
        await message.answer(
            f"🎉 Регистрация завершена!\n\n"
            f"👤 Имя: {user_data['name']}\n"
            f"🏢 Компания: {user_data['company_name']}\n"
            f"📋 ИНН: {user_data['inn']}\n"
            f"📞 Телефон: {user_data['phone']}\n"
            f"📧 Email: {message.text}\n"
            f"🎯 Роль: {user_data['role']}"
            f"{role_permissions}\n\n"
            "Используйте команды для работы с системой:",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
        
    except Exception as e:
        logger.error(f"Registration error: {e}")
        await message.answer("❌ Произошла ошибка при регистрации. Попробуйте снова.")
    
    await state.clear()

# =============================================================================
# КОМАНДЫ ДИЛЕРОВ
# =============================================================================

@dp.message(Command("addstock"))
async def cmd_addstock(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    if user[1] != 'Дилер':
        await message.answer("❌ Только дилеры могут добавлять товары на склад")
        return
    
    stock_count = await db.get_user_stock_count(user[0])
    if stock_count >= MAX_STOCK_ITEMS:
        await message.answer(f"❌ Достигнут лимит товаров ({MAX_STOCK_ITEMS}). Удалите часть товаров чтобы добавить новые.")
        return
    
    current_state = await state.get_state()
    if current_state:
        await message.answer("⚠️ У вас есть незавершенная операция. Завершите ее или отмените командой /cancel")
        return
        
    await message.answer(
        "Давайте добавим новый товар на склад.\n"
        "Введите артикул (SKU):\n\n"
        "❌ Для отмены введите /cancel"
    )
    await state.set_state(AddStock.waiting_for_sku)

@dp.message(AddStock.waiting_for_sku)
async def process_sku(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(sku=message.text)
    await message.answer("Введите типоразмер шины (например: 195/65 R15):\n\n❌ Для отмены введите /cancel")
    await state.set_state(AddStock.waiting_for_size)

@dp.message(AddStock.waiting_for_size)
async def process_size(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(tyre_size=message.text)
    await message.answer("Введите модель шины (tyre pattern):\n\n❌ Для отмены введите /cancel")
    await state.set_state(AddStock.waiting_for_pattern)

@dp.message(AddStock.waiting_for_pattern)
async def process_pattern(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(tyre_pattern=message.text)
    await message.answer("Введите бренд шины:\n\n❌ Для отмены введите /cancel")
    await state.set_state(AddStock.waiting_for_brand)

@dp.message(AddStock.waiting_for_brand)
async def process_brand(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(brand=message.text)
    await message.answer("Введите страну производства:\n\n❌ Для отмены введите /cancel")
    await state.set_state(AddStock.waiting_for_country)

@dp.message(AddStock.waiting_for_country)
async def process_country(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(country=message.text)
    await message.answer("Введите доступное количество (только цифры):\n\n❌ Для отмены введите /cancel")
    await state.set_state(AddStock.waiting_for_qty)

@dp.message(AddStock.waiting_for_qty)
async def process_qty(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    try:
        qty = int(message.text)
        if qty <= 0:
            await message.answer("Количество должно быть положительным числом. Попробуйте снова:\n\n❌ Для отмены введите /cancel")
            return
        await state.update_data(qty_available=qty)
        await message.answer("Введите розничную цену (только цифры):\n\n❌ Для отмены введите /cancel")
        await state.set_state(AddStock.waiting_for_retail_price)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число для количества:\n\n❌ Для отмены введите /cancel")

@dp.message(AddStock.waiting_for_retail_price)
async def process_retail_price(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    try:
        retail_price = float(message.text)
        if retail_price <= 0:
            await message.answer("Цена должна быть положительным числом. Попробуйте снова:\n\n❌ Для отмены введите /cancel")
            return
        await state.update_data(retail_price=retail_price)
        await message.answer("Введите оптовую цену (только цифры):\n\n❌ Для отмены введите /cancel")
        await state.set_state(AddStock.waiting_for_wholesale_price)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число для цены:\n\n❌ Для отмены введите /cancel")

@dp.message(AddStock.waiting_for_wholesale_price)
async def process_wholesale_price(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    try:
        wholesale_price = float(message.text)
        if wholesale_price <= 0:
            await message.answer("Цена должна быть положительным числом. Попробуйте снова:\n\n❌ Для отмены введите /cancel")
            return
        await state.update_data(wholesale_price=wholesale_price)
        await message.answer("Введите расположение склада:\n\n❌ Для отмены введите /cancel")
        await state.set_state(AddStock.waiting_for_warehouse)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число для цены:\n\n❌ Для отмены введите /cancel")

@dp.message(AddStock.waiting_for_warehouse)
async def process_warehouse(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    try:
        user_data = await state.get_data()
        
        user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
        
        if user:
            user_id = user[0]
            
            await db.execute(
                """INSERT INTO stock 
                (user_id, sku, tyre_size, tyre_pattern, brand, country, qty_available, retail_price, wholesale_price, warehouse_location) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, user_data['sku'], user_data['tyre_size'], user_data['tyre_pattern'],
                 user_data['brand'], user_data['country'], user_data['qty_available'],
                 user_data['retail_price'], user_data['wholesale_price'], message.text)
            )
            
            await message.answer(
                "✅ Товар успешно добавлен на склад!\n\n"
                f"🏷️ Артикул: {user_data['sku']}\n"
                f"📏 Типоразмер: {user_data['tyre_size']}\n"
                f"🔧 Модель: {user_data['tyre_pattern']}\n"
                f"🏭 Бренд: {user_data['brand']}\n"
                f"🌍 Страна: {user_data['country']}\n"
                f"📊 Количество: {user_data['qty_available']}\n"
                f"💰 Розничная цена: {user_data['retail_price']} руб.\n"
                f"💼 Оптовая цена: {user_data['wholesale_price']} руб.\n"
                f"📍 Склад: {message.text}",
                reply_markup=await get_main_keyboard(message.from_user.id)
            )
        else:
            await message.answer("Ошибка: пользователь не найден. Используйте /start для регистрации.")
        
    except Exception as e:
        logger.error(f"Add stock error: {e}")
        await message.answer(f"❌ Произошла ошибка при добавлении товара: {str(e)}")
    
    await state.clear()

@dp.message(Command("mystock"))
async def cmd_mystock(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    try:
        user = await db.fetchone("SELECT id, name, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
        
        if not user:
            await message.answer("Сначала зарегистрируйтесь с помощью /start")
            return
        
        user_id, user_name, role = user[0], user[1], user[2]
        
        if role != 'Дилер':
            await message.answer("❌ Только дилеры могут выгружать свой склад")
            return
        
        cache_key = f"mystock_{user_id}"
        cached_data = cache.get(cache_key)
        
        if cached_data:
            filename, stock_count = cached_data
            if os.path.exists(filename):
                with open(filename, 'rb') as file:
                    await message.answer_document(
                        document=types.BufferedInputFile(file.read(), filename=f"мой_склад_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"),
                        caption=f"📊 Ваш склад ({stock_count} товаров) [КЭШ]\n👤 Пользователь: {user_name}"
                    )
                return
        
        # ИСПРАВЛЕННЫЙ ЗАПРОС - выбираем ВСЕ 10 столбцов
        stock_items = await db.fetchall(
            """SELECT sku, tyre_size, tyre_pattern, brand, country, qty_available, 
                      retail_price, wholesale_price, warehouse_location, date 
            FROM stock WHERE user_id = ? ORDER BY date DESC""",
            (user_id,)
        )
        
        if not stock_items:
            await message.answer("Ваш склад пуст. Используйте /addstock чтобы добавить товары.")
            return
        
        if not os.path.exists('temp_files'):
            os.makedirs('temp_files')
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"temp_files/stock_{user_id}_{timestamp}.xlsx"
        
        # ИСПРАВЛЕННОЕ СОЗДАНИЕ DATAFRAME
        columns = ['sku', 'tyre_size', 'tyre_pattern', 'brand', 'country', 
                  'qty_available', 'retail_price', 'wholesale_price', 'warehouse_location', 'date']
        
        df = pd.DataFrame(stock_items, columns=columns)
        df.to_excel(filename, index=False, engine='openpyxl')
        
        cache.set(cache_key, (filename, len(stock_items)))
        
        with open(filename, 'rb') as file:
            await message.answer_document(
                document=types.BufferedInputFile(file.read(), filename=f"мой_склад_{timestamp}.xlsx"),
                caption=f"📊 Ваш склад ({len(stock_items)} товаров)\n👤 Пользователь: {user_name}"
            )
            
    except Exception as e:
        logger.error(f"Error in mystock: {e}")
        await message.answer(f"❌ Ошибка при выгрузке склада: {str(e)}")

@dp.message(Command("deletestock"))
async def cmd_deletestock(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    user_id, role = user[0], user[1]
    
    if role != 'Дилер':
        await message.answer("❌ Только дилеры могут удалять свой склад")
        return
    
    stock_count = await db.fetchone("SELECT COUNT(*) FROM stock WHERE user_id = ?", (user_id,))
    
    if not stock_count or stock_count[0] == 0:
        await message.answer("❌ Ваш склад уже пуст.")
        return
    
    await message.answer(
        f"⚠️ ВНИМАНИЕ: Вы собираетесь удалить ВЕСЬ свой склад ({stock_count[0]} товаров).\n"
        "Это действие НЕЛЬЗЯ отменить!\n\n"
        "Вы уверены, что хотите продолжить?\n\n"
        "❌ Для отмены введите /cancel",
        reply_markup=get_confirmation_keyboard()
    )
    await state.set_state(DeleteAllStock.confirmation)

@dp.message(DeleteAllStock.confirmation)
async def process_delete_all_confirmation(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
    
    if message.text == 'Да':
        user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
        user_id = user[0]
        
        await db.execute("DELETE FROM stock WHERE user_id = ?", (user_id,))
        
        await message.answer(
            "✅ Весь ваш склад успешно удален!",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
    elif message.text == 'Нет':
        await message.answer(
            "❌ Удаление склада отменено.",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
    else:
        await message.answer("Пожалуйста, выберите 'Да' или 'Нет':")
        return
    
    await state.clear()

@dp.message(Command("deleteitem"))
async def cmd_deleteitem(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    user_id, role = user[0], user[1]
    
    if role != 'Дилер':
        await message.answer("❌ Только дилеры могут удалять товары")
        return
    
    await message.answer(
        "Введите SKU товара, который хотите удалить:\n\n"
        "❌ Для отмены введите /cancel",
        reply_markup=await get_main_keyboard(message.from_user.id)
    )
    await state.set_state(DeleteItem.waiting_for_sku)

@dp.message(DeleteItem.waiting_for_sku)
async def process_delete_sku(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
    
    sku = message.text
    user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
    user_id = user[0]
    
    item = await db.fetchone(
        "SELECT * FROM stock WHERE user_id = ? AND sku = ?", 
        (user_id, sku)
    )
    
    if not item:
        await message.answer(
            f"❌ Товар с SKU '{sku}' не найден в вашем складе.\n"
            "Пожалуйста, проверьте SKU и попробуйте снова:\n\n"
            "❌ Для отмены введите /cancel"
        )
        return
    
    await state.update_data(sku=sku)
    
    await message.answer(
        f"Найден товар:\n"
        f"🏷️ SKU: {item[2]}\n"
        f"📏 Типоразмер: {item[3]}\n"
        f"🏭 Бренд: {item[5]}\n"
        f"📊 Количество: {item[7]}\n\n"
        f"Вы уверены, что хотите удалить этот товар?\n\n"
        "❌ Для отмена введите /cancel",
        reply_markup=get_confirmation_keyboard()
    )
    await state.set_state(DeleteItem.confirmation)

@dp.message(DeleteItem.confirmation)
async def process_delete_confirmation(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
    
    if message.text == 'Да':
        user_data = await state.get_data()
        sku = user_data['sku']
        
        user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
        user_id = user[0]
        
        await db.execute(
            "DELETE FROM stock WHERE user_id = ? AND sku = ?", 
            (user_id, sku)
        )
        
        await message.answer(
            f"✅ Товар с SKU '{sku}' успешно удален!",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
    elif message.text == 'Нет':
        await message.answer(
            "❌ Удаление товара отменено.",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
    else:
        await message.answer("Пожалуйста, выберите 'Да' или 'Нет':")
        return
    
    await state.clear()

# =============================================================================
# КОМАНДА ПОИСКА (ДЛЯ ВСЕХ)
# =============================================================================

@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    await message.answer(
        "🔍 Поиск товаров\n"
        "Выберите параметр для поиска:\n\n"
        "❌ Для отмены введите /cancel",
        reply_markup=get_search_keyboard()
    )
    await state.set_state(SearchStock.waiting_for_search_type)

@dp.message(SearchStock.waiting_for_search_type)
async def process_search_type(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    search_param = message.text
    
    if search_param == 'Все':
        await state.update_data(search_type='all', search_value='%')
        await execute_search(message, state)
        return
    
    if search_param not in ['SKU', 'Типоразмер', 'Бренд', 'Склад']:
        await message.answer("Пожалуйста, выберите параметр из предложенных вариантов:")
        return
    
    param_map = {
        'SKU': 'sku',
        'Типоразмер': 'tyre_size', 
        'Бренд': 'brand',
        'Склад': 'warehouse_location'
    }
    
    await state.update_data(search_type=param_map[search_param])
    
    prompt_text = f"Введите {search_param.lower()} для поиска (или 'все' для всех товаров):\n\n❌ Для отмены введите /cancel"
    await message.answer(prompt_text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(SearchStock.waiting_for_search_value)

@dp.message(SearchStock.waiting_for_search_value)
async def process_search_value(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    search_data = await state.get_data()
    
    if message.text.lower() == 'все':
        search_value = '%'
    else:
        search_value = f'%{message.text}%'
    
    await state.update_data(search_value=search_value)
    await execute_search(message, state)

async def execute_search(message: Message, state: FSMContext):
    try:
        search_data = await state.get_data()
        user_role = await get_user_role(message.from_user.id)
        
        cache_key = f"search_{search_data.get('search_type', 'all')}_{search_data.get('search_value', 'all')}_{user_role}"
        cached_data = cache.get(cache_key)
        
        if cached_data:
            filename, stock_count = cached_data
            if os.path.exists(filename):
                with open(filename, 'rb') as file:
                    caption = f"🔍 Результаты поиска ({stock_count} товаров) [КЭШ]"
                    if user_role == 'Покупатель':
                        caption += "\n👀 Показаны только розничные цены"
                    
                    await message.answer_document(
                        document=types.BufferedInputFile(file.read(), filename=f"результаты_поиска_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"),
                        caption=caption
                    )
                await state.clear()
                await message.answer("Поиск завершен.", reply_markup=await get_main_keyboard(message.from_user.id))
                return
        
        if search_data['search_type'] == 'all':
            query = """SELECT s.sku, s.tyre_size, s.tyre_pattern, s.brand, s.country, 
                              s.qty_available, s.retail_price, s.wholesale_price, 
                              s.warehouse_location, u.company_name, u.phone, u.email
                       FROM stock s 
                       JOIN users u ON s.user_id = u.id 
                       ORDER BY s.date DESC"""
            params = ()
        else:
            query = f"""SELECT s.sku, s.tyre_size, s.tyre_pattern, s.brand, s.country, 
                               s.qty_available, s.retail_price, s.wholesale_price, 
                               s.warehouse_location, u.company_name, u.phone, u.email
                        FROM stock s 
                        JOIN users u ON s.user_id = u.id 
                        WHERE s.{search_data['search_type']} LIKE ?
                        ORDER BY s.date DESC"""
            params = (search_data['search_value'],)
        
        stock_items = await db.fetchall(query, params)
        
        if not stock_items:
            await message.answer(
                "❌ По вашему запросу ничего не найдено.",
                reply_markup=await get_main_keyboard(message.from_user.id)
            )
        else:
            filename = await create_search_excel(stock_items, user_role, search_data.get('search_type', 'search'))
            
            if filename:
                cache.set(cache_key, (filename, len(stock_items)))
                
                with open(filename, 'rb') as file:
                    caption = f"🔍 Результаты поиска ({len(stock_items)} товаров)"
                    if user_role == 'Покупатель':
                        caption += "\n👀 Показаны только розничные цены"
                    
                    await message.answer_document(
                        document=types.BufferedInputFile(file.read(), filename=f"результаты_поиска_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"),
                        caption=caption
                    )
                
    except Exception as e:
        logger.error(f"Search error: {e}")
        await message.answer(f"❌ Ошибка при поиске: {str(e)}")
    
    await state.clear()
    await message.answer("Поиск завершен.", reply_markup=await get_main_keyboard(message.from_user.id))

# =============================================================================
# АДМИН-ПАНЕЛЬ
# =============================================================================

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    admin_text = (
        "🛠️ <b>Админ-панель Tyreterra</b>\n\n"
        "👥 <b>Пользователи:</b>\n"
        "/admin_users - Просмотр всех пользователей\n"
        "/admin_edit_user - Редактировать пользователя\n"
        "/admin_delete_user - Удалить пользователя\n\n"
        "📦 <b>Склад:</b>\n"
        "/admin_stock - Просмотр всего склада\n"
        "/admin_edit_stock - Редактировать запись склада\n"
        "/admin_delete_stock - Удалить запись склада\n\n"
        "📊 <b>Аналитика:</b>\n"
        "/admin_stats - Статистика системы\n"
        "/admin_export - Полный экспорт данных\n\n"
        "💾 <b>Утилиты:</b>\n"
        "/admin_backup - Создать бэкап БД\n"
        "/admin_sql - Выполнить SQL запрос\n"
        "/admin_clear_cache - Очистить кэш\n\n"
        "❌ Для отмены операций используйте /cancel"
    )
    await message.answer(admin_text, reply_markup=get_admin_keyboard())

@dp.message(Command("admin_clear_cache"))
async def cmd_admin_clear_cache(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    try:
        cache.clear()
        cleanup_temp_files()
        await message.answer("✅ Кэш и временные файлы очищены!")
    except Exception as e:
        logger.error(f"Clear cache error: {e}")
        await message.answer(f"❌ Ошибка очистки кэша: {str(e)}")

# =============================================================================
# ОБРАБОТКА EXCEL ФАЙЛОВ И HELP
# =============================================================================

@dp.message(F.document)
async def handle_excel_file(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь с помощью /start")
        return
    
    user_id, role = user[0], user[1]
    
    if role != 'Дилер':
        await message.answer("❌ Только дилеры могут загружать товары через Excel")
        return
    
    if message.document.file_size > MAX_FILE_SIZE:
        await message.answer(f"❌ Файл слишком большой. Максимальный размер: {MAX_FILE_SIZE // 1024 // 1024}MB")
        return
    
    if message.document.mime_type in ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 
                                    'application/vnd.ms-excel']:
        
        try:
            file_id = message.document.file_id
            file = await bot.get_file(file_id)
            file_path = file.file_path
            
            if not os.path.exists('uploads'):
                os.makedirs('uploads')
            
            download_path = f"uploads/{message.document.file_name}"
            await bot.download_file(file_path, download_path)
            
            df = pd.read_excel(download_path)
            
            required_columns = ['sku', 'tyre_size', 'tyre_pattern', 'brand', 'country', 
                              'qty_available', 'retail_price', 'wholesale_price', 'warehouse_location']
            missing_columns = [col for col in required_columns if col not in df.columns]
            
            if missing_columns:
                await message.answer(f"❌ В файле отсутствуют колонки: {', '.join(missing_columns)}")
                return
            
            current_count = await db.get_user_stock_count(user_id)
            if current_count + len(df) > MAX_STOCK_ITEMS:
                await message.answer(f"❌ Превышен лимит товаров. Можно добавить еще {MAX_STOCK_ITEMS - current_count} товаров")
                return
            
            added_count = 0
            
            for _, row in df.iterrows():
                try:
                    await db.execute(
                        """INSERT INTO stock 
                        (user_id, sku, tyre_size, tyre_pattern, brand, country, 
                         qty_available, retail_price, wholesale_price, warehouse_location) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, str(row['sku']), str(row['tyre_size']), str(row['tyre_pattern']),
                         str(row['brand']), str(row['country']), int(row['qty_available']),
                         float(row['retail_price']), float(row['wholesale_price']), str(row['warehouse_location']))
                    )
                    added_count += 1
                except Exception as e:
                    logger.error(f"Ошибка при добавлении строки: {e}")
                    continue
            
            await message.answer(f"✅ Успешно добавлено {added_count} товаров из Excel файла!")
            
        except Exception as e:
            logger.error(f"Excel processing error: {e}")
            await message.answer(f"❌ Ошибка при обработке Excel файла: {str(e)}")
        
        try:
            os.remove(download_path)
        except:
            pass
    else:
        await message.answer("❌ Пожалуйста, отправьте файл в формате Excel (.xlsx)")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user_role = await get_user_role(message.from_user.id)
    
    if is_admin(message.from_user.id):
        help_text = (
            "🤖 <b>Tyreterra Bot - Помощь (Админ)</b>\n\n"
            "👤 <b>Основные команды:</b>\n"
            "/addstock - Добавить товар на склад\n"
            "/mystock - Скачать мой склад\n"
            "/search - Поиск товаров\n"
            "/deletestock - Удалить весь склад\n"
            "/deleteitem - Удалить товар по SKU\n\n"
            "🛠️ <b>Админ-команды:</b>\n"
            "/admin - Админ-панель\n"
            "/admin_users - Просмотр пользователей\n"
            "/admin_stock - Просмотр склада\n"
            "/admin_stats - Статистика\n"
            "/admin_export - Экспорт данных\n"
            "/admin_backup - Бэкап БД\n"
            "/admin_clear_cache - Очистка кэша\n\n"
            "❌ Отмена операций: /cancel"
        )
    elif user_role == 'Дилер':
        help_text = (
            "🤖 <b>Tyreterra Bot - Помощь (Дилер)</b>\n\n"
            "📦 <b>Управление складом:</b>\n"
            "/addstock - Добавить товар на склад\n"
            "/mystock - Скачать мой склад в Excel\n"
            "/deletestock - Удалить ВЕСЬ склад\n"
            "/deleteitem - Удалить товар по SKU\n\n"
            "🔍 <b>Поиск:</b>\n"
            "/search - Поиск товаров у других пользователей\n"
            "Показывает все цены (розничные и оптовые)\n\n"
            "📊 <b>Загрузка данных:</b>\n"
            "Можно загружать данные из Excel файла\n\n"
            "❌ <b>Отмена операций:</b>\n"
            "В любой момент можно отменить операцию командой /cancel"
        )
    else:
        help_text = (
            "🤖 <b>Tyreterra Bot - Помощь (Покупатель)</b>\n\n"
            "🔍 <b>Поиск:</b>\n"
            "/search - Поиск товаров у дилеров\n"
            "Показываются только розничные цены\n\n"
            "📞 <b>Контакты:</b>\n"
            "В результатах поиска вы увидите контакты компаний\n\n"
            "❌ <b>Отмена операций:</b>\n"
            "В любой момент можно отменить операцию командой /cancel"
        )
    
    await message.answer(help_text)

@dp.message()
async def unknown_message(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    await message.answer(
        "Неизвестная команда. Используйте /help для списка доступных команд.",
        reply_markup=await get_main_keyboard(message.from_user.id)
    )

# =============================================================================
# ФОНОВЫЕ ЗАДАЧИ И ЗАПУСК
# =============================================================================

async def periodic_cleanup():
    while True:
        try:
            await asyncio.sleep(3600)
            cleanup_temp_files()
            logger.info("✅ Автоочистка временных файлов выполнена")
        except Exception as e:
            logger.error(f"❌ Ошибка в фоновой очистке: {e}")

async def main():
    logger.info("Бот Tyreterra запускается...")
    
    await db.init_db()
    logger.info("✅ База данных инициализирована")
    
    for folder in ['temp_files', 'uploads']:
        if not os.path.exists(folder):
            os.makedirs(folder)
    
    asyncio.create_task(periodic_cleanup())
    logger.info("✅ Фоновая очистка временных файлов запущена")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())