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
import openpyxl

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

    async def search_stock_suggestions(self, user_id: int, search_term: str) -> List[tuple]:
        """Поиск товаров для подсказок"""
        query = """
            SELECT id, sku, tyre_size, tyre_pattern, brand, qty_available 
            FROM stock 
            WHERE user_id = ? AND (
                sku LIKE ? OR tyre_size LIKE ? OR tyre_pattern LIKE ? OR brand LIKE ?
            )
            ORDER BY 
                CASE 
                    WHEN sku LIKE ? THEN 1
                    WHEN tyre_size LIKE ? THEN 2
                    WHEN tyre_pattern LIKE ? THEN 3
                    WHEN brand LIKE ? THEN 4
                    ELSE 5
                END
            LIMIT 10
        """
        search_pattern = f'%{search_term}%'
        params = (user_id, search_pattern, search_pattern, search_pattern, search_pattern,
                 search_pattern, search_pattern, search_pattern, search_pattern)
        return await self.fetchall(query, params)

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

class UploadExcel(StatesGroup):
    waiting_for_file = State()
    processing = State()

class SearchStock(StatesGroup):
    waiting_for_search_type = State()
    waiting_for_search_value = State()
    waiting_for_combined_search = State()

class DeleteItem(StatesGroup):
    waiting_for_search_term = State()
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
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="📤 Загрузить Excel")],
            [KeyboardButton(text="🗑️ Удалить товар"), KeyboardButton(text="🗑️ Удалить весь склад")],
            [KeyboardButton(text="✏️ Профиль"), KeyboardButton(text="🔔 Уведомления")],
            [KeyboardButton(text="🛠️ Админ"), KeyboardButton(text="❓ Помощь")]
        ]
    elif role == "Дилер":
        buttons = [
            [KeyboardButton(text="📦 Мой склад"), KeyboardButton(text="🔍 Поиск")],
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="📤 Загрузить Excel")],
            [KeyboardButton(text="🗑️ Удалить товар"), KeyboardButton(text="🗑️ Удалить весь склад")],
            [KeyboardButton(text="✏️ Профиль"), KeyboardButton(text="🔔 Уведомления")],
            [KeyboardButton(text="❓ Помощь")]
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
def get_confirmation_keyboard():
    """Клавиатура подтверждения действий"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да"), KeyboardButton(text="❌ Нет")]
        ],
        resize_keyboard=True
    )


# =============================================================================
# ЗАГРУЗКА EXCEL ФАЙЛОВ
# =============================================================================

@dp.message(F.text == "📤 Загрузить Excel")
async def cmd_upload_excel(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    if user[1] != 'Дилер' and not is_admin(message.from_user.id):
        await message.answer("❌ Только дилеры могут загружать товары через Excel")
        return
    
    await message.answer(
        "📤 <b>Загрузка товаров из Excel файла</b>\n\n"
        "Пожалуйста, отправьте Excel файл (.xlsx) со следующими колонками:\n"
        "• SKU (артикул) - обязательно\n"
        "• Типоразмер - обязательно\n" 
        "• Модель (паттерн)\n"
        "• Бренд - обязательно\n"
        "• Страна\n"
        "• Количество - обязательно\n"
        "• Розничная цена - обязательно\n"
        "• Оптовая цена\n"
        "• Склад\n\n"
        "💡 <i>Первая строка должна содержать заголовки колонок</i>\n\n"
        "❌ Для отмены введите /cancel",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(UploadExcel.waiting_for_file)

@dp.message(UploadExcel.waiting_for_file)
@dp.message(F.document)
async def process_excel_upload(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
    
    # Проверяем, что это документ
    if not message.document:
        await message.answer("❌ Пожалуйста, отправьте Excel файл (.xlsx)")
        return
    
    # Проверяем расширение файла
    file_name = message.document.file_name
    if not file_name or not file_name.lower().endswith(('.xlsx', '.xls')):
        await message.answer("❌ Пожалуйста, отправьте файл в формате Excel (.xlsx или .xls)")
        return
    
    # Проверяем размер файла
    if message.document.file_size > MAX_FILE_SIZE:
        await message.answer(f"❌ Файл слишком большой. Максимальный размер: {MAX_FILE_SIZE // 1024 // 1024}MB")
        return
    
    try:
        await message.answer("⏳ Начинаю обработку файла...")
        
        # Скачиваем файл
        file_id = message.document.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path
        
        # Создаем папку для загрузок если нет
        if not os.path.exists('uploads'):
            os.makedirs('uploads')
        
        # Сохраняем файл
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_filename = f"uploads/{message.from_user.id}_{timestamp}_{file_name}"
        await bot.download_file(file_path, local_filename)
        
        # Обрабатываем Excel файл
        await process_excel_file(message, local_filename, state)
        
    except Exception as e:
        logger.error(f"Excel upload error: {e}")
        await message.answer(f"❌ Ошибка при обработке файла: {str(e)}")
        await state.clear()

async def process_excel_file(message: Message, file_path: str, state: FSMContext):
    """Обработка Excel файла и добавление товаров в базу"""
    try:
        user = await db.fetchone("SELECT id, company_name FROM users WHERE telegram_id = ?", (message.from_user.id,))
        if not user:
            await message.answer("❌ Ошибка: пользователь не найден")
            return
        
        user_id, company_name = user[0], user[1]
        
        # Читаем Excel файл
        df = pd.read_excel(file_path)
        
        # Приводим названия колонок к стандартному виду (убираем пробелы, приводим к нижнему регистру)
        df.columns = [str(col).strip().lower() for col in df.columns]
        
        # Создаем маппинг возможных названий колонок
        column_mapping = {
            'sku': ['sku', 'артикул', 'код', 'articul'],
            'типоразмер': ['типоразмер', 'размер', 'size', 'tyre_size'],
            'бренд': ['бренд', 'brand', 'производитель'],
            'количество': ['количество', 'кол-во', 'qty', 'quantity', 'qty_available'],
            'розничная цена': ['розничная цена', 'розничная', 'retail', 'retail_price', 'цена розница'],
            'модель': ['модель', 'pattern', 'tyre_pattern', 'модель шины'],
            'страна': ['страна', 'country', 'страна производства'],
            'оптовая цена': ['оптовая цена', 'оптовая', 'wholesale', 'wholesale_price', 'цена опт'],
            'склад': ['склад', 'warehouse', 'warehouse_location', 'локация']
        }
        
        # Находим соответствующие колонки
        actual_columns = {}
        for standard_name, possible_names in column_mapping.items():
            for possible_name in possible_names:
                if possible_name in df.columns:
                    actual_columns[standard_name] = possible_name
                    break
        
        # ВСЕ ПОЛЯ ТЕПЕРЬ ОПЦИОНАЛЬНЫ - загружаем даже если нет некоторых колонок
        available_columns_info = "\n".join([f"• {col}" for col in actual_columns.keys()])
        await message.answer(f"📋 <b>Найдены колонки:</b>\n{available_columns_info}")
        
        # Обрабатываем каждую строку
        success_count = 0
        error_count = 0
        errors = []
        
        for index, row in df.iterrows():
            try:
                # Пропускаем только полностью пустые строки
                if all(pd.isna(row[col]) for col in df.columns if col in actual_columns.values()):
                    continue
                
                # Обрабатываем ВСЕ поля как опциональные
                sku = ""
                if 'sku' in actual_columns and not pd.isna(row[actual_columns['sku']]):
                    sku = str(row[actual_columns['sku']]).strip()
                
                tyre_size = ""
                if 'типоразмер' in actual_columns and not pd.isna(row[actual_columns['типоразмер']]):
                    tyre_size = str(row[actual_columns['типоразмер']]).strip()
                
                brand = ""
                if 'бренд' in actual_columns and not pd.isna(row[actual_columns['бренд']]):
                    brand = str(row[actual_columns['бренд']]).strip()
                
                # Если нет ни SKU, ни размера, ни бренда - пропускаем строку
                if not sku and not tyre_size and not brand:
                    continue
                
                # Генерируем временный SKU если нет
                if not sku:
                    sku = f"temp_{user_id}_{index}_{int(time.time())}"
                
                # Обрабатываем опциональные поля
                tyre_pattern = ""
                if 'модель' in actual_columns and not pd.isna(row[actual_columns['модель']]):
                    tyre_pattern_value = row[actual_columns['модель']]
                    if not pd.isna(tyre_pattern_value):
                        tyre_pattern = str(tyre_pattern_value).strip()
                
                country = ""
                if 'страна' in actual_columns and not pd.isna(row[actual_columns['страна']]):
                    country_value = row[actual_columns['страна']]
                    if not pd.isna(country_value):
                        country = str(country_value).strip()
                
                # Обрабатываем количество (по умолчанию 1 если не указано)
                qty_available = 1
                if 'количество' in actual_columns and not pd.isna(row[actual_columns['количество']]):
                    try:
                        qty_value = row[actual_columns['количество']]
                        if not pd.isna(qty_value):
                            qty_available = int(float(qty_value))
                            if qty_available <= 0:
                                qty_available = 1
                    except (ValueError, TypeError):
                        qty_available = 1
                
                # Обрабатываем розничную цену (по умолчанию 0 если не указано)
                retail_price = 0.0
                if 'розничная цена' in actual_columns and not pd.isna(row[actual_columns['розничная цена']]):
                    try:
                        retail_value = row[actual_columns['розничная цена']]
                        if not pd.isna(retail_value):
                            retail_price = float(retail_value)
                            if retail_price < 0:
                                retail_price = 0.0
                    except (ValueError, TypeError):
                        retail_price = 0.0
                
                # Обрабатываем оптовую цену (по умолчанию NULL если не указано)
                wholesale_price = None
                if 'оптовая цена' in actual_columns and not pd.isna(row[actual_columns['оптовая цена']]):
                    try:
                        wholesale_value = row[actual_columns['оптовая цена']]
                        if not pd.isna(wholesale_value):
                            wholesale_price = float(wholesale_value)
                            if wholesale_price < 0:
                                wholesale_price = None
                    except (ValueError, TypeError):
                        wholesale_price = None
                
                # Обрабатываем склад (опционально)
                warehouse_location = ""
                if 'склад' in actual_columns and not pd.isna(row[actual_columns['склад']]):
                    warehouse_value = row[actual_columns['склад']]
                    if not pd.isna(warehouse_value):
                        warehouse_location = str(warehouse_value).strip()
                
                # Проверяем лимит товаров
                current_stock_count = await db.get_user_stock_count(user_id)
                if current_stock_count >= MAX_STOCK_ITEMS:
                    errors.append(f"Достигнут лимит товаров ({MAX_STOCK_ITEMS}). Прерываю загрузку.")
                    break
                
                # Добавляем товар в базу
                await db.execute(
                    """INSERT INTO stock 
                    (user_id, sku, tyre_size, tyre_pattern, brand, country, qty_available, retail_price, wholesale_price, warehouse_location) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, sku, tyre_size, tyre_pattern, brand, country, qty_available, retail_price, wholesale_price, warehouse_location)
                )
                
                success_count += 1
                
            except Exception as e:
                error_count += 1
                errors.append(f"Строка {index+2}: {str(e)}")
                logger.error(f"Error processing row {index+2}: {e}")
                continue
        
        # Формируем отчет
        report_text = f"📊 <b>Отчет о загрузке</b>\n\n"
        report_text += f"✅ Успешно загружено: {success_count} товаров\n"
        report_text += f"❌ Ошибок: {error_count}\n"
        
        if errors and error_count > 0:
            report_text += f"\n📋 <b>Ошибки (первые 10):</b>\n"
            for i, error in enumerate(errors[:10]):
                report_text += f"{i+1}. {error}\n"
            if len(errors) > 10:
                report_text += f"... и еще {len(errors) - 10} ошибок\n"
        
        # Отправляем уведомления о новых товарах
        if success_count > 0:
            notification_sent = False
            
            # Уведомления по брендам
            unique_brands = await db.fetchall(
                "SELECT DISTINCT brand FROM stock WHERE user_id = ? AND brand != '' ORDER BY id DESC LIMIT ?",
                (user_id, 10)
            )
            
            for brand_row in unique_brands:
                brand = brand_row[0]
                if brand:  # Только если бренд не пустой
                    brand_subscribers = await db.get_subscribers("brand", brand)
                    if brand_subscribers:
                        notification_text = f"Загружены новые товары бренда {brand}"
                        await send_notifications("brand", brand, notification_text)
                        notification_sent = True
            
            # Уведомления по типоразмерам
            unique_sizes = await db.fetchall(
                "SELECT DISTINCT tyre_size FROM stock WHERE user_id = ? AND tyre_size != '' ORDER BY id DESC LIMIT ?",
                (user_id, 10)
            )
            
            for size_row in unique_sizes:
                size = size_row[0]
                if size:  # Только если размер не пустой
                    size_subscribers = await db.get_subscribers("tyre_size", size)
                    if size_subscribers:
                        notification_text = f"Загружены новые товары размера {size}"
                        await send_notifications("tyre_size", size, notification_text)
                        notification_sent = True
            
            # Уведомления по дилеру
            dealer_subscribers = await db.get_subscribers("dealer", company_name)
            if dealer_subscribers:
                notification_text = f"Дилер {company_name} загрузил новые товары"
                await send_notifications("dealer", company_name, notification_text)
                notification_sent = True
            
            if notification_sent:
                report_text += f"\n🔔 Уведомления отправлены подписчикам!"
        
        await message.answer(report_text)
        
        # Очищаем временный файл
        try:
            os.remove(file_path)
        except Exception as e:
            logger.error(f"Error removing temp file: {e}")
            
    except Exception as e:
        logger.error(f"Excel processing error: {e}")
        await message.answer(f"❌ Ошибка при обработке Excel файла: {str(e)}")
    
    await state.clear()
    user_role = await get_user_role(message.from_user.id)
    is_admin_user = is_admin(message.from_user.id)
    await message.answer(
        "Загрузка завершена.",
        reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
    )

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
            reply_markup=get_main_menu_keyboard(user_id, is_admin_user, role)
        )

@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def cmd_help(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user_role = await get_user_role(message.from_user.id)
    is_admin_user = is_admin(message.from_user.id)
    
    if is_admin_user:
        help_text = (
            "🤖 <b>Tyreterra Bot - Помощь (Админ)</b>\n\n"
            "👤 <b>Основные команды:</b>\n"
            "📦 Мой склад - Скачать мой склад\n"
            "🔍 Поиск - Поиск товаров\n"
            "➕ Добавить товар - Добавить товар на склад\n"
            "📤 Загрузить Excel - Массовая загрузка товаров\n"
            "🗑️ Удалить товар - Удалить конкретный товар\n"
            "🗑️ Удалить весь склад - Очистить весь склад\n"
            "✏️ Профиль - Редактировать профиль\n"
            "🔔 Уведомления - Управление подписками\n\n"
            "🛠️ <b>Админ-команды:</b>\n"
            "🛠️ Админ - Управление системой\n\n"
            "❌ Отмена операций: кнопка '❌ Отмена'"
        )
    elif user_role == 'Дилер':
        help_text = (
            "🤖 <b>Tyreterra Bot - Помощь (Дилер)</b>\n\n"
            "📦 <b>Управление складом:</b>\n"
            "📦 Мой склад - Скачать мой склад в Excel\n"
            "➕ Добавить товар - Добавить товар на склад\n"
            "📤 Загрузить Excel - Массовая загрузка товаров\n"
            "🗑️ Удалить товар - Удалить конкретный товар\n"
            "🗑️ Удалить весь склад - Очистить весь склад\n\n"
            "🔍 <b>Поиск:</b>\n"
            "🔍 Поиск - Поиск товаров у других пользователей\n\n"
            "🔔 <b>Уведомления:</b>\n"
            "Подпишитесь на интересующие товары\n\n"
            "❌ <b>Отмена операций:</b>\n"
            "В любой момент можно отменить операцию кнопкой '❌ Отмена'"
        )
    else:
        help_text = (
            "🤖 <b>Tyreterra Bot - Помощь (Покупатель)</b>\n\n"
            "🔍 <b>Поиск:</b>\n"
            "🔍 Поиск - Поиск товаров у дилеров\n"
            "Показываются только розничные цены\n\n"
            "🔔 <b>Уведомления:</b>\n"
            "Подпишитесь на интересующие товары\n\n"
            "📞 <b>Контакты:</b>\n"
            "В результатах поиска вы увидите контакты компаний\n\n"
            "❌ <b>Отмена операций:</b>\n"
            "В любой момент можно отменить операцию кнопкой '❌ Отмена'"
        )
    
    await message.answer(help_text)

# =============================================================================
# ПРОФИЛЬ И РЕДАКТИРОВАНИЕ
# =============================================================================

@dp.message(F.text == "✏️ Профиль")
@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT * FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    profile_text = (
        f"👤 <b>Ваш профиль:</b>\n\n"
        f"🆔 ID: {user[0]}\n"
        f"👤 Имя: {user[2]}\n"
        f"🏢 Компания: {user[3]}\n"
        f"📋 ИНН: {user[4]}\n"
        f"📞 Телефон: {user[5]}\n"
        f"📧 Email: {user[6]}\n"
        f"🎯 Роль: {user[7]}\n"
        f"📅 Регистрация: {user[8]}\n\n"
        f"✏️ Для редактирования используйте команду /editprofile"
    )
    
    await message.answer(profile_text)

# =============================================================================
# ВЫГРУЗКА СКЛАДА
# =============================================================================

@dp.message(F.text == "📦 Мой склад")
@dp.message(Command("mystock"))
async def cmd_my_stock(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    user_id, user_role = user[0], user[1]
    
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
# УЛУЧШЕННАЯ СИСТЕМА ПОИСКА С ПОДСКАЗКАМИ
# =============================================================================

@dp.message(F.text == "🔍 Поиск")
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
        "🔍 <b>Умный поиск товаров</b>\n\n"
        "Введите SKU, типоразмер, модель или бренд для поиска:\n\n"
        "💡 <i>Система найдет товары по любому из этих параметров</i>",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(SearchStock.waiting_for_search_value)

@dp.message(SearchStock.waiting_for_search_value)
async def process_smart_search(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
    
    search_term = message.text.strip()
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("❌ Ошибка: пользователь не найден")
        return
    
    user_id, user_role = user[0], user[1]
    
    try:
        # Поиск по ВСЕМ товарам для ВСЕХ пользователей
        query = """
            SELECT s.sku, s.tyre_size, s.tyre_pattern, s.brand, s.country, 
                   s.qty_available, s.retail_price, s.wholesale_price, 
                   s.warehouse_location, u.company_name, u.phone, u.email
            FROM stock s 
            JOIN users u ON s.user_id = u.id 
            WHERE s.sku LIKE ? OR s.tyre_size LIKE ? OR s.tyre_pattern LIKE ? OR s.brand LIKE ?
            ORDER BY s.date DESC
        """
        params = (f'%{search_term}%', f'%{search_term}%', f'%{search_term}%', f'%{search_term}%')
        
        stock_items = await db.fetchall(query, params)
        
        if not stock_items:
            await message.answer(
                f"❌ По запросу '{search_term}' ничего не найдено.",
                reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin(message.from_user.id), user_role)
            )
            await state.clear()
            return
        
        filename = await create_search_excel(stock_items, user_role, "smart_search")
        
        if filename:
            with open(filename, 'rb') as file:
                caption = f"🔍 Результаты поиска по '{search_term}' ({len(stock_items)} товаров)"
                if user_role == 'Покупатель':
                    caption += "\n👀 Показаны только розничные цены"
                else:
                    caption += "\n💰 Показаны розничные и оптовые цены"
                
                await message.answer_document(
                    document=types.BufferedInputFile(
                        file.read(), 
                        filename=f"поиск_{search_term}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                    ),
                    caption=caption
                )
        
    except Exception as e:
        logger.error(f"Smart search error: {e}")
        await message.answer(f"❌ Ошибка при поиске: {str(e)}")
    
    await state.clear()
    user_role = await get_user_role(message.from_user.id)
    await message.answer(
        "Поиск завершен.", 
        reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin(message.from_user.id), user_role)
    )

# =============================================================================
# СИСТЕМА УДАЛЕНИЯ ТОВАРОВ С ПОДСКАЗКАМИ
# =============================================================================

@dp.message(F.text == "🗑️ Удалить товар")
async def cmd_delete_item(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    if user[1] != 'Дилер' and not is_admin(message.from_user.id):
        await message.answer("❌ Только дилеры могут удалять товары")
        return
    
    await message.answer(
        "🗑️ <b>Удаление товара</b>\n\n"
        "Введите SKU, типоразмер, модель или бренд для поиска товара:\n\n"
        "💡 <i>Система найдет товары по любому из этих параметров</i>",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(DeleteItem.waiting_for_search_term)

@dp.message(DeleteItem.waiting_for_search_term)
async def process_delete_search(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
    
    search_term = message.text.strip()
    user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("❌ Ошибка: пользователь не найден")
        return
    
    user_id = user[0]
    
    try:
        # Ищем товары для удаления
        suggestions = await db.search_stock_suggestions(user_id, search_term)
        
        if not suggestions:
            await message.answer(
                "❌ По вашему запросу ничего не найдено.",
                reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin(message.from_user.id), await get_user_role(message.from_user.id))
            )
            await state.clear()
            return
        
        await state.update_data(suggestions=suggestions, search_term=search_term)
        
        if len(suggestions) == 1:
            # Если найден только один товар, сразу переходим к подтверждению
            item = suggestions[0]
            await state.update_data(selected_item_id=item[0])
            await show_delete_confirmation(message, state, item)
        else:
            # Показываем список для выбора
            await message.answer(
                f"🔍 Найдено {len(suggestions)} товаров по запросу '{search_term}':\n\n"
                "Выберите товар для удаления:",
                reply_markup=get_delete_selection_keyboard(suggestions)
            )
            await state.set_state(DeleteItem.waiting_for_selection)
            
    except Exception as e:
        logger.error(f"Delete search error: {e}")
        await message.answer(f"❌ Ошибка при поиске товара: {str(e)}")
        await state.clear()

@dp.message(DeleteItem.waiting_for_selection)
async def process_delete_selection(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
    
    user_data = await state.get_data()
    suggestions = user_data.get('suggestions', [])
    
    # Ищем выбранный товар
    selected_item = None
    for item in suggestions:
        item_id, sku, size, pattern, brand, qty = item
        button_text = f"{sku} | {size} | {brand} | {qty}шт"
        if len(button_text) > 50:
            button_text = button_text[:47] + "..."
        
        if message.text == button_text:
            selected_item = item
            break
    
    if not selected_item:
        await message.answer("❌ Пожалуйста, выберите товар из списка:")
        return
    
    await state.update_data(selected_item_id=selected_item[0])
    await show_delete_confirmation(message, state, selected_item)

async def show_delete_confirmation(message: Message, state: FSMContext, item):
    """Показать подтверждение удаления"""
    item_id, sku, size, pattern, brand, qty = item
    
    confirmation_text = (
        "🗑️ <b>Подтверждение удаления</b>\n\n"
        f"🏷️ <b>SKU:</b> {sku}\n"
        f"📏 <b>Размер:</b> {size}\n"
        f"🔧 <b>Модель:</b> {pattern if pattern else 'Не указано'}\n"
        f"🏭 <b>Бренд:</b> {brand}\n"
        f"📊 <b>Количество:</b> {qty} шт.\n\n"
        "❌ <b>Вы уверены что хотите удалить этот товар?</b>"
    )
    
    await message.answer(
        confirmation_text,
        reply_markup=get_confirmation_keyboard()
    )
    await state.set_state(DeleteItem.confirmation)

@dp.message(DeleteItem.confirmation)
async def process_delete_confirmation(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text not in ['✅ Да', '❌ Нет']:
        await message.answer("❌ Пожалуйста, выберите '✅ Да' или '❌ Нет':")
        return
    
    if message.text == '❌ Нет':
        await message.answer("❌ Удаление отменено.")
        await state.clear()
        user_role = await get_user_role(message.from_user.id)
        is_admin_user = is_admin(message.from_user.id)
        await message.answer(
            "Выберите действие:",
            reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
        )
        return
    
    # Подтверждено удаление
    user_data = await state.get_data()
    item_id = user_data.get('selected_item_id')
    
    try:
        # Получаем информацию о товаре перед удалением (для логов)
        item_info = await db.fetchone(
            "SELECT sku, tyre_size, brand FROM stock WHERE id = ?", 
            (item_id,)
        )
        
        # Удаляем товар
        await db.execute("DELETE FROM stock WHERE id = ?", (item_id,))
        
        if item_info:
            sku, size, brand = item_info
            await message.answer(
                f"✅ Товар успешно удален:\n\n"
                f"🏷️ SKU: {sku}\n"
                f"📏 Размер: {size}\n"
                f"🏭 Бренд: {brand}"
            )
        else:
            await message.answer("✅ Товар успешно удален")
            
    except Exception as e:
        logger.error(f"Delete item error: {e}")
        await message.answer(f"❌ Ошибка при удалении товара: {str(e)}")
    
    await state.clear()
    user_role = await get_user_role(message.from_user.id)
    is_admin_user = is_admin(message.from_user.id)
    await message.answer(
        "Выберите действие:",
        reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
    )

# =============================================================================
# УДАЛЕНИЕ ВСЕГО СКЛАДА
# =============================================================================

@dp.message(F.text == "🗑️ Удалить весь склад")
async def cmd_delete_all_stock(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    if user[1] != 'Дилер' and not is_admin(message.from_user.id):
        await message.answer("❌ Только дилеры могут удалять товары")
        return
    
    # Получаем количество товаров пользователя
    stock_count = await db.get_user_stock_count(user[0])
    
    if stock_count == 0:
        await message.answer("📭 Ваш склад уже пуст.")
        return
    
    await message.answer(
        f"⚠️ <b>ВНИМАНИЕ!</b>\n\n"
        f"Вы собираетесь удалить ВСЕ товары со своего склада.\n"
        f"📦 Будет удалено: <b>{stock_count} товаров</b>\n\n"
        f"❌ <b>Это действие нельзя отменить!</b>\n\n"
        f"Вы уверены что хотите продолжить?",
        reply_markup=get_confirmation_keyboard()
    )
    await state.set_state(DeleteAllStock.confirmation)

@dp.message(DeleteAllStock.confirmation)
async def process_delete_all_confirmation(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text not in ['✅ Да', '❌ Нет']:
        await message.answer("❌ Пожалуйста, выберите '✅ Да' или '❌ Нет':")
        return
    
    if message.text == '❌ Нет':
        await message.answer("❌ Удаление всего склада отменено.")
        await state.clear()
        user_role = await get_user_role(message.from_user.id)
        is_admin_user = is_admin(message.from_user.id)
        await message.answer(
            "Выберите действие:",
            reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
        )
        return
    
    # Подтверждено удаление всего склада
    user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    try:
        # Получаем количество перед удалением
        stock_count = await db.get_user_stock_count(user[0])
        
        # Удаляем все товары пользователя
        await db.execute("DELETE FROM stock WHERE user_id = ?", (user[0],))
        
        await message.answer(f"✅ Весь склад успешно очищен! Удалено {stock_count} товаров.")
        
    except Exception as e:
        logger.error(f"Delete all stock error: {e}")
        await message.answer(f"❌ Ошибка при удалении склада: {str(e)}")
    
    await state.clear()
    user_role = await get_user_role(message.from_user.id)
    is_admin_user = is_admin(message.from_user.id)
    await message.answer(
        "Выберите действие:",
        reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
    )

# =============================================================================
# СИСТЕМА УВЕДОМЛЕНИЙ И ПОДПИСОК
# =============================================================================

@dp.message(F.text == "🔔 Уведомления")
@dp.message(Command("subscriptions"))
async def cmd_subscriptions(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Сначала зарегистрируйтесь с помощью /start")
        return
    
    await message.answer(
        "🔔 <b>Управление подписками</b>\n\n"
        "Вы можете подписаться на уведомления о:\n"
        "• 🏭 Новые товары определенного бренда\n"
        "• 📏 Новые товары определенного типоразмера\n"
        "• 🏢 Новые товары от определенного дилера\n\n"
        "<i>💡 Уведомления приходят одним сообщением при добавлении новых товаров</i>\n\n"
        "Выберите действие:",
        reply_markup=get_subscription_keyboard()
    )
    await state.set_state(SubscriptionState.waiting_for_type)

@dp.message(SubscriptionState.waiting_for_type)
async def process_subscription_type(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == "📋 Мои подписки":
        await show_user_subscriptions(message, state)
        return
        
    if message.text == "❌ Отмена":
        await cancel_handler(message, state)
        return
    
    type_map = {
        "🏭 Бренд": "brand",
        "📏 Типоразмер": "tyre_size", 
        "🏢 Дилер": "dealer"
    }
    
    if message.text not in type_map:
        await message.answer("Пожалуйста, выберите тип подписки из предложенных вариантов:")
        return
    
    sub_type = type_map[message.text]
    await state.update_data(subscription_type=sub_type)
    
    type_display = {
        "brand": "бренд",
        "tyre_size": "типоразмер", 
        "dealer": "дилера"
    }.get(sub_type, sub_type)
    
    prompt_text = f"Введите {type_display} для подписки:"
    
    await message.answer(prompt_text, reply_markup=get_cancel_keyboard())
    await state.set_state(SubscriptionState.waiting_for_value)

@dp.message(SubscriptionState.waiting_for_value)
async def process_subscription_value(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
    
    await process_subscription_value_internal(message, state, message.text)

async def process_subscription_value_internal(message: Message, state: FSMContext, value: str):
    """Общая логика обработки значения подписки"""
    user_data = await state.get_data()
    sub_type = user_data['subscription_type']
    
    user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
    user_id = user[0]
    
    # Проверяем, нет ли уже такой подписки
    existing = await db.fetchone(
        "SELECT id FROM subscriptions WHERE user_id = ? AND subscription_type = ? AND subscription_value = ?",
        (user_id, sub_type, value)
    )
    
    type_display = {
        "brand": "бренд",
        "tyre_size": "типоразмер", 
        "dealer": "дилер"
    }.get(sub_type, sub_type)
    
    if existing:
        await message.answer(f"❌ Вы уже подписаны на {type_display} <b>{value}</b>")
    else:
        await db.add_subscription(user_id, sub_type, value)
        await message.answer(f"✅ Вы успешно подписались на уведомления:\n{type_display} <b>{value}</b>")
    
    user_role = await get_user_role(message.from_user.id)
    is_admin_user = is_admin(message.from_user.id)
    await message.answer(
        "Выберите следующее действие:",
        reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
    )
    await state.clear()

async def show_user_subscriptions(message: Message, state: FSMContext):
    """Показать текущие подписки пользователя с кнопками удаления"""
    user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
    user_id = user[0]
    
    subscriptions = await db.get_user_subscriptions(user_id)
    
    if not subscriptions:
        await message.answer("📭 У вас пока нет активных подписок.")
        await state.clear()
        user_role = await get_user_role(message.from_user.id)
        is_admin_user = is_admin(message.from_user.id)
        await message.answer(
            "Выберите действие:",
            reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
        )
        return
    
    subs_text = "📋 <b>Ваши подписки:</b>\n\n"
    keyboard = []
    
    for sub_id, sub_type, sub_value in subscriptions:
        type_display = {
            "brand": "🏭 Бренд",
            "tyre_size": "📏 Типоразмер", 
            "dealer": "🏢 Дилер"
        }.get(sub_type, sub_type)
        
        subs_text += f"• {type_display}: <b>{sub_value}</b>\n"
        keyboard.append([InlineKeyboardButton(
            text=f"❌ Отписаться от {sub_value}", 
            callback_data=f"unsub_{sub_id}"
        )])
    
    # Добавляем кнопку "Отписаться от всего"
    keyboard.append([InlineKeyboardButton(
        text="🗑️ Отписаться от ВСЕХ уведомлений", 
        callback_data="unsub_all"
    )])
    
    await message.answer(subs_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.clear()

@dp.callback_query(F.data.startswith("unsub_"))
async def process_unsubscribe(callback: types.CallbackQuery):
    """Обработка отписки"""
    if callback.data == "unsub_all":
        # Отписка от всех уведомлений
        user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (callback.from_user.id,))
        if user:
            user_id = user[0]
            await db.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
            await callback.message.edit_text("✅ Вы отписались от всех уведомлений!")
        else:
            await callback.message.edit_text("❌ Ошибка: пользователь не найден")
    else:
        # Отписка от конкретной подписки
        sub_id = int(callback.data[6:])  # Убираем префикс "unsub_"
        
        # Получаем информацию о подписке перед удалением
        subscription = await db.fetchone(
            "SELECT subscription_type, subscription_value FROM subscriptions WHERE id = ?", 
            (sub_id,)
        )
        
        if subscription:
            sub_type, sub_value = subscription
            await db.remove_subscription(sub_id)
            
            type_display = {
                "brand": "бренда",
                "tyre_size": "типоразмера", 
                "dealer": "дилера"
            }.get(sub_type, sub_type)
            
            await callback.message.edit_text(f"✅ Вы отписались от {type_display} <b>{sub_value}</b>")
        else:
            await callback.message.edit_text("❌ Подписка не найдена")
    
    await callback.answer()

# =============================================================================
# ДОБАВЛЕНИЕ ТОВАРОВ
# =============================================================================

@dp.message(F.text == "➕ Добавить товар")
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
        "📦 <b>Добавление нового товара</b>\n\n"
        "Введите артикул (SKU):\n\n"
        "❌ Для отмены введите /cancel или нажмите кнопку",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(AddStock.waiting_for_sku)

@dp.message(AddStock.waiting_for_sku)
async def process_sku(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
        
    await state.update_data(sku=message.text)
    await message.answer(
        "Введите типоразмер шины (например: 195/65 R15):",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(AddStock.waiting_for_size)

@dp.message(AddStock.waiting_for_size)
async def process_size(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
        
    await state.update_data(tyre_size=message.text)
    await message.answer(
        "Введите модель шины (tyre pattern):",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(AddStock.waiting_for_pattern)

@dp.message(AddStock.waiting_for_pattern)
async def process_pattern(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
        
    await state.update_data(tyre_pattern=message.text)
    await message.answer(
        "Введите бренд шины:",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(AddStock.waiting_for_brand)

@dp.message(AddStock.waiting_for_brand)
async def process_brand(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
        
    await state.update_data(brand=message.text)
    await message.answer(
        "Введите страну производства:",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(AddStock.waiting_for_country)

@dp.message(AddStock.waiting_for_country)
async def process_country(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
        
    await state.update_data(country=message.text)
    await message.answer(
        "Введите доступное количество (только цифры):",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(AddStock.waiting_for_qty)

@dp.message(AddStock.waiting_for_qty)
async def process_qty(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
        
    try:
        # Убираем возможные пробелы и проверяем что это число
        qty_text = message.text.strip()
        if not qty_text.isdigit():
            await message.answer("❌ Количество должно быть числом. Попробуйте снова:")
            return
            
        qty = int(qty_text)
        if qty <= 0:
            await message.answer("❌ Количество должно быть положительным числом. Попробуйте снова:")
            return
            
        await state.update_data(qty_available=qty)
        await message.answer(
            f"📊 Количество: {qty}\n\nВведите розничную цену (только цифры, можно с точкой):",
            reply_markup=get_cancel_keyboard()
        )
        await state.set_state(AddStock.waiting_for_retail_price)
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число для количества:")

@dp.message(AddStock.waiting_for_retail_price)
async def process_retail_price(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
        
    try:
        # Заменяем запятые на точки и убираем пробелы
        price_text = message.text.strip().replace(',', '.').replace(' ', '')
        if not re.match(r'^\d+(\.\d+)?$', price_text):
            await message.answer("❌ Цена должна быть числом. Попробуйте снова:")
            return
            
        retail_price = float(price_text)
        if retail_price <= 0:
            await message.answer("❌ Цена должна быть положительным числом. Попробуйте снова:")
            return
            
        await state.update_data(retail_price=retail_price)
        await message.answer(
            f"💰 Розничная цена: {retail_price} руб.\n\nВведите оптовую цену (только цифры, можно с точкой):",
            reply_markup=get_cancel_keyboard()
        )
        await state.set_state(AddStock.waiting_for_wholesale_price)
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число для цены:")

@dp.message(AddStock.waiting_for_wholesale_price)
async def process_wholesale_price(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
        
    try:
        # Заменяем запятые на точки и убираем пробелы
        price_text = message.text.strip().replace(',', '.').replace(' ', '')
        if not re.match(r'^\d+(\.\d+)?$', price_text):
            await message.answer("❌ Цена должна быть числом. Попробуйте снова:")
            return
            
        wholesale_price = float(price_text)
        if wholesale_price <= 0:
            await message.answer("❌ Цена должна быть положительным числом. Попробуйте снова:")
            return
            
        await state.update_data(wholesale_price=wholesale_price)
        await message.answer(
            f"💼 Оптовая цена: {wholesale_price} руб.\n\nВведите расположение склада:",
            reply_markup=get_cancel_keyboard()
        )
        await state.set_state(AddStock.waiting_for_warehouse)
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число для цены:")

@dp.message(AddStock.waiting_for_warehouse)
async def process_warehouse(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await cancel_handler(message, state)
        return
        
    await process_warehouse_final(message, state, message.text)

async def process_warehouse_final(message: Message, state: FSMContext, warehouse_location: str):
    """Финальная обработка добавления товара"""
    try:
        user_data = await state.get_data()
        
        user = await db.fetchone("SELECT id, company_name FROM users WHERE telegram_id = ?", (message.from_user.id,))
        
        if user:
            user_id, company_name = user[0], user[1]
            
            # Добавляем товар в базу
            await db.execute(
                """INSERT INTO stock 
                (user_id, sku, tyre_size, tyre_pattern, brand, country, qty_available, retail_price, wholesale_price, warehouse_location) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, 
                 user_data['sku'], 
                 user_data['tyre_size'], 
                 user_data['tyre_pattern'],
                 user_data['brand'], 
                 user_data['country'], 
                 user_data['qty_available'],
                 user_data['retail_price'], 
                 user_data['wholesale_price'], 
                 warehouse_location)
            )
            
            # Отправляем уведомления подписчикам
            notification_sent = False
            
            # Уведомления по бренду
            brand_subscribers = await db.get_subscribers("brand", user_data['brand'])
            if brand_subscribers:
                notification_text = f"Новый товар бренда {user_data['brand']}: {user_data['tyre_size']} {user_data.get('tyre_pattern', '')}"
                await send_notifications("brand", user_data['brand'], notification_text)
                notification_sent = True
            
            # Уведомления по типоразмеру
            size_subscribers = await db.get_subscribers("tyre_size", user_data['tyre_size'])
            if size_subscribers:
                notification_text = f"Новый товар размера {user_data['tyre_size']}: {user_data['brand']} {user_data.get('tyre_pattern', '')}"
                await send_notifications("tyre_size", user_data['tyre_size'], notification_text)
                notification_sent = True
            
            # Уведомления по дилеру
            dealer_subscribers = await db.get_subscribers("dealer", company_name)
            if dealer_subscribers:
                notification_text = f"Новый товар от {company_name}: {user_data['brand']} {user_data['tyre_size']}"
                await send_notifications("dealer", company_name, notification_text)
                notification_sent = True
            
            success_message = (
                "✅ Товар успешно добавлен на склад!\n\n"
                f"🏷️ Артикул: {user_data['sku']}\n"
                f"📏 Типоразмер: {user_data['tyre_size']}\n"
                f"🔧 Модель: {user_data.get('tyre_pattern', 'Не указано')}\n"
                f"🏭 Бренд: {user_data['brand']}\n"
                f"🌍 Страна: {user_data['country']}\n"
                f"📊 Количество: {user_data['qty_available']}\n"
                f"💰 Розничная цена: {user_data['retail_price']} руб.\n"
                f"💼 Оптовая цена: {user_data['wholesale_price']} руб.\n"
                f"📍 Склад: {warehouse_location}"
            )
            
            if notification_sent:
                success_message += "\n\n🔔 Уведомления отправлены подписчикам!"
            
            user_role = await get_user_role(message.from_user.id)
            is_admin_user = is_admin(message.from_user.id)
            await message.answer(
                success_message,
                reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
            )
        else:
            await message.answer("❌ Ошибка: пользователь не найден. Используйте /start для регистрации.")
        
    except Exception as e:
        logger.error(f"Add stock error: {e}")
        await message.answer(f"❌ Произошла ошибка при добавлении товара: {str(e)}")
    
    await state.clear()

# =============================================================================
# АДМИН-ПАНЕЛЬ (БЕЗ ФУНКЦИИ "ВЕСЬ СКЛАД")
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

@dp.message(F.text == "👥 Пользователи")
async def cmd_admin_users(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    users = await db.fetchall("""
        SELECT id, telegram_id, name, company_name, role, created_at 
        FROM users ORDER BY created_at DESC
    """)
    
    if not users:
        await message.answer("📭 В системе нет пользователей.")
        return
    
    users_text = "👥 <b>Все пользователи:</b>\n\n"
    for user in users[:20]:  # Ограничиваем первые 20 пользователей
        users_text += (
            f"🆔 ID: {user[0]}\n"
            f"👤 Имя: {user[2]}\n"
            f"🏢 Компания: {user[3]}\n"
            f"🎯 Роль: {user[4]}\n"
            f"📅 Регистрация: {user[5]}\n"
            f"────────────────────\n"
        )
    
    if len(users) > 20:
        users_text += f"\n... и еще {len(users) - 20} пользователей"
    
    await message.answer(users_text)

@dp.message(F.text == "📊 Статистика")
async def cmd_admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    try:
        # Базовая статистика
        total_users = await db.fetchone("SELECT COUNT(*) FROM users")
        total_stock = await db.fetchone("SELECT COUNT(*) FROM stock")
        total_dealers = await db.fetchone("SELECT COUNT(*) FROM users WHERE role = 'Дилер'")
        total_buyers = await db.fetchone("SELECT COUNT(*) FROM users WHERE role = 'Покупатель'")
        
        # Статистика по брендам
        brand_stats = await db.fetchall("""
            SELECT brand, COUNT(*) as count, SUM(qty_available) as total_qty
            FROM stock 
            WHERE brand IS NOT NULL AND brand != ''
            GROUP BY brand 
            ORDER BY count DESC 
            LIMIT 10
        """)
        
        # Статистика по размерам
        size_stats = await db.fetchall("""
            SELECT tyre_size, COUNT(*) as count
            FROM stock 
            WHERE tyre_size IS NOT NULL AND tyre_size != ''
            GROUP BY tyre_size 
            ORDER BY count DESC 
            LIMIT 10
        """)
        
        # Последние регистрации
        recent_users = await db.fetchall("""
            SELECT name, company_name, role, created_at 
            FROM users 
            ORDER BY created_at DESC 
            LIMIT 5
        """)
        
        stats_text = (
            "📊 <b>Статистика системы</b>\n\n"
            f"👥 <b>Пользователи:</b> {total_users[0] if total_users else 0}\n"
            f"🏭 Дилеры: {total_dealers[0] if total_dealers else 0}\n"
            f"👤 Покупатели: {total_buyers[0] if total_buyers else 0}\n"
            f"📦 <b>Товары:</b> {total_stock[0] if total_stock else 0}\n\n"
        )
        
        if brand_stats:
            stats_text += "🏭 <b>Топ брендов:</b>\n"
            for brand, count, total_qty in brand_stats:
                stats_text += f"• {brand}: {count} позиций, {total_qty} шт.\n"
            stats_text += "\n"
        
        if size_stats:
            stats_text += "📏 <b>Популярные размеры:</b>\n"
            for size, count in size_stats:
                stats_text += f"• {size}: {count} позиций\n"
            stats_text += "\n"
        
        if recent_users:
            stats_text += "🆕 <b>Последние регистрации:</b>\n"
            for user in recent_users:
                stats_text += f"• {user[0]} ({user[1]}) - {user[2]}\n"
        
        await message.answer(stats_text)
        
    except Exception as e:
        logger.error(f"Admin stats error: {e}")
        await message.answer(f"❌ Ошибка при получении статистики: {str(e)}")

@dp.message(F.text == "💾 Экспорт")
async def cmd_admin_export(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    try:
        # Экспорт пользователей
        users = await db.fetchall("""
            SELECT telegram_id, name, company_name, inn, phone, email, role, created_at
            FROM users ORDER BY created_at DESC
        """)
        
        # Экспорт товаров - ИСПРАВЛЕННЫЙ ЗАПРОС
        stock = await db.fetchall("""
            SELECT s.sku, s.tyre_size, s.tyre_pattern, s.brand, s.country, 
                   s.qty_available, s.retail_price, s.wholesale_price, 
                   s.warehouse_location, u.company_name, s.date as created_at
            FROM stock s 
            JOIN users u ON s.user_id = u.id 
            ORDER BY s.date DESC
        """)
        
        if not os.path.exists('temp_files'):
            os.makedirs('temp_files')
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Создаем Excel файл с несколькими листами
        with pd.ExcelWriter(f"temp_files/full_export_{timestamp}.xlsx", engine='openpyxl') as writer:
            # Лист пользователей
            if users:
                users_columns = ['Telegram ID', 'Имя', 'Компания', 'ИНН', 'Телефон', 'Email', 'Роль', 'Дата регистрации']
                users_df = pd.DataFrame(users, columns=users_columns)
                users_df.to_excel(writer, sheet_name='Пользователи', index=False)
            
            # Лист товаров
            if stock:
                stock_columns = ['SKU', 'Типоразмер', 'Модель', 'Бренд', 'Страна', 'Количество', 
                               'Розничная цена', 'Оптовая цена', 'Склад', 'Дилер', 'Дата добавления']
                stock_df = pd.DataFrame(stock, columns=stock_columns)
                stock_df.to_excel(writer, sheet_name='Товары', index=False)
        
        with open(f"temp_files/full_export_{timestamp}.xlsx", 'rb') as file:
            await message.answer_document(
                document=types.BufferedInputFile(
                    file.read(), 
                    filename=f"полный_экспорт_{timestamp}.xlsx"
                ),
                caption=f"💾 Полный экспорт данных\n👥 Пользователей: {len(users) if users else 0}\n📦 Товаров: {len(stock) if stock else 0}"
            )
            
    except Exception as e:
        logger.error(f"Admin export error: {e}")
        await message.answer(f"❌ Ошибка при экспорте данных: {str(e)}")
        
@dp.message(F.text == "🔄 Бэкап")
async def cmd_admin_backup(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    try:
        if not os.path.exists('backups'):
            os.makedirs('backups')
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backups/tyreterra_backup_{timestamp}.db"
        
        # Копируем базу данных
        shutil.copy2(DB_PATH, backup_filename)
        
        # Получаем список бэкапов
        backups = []
        if os.path.exists('backups'):
            for file in os.listdir('backups'):
                if file.endswith('.db'):
                    file_path = os.path.join('backups', file)
                    backups.append((file, os.path.getctime(file_path)))
        
        backups.sort(key=lambda x: x[1], reverse=True)
        
        backup_text = "✅ Бэкап базы данных создан!\n\n"
        backup_text += "📂 <b>Последние бэкапы:</b>\n"
        
        for i, (backup_file, _) in enumerate(backups[:5], 1):
            backup_text += f"{i}. {backup_file}\n"
        
        if len(backups) > 5:
            backup_text += f"... и еще {len(backups) - 5} бэкапов\n"
        
        backup_text += f"\n💾 Размер базы: {os.path.getsize(DB_PATH) // 1024 // 1024} MB"
        
        await message.answer(backup_text)
        
    except Exception as e:
        logger.error(f"Admin backup error: {e}")
        await message.answer(f"❌ Ошибка при создании бэкапа: {str(e)}")

@dp.message(F.text == "🗃️ SQL")
async def cmd_admin_sql(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    await message.answer(
        "🗃️ <b>Выполнение SQL запроса</b>\n\n"
        "Введите SQL запрос для выполнения:\n"
        "• SELECT запросы вернут результат\n"
        "• UPDATE/DELETE запросы будут выполнены\n"
        "• Будьте осторожны!\n\n"
        "❌ Для отмены введите /cancel",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(AdminPanel.waiting_for_sql_query)

@dp.message(AdminPanel.waiting_for_sql_query)
async def process_sql_query(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
        
    if message.text == '/cancel' or message.text == '❌ Отмена':
        await state.clear()
        await message.answer("❌ SQL запрос отменен", reply_markup=get_admin_keyboard())
        return
    
    try:
        sql_query = message.text.strip()
        
        # Проверяем на опасные операции
        dangerous_keywords = ['DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER']
        is_select = sql_query.upper().startswith('SELECT')
        
        if any(keyword in sql_query.upper() for keyword in dangerous_keywords) and not is_select:
            # Запрос на изменение данных - требуем подтверждение
            await state.update_data(sql_query=sql_query)
            await message.answer(
                f"⚠️ <b>Внимание! Это запрос на изменение данных:</b>\n\n<code>{sql_query}</code>\n\n"
                "Вы уверены что хотите выполнить этот запрос?",
                reply_markup=get_confirmation_keyboard()
            )
            await state.set_state(AdminPanel.confirmation)
            return
        
        # Выполняем запрос
        if is_select:
            # SELECT запрос - возвращаем результат
            result = await db.fetchall(sql_query)
            if not result:
                await message.answer("✅ Запрос выполнен. Результат пуст.")
            else:
                result_text = f"✅ Результат ({len(result)} строк):\n\n"
                for i, row in enumerate(result[:10], 1):  # Ограничиваем первые 10 строк
                    result_text += f"{i}. {row}\n"
                
                if len(result) > 10:
                    result_text += f"\n... и еще {len(result) - 10} строк"
                
                await message.answer(result_text)
        else:
            # Другие запросы - просто выполняем
            await db.execute(sql_query)
            await message.answer("✅ Запрос выполнен успешно!")
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"SQL query error: {e}")
        await message.answer(f"❌ Ошибка выполнения SQL запроса: {str(e)}")
        await state.clear()

@dp.message(AdminPanel.confirmation)
async def process_sql_confirmation(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
        
    if message.text == '✅ Да':
        user_data = await state.get_data()
        sql_query = user_data['sql_query']
        
        try:
            await db.execute(sql_query)
            await message.answer("✅ Запрос выполнен успешно!")
        except Exception as e:
            await message.answer(f"❌ Ошибка выполнения SQL запроса: {str(e)}")
    else:
        await message.answer("❌ Запрос отменен")
    
    await state.clear()
    await message.answer("Выберите действие:", reply_markup=get_admin_keyboard())

@dp.message(F.text == "⚙️ Настройки")
async def cmd_admin_settings(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    settings_text = (
        "⚙️ <b>Настройки системы</b>\n\n"
        f"🔐 Администраторы: {', '.join(map(str, ADMIN_IDS))}\n"
        f"📦 Макс. товаров на пользователя: {MAX_STOCK_ITEMS}\n"
        f"📎 Макс. размер файла: {MAX_FILE_SIZE // 1024 // 1024} MB\n"
        f"💾 Путь к БД: {DB_PATH}\n\n"
        "Для изменения настроек отредактируйте переменные окружения или конфигурационный файл."
    )
    
    await message.answer(settings_text)

@dp.message(F.text == "🏠 Главное меню")
async def cmd_admin_back_to_main(message: Message):
    user_role = await get_user_role(message.from_user.id)
    is_admin_user = is_admin(message.from_user.id)
    await message.answer(
        "Возврат в главное меню",
        reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
    )

# =============================================================================
# РЕГИСТРАЦИЯ
# =============================================================================

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
        
    inn = message.text.strip()
    if not validate_inn(inn):
        await message.answer("❌ Неверный формат ИНН. Введите 10 или 12 цифр:")
        return
    
    await state.update_data(inn=inn)
    await message.answer("Введите ваш номер телефона (в формате 89123456789):")
    await state.set_state(Registration.waiting_for_phone)

@dp.message(Registration.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    phone = message.text.strip()
    if not validate_phone(phone):
        await message.answer("❌ Неверный формат телефона. Введите номер в формате 89123456789:")
        return
    
    await state.update_data(phone=phone)
    await message.answer("Введите ваш email:")
    await state.set_state(Registration.waiting_for_email)

@dp.message(Registration.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    email = message.text.strip()
    if not validate_email(email):
        await message.answer("❌ Неверный формат email. Попробуйте снова:")
        return
    
    user_data = await state.get_data()
    
    try:
        await db.execute(
            """INSERT INTO users (telegram_id, name, company_name, inn, phone, email, role) 
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message.from_user.id, user_data['name'], user_data['company_name'], 
             user_data['inn'], user_data['phone'], email, user_data['role'])
        )
        
        await message.answer(
            f"✅ Регистрация завершена!\n\n"
            f"👤 Имя: {user_data['name']}\n"
            f"🏢 Компания: {user_data['company_name']}\n"
            f"📋 ИНН: {user_data['inn']}\n"
            f"📞 Телефон: {user_data['phone']}\n"
            f"📧 Email: {email}\n"
            f"🎯 Роль: {user_data['role']}\n\n"
            f"Теперь вы можете пользоваться всеми функциями бота.",
            reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin(message.from_user.id), user_data['role'])
        )
        
    except Exception as e:
        logger.error(f"Registration error: {e}")
        await message.answer(f"❌ Ошибка при регистрации: {str(e)}")
    
    await state.clear()

@dp.message()
async def unknown_message(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подождите немного.")
        return
        
    user_role = await get_user_role(message.from_user.id)
    is_admin_user = is_admin(message.from_user.id)
    await message.answer(
        "Неизвестная команда. Используйте меню для выбора действия.",
        reply_markup=get_main_menu_keyboard(message.from_user.id, is_admin_user, user_role)
    )

# =============================================================================
# ФОНОВЫЕ ЗАДАЧИ И ЗАПУСК
# =============================================================================

async def periodic_cleanup():
    while True:
        try:
            await asyncio.sleep(3600)
            cleanup_temp_files()
            
            # Также очищаем папку uploads
            current_time = time.time()
            if os.path.exists('uploads'):
                for filename in os.listdir('uploads'):
                    filepath = os.path.join('uploads', filename)
                    if os.path.isfile(filepath):
                        if current_time - os.path.getmtime(filepath) > 3600:
                            os.remove(filepath)
            
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