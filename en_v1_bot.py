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
# CONFIGURATION
# =============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8294936286:AAGfR-q_GGWIlxS4QlOwhAsJyFtSgFKKK_I")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "7975448643").split(',')))
DB_PATH = os.getenv("DB_PATH", "tyreterra.db")
MAX_STOCK_ITEMS = int(os.getenv("MAX_STOCK_ITEMS", "10000"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "52428800"))  # 50MB

# Logging setup
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
# LOAD OPTIMIZATIONS
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
    """Clean up files older than 1 hour"""
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
# DATABASE (ASYNC)
# =============================================================================

class AsyncDatabase:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
    
    async def init_db(self):
        """Initialize database"""
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
# BOT INITIALIZATION
# =============================================================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()

# =============================================================================
# FSM STATES
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
# HELPER FUNCTIONS
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

# Keyboards
def get_role_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Dealer"), KeyboardButton(text="Buyer")]],
        resize_keyboard=True
    )

async def get_main_keyboard(telegram_id):
    """Returns keyboard based on user role"""
    
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
    
    if user_role == 'Dealer':
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
            [KeyboardButton(text="SKU"), KeyboardButton(text="Tyre Size")],
            [KeyboardButton(text="Brand"), KeyboardButton(text="Warehouse")],
            [KeyboardButton(text="All")]
        ],
        resize_keyboard=True
    )

def get_confirmation_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Yes"), KeyboardButton(text="No")]],
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

async def create_search_excel(stock_items, user_role, search_type="results"):
    """Creates Excel file with search results (hides wholesale price for buyers)"""
    if not stock_items:
        return None
    
    if not os.path.exists('temp_files'):
        os.makedirs('temp_files')
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"temp_files/search_{timestamp}.xlsx"
    
    # For buyers, hide wholesale price and other users' contacts
    if user_role == 'Buyer':
        columns = ['sku', 'tyre_size', 'tyre_pattern', 'brand', 'country', 
                  'qty_available', 'retail_price', 'warehouse_location', 'company_name']
        
        processed_items = []
        for item in stock_items:
            processed_item = list(item[:6]) + [item[6]] + [item[8]] + [item[9]]  # Skip wholesale_price and contacts
            processed_items.append(processed_item)
        
        df = pd.DataFrame(processed_items, columns=columns)
    else:
        # For dealers and admins, show all data
        columns = ['sku', 'tyre_size', 'tyre_pattern', 'brand', 'country', 
                  'qty_available', 'retail_price', 'wholesale_price', 'warehouse_location',
                  'company_name', 'phone', 'email']
        df = pd.DataFrame(stock_items, columns=columns)
    
    df.to_excel(filename, index=False, engine='openpyxl')
    return filename

# =============================================================================
# BASIC COMMANDS
# =============================================================================

@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("No active operations to cancel.")
        return
    
    await state.clear()
    await message.answer("❌ Operation cancelled.", reply_markup=await get_main_keyboard(message.from_user.id))

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    
    user = await db.fetchone("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
    
    if not user:
        await message.answer(
            f"Welcome to Tyreterra, {user_name}!\n"
            "Let's register you in the system.\n"
            "Please choose your role:",
            reply_markup=get_role_keyboard()
        )
        await state.set_state(Registration.waiting_for_role)
    else:
        role = user[7]
        await message.answer(
            f"Welcome back, {user_name}!\n"
            f"Your role: {role}\n"
            "Use commands to work with the system:",
            reply_markup=await get_main_keyboard(user_id)
        )

@dp.message(Registration.waiting_for_role)
async def process_role(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text not in ["Dealer", "Buyer"]:
        await message.answer("Please choose a role from the suggested options:")
        return
    
    await state.update_data(role=message.text, name=message.from_user.full_name)
    await message.answer("Enter your company name:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Registration.waiting_for_company)

@dp.message(Registration.waiting_for_company)
async def process_company(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    await state.update_data(company_name=message.text)
    await message.answer("Enter your company TIN (10 or 12 digits):")
    await state.set_state(Registration.waiting_for_inn)

@dp.message(Registration.waiting_for_inn)
async def process_inn(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not validate_inn(message.text):
        await message.answer("❌ Invalid TIN format. Enter 10 or 12 digits:")
        return
    
    await state.update_data(inn=message.text)
    await message.answer("Enter your contact phone (format: 89991234567):")
    await state.set_state(Registration.waiting_for_phone)

@dp.message(Registration.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not validate_phone(message.text):
        await message.answer("❌ Invalid phone format. Enter in format 89991234567:")
        return
    
    await state.update_data(phone=message.text)
    await message.answer("Enter your email:")
    await state.set_state(Registration.waiting_for_email)

@dp.message(Registration.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not validate_email(message.text):
        await message.answer("❌ Invalid email format. Enter a valid email:")
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
        if user_data['role'] == 'Dealer':
            role_permissions = "\n✅ You can: upload stock, download your stock, view other users' stock"
        else:
            role_permissions = "\n✅ You can: view other users' stock"
        
        await message.answer(
            f"🎉 Registration completed!\n\n"
            f"👤 Name: {user_data['name']}\n"
            f"🏢 Company: {user_data['company_name']}\n"
            f"📋 TIN: {user_data['inn']}\n"
            f"📞 Phone: {user_data['phone']}\n"
            f"📧 Email: {message.text}\n"
            f"🎯 Role: {user_data['role']}"
            f"{role_permissions}\n\n"
            "Use commands to work with the system:",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
        
    except Exception as e:
        logger.error(f"Registration error: {e}")
        await message.answer("❌ An error occurred during registration. Please try again.")
    
    await state.clear()

# =============================================================================
# DEALER COMMANDS
# =============================================================================

@dp.message(Command("addstock"))
async def cmd_addstock(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Please register first using /start")
        return
    
    if user[1] != 'Dealer':
        await message.answer("❌ Only dealers can add items to stock")
        return
    
    stock_count = await db.get_user_stock_count(user[0])
    if stock_count >= MAX_STOCK_ITEMS:
        await message.answer(f"❌ Item limit reached ({MAX_STOCK_ITEMS}). Delete some items to add new ones.")
        return
    
    current_state = await state.get_state()
    if current_state:
        await message.answer("⚠️ You have an unfinished operation. Complete it or cancel with /cancel")
        return
        
    await message.answer(
        "Let's add a new item to stock.\n"
        "Enter the article (SKU):\n\n"
        "❌ To cancel enter /cancel"
    )
    await state.set_state(AddStock.waiting_for_sku)

@dp.message(AddStock.waiting_for_sku)
async def process_sku(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(sku=message.text)
    await message.answer("Enter tyre size (e.g.: 195/65 R15):\n\n❌ To cancel enter /cancel")
    await state.set_state(AddStock.waiting_for_size)

@dp.message(AddStock.waiting_for_size)
async def process_size(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(tyre_size=message.text)
    await message.answer("Enter tyre model (tyre pattern):\n\n❌ To cancel enter /cancel")
    await state.set_state(AddStock.waiting_for_pattern)

@dp.message(AddStock.waiting_for_pattern)
async def process_pattern(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(tyre_pattern=message.text)
    await message.answer("Enter tyre brand:\n\n❌ To cancel enter /cancel")
    await state.set_state(AddStock.waiting_for_brand)

@dp.message(AddStock.waiting_for_brand)
async def process_brand(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(brand=message.text)
    await message.answer("Enter country of origin:\n\n❌ To cancel enter /cancel")
    await state.set_state(AddStock.waiting_for_country)

@dp.message(AddStock.waiting_for_country)
async def process_country(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    await state.update_data(country=message.text)
    await message.answer("Enter available quantity (numbers only):\n\n❌ To cancel enter /cancel")
    await state.set_state(AddStock.waiting_for_qty)

@dp.message(AddStock.waiting_for_qty)
async def process_qty(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    try:
        qty = int(message.text)
        if qty <= 0:
            await message.answer("Quantity must be a positive number. Try again:\n\n❌ To cancel enter /cancel")
            return
        await state.update_data(qty_available=qty)
        await message.answer("Enter retail price (numbers only):\n\n❌ To cancel enter /cancel")
        await state.set_state(AddStock.waiting_for_retail_price)
    except ValueError:
        await message.answer("Please enter a valid number for quantity:\n\n❌ To cancel enter /cancel")

@dp.message(AddStock.waiting_for_retail_price)
async def process_retail_price(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    try:
        retail_price = float(message.text)
        if retail_price <= 0:
            await message.answer("Price must be a positive number. Try again:\n\n❌ To cancel enter /cancel")
            return
        await state.update_data(retail_price=retail_price)
        await message.answer("Enter wholesale price (numbers only):\n\n❌ To cancel enter /cancel")
        await state.set_state(AddStock.waiting_for_wholesale_price)
    except ValueError:
        await message.answer("Please enter a valid number for price:\n\n❌ To cancel enter /cancel")

@dp.message(AddStock.waiting_for_wholesale_price)
async def process_wholesale_price(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    try:
        wholesale_price = float(message.text)
        if wholesale_price <= 0:
            await message.answer("Price must be a positive number. Try again:\n\n❌ To cancel enter /cancel")
            return
        await state.update_data(wholesale_price=wholesale_price)
        await message.answer("Enter warehouse location:\n\n❌ To cancel enter /cancel")
    except ValueError:
        await message.answer("Please enter a valid number for price:\n\n❌ To cancel enter /cancel")

@dp.message(AddStock.waiting_for_warehouse)
async def process_warehouse(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    try:
        user_data = await state.get_data()
        await state.clear()
        
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
                "✅ Item successfully added to stock!\n\n"
                f"🏷️ SKU: {user_data['sku']}\n"
                f"📏 Size: {user_data['tyre_size']}\n"
                f"🔧 Model: {user_data['tyre_pattern']}\n"
                f"🏭 Brand: {user_data['brand']}\n"
                f"🌍 Country: {user_data['country']}\n"
                f"📊 Quantity: {user_data['qty_available']}\n"
                f"💰 Retail price: {user_data['retail_price']} rub.\n"
                f"💼 Wholesale price: {user_data['wholesale_price']} rub.\n"
                f"📍 Warehouse: {message.text}",
                reply_markup=await get_main_keyboard(message.from_user.id)
            )
        else:
            await message.answer("Error: user not found. Use /start to register.")
        
    except Exception as e:
        logger.error(f"Add stock error: {e}")
        await state.clear()
        await message.answer("❌ An error occurred while adding the item. Please try again.")

@dp.message(Command("mystock"))
async def cmd_mystock(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    try:
        user = await db.fetchone("SELECT id, name, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
        
        if not user:
            await message.answer("Please register first using /start")
            return
        
        user_id, user_name, role = user[0], user[1], user[2]
        
        if role != 'Dealer':
            await message.answer("❌ Only dealers can download their stock")
            return
        
        cache_key = f"mystock_{user_id}"
        cached_data = cache.get(cache_key)
        
        if cached_data:
            filename, stock_count = cached_data
            if os.path.exists(filename):
                with open(filename, 'rb') as file:
                    await message.answer_document(
                        document=types.BufferedInputFile(file.read(), filename=f"my_stock_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"),
                        caption=f"📊 Your stock ({stock_count} items) [CACHE]\n👤 User: {user_name}"
                    )
                return
        
        # FIXED QUERY - select ALL 10 columns
        stock_items = await db.fetchall(
            """SELECT sku, tyre_size, tyre_pattern, brand, country, qty_available, 
                      retail_price, wholesale_price, warehouse_location, date 
            FROM stock WHERE user_id = ? ORDER BY date DESC""",
            (user_id,)
        )
        
        if not stock_items:
            await message.answer("Your stock is empty. Use /addstock to add items.")
            return
        
        if not os.path.exists('temp_files'):
            os.makedirs('temp_files')
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"temp_files/stock_{user_id}_{timestamp}.xlsx"
        
        # FIXED DATAFRAME CREATION
        columns = ['sku', 'tyre_size', 'tyre_pattern', 'brand', 'country', 
                  'qty_available', 'retail_price', 'wholesale_price', 'warehouse_location', 'date']
        
        df = pd.DataFrame(stock_items, columns=columns)
        df.to_excel(filename, index=False, engine='openpyxl')
        
        cache.set(cache_key, (filename, len(stock_items)))
        
        with open(filename, 'rb') as file:
            await message.answer_document(
                document=types.BufferedInputFile(file.read(), filename=f"my_stock_{timestamp}.xlsx"),
                caption=f"📊 Your stock ({len(stock_items)} items)\n👤 User: {user_name}"
            )
            
    except Exception as e:
        logger.error(f"Error in mystock: {e}")
        await message.answer(f"❌ Error downloading stock: {str(e)}")

@dp.message(Command("deletestock"))
async def cmd_deletestock(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Please register first using /start")
        return
    
    user_id, role = user[0], user[1]
    
    if role != 'Dealer':
        await message.answer("❌ Only dealers can delete their stock")
        return
    
    stock_count = await db.fetchone("SELECT COUNT(*) FROM stock WHERE user_id = ?", (user_id,))
    
    if not stock_count or stock_count[0] == 0:
        await message.answer("❌ Your stock is already empty.")
        return
    
    await message.answer(
        f"⚠️ WARNING: You are about to delete your ENTIRE stock ({stock_count[0]} items).\n"
        "This action CANNOT be undone!\n\n"
        "Are you sure you want to continue?\n\n"
        "❌ To cancel enter /cancel",
        reply_markup=get_confirmation_keyboard()
    )
    await state.set_state(DeleteAllStock.confirmation)

@dp.message(DeleteAllStock.confirmation)
async def process_delete_all_confirmation(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
    
    if message.text == 'Yes':
        user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
        user_id = user[0]
        
        await db.execute("DELETE FROM stock WHERE user_id = ?", (user_id,))
        
        await message.answer(
            "✅ Your entire stock has been successfully deleted!",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
    elif message.text == 'No':
        await message.answer(
            "❌ Stock deletion cancelled.",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
    else:
        await message.answer("Please choose 'Yes' or 'No':")
        return
    
    await state.clear()

@dp.message(Command("deleteitem"))
async def cmd_deleteitem(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Please register first using /start")
        return
    
    user_id, role = user[0], user[1]
    
    if role != 'Dealer':
        await message.answer("❌ Only dealers can delete items")
        return
    
    await message.answer(
        "Enter the SKU of the item you want to delete:\n\n"
        "❌ To cancel enter /cancel",
        reply_markup=await get_main_keyboard(message.from_user.id)
    )
    await state.set_state(DeleteItem.waiting_for_sku)

@dp.message(DeleteItem.waiting_for_sku)
async def process_delete_sku(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
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
            f"❌ Item with SKU '{sku}' not found in your stock.\n"
            "Please check the SKU and try again:\n\n"
            "❌ To cancel enter /cancel"
        )
        return
    
    await state.update_data(sku=sku)
    
    await message.answer(
        f"Found item:\n"
        f"🏷️ SKU: {item[2]}\n"
        f"📏 Size: {item[3]}\n"
        f"🏭 Brand: {item[5]}\n"
        f"📊 Quantity: {item[7]}\n\n"
        f"Are you sure you want to delete this item?\n\n"
        "❌ To cancel enter /cancel",
        reply_markup=get_confirmation_keyboard()
    )
    await state.set_state(DeleteItem.confirmation)

@dp.message(DeleteItem.confirmation)
async def process_delete_confirmation(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
    
    if message.text == 'Yes':
        user_data = await state.get_data()
        sku = user_data['sku']
        
        user = await db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
        user_id = user[0]
        
        await db.execute(
            "DELETE FROM stock WHERE user_id = ? AND sku = ?", 
            (user_id, sku)
        )
        
        await message.answer(
            f"✅ Item with SKU '{sku}' successfully deleted!",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
    elif message.text == 'No':
        await message.answer(
            "❌ Item deletion cancelled.",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
    else:
        await message.answer("Please choose 'Yes' or 'No':")
        return
    
    await state.clear()

# =============================================================================
# SEARCH COMMANDS (FOR ALL USERS)
# =============================================================================

@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        await message.answer("Please register first using /start")
        return
    
    await message.answer(
        "Choose search type:",
        reply_markup=get_search_keyboard()
    )
    await state.set_state(SearchStock.waiting_for_search_type)

@dp.message(SearchStock.waiting_for_search_type)
async def process_search_type(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text not in ["SKU", "Tyre Size", "Brand", "Warehouse", "All"]:
        await message.answer("Please choose a search type from the suggested options:")
        return
    
    await state.update_data(search_type=message.text)
    
    if message.text == "All":
        await process_search_value(message, state)
    else:
        await message.answer(f"Enter {message.text} to search:\n\n❌ To cancel enter /cancel", reply_markup=ReplyKeyboardRemove())
        await state.set_state(SearchStock.waiting_for_search_value)

@dp.message(SearchStock.waiting_for_search_value)
async def process_search_value(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
        
    try:
        user_data = await state.get_data()
        search_type = user_data['search_type']
        search_value = message.text
        
        user = await db.fetchone("SELECT id, role FROM users WHERE telegram_id = ?", (message.from_user.id,))
        user_role = user[1]
        
        # Build query based on search type
        if search_type == "All":
            query = """
                SELECT s.sku, s.tyre_size, s.tyre_pattern, s.brand, s.country, 
                       s.qty_available, s.retail_price, s.wholesale_price, s.warehouse_location,
                       u.company_name, u.phone, u.email
                FROM stock s
                JOIN users u ON s.user_id = u.id
                WHERE u.telegram_id != ?
                ORDER BY s.date DESC
            """
            params = (message.from_user.id,)
        else:
            if search_type == "SKU":
                where_clause = "s.sku LIKE ?"
            elif search_type == "Tyre Size":
                where_clause = "s.tyre_size LIKE ?"
            elif search_type == "Brand":
                where_clause = "s.brand LIKE ?"
            elif search_type == "Warehouse":
                where_clause = "s.warehouse_location LIKE ?"
            
            query = f"""
                SELECT s.sku, s.tyre_size, s.tyre_pattern, s.brand, s.country, 
                       s.qty_available, s.retail_price, s.wholesale_price, s.warehouse_location,
                       u.company_name, u.phone, u.email
                FROM stock s
                JOIN users u ON s.user_id = u.id
                WHERE {where_clause} AND u.telegram_id != ?
                ORDER BY s.date DESC
            """
            params = (f"%{search_value}%", message.from_user.id)
        
        stock_items = await db.fetchall(query, params)
        
        if not stock_items:
            await message.answer(
                f"❌ No items found for your search.\n"
                f"Type: {search_type}\n"
                f"Value: {search_value}",
                reply_markup=await get_main_keyboard(message.from_user.id)
            )
            await state.clear()
            return
        
        # Create Excel file
        filename = await create_search_excel(stock_items, user_role, f"{search_type}_{search_value}")
        
        if filename:
            with open(filename, 'rb') as file:
                await message.answer_document(
                    document=types.BufferedInputFile(file.read(), filename=f"search_results_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"),
                    caption=f"🔍 Search results: {len(stock_items)} items found\n"
                           f"Type: {search_type}\n"
                           f"Value: {search_value if search_type != 'All' else 'All items'}"
                )
        else:
            await message.answer("❌ Error creating file with search results")
        
        await message.answer(
            "Search completed!",
            reply_markup=await get_main_keyboard(message.from_user.id)
        )
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        await message.answer("❌ An error occurred during search. Please try again.")
    
    await state.clear()

# =============================================================================
# ADMIN COMMANDS
# =============================================================================

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied. Admin only.")
        return
    
    await message.answer(
        "🛠️ <b>Admin Panel</b>\n\n"
        "Available commands:\n"
        "• /admin_users - View all users\n"
        "• /admin_stock - View all stock\n"
        "• /admin_stats - System statistics\n"
        "• /admin_export - Export all data\n"
        "• /admin_backup - Create database backup\n"
        "• /admin_sql - Execute SQL query\n"
        "• /admin_edit_user - Edit user\n"
        "• /admin_edit_stock - Edit stock\n"
        "• /admin_delete_user - Delete user\n"
        "• /admin_delete_stock - Delete stock item\n"
        "• /admin_clear_cache - Clear cache",
        reply_markup=get_admin_keyboard()
    )

@dp.message(Command("admin_users"))
async def cmd_admin_users(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied. Admin only.")
        return
    
    try:
        users = await db.fetchall("SELECT * FROM users ORDER BY created_at DESC")
        
        if not users:
            await message.answer("❌ No users found.")
            return
        
        if not os.path.exists('temp_files'):
            os.makedirs('temp_files')
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"temp_files/users_{timestamp}.xlsx"
        
        columns = ['id', 'telegram_id', 'name', 'company_name', 'inn', 'phone', 'email', 'role', 'created_at']
        df = pd.DataFrame(users, columns=columns)
        df.to_excel(filename, index=False, engine='openpyxl')
        
        with open(filename, 'rb') as file:
            await message.answer_document(
                document=types.BufferedInputFile(file.read(), filename=f"users_{timestamp}.xlsx"),
                caption=f"👥 Users list: {len(users)} users"
            )
            
    except Exception as e:
        logger.error(f"Admin users error: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("admin_stock"))
async def cmd_admin_stock(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied. Admin only.")
        return
    
    try:
        stock_items = await db.fetchall("""
            SELECT s.*, u.name, u.company_name, u.phone, u.email 
            FROM stock s 
            JOIN users u ON s.user_id = u.id 
            ORDER BY s.date DESC
        """)
        
        if not stock_items:
            await message.answer("❌ No stock items found.")
            return
        
        if not os.path.exists('temp_files'):
            os.makedirs('temp_files')
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"temp_files/all_stock_{timestamp}.xlsx"
        
        columns = ['id', 'user_id', 'sku', 'tyre_size', 'tyre_pattern', 'brand', 'country', 
                  'qty_available', 'retail_price', 'wholesale_price', 'warehouse_location', 'date',
                  'user_name', 'company_name', 'phone', 'email']
        df = pd.DataFrame(stock_items, columns=columns)
        df.to_excel(filename, index=False, engine='openpyxl')
        
        with open(filename, 'rb') as file:
            await message.answer_document(
                document=types.BufferedInputFile(file.read(), filename=f"all_stock_{timestamp}.xlsx"),
                caption=f"📊 All stock: {len(stock_items)} items"
            )
            
    except Exception as e:
        logger.error(f"Admin stock error: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("admin_stats"))
async def cmd_admin_stats(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied. Admin only.")
        return
    
    try:
        # Get statistics
        total_users = await db.fetchone("SELECT COUNT(*) FROM users")
        total_dealers = await db.fetchone("SELECT COUNT(*) FROM users WHERE role = 'Dealer'")
        total_buyers = await db.fetchone("SELECT COUNT(*) FROM users WHERE role = 'Buyer'")
        total_stock = await db.fetchone("SELECT COUNT(*) FROM stock")
        total_stock_value = await db.fetchone("SELECT SUM(retail_price * qty_available) FROM stock")
        avg_stock_per_dealer = await db.fetchone("SELECT AVG(stock_count) FROM (SELECT COUNT(*) as stock_count FROM stock GROUP BY user_id)")
        
        # Recent activity
        recent_users = await db.fetchone("SELECT COUNT(*) FROM users WHERE created_at > datetime('now', '-7 days')")
        recent_stock = await db.fetchone("SELECT COUNT(*) FROM stock WHERE date > datetime('now', '-7 days')")
        
        stats_text = (
            "📊 <b>System Statistics</b>\n\n"
            f"👥 <b>Users:</b> {total_users[0]}\n"
            f"   • Dealers: {total_dealers[0]}\n"
            f"   • Buyers: {total_buyers[0]}\n"
            f"📦 <b>Stock items:</b> {total_stock[0]}\n"
            f"💰 <b>Total stock value:</b> {total_stock_value[0] or 0:.2f} rub.\n"
            f"📈 <b>Avg items per dealer:</b> {avg_stock_per_dealer[0] or 0:.1f}\n\n"
            f"🔄 <b>Last 7 days:</b>\n"
            f"   • New users: {recent_users[0]}\n"
            f"   • New stock: {recent_stock[0]}"
        )
        
        await message.answer(stats_text)
        
    except Exception as e:
        logger.error(f"Admin stats error: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("admin_export"))
async def cmd_admin_export(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied. Admin only.")
        return
    
    try:
        # Get all data
        users = await db.fetchall("SELECT * FROM users")
        stock = await db.fetchall("SELECT * FROM stock")
        
        if not os.path.exists('temp_files'):
            os.makedirs('temp_files')
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"temp_files/full_export_{timestamp}.xlsx"
        
        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            # Users sheet
            if users:
                users_df = pd.DataFrame(users, columns=['id', 'telegram_id', 'name', 'company_name', 'inn', 'phone', 'email', 'role', 'created_at'])
                users_df.to_excel(writer, sheet_name='Users', index=False)
            
            # Stock sheet
            if stock:
                stock_df = pd.DataFrame(stock, columns=['id', 'user_id', 'sku', 'tyre_size', 'tyre_pattern', 'brand', 'country', 
                                                       'qty_available', 'retail_price', 'wholesale_price', 'warehouse_location', 'date'])
                stock_df.to_excel(writer, sheet_name='Stock', index=False)
        
        with open(filename, 'rb') as file:
            await message.answer_document(
                document=types.BufferedInputFile(file.read(), filename=f"full_export_{timestamp}.xlsx"),
                caption=f"📁 Full data export\n👥 Users: {len(users) if users else 0}\n📦 Stock: {len(stock) if stock else 0}"
            )
            
    except Exception as e:
        logger.error(f"Admin export error: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("admin_backup"))
async def cmd_admin_backup(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied. Admin only.")
        return
    
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"tyreterra_backup_{timestamp}.db"
        
        shutil.copy2(DB_PATH, backup_filename)
        
        with open(backup_filename, 'rb') as file:
            await message.answer_document(
                document=types.BufferedInputFile(file.read(), filename=backup_filename),
                caption="💾 Database backup created successfully"
            )
        
        os.remove(backup_filename)
        
    except Exception as e:
        logger.error(f"Admin backup error: {e}")
        await message.answer(f"❌ Error creating backup: {str(e)}")

@dp.message(Command("admin_clear_cache"))
async def cmd_admin_clear_cache(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied. Admin only.")
        return
    
    try:
        cache.clear()
        cleanup_temp_files()
        await message.answer("✅ Cache cleared successfully!")
        
    except Exception as e:
        logger.error(f"Clear cache error: {e}")
        await message.answer(f"❌ Error clearing cache: {str(e)}")

@dp.message(Command("admin_sql"))
async def cmd_admin_sql(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if not is_admin(message.from_user.id):
        await message.answer("❌ Access denied. Admin only.")
        return
    
    await message.answer(
        "Enter SQL query to execute:\n\n"
        "⚠️ <b>WARNING:</b> Be careful with modifying queries!\n"
        "❌ To cancel enter /cancel",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(AdminPanel.waiting_for_sql_query)

@dp.message(AdminPanel.waiting_for_sql_query)
async def process_admin_sql(message: Message, state: FSMContext):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    if message.text == '/cancel':
        await cancel_handler(message, state)
        return
    
    try:
        query = message.text.strip()
        
        if query.lower().startswith('select'):
            result = await db.fetchall(query)
            
            if not result:
                await message.answer("✅ Query executed successfully. No results.")
                await state.clear()
                return
            
            if not os.path.exists('temp_files'):
                os.makedirs('temp_files')
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"temp_files/sql_result_{timestamp}.xlsx"
            
            df = pd.DataFrame(result)
            df.to_excel(filename, index=False, engine='openpyxl')
            
            with open(filename, 'rb') as file:
                await message.answer_document(
                    document=types.BufferedInputFile(file.read(), filename=f"sql_result_{timestamp}.xlsx"),
                    caption=f"📋 SQL query result: {len(result)} rows"
                )
        else:
            result = await db.execute(query)
            await message.answer(f"✅ Query executed successfully. Rows affected: {result}")
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"SQL query error: {e}")
        await message.answer(f"❌ SQL error: {str(e)}")
        await state.clear()

# =============================================================================
# HELP COMMAND
# =============================================================================

@dp.message(Command("help"))
async def cmd_help(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    user = await db.fetchone("SELECT role FROM users WHERE telegram_id = ?", (message.from_user.id,))
    
    if not user:
        help_text = (
            "🤖 <b>Tyreterra Bot Help</b>\n\n"
            "To start working with the bot:\n"
            "1. Use /start to register\n"
            "2. Choose your role (Dealer/Buyer)\n"
            "3. Fill in your details\n\n"
            "❌ Cancel any operation: /cancel\n"
            "🆘 Help: /help"
        )
    else:
        role = user[0]
        
        if role == 'Dealer':
            help_text = (
                "🤖 <b>Tyreterra Bot Help</b>\n\n"
                "<b>Available commands:</b>\n"
                "• /addstock - Add item to stock\n"
                "• /mystock - Download your stock\n"
                "• /search - Search in other users' stock\n"
                "• /deletestock - Delete your entire stock\n"
                "• /deleteitem - Delete specific item\n"
                "• /help - This help\n\n"
                "❌ Cancel any operation: /cancel"
            )
        elif role == 'Buyer':
            help_text = (
                "🤖 <b>Tyreterra Bot Help</b>\n\n"
                "<b>Available commands:</b>\n"
                "• /search - Search in other users' stock\n"
                "• /help - This help\n\n"
                "❌ Cancel any operation: /cancel"
            )
        else:
            help_text = "Unknown role. Please contact administrator."
    
    await message.answer(help_text, reply_markup=await get_main_keyboard(message.from_user.id))

# =============================================================================
# UNKNOWN COMMANDS HANDLER
# =============================================================================

@dp.message()
async def unknown_command(message: Message):
    if await check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Too many requests. Please wait a bit.")
        return
        
    await message.answer(
        "❌ Unknown command. Use /help to see available commands.",
        reply_markup=await get_main_keyboard(message.from_user.id)
    )

# =============================================================================
# MAIN FUNCTION
# =============================================================================

async def main():
    # Initialize database
    await db.init_db()
    
    # Create temp directory
    if not os.path.exists('temp_files'):
        os.makedirs('temp_files')
    
    # Cleanup old temp files
    cleanup_temp_files()
    
    logger.info("Bot started successfully")
    
    # Start polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())