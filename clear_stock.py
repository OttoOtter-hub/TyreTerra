from database import db

def clear_stock():
    db.execute("DELETE FROM stock")
    print("✅ Склад полностью очищен!")
    print("✅ Все товары удалены!")

if __name__ == "__main__":
    clear_stock()