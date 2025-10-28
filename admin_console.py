import sqlite3
import pandas as pd
import os
from datetime import datetime

class AdminConsole:
    def __init__(self, db_path='tyreterra.db'):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
    
    def show_users(self):
        """Показать всех пользователей"""
        df = pd.read_sql("SELECT * FROM users ORDER BY created_at DESC", self.conn)
        print("\n👥 ПОЛЬЗОВАТЕЛИ:")
        print(df.to_string(index=False))
        return df
    
    def show_stock(self):
        """Показать весь склад"""
        df = pd.read_sql("""
            SELECT s.*, u.name, u.company_name 
            FROM stock s 
            JOIN users u ON s.user_id = u.id 
            ORDER BY s.date DESC
        """, self.conn)
        print("\n📦 СКЛАД:")
        print(df.to_string(index=False))
        return df
    
    def edit_user(self, user_id, field, value):
        """Редактировать пользователя"""
        try:
            cursor = self.conn.cursor()
            cursor.execute(f"UPDATE users SET {field} = ? WHERE id = ?", (value, user_id))
            self.conn.commit()
            print(f"✅ Пользователь #{user_id} обновлен: {field} = {value}")
        except Exception as e:
            print(f"❌ Ошибка: {e}")
    
    def edit_stock(self, stock_id, field, value):
        """Редактировать запись склада"""
        try:
            cursor = self.conn.cursor()
            cursor.execute(f"UPDATE stock SET {field} = ? WHERE id = ?", (value, stock_id))
            self.conn.commit()
            print(f"✅ Запись склада #{stock_id} обновлена: {field} = {value}")
        except Exception as e:
            print(f"❌ Ошибка: {e}")
    
    def delete_user(self, user_id):
        """Удалить пользователя"""
        try:
            cursor = self.conn.cursor()
            # Сначала удаляем товары пользователя
            cursor.execute("DELETE FROM stock WHERE user_id = ?", (user_id,))
            # Затем удаляем пользователя
            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self.conn.commit()
            print(f"✅ Пользователь #{user_id} и его товары удалены")
        except Exception as e:
            print(f"❌ Ошибка: {e}")
    
    def delete_stock(self, stock_id):
        """Удалить запись склада"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM stock WHERE id = ?", (stock_id,))
            self.conn.commit()
            print(f"✅ Запись склада #{stock_id} удалена")
        except Exception as e:
            print(f"❌ Ошибка: {e}")
    
    def export_data(self):
        """Экспортировать все данные в Excel"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"admin_export_{timestamp}.xlsx"
        
        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            pd.read_sql("SELECT * FROM users", self.conn).to_excel(writer, sheet_name='Пользователи', index=False)
            pd.read_sql("SELECT * FROM stock", self.conn).to_excel(writer, sheet_name='Склад', index=False)
        
        print(f"✅ Данные экспортированы в {filename}")
        return filename
    
    def backup_database(self):
        """Создать бэкап базы данных"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"backup_tyreterra_{timestamp}.db"
        
        import shutil
        shutil.copy2(self.db_path, backup_name)
        print(f"✅ Бэкап создан: {backup_name}")
        return backup_name
    
    def show_stats(self):
        """Показать статистику"""
        cursor = self.conn.cursor()
        
        total_users = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_dealers = cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'Дилер'").fetchone()[0]
        total_buyers = cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'Покупатель'").fetchone()[0]
        total_stock = cursor.execute("SELECT COUNT(*) FROM stock").fetchone()[0]
        total_items = cursor.execute("SELECT SUM(qty_available) FROM stock").fetchone()[0] or 0
        
        print("\n📊 СТАТИСТИКА СИСТЕМЫ:")
        print(f"👥 Всего пользователей: {total_users}")
        print(f"🏭 Дилеров: {total_dealers}")
        print(f"🛒 Покупателей: {total_buyers}")
        print(f"📦 Записей на складе: {total_stock}")
        print(f"🔢 Общее количество товаров: {total_items}")
    
    def run_console(self):
        """Запуск консольного интерфейса"""
        while True:
            print("\n" + "="*60)
            print("🛠️  АДМИН-КОНСОЛЬ TYRETERRA")
            print("="*60)
            print("1. 👥 Показать пользователей")
            print("2. 📦 Показать склад")
            print("3. ✏️  Редактировать пользователя")
            print("4. 🔧 Редактировать запись склада")
            print("5. 🗑️  Удалить пользователя")
            print("6. ❌ Удалить запись склада")
            print("7. 📊 Статистика")
            print("8. 💾 Экспорт данных")
            print("9. 🔄 Создать бэкап")
            print("0. 🚪 Выход")
            print("-"*60)
            
            choice = input("Выберите действие (0-9): ").strip()
            
            if choice == '1':
                self.show_users()
            
            elif choice == '2':
                self.show_stock()
            
            elif choice == '3':
                user_id = input("ID пользователя: ")
                field = input("Поле для редактирования: ")
                value = input("Новое значение: ")
                self.edit_user(user_id, field, value)
            
            elif choice == '4':
                stock_id = input("ID записи склада: ")
                field = input("Поле для редактирования: ")
                value = input("Новое значение: ")
                self.edit_stock(stock_id, field, value)
            
            elif choice == '5':
                user_id = input("ID пользователя для удаления: ")
                confirm = input(f"Вы уверены, что хотите удалить пользователя #{user_id}? (y/n): ")
                if confirm.lower() == 'y':
                    self.delete_user(user_id)
            
            elif choice == '6':
                stock_id = input("ID записи склада для удаления: ")
                confirm = input(f"Вы уверены, что хотите удалить запись #{stock_id}? (y/n): ")
                if confirm.lower() == 'y':
                    self.delete_stock(stock_id)
            
            elif choice == '7':
                self.show_stats()
            
            elif choice == '8':
                self.export_data()
            
            elif choice == '9':
                self.backup_database()
            
            elif choice == '0':
                print("👋 До свидания!")
                break
            
            else:
                print("❌ Неверный выбор. Попробуйте снова.")
            
            input("\nНажмите Enter для продолжения...")
    
    def __del__(self):
        if hasattr(self, 'conn'):
            self.conn.close()

if __name__ == "__main__":
    console = AdminConsole()
    console.run_console()