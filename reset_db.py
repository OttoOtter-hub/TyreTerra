import os
import sqlite3

def reset_database():
    # Удаляем файл базы данных если он существует
    if os.path.exists('tyreterra.db'):
        os.remove('tyreterra.db')
        print("✅ База данных удалена")
    
    # Создаем новую базу
    conn = sqlite3.connect('tyreterra.db')
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
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
    cursor.execute('''
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
    
    conn.commit()
    conn.close()
    print("✅ Новая база данных создана")
    print("✅ Бот полностью сброшен!")

if __name__ == "__main__":
    reset_database()