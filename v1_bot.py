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
from typing import Dict, List, Set

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
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
# БАЗА ДАННЫХ (АСИНХРОННАЯ) С МИГРАЦИЯМИ
# =============================================================================

class AsyncDatabase:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
    
    async def init_db(self):
        """Инициализация базы данных"""
        try:
            async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
                # Таблица пользователей
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
                
                # Таблица склада
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
                
                # Таблица подписок
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        subscription_type TEXT,
                        subscription_value TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users (id)
                    )
                ''')
                
                # Индексы
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_stock_sku ON stock(sku)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_stock_brand ON stock(brand)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_stock_user ON stock(user_id)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_stock_size ON stock(tyre_size)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_subscriptions_type ON subscriptions(subscription_type)')
                
                await conn.commit()
                logger.info("✅ Database tables created successfully")
        except Exception as e:
            logger.error(f"❌ Database initialization error: {e}")
            raise
    
    async def migrate_database(self):
        """Миграция базы данных - добавление недостающих колонок и исправления"""
        try:
            async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
                # Проверяем существование колонок в таблице stock
                cursor = await conn.execute("PRAGMA table_info(stock)")
                columns = await cursor.fetchall()
                column_names = [column[1] for column in columns]
                
                # Добавляем created_at если нет
                if 'created_at' not in column_names:
                    logger.info("Adding created_at column to stock table...")
                    await conn.execute('''
                        ALTER TABLE stock ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    ''')
                    await conn.commit()
                    logger.info("✅ created_at column added to stock table")
                
                # Проверяем таблицу users
                cursor = await conn.execute("PRAGMA table_info(users)")
                users_columns = await cursor.fetchall()
                users_column_names = [column[1] for column in users_columns]
                
                if 'created_at' not in users_column_names:
                    logger.info("Adding created_at column to users table...")
                    await conn.execute('''
                        ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    ''')
                    await conn.commit()
                    logger.info("✅ created_at column added to users table")
                
                logger.info("✅ Database migration completed")
                
        except Exception as e:
            logger.error(f"❌ Migration error: {e}")
    
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
    
    async def get_user_subscriptions(self, user_id: int) -> List[tuple]:
        """Получить подписки пользователя"""
        return await self.fetchall(
            "SELECT id, subscription_type, subscription_value FROM subscriptions WHERE user_id = ?",
            (user_id,)
        )
    
    async def add_subscription(self, user_id: int, sub_type: str, sub_value: str):
        """Добавить подписку"""
        await self.execute(
            "INSERT INTO subscriptions (user_id, subscription_type, subscription_value) VALUES (?, ?, ?)",
            (user_id, sub_type, sub_value)
        )
    
    async def remove_subscription(self, subscription_id: int):
        """Удалить подписку"""
        await self.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
    
    async def get_subscribers(self, sub_type: str, sub_value: str) -> List[int]:
        """Получить пользователей подписанных на определенные уведомления"""
        result = await self.fetchall(
            "SELECT DISTINCT u.telegram_id FROM users u JOIN subscriptions s ON u.id = s.user_id WHERE s.subscription_type = ? AND s.subscription_value = ?",
            (sub_type, sub_value)
        )
        return [row[0] for row in result]
    
    async def search_stock_items(self, user_id: int, search_term: str) -> List[tuple]:
        """Поиск товаров пользователя по SKU, типоразмеру или модели"""
        search_term = f'%{search_term}%'
        return await self.fetchall("""
            SELECT id, sku, tyre_size, tyre_pattern, brand, qty_available 
            FROM stock 
            WHERE user_id = ? AND (sku LIKE ? OR tyre_size LIKE ? OR tyre_pattern LIKE ? OR brand LIKE ?)
            ORDER BY 
                CASE 
                    WHEN sku LIKE ? THEN 1
                    WHEN tyre_size LIKE ? THEN 2
                    WHEN tyre_pattern LIKE ? THEN 3
                    WHEN brand LIKE ? THEN 4
                    ELSE 5
                END
            LIMIT 20
        """, (user_id, search_term, search_term, search_term, search_term, 
              search_term.replace('%', ''), search_term.replace('%', ''), 
              search_term.replace('%', ''), search_term.replace('%', '')))
    
    async def delete_stock_item(self, item_id: int):
        """Удалить товар по ID"""
        await self.execute("DELETE FROM stock WHERE id = ?", (item_id,))
    
    async def delete_all_user_stock(self, user_id: int):
        """Удалить все товары пользователя"""
        await self.execute("DELETE FROM stock WHERE user_id = ?", (user_id,))

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
    waiting_for_combined_search = State()

class DeleteStock(StatesGroup):
    waiting_for_search = State()
    waiting_for_selection = State()
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

class EditProfile(StatesGroup):
    waiting_for_field = State()
    waiting_for_new_value = State()

class SubscriptionState(StatesGroup):
    waiting_for_type = State()
    waiting_for_value = State()

class UploadStock(StatesGroup):
    waiting_for_file = State()

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

def normalize_tyre_size(size: str) -> str:
    """Нормализация типоразмера для поиска"""
    if not size:
        return ""
    
    # Убираем лишние пробелы, приводим к верхнему регистру
    size = size.upper().strip()
    
    # Заменяем точки на слеши, убираем лишние пробелы вокруг R
    size = re.sub(r'[\.]', '/', size)  # Заменяем точки на слеши
    size = re.sub(r'\s*R\s*', 'R', size)  # Убираем пробелы вокруг R
    size = re.sub(r'\s+', ' ', size)  # Заменяем множественные пробелы на один
    
    return size

def size_matches(search_size: str, stock_size: str) -> bool:
    """Проверяет совпадение типоразмеров с учетом разных форматов"""
    normalized_search = normalize_tyre_size(search_size)
    normalized_stock = normalize_tyre_size(stock_size)
    
    # Точное совпадение после нормализации
    if normalized_search == normalized_stock:
        return True
    
    # Частичное совпадение (если поисковый запрос содержится в размере)
    if normalized_search in normalized_stock or normalized_stock in normalized_search:
        return True
    
    return False

# =============================================================================
# КЛАВИАТУРЫ
# =============================================================================

def get_role_keyboard():
    """Клавиатура выбора роли при регистрации"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Дилер"), KeyboardButton(text="Покупатель")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_main_menu_keyboard(telegram_id: int, is_admin: bool = False, role: str = "Покупатель"):
    """Основное меню с кнопками"""
    buttons = []
    
    if is_admin:
        buttons = [
            [KeyboardButton(text="📦 Мой склад"), KeyboardButton(text="🔍 Поиск")],
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="📤 Загрузить склад")],
            [KeyboardButton(text="🗑️ Удалить товары"), KeyboardButton(text="✏️ Профиль")],
            [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="🛠️ Админ")],
            [KeyboardButton(text="❓ Помощь")]
        ]
    elif role == "Дилер":
        buttons = [
            [KeyboardButton(text="📦 Мой склад"), KeyboardButton(text="🔍 Поиск")],
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="📤 Загрузить склад")],
            [KeyboardButton(text="🗑️ Удалить товары"), KeyboardButton(text="✏️ Профиль")],
            [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="❓ Помощь")]
        ]
    else:  # Покупатель
        buttons = [
            [KeyboardButton(text="🔍 Поиск"), KeyboardButton(text="✏️ Профиль")],
            [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="❓ Помощь")]
        ]
    
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )

def get_search_keyboard():
    """Клавиатура выбора типа поиска"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Поиск по SKU"), KeyboardButton(text="📏 Поиск по размеру")],
            [KeyboardButton(text="🏭 Поиск по бренду"), KeyboardButton(text="📍 Поиск по складу")],
            [KeyboardButton(text="📊 Все товары"), KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )

def get_management_keyboard():
    """Клавиатура управления складом"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗑️ Удалить позицию"), KeyboardButton(text="🗑️ Очистить весь склад")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )

def get_confirmation_keyboard():
    """Клавиатура подтверждения действий"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да"), KeyboardButton(text="❌ Нет")]
        ],
        resize_keyboard=True
    )

def get_admin_keyboard():
    """Клавиатура админ-панели"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="💾 Экспорт"), KeyboardButton(text="🔄 Бэкап")],
            [KeyboardButton(text="🗃️ SQL"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="🏠 Главное меню")]
        ],
        resize_keyboard=True
    )

def get_cancel_keyboard():
    """Простая клавиатура с кнопкой отмены"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
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
    
    try:
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
    except Exception as e:
        logger.error(f"Error creating Excel file: {e}")
        return None

async def send_notifications(sub_type: str, sub_value: str, message: str):
    """Отправка уведомлений подписчикам"""
    try:
        subscribers = await db.get_subscribers(sub_type, sub_value)
        for subscriber_id in subscribers:
            try:
                await bot.send_message(subscriber_id, f"🔔 Уведомление: {message}")
            except Exception as e:
                logger.error(f"Error sending notification to {subscriber_id}: {e}")
    except Exception as e:
        logger.error(f"Error getting subscribers: {e}")

# =============================================================================
# ОСНОВНЫЕ КОМАНДЫ И МЕНЮ
# =============================================================================

@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активных операций для отмены.")
        return
    
    await state.clear()
    user_role = await get_user_role(message.from_user.id)
    is_admin_user = is_admin(message.from_user.id)
    await message.answer(
        "❌ Операция отменена.", 
        reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
    )

@dp.message(Command("start"))
@dp.message(F.text == "🏠 Главное меню")
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
        is_admin_user = is_admin(user_id)
        await message.answer(
            f"👋 С возвращением, {user_name}!\n"
            f"🎯 Ваша роль: {role}\n\n"
            "Выберите действие:",
            reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, role)
        )

# =============================================================================
# УПРАВЛЕНИЕ СКЛАДОМ - УДАЛЕНИЕ ТОВАРОВ
# =============================================================================

@dp.message(F.text == "🗑️ Удалить товары")
async def cmd_delete_stock(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    if user[1] != 'Дилер':
        await message.answer("❌ Только дилеры могут управлять складом")
        return
    
    await message.answer(
        "🗑️ <b>Управление удалением товаров</b>\n\n"
        "Выберите действие:",
        reply_markup=get_management_keyboard()
    )
    await state.set_state(DeleteStock.waiting_for_search)

@dp.message(F.text == "🗑️ Удалить позицию")
async def cmd_delete_item(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    await message.answer(
        "🔍 <b>Поиск товара для удаления</b>\n\n"
        "Введите SKU, типоразмер, модель или бренд товара:\n\n"
        "💡 <i>Будет показано до 20 подходящих товаров</i>",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(DeleteStock.waiting_for_search)

@dp.message(DeleteStock.waiting_for_search)
async def process_delete_search(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
    
    user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
    user_id = user[0]
    
    search_term = message.text.strip()
    items = await db.search_stock_items(user_id, search_term)
    
    if not items:
        await message.answer(
            "❌ По вашему запросу ничего не найдено.\n\n"
            "Попробуйте другой поисковый запрос:",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    # Сохраняем найденные товары в состоянии
    await state.update_data(search_results=items, search_term=search_term)
    
    # Создаем клавиатуру с найденными товарами
    keyboard = []
    items_text = "🔍 <b>Найденные товары:</b>\n\n"
    
    for i, (item_id, sku, size, pattern, brand, qty) in enumerate(items, 1):
        items_text += f"{i}. {sku} | {size} | {pattern} | {brand} | {qty} шт.\n"
        
        keyboard.append([InlineKeyboardButton(
            text=f"❌ {sku} - {size} ({qty} шт.)",
            callback_data=f"delete_{item_id}"
        )])
    
    keyboard.append([InlineKeyboardButton(
        text="🔍 Новый поиск",
        callback_data="new_search"
    )])
    
    items_text += "\nВыберите товар для удаления:"
    
    await message.answer(items_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(DeleteStock.waiting_for_selection)

@dp.callback_query(DeleteStock.waiting_for_selection)
async def process_delete_selection(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "new_search":
        await callback.message.edit_text(
            "🔍 Введите новый поисковый запрос:",
            reply_markup=get_cancel_keyboard()
        )
        await state.set_state(DeleteStock.waiting_for_search)
        await callback.answer()
        return
    
    if callback.data.startswith("delete_"):
        item_id = int(callback.data[7:])  # Убираем префикс "delete_"
        
        # Получаем информацию о товаре
        item = await db.fetchone(
            "SELECT sku, tyre_size, tyre_pattern, brand, qty_available FROM stock WHERE id = ?",
            (item_id,)
        )
        
        if item:
            sku, size, pattern, brand, qty = item
            await state.update_data(delete_item_id=item_id)
            
            confirmation_text = (
                "⚠️ <b>Подтверждение удаления</b>\n\n"
                f"🏷️ SKU: <b>{sku}</b>\n"
                f"📏 Размер: <b>{size}</b>\n"
                f"🔧 Модель: <b>{pattern}</b>\n"
                f"🏭 Бренд: <b>{brand}</b>\n"
                f"📊 Количество: <b>{qty} шт.</b>\n\n"
                "Вы уверены что хотите удалить этот товар?"
            )
            
            await callback.message.edit_text(
                confirmation_text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_delete")]
                ])
            )
        else:
            await callback.message.edit_text("❌ Товар не найден")
        
        await callback.answer()

@dp.callback_query(F.data == "confirm_delete")
async def process_confirm_delete(callback: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    item_id = user_data.get('delete_item_id')
    
    if item_id:
        # Получаем информацию о товаре перед удалением для сообщения
        item = await db.fetchone(
            "SELECT sku, tyre_size FROM stock WHERE id = ?",
            (item_id,)
        )
        
        if item:
            sku, size = item
            await db.delete_stock_item(item_id)
            
            await callback.message.edit_text(
                f"✅ Товар успешно удален!\n\n"
                f"🏷️ SKU: <b>{sku}</b>\n"
                f"📏 Размер: <b>{size}</b>"
            )
        else:
            await callback.message.edit_text("❌ Товар не найден")
    else:
        await callback.message.edit_text("❌ Ошибка: ID товара не найден")
    
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "cancel_delete")
async def process_cancel_delete(callback: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    search_results = user_data.get('search_results', [])
    search_term = user_data.get('search_term', '')
    
    # Восстанавливаем список найденных товаров
    keyboard = []
    items_text = "🔍 <b>Найденные товары:</b>\n\n"
    
    for i, (item_id, sku, size, pattern, brand, qty) in enumerate(search_results, 1):
        items_text += f"{i}. {sku} | {size} | {pattern} | {brand} | {qty} шт.\n"
        
        keyboard.append([InlineKeyboardButton(
            text=f"❌ {sku} - {size} ({qty} шт.)",
            callback_data=f"delete_{item_id}"
        )])
    
    keyboard.append([InlineKeyboardButton(
        text="🔍 Новый поиск",
        callback_data="new_search"
    )])
    
    items_text += "\nВыберите товар для удаления:"
    
    await callback.message.edit_text(items_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(DeleteStock.waiting_for_selection)
    await callback.answer()

@dp.message(F.text == "🗑️ Очистить весь склад")
async def cmd_delete_all_stock(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    if user[1] != 'Дилер':
        await message.answer("❌ Только дилеры могут управлять складом")
        return
    
    # Получаем количество товаров
    stock_count = await db.get_user_stock_count(user[0])
    
    if stock_count == 0:
        await message.answer("📭 Ваш склад и так пуст.")
        return
    
    await message.answer(
        f"⚠️ <b>ВНИМАНИЕ!</b>\n\n"
        f"Вы собираетесь удалить <b>ВЕСЬ</b> ваш склад!\n"
        f"Будет удалено: <b>{stock_count} товаров</b>\n\n"
        f"❌ Это действие нельзя отменить!\n\n"
        f"Вы уверены что хотите продолжить?",
        reply_markup=get_confirmation_keyboard()
    )
    await state.set_state(DeleteAllStock.confirmation)

@dp.message(DeleteAllStock.confirmation)
async def process_delete_all_confirmation(message: Message, state: FSMContext):
    if message.text == '✅ Да':
        user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
        user_id = user[0]
        
        # Получаем количество перед удалением
        stock_count = await db.get_user_stock_count(user_id)
        
        await db.delete_all_user_stock(user_id)
        
        await message.answer(
            f"✅ Весь склад успешно очищен!\n\n"
            f"🗑️ Удалено товаров: <b>{stock_count}</b>",
            reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin(message.from_user.id), 'Дилер')
        )
    else:
        await message.answer(
            "❌ Удаление отменено",
            reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin(message.from_user.id), 'Дилер')
        )
    
    await state.clear()

# =============================================================================
# ЗАГРУЗКА СКЛАДА ИЗ EXCEL
# =============================================================================

@dp.message(F.text == "📤 Загрузить склад")
async def cmd_upload_stock(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    if user[1] != 'Дилер':
        await message.answer("❌ Только дилеры могут загружать склад")
        return
    
    await message.answer(
        "📤 <b>Загрузка склада из Excel файла</b>\n\n"
        "Отправьте Excel файл (.xlsx) со следующими колонками:\n"
        "• SKU (артикул)\n"
        "• Типоразмер\n" 
        "• Модель\n"
        "• Бренд\n"
        "• Страна\n"
        "• Количество\n"
        "• Розничная цена\n"
        "• Оптовая цена\n"
        "• Склад\n\n"
        "💡 <i>Первая строка должна содержать заголовки колонок</i>\n\n"
        "❌ Для отмены введите /cancel",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(UploadStock.waiting_for_file)

@dp.message(UploadStock.waiting_for_file)
async def process_upload_file(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
    
    if not message.document:
        await message.answer("❌ Пожалуйста, отправьте Excel файл (.xlsx)")
        return
    
    if not message.document.file_name.endswith('.xlsx'):
        await message.answer("❌ Файл должен быть в формате Excel (.xlsx)")
        return
    
    try:
        user = await db.fetchone("SELECT id, company_name FROM users WHERE telegram_id = ?", (message.from_user.id,))
        user_id, company_name = user[0], user[1]
        
        # Скачиваем файл
        file_info = await bot.get_file(message.document.file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        
        # Сохраняем временный файл
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_filename = f"temp_files/upload_{timestamp}.xlsx"
        
        if not os.path.exists('temp_files'):
            os.makedirs('temp_files')
        
        with open(temp_filename, 'wb') as f:
            f.write(downloaded_file.read())
        
        # Читаем Excel файл
        df = pd.read_excel(temp_filename)
        
        # Проверяем необходимые колонки
        required_columns = ['SKU', 'Типоразмер', 'Модель', 'Бренд', 'Страна', 
                           'Количество', 'Розничная цена', 'Оптовая цена', 'Склад']
        
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            await message.answer(
                f"❌ В файле отсутствуют необходимые колонки:\n"
                f"{', '.join(missing_columns)}\n\n"
                f"Пожалуйста, проверьте формат файла."
            )
            os.remove(temp_filename)
            return
        
        # Проверяем лимит товаров
        current_count = await db.get_user_stock_count(user_id)
        if current_count + len(df) > MAX_STOCK_ITEMS:
            await message.answer(
                f"❌ Превышен лимит товаров!\n"
                f"Текущее количество: {current_count}\n"
                f"Новых товаров: {len(df)}\n"
                f"Лимит: {MAX_STOCK_ITEMS}\n\n"
                f"Удалите часть товаров или уменьшите файл."
            )
            os.remove(temp_filename)
            return
        
        # Обрабатываем данные
        success_count = 0
        error_count = 0
        errors = []
        
        for index, row in df.iterrows():
            try:
                # Проверяем обязательные поля
                if pd.isna(row['SKU']) or pd.isna(row['Типоразмер']) or pd.isna(row['Количество']):
                    error_count += 1
                    errors.append(f"Строка {index+2}: отсутствуют обязательные поля")
                    continue
                
                # Добавляем товар в базу
                await db.execute(
                    """INSERT INTO stock 
                    (user_id, sku, tyre_size, tyre_pattern, brand, country, qty_available, retail_price, wholesale_price, warehouse_location) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, 
                     str(row['SKU']), 
                     str(row['Типоразмер']), 
                     str(row['Модель']) if not pd.isna(row['Модель']) else '',
                     str(row['Бренд']) if not pd.isna(row['Бренд']) else '',
                     str(row['Страна']) if not pd.isna(row['Страна']) else '',
                     int(row['Количество']),
                     float(row['Розничная цена']) if not pd.isna(row['Розничная цена']) else 0,
                     float(row['Оптовая цена']) if not pd.isna(row['Оптовая цена']) else 0,
                     str(row['Склад']) if not pd.isna(row['Склад']) else '')
                )
                success_count += 1
                
            except Exception as e:
                error_count += 1
                errors.append(f"Строка {index+2}: {str(e)}")
        
        # Отправляем уведомления о новых товарах
        if success_count > 0:
            new_items = []
            for index, row in df.head(5).iterrows():  # Берем первые 5 товаров для уведомления
                if not pd.isna(row['SKU']) and not pd.isna(row['Типоразмер']):
                    new_items.append({
                        'brand': str(row['Бренд']) if not pd.isna(row['Бренд']) else '',
                        'tyre_size': str(row['Типоразмер']),
                        'tyre_pattern': str(row['Модель']) if not pd.isna(row['Модель']) else '',
                        'qty_available': int(row['Количество'])
                    })
            
            # Отправляем уведомления
            notification_sent = False
            for item in new_items:
                if item['brand']:
                    brand_subscribers = await db.get_subscribers("brand", item['brand'])
                    if brand_subscribers:
                        notification_text = f"Новые товары бренда {item['brand']} загружены"
                        await send_notifications("brand", item['brand'], notification_text)
                        notification_sent = True
                
                if item['tyre_size']:
                    size_subscribers = await db.get_subscribers("tyre_size", item['tyre_size'])
                    if size_subscribers:
                        notification_text = f"Новые товары размера {item['tyre_size']} загружены"
                        await send_notifications("tyre_size", item['tyre_size'], notification_text)
                        notification_sent = True
            
            dealer_subscribers = await db.get_subscribers("dealer", company_name)
            if dealer_subscribers:
                notification_text = f"Новые товары от {company_name} загружены"
                await send_notifications("dealer", company_name, notification_text)
                notification_sent = True
        
        # Формируем отчет
        result_text = f"📤 <b>Загрузка завершена!</b>\n\n"
        result_text += f"✅ Успешно загружено: <b>{success_count}</b> товаров\n"
        result_text += f"❌ Ошибок: <b>{error_count}</b>\n"
        
        if error_count > 0 and len(errors) > 0:
            result_text += f"\n📋 Первые 5 ошибок:\n"
            for error in errors[:5]:
                result_text += f"• {error}\n"
        
        if notification_sent:
            result_text += f"\n🔔 Уведомления отправлены подписчикам!"
        
        await message.answer(result_text)
        
        # Удаляем временный файл
        os.remove(temp_filename)
        
    except Exception as e:
        logger.error(f"Upload stock error: {e}")
        await message.answer(f"❌ Ошибка при загрузке файла: {str(e)}")
    
    await state.clear()
    user_role = await get_user_role(message.from_user.id)
    is_admin_user = is_admin(message.from_user.id)
    await message.answer(
        "Выберите следующее действие:",
        reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
    )

# =============================================================================
# ОСТАЛЬНЫЕ КОМАНДЫ (сокращено для экономии места)
# =============================================================================

# ... остальной код (поиск, профиль, уведомления, админ-панель, регистрация) ...
# Остальные функции остаются без изменений, как в предыдущей версии

@dp.message(F.text == "📦 Мой склад")
@dp.message(Command("mystock"))
async def cmd_my_stock(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    logger.info(f"User {message.from_user.id} requested 'My Stock'")
    
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь с помощью /start")
        return
    
    user_id, user_role = user[0], user[1]
    
    if user_role != 'Дилер':
        await message.answer("❌ Только дилеры могут иметь склад")
        return
    
    try:
        # Получаем товары пользователя
        stock_items = await db.fetchall("""
            SELECT sku, tyre_size, tyre_pattern, brand, country, 
                   qty_available, retail_price, wholesale_price, warehouse_location
            FROM stock 
            WHERE user_id = ?
            ORDER BY date DESC
        """, (user_id,))
        
        if not stock_items:
            await message.answer("📭 Ваш склад пуст.")
            return
        
        logger.info(f"Found {len(stock_items)} items for user {user_id}")
        
        # Создаем Excel файл
        if not os.path.exists('temp_files'):
            os.makedirs('temp_files')
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"temp_files/my_stock_{timestamp}.xlsx"
        
        columns = ['SKU', 'Типоразмер', 'Модель', 'Бренд', 'Страна', 
                  'Количество', 'Розничная цена', 'Оптовая цена', 'Склад']
        
        df = pd.DataFrame(stock_items, columns=columns)
        df.to_excel(filename, index=False, engine='openpyxl')
        
        with open(filename, 'rb') as file:
            await message.answer_document(
                document=types.BufferedInputFile(
                    file.read(), 
                    filename=f"мой_склад_{timestamp}.xlsx"
                ),
                caption=f"📦 Ваш склад ({len(stock_items)} товаров)"
            )
            
    except Exception as e:
        logger.error(f"My stock export error: {e}")
        await message.answer(f"❌ Ошибка при выгрузке склада: {str(e)}")

# =============================================================================
# АДМИН-ПАНЕЛЬ (БЕЗ "ВЕСЬ СКЛАД")
# =============================================================================

@dp.message(F.text == "🛠️ Админ")
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    # Статистика системы
    users_count = await db.fetchone("SELECT COUNT(*) FROM users")
    stock_count = await db.fetchone("SELECT COUNT(*) FROM stock")
    dealers_count = await db.fetchone("SELECT COUNT(*) FROM users WHERE role = 'Дилер'")
    buyers_count = await db.fetchone("SELECT COUNT(*) FROM users WHERE role = 'Покупатель'")
    
    admin_text = (
        "🛠️ <b>Админ-панель Tyreterra</b>\n\n"
        f"📊 <b>Статистика системы:</b>\n"
        f"👥 Пользователи: {users_count[0] if users_count else 0}\n"
        f"📦 Товаров на складах: {stock_count[0] if stock_count else 0}\n"
        f"🏭 Дилеров: {dealers_count[0] if dealers_count else 0}\n"
        f"👤 Покупателей: {buyers_count[0] if buyers_count else 0}\n\n"
        "Выберите действие:"
    )
    await message.answer(admin_text, reply_markup=get_admin_keyboard())

# Убрана функция "📦 Весь склад" из админ-панели

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
    
    await db.migrate_database()
    logger.info("✅ Миграции базы данных выполнены")
    
    for folder in ['temp_files', 'uploads', 'backups']:
        if not os.path.exists(folder):
            os.makedirs(folder)
    
    asyncio.create_task(periodic_cleanup())
    logger.info("✅ Фоновая очистка временных файлов запущена")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())