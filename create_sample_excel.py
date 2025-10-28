import pandas as pd

# Создаем пример Excel файла для тестирования
data = {
    'tyre_size': ['195/65 R15', '205/55 R16', '225/45 R17'],
    'load_index': ['91', '94', '95'],
    'brand': ['Michelin', 'Bridgestone', 'Pirelli'],
    'country': ['Франция', 'Япония', 'Италия'],
    'qty': [10, 15, 8],
    'price': [4500.0, 5200.0, 6800.0],
    'region': ['Москва', 'Санкт-Петербург', 'Москва']
}

df = pd.DataFrame(data)
df.to_excel('пример_файла.xlsx', index=False)
print("Создан пример файла 'пример_файла.xlsx'")