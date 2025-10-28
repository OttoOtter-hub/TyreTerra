import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
import pandas as pd
import aiofiles
from database import db
import os

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токен бота
BOT_TOKEN = "8294936286:AAGfR-q_GGWIlxS4QlOwhAsJyFtSgFKKK_I"

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Состояния для добавления товара
class AddStock(StatesGroup):
    waiting_for_size = State()
    waiting_for_load_index = State()
    waiting_for_brand = State()
    waiting_for_country = State()
    waiting_for_qty = State()
    waiting_for_price = State()
    waiting_for_region = State()

# Состояния для поиска
class SearchStock(StatesGroup):
    waiting_for_size = State()
    waiting_for_load_index = State()
    waiting_for_brand = State()
    waiting_for_country = State()
    waiting_for_region = State()

# Клавиатура для выбора роли
def get_role_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Дилер"), KeyboardButton(text="Покупатель")],
            [KeyboardButton(text="Дилер и Покупатель")]
        ],
        resize_keyboard=True
    )
    return keyboard

# Клавиатура для главного меню
def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/addstock"), KeyboardButton(text="/mystock")],
            [KeyboardButton(text="/search"), KeyboardButton(text="/help")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )
    return keyboard

# Команда /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    
    # Проверяем, зарегистрирован ли пользователь
    user = db.fetchone("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
    
    if not user:
        await message.answer(
            f"Добро пожаловать в Tyreterra, {user_name}!\n"
            "Пожалуйста, выберите вашу роль:",
            reply_markup=get_role_keyboard()
        )
    else:
        await message.answer(
            f"С возвращением, {user_name}!\n"
            "Используйте команды для работы с системой:",
            reply_markup=get_main_keyboard()
        )

# Обработка выбора роли
@dp.message(F.text.in_(["Дилер", "Покупатель", "Дилер и Покупатель"]))
async def process_role(message: Message):
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    role = message.text
    
    # Сохраняем пользователя в базу данных
    try:
        db.execute(
            "INSERT INTO users (telegram_id, name, role) VALUES (?, ?, ?)",
            (user_id, user_name, role)
        )
        await message.answer(
            f"Отлично! Вы зарегистрированы как {role}.\n\n"
            "Доступные команды:\n"
            "/addstock - добавить товар на склад\n"
            "/mystock - посмотреть мой склад\n"
            "/search - поиск товаров\n"
            "/help - помощь",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        await message.answer("Произошла ошибка при регистрации. Попробуйте снова.")

# Команда /addstock - начало процесса добавления товара
@dp.message(Command("addstock"))
async def cmd_addstock(message: Message, state: FSMContext):
    await message.answer(
        "Давайте добавим новый товар на склад.\n"
        "Введите размер шины (например: 195/65 R15):\n\n"
        "❌ Для отмены введите 'отмена' или нажмите кнопку 'Отмена'",
        reply_markup=get_main_keyboard()  # Это добавит кнопку отмены
    )
    await state.set_state(AddStock.waiting_for_size)

# Обработка размера шины
@dp.message(AddStock.waiting_for_size)
async def process_size(message: Message, state: FSMContext):
    await state.update_data(tyre_size=message.text)
    await message.answer("Введите индекс нагрузки (например: 91):")
    await state.set_state(AddStock.waiting_for_load_index)

# Обработка индекса нагрузки
@dp.message(AddStock.waiting_for_load_index)
async def process_load_index(message: Message, state: FSMContext):
    await state.update_data(load_index=message.text)
    await message.answer("Введите бренд шины:")
    await state.set_state(AddStock.waiting_for_brand)

# Обработка бренда
@dp.message(AddStock.waiting_for_brand)
async def process_brand(message: Message, state: FSMContext):
    await state.update_data(brand=message.text)
    await message.answer("Введите страну производства:")
    await state.set_state(AddStock.waiting_for_country)

# Обработка страны производства
@dp.message(AddStock.waiting_for_country)
async def process_country(message: Message, state: FSMContext):
    await state.update_data(country=message.text)
    await message.answer("Введите количество (только цифры):")
    await state.set_state(AddStock.waiting_for_qty)

# Обработка количества
@dp.message(AddStock.waiting_for_qty)
async def process_qty(message: Message, state: FSMContext):
    try:
        qty = int(message.text)
        if qty <= 0:
            await message.answer("Количество должно быть положительным числом. Попробуйте снова:")
            return
        await state.update_data(qty=qty)
        await message.answer("Введите цену за единицу (только цифры):")
        await state.set_state(AddStock.waiting_for_price)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число для количества:")

# Обработка цены
@dp.message(AddStock.waiting_for_price)
async def process_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
        if price <= 0:
            await message.answer("Цена должна быть положительным числом. Попробуйте снова:")
            return
        await state.update_data(price=price)
        await message.answer("Введите регион:")
        await state.set_state(AddStock.waiting_for_region)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число для цены:")

# Обработка региона и сохранение товара
@dp.message(AddStock.waiting_for_region)
async def process_region(message: Message, state: FSMContext):
    try:
        user_data = await state.get_data()
        
        # Получаем ID пользователя
        user = db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
        
        if user:
            user_id = user[0]
            
            # Сохраняем товар в базу данных
            db.execute(
                """INSERT INTO stock 
                (user_id, tyre_size, load_index, brand, country, qty, price, region) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, user_data['tyre_size'], user_data['load_index'], 
                 user_data['brand'], user_data['country'], user_data['qty'], 
                 user_data['price'], message.text)
            )
            
            await message.answer(
                "✅ Товар успешно добавлен на склад!\n\n"
                f"📏 Размер: {user_data['tyre_size']}\n"
                f"⚡ Индекс нагрузки: {user_data['load_index']}\n"
                f"🏷️ Бренд: {user_data['brand']}\n"
                f"🌍 Страна: {user_data['country']}\n"
                f"📊 Количество: {user_data['qty']}\n"
                f"💰 Цена: {user_data['price']} руб.\n"
                f"📍 Регион: {message.text}",
                reply_markup=get_main_keyboard()
            )
        else:
            await message.answer("Ошибка: пользователь не найден. Используйте /start для регистрации.")
        
        # ОЧИЩАЕМ СОСТОЯНИЕ В ЛЮБОМ СЛУЧАЕ
        await state.clear()
        
    except Exception as e:
        # Если произошла ошибка, все равно очищаем состояние
        await state.clear()
        await message.answer(
            "❌ Произошла ошибка при добавлении товара. Попробуйте снова.",
            reply_markup=get_main_keyboard()
        )

        # Состояния для добавления товара
class AddStock(StatesGroup):
    waiting_for_size = State()
    waiting_for_load_index = State()
    waiting_for_brand = State()
    waiting_for_country = State()
    waiting_for_qty = State()
    waiting_for_price = State()
    waiting_for_region = State()

# Состояния для поиска
class SearchStock(StatesGroup):
    waiting_for_size = State()
    waiting_for_load_index = State()
    waiting_for_brand = State()
    waiting_for_country = State()
    waiting_for_region = State()

# ↓↓↓ ДОБАВЬТЕ ЭТОТ КОД ЗДЕСЬ ↓↓↓

# Команда для отмены операции
@dp.message(Command("cancel"))
@dp.message(F.text.casefold() == "отмена")
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer(
            "Нет активных операций для отмены.",
            reply_markup=get_main_keyboard()
        )
        return
    
    await state.clear()
    await message.answer(
        "❌ Операция отменена.",
        reply_markup=get_main_keyboard()
    )

# ↑↑↑ ДОБАВЬТЕ ЭТОТ КОД ЗДЕСЬ ↑↑↑

# Клавиатура для выбора роли
def get_role_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Дилер"), KeyboardButton(text="Покупатель")],
            [KeyboardButton(text="Дилер и Покупатель")]
        ],
        resize_keyboard=True
    )
    return keyboard

# Команда /search - поиск товаров
@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await message.answer(
        "🔍 Поиск товаров\n"
        "Введите размер шины для поиска (или 'все' для поиска всех товаров):\n\n"
        "❌ Для отмены введите 'отмена' или нажмите кнопку 'Отмена'",
        reply_markup=get_main_keyboard()  # Это добавит кнопку отмены
    )
    await state.set_state(SearchStock.waiting_for_size)

# Обработка поиска по размеру
@dp.message(SearchStock.waiting_for_size)
async def process_search_size(message: Message, state: FSMContext):
    if message.text.lower() == 'все':
        await state.update_data(tyre_size='%')
    else:
        await state.update_data(tyre_size=f'%{message.text}%')
    
    await message.answer("Введите индекс нагрузки для поиска (или 'все' для пропуска):")
    await state.set_state(SearchStock.waiting_for_load_index)

# Обработка поиска по индексу нагрузки
@dp.message(SearchStock.waiting_for_load_index)
async def process_search_load_index(message: Message, state: FSMContext):
    if message.text.lower() == 'все':
        await state.update_data(load_index='%')
    else:
        await state.update_data(load_index=f'%{message.text}%')
    
    await message.answer("Введите бренд для поиска (или 'все' для пропуска):")
    await state.set_state(SearchStock.waiting_for_brand)

# Обработка поиска по бренду
@dp.message(SearchStock.waiting_for_brand)
async def process_search_brand(message: Message, state: FSMContext):
    if message.text.lower() == 'все':
        await state.update_data(brand='%')
    else:
        await state.update_data(brand=f'%{message.text}%')
    
    await message.answer("Введите страну производства для поиска (или 'все' для пропуска):")
    await state.set_state(SearchStock.waiting_for_country)

# Обработка поиска по стране и выполнение поиска
@dp.message(SearchStock.waiting_for_country)
async def process_search_country(message: Message, state: FSMContext):
    if message.text.lower() == 'все':
        await state.update_data(country='%')
    else:
        await state.update_data(country=f'%{message.text}%')
    
    await message.answer("Введите регион для поиска (или 'все' для пропуска):")
    await state.set_state(SearchStock.waiting_for_region)

# Обработка поиска по региону и выполнение поиска
@dp.message(SearchStock.waiting_for_region)
async def process_search_region(message: Message, state: FSMContext):
    search_data = await state.get_data()
    
    if message.text.lower() == 'все':
        region_filter = '%'
    else:
        region_filter = f'%{message.text}%'
    
    # Выполняем поиск
    stock_items = db.fetchall(
        """SELECT s.*, u.name, u.contact 
        FROM stock s 
        JOIN users u ON s.user_id = u.id 
        WHERE s.tyre_size LIKE ? 
        AND s.load_index LIKE ? 
        AND s.brand LIKE ? 
        AND s.country LIKE ? 
        AND s.region LIKE ? 
        ORDER BY s.date DESC""",
        (search_data['tyre_size'], search_data['load_index'], 
         search_data['brand'], search_data['country'], region_filter)
    )
    
    if not stock_items:
        await message.answer(
            "❌ По вашему запросу ничего не найдено.",
            reply_markup=get_main_keyboard()
        )
    else:
        response = f"🔍 Найдено товаров: {len(stock_items)}\n\n"
        
        for item in stock_items:
            response += (
                f"📏 Размер: {item[2]}\n"
                f"⚡ Индекс нагрузки: {item[3]}\n"
                f"🏷️ Бренд: {item[4]}\n"
                f"🌍 Страна: {item[5]}\n"
                f"📊 Количество: {item[6]}\n"
                f"💰 Цена: {item[7]} руб.\n"
                f"📍 Регион: {item[8]}\n"
                f"👤 Продавец: {item[10]}\n"
                f"📞 Контакт: {item[11] if item[11] else 'Не указан'}\n"
                "─" * 30 + "\n"
            )
        
        # Разбиваем сообщение если оно слишком длинное
        if len(response) > 4000:
            parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
            for part in parts:
                await message.answer(part)
        else:
            await message.answer(response)
    
    await state.clear()
    await message.answer("Поиск завершен.", reply_markup=get_main_keyboard())

# Состояния для добавления товара
class AddStock(StatesGroup):
    waiting_for_size = State()
    waiting_for_load_index = State()
    waiting_for_brand = State()
    waiting_for_country = State()
    waiting_for_qty = State()
    waiting_for_price = State()
    waiting_for_region = State()

# Состояния для поиска
class SearchStock(StatesGroup):
    waiting_for_size = State()
    waiting_for_load_index = State()
    waiting_for_brand = State()
    waiting_for_country = State()
    waiting_for_region = State()

# ↓↓↓ ДОБАВЬТЕ ЭТОТ КОД ЗДЕСЬ ↓↓↓

# Команда для отмены операции
@dp.message(Command("cancel"))
@dp.message(F.text.casefold() == "отмена")
async def cancel_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer(
            "Нет активных операций для отмены.",
            reply_markup=get_main_keyboard()
        )
        return
    
    await state.clear()
    await message.answer(
        "❌ Операция отменена.",
        reply_markup=get_main_keyboard()
    )

# ↑↑↑ ДОБАВЬТЕ ЭТОТ КОД ЗДЕСЬ ↑↑↑

# Клавиатура для выбора роли
def get_role_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Дилер"), KeyboardButton(text="Покупатель")],
            [KeyboardButton(text="Дилер и Покупатель")]
        ],
        resize_keyboard=True
    )
    return keyboard

# Обработка загрузки Excel файлов
@dp.message(F.document)
async def handle_excel_file(message: Message):
    if message.document.mime_type in ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 
                                    'application/vnd.ms-excel']:
        
        # Получаем информацию о файле
        file_id = message.document.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path
        
        # Создаем папку для загрузок если ее нет
        if not os.path.exists('uploads'):
            os.makedirs('uploads')
        
        # Скачиваем файл
        download_path = f"uploads/{message.document.file_name}"
        await bot.download_file(file_path, download_path)
        
        try:
            # Читаем Excel файл
            df = pd.read_excel(download_path)
            
            # Проверяем необходимые колонки
            required_columns = ['tyre_size', 'load_index', 'brand', 'country', 'qty', 'price', 'region']
            missing_columns = [col for col in required_columns if col not in df.columns]
            
            if missing_columns:
                await message.answer(f"❌ В файле отсутствуют колонки: {', '.join(missing_columns)}")
                return
            
            # Получаем ID пользователя
            user = db.fetchone("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,))
            if not user:
                await message.answer("❌ Сначала зарегистрируйтесь с помощью /start")
                return
            
            user_id = user[0]
            added_count = 0
            
            # Добавляем товары в базу данных
            for _, row in df.iterrows():
                try:
                    db.execute(
                        """INSERT INTO stock 
                        (user_id, tyre_size, load_index, brand, country, qty, price, region) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, row['tyre_size'], row['load_index'], row['brand'], 
                         row['country'], int(row['qty']), float(row['price']), row['region'])
                    )
                    added_count += 1
                except Exception as e:
                    logger.error(f"Ошибка при добавлении строки: {e}")
                    continue
            
            await message.answer(f"✅ Успешно добавлено {added_count} товаров из Excel файла!")
            
        except Exception as e:
            await message.answer(f"❌ Ошибка при обработке Excel файла: {str(e)}")
        
        # Удаляем временный файл
        try:
            os.remove(download_path)
        except:
            pass
    else:
        await message.answer("❌ Пожалуйста, отправьте файл в формате Excel (.xlsx)")

# Команда /help
@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "🤖 Tyreterra Bot - Помощь\n\n"
        "Доступные команды:\n"
        "/start - Регистрация и начало работы\n"
        "/addstock - Добавить товар на склад\n"
        "/mystock - Показать мой склад\n"
        "/search - Поиск товаров у других пользователей\n"
        "/help - Эта справка\n\n"
        "📊 Загрузка данных:\n"
        "Вы можете загрузить данные из Excel файла. "
        "Просто отправьте .xlsx файл боту. Файл должен содержать колонки:\n"
        "tyre_size, load_index, brand, country, qty, price, region"
    )
    await message.answer(help_text)

# Обработка неизвестных команд
@dp.message()
async def unknown_message(message: Message):
    await message.answer(
        "Неизвестная команда. Используйте /help для списка доступных команд.",
        reply_markup=get_main_keyboard()
    )

# Запуск бота
async def main():
    logger.info("Бот Tyreterra запускается...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())