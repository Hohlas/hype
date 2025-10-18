#!/usr/bin/env python3
"""
Тест функции float_to_wire для проверки округления цен
"""

def float_to_wire(x: float, is_price: bool = False, btc_tick_size: float = 0.5) -> str:
    """Конвертирует float в строку с правильным форматированием"""
    if is_price:
            # Для цен округляем до размера тика
        if btc_tick_size:
            x = round(x / btc_tick_size) * btc_tick_size
        
        # Для BTC-SPOT принудительно округляем до целых чисел
        # (API может требовать только целые числа для некоторых активов)
        x = round(x)
        return str(int(x))
    else:
        # Для остальных чисел (размер ордера и т.д.) используем прежнюю логику
        rounded = "{:.8f}".format(x)
        # Убираем trailing zeros, но оставляем хотя бы один знак после точки
        if '.' in rounded:
            rounded = rounded.rstrip('0')
            if rounded.endswith('.'):
                rounded = rounded + '0'
    return rounded

# Тестируем различные цены
test_prices = [
    106954.5,   # Должно стать 106954 или 106955
    106954.0,   # Должно стать 106954
    106954.3,   # Должно стать 106954
    106954.7,   # Должно стать 106955
    106954.25,  # Должно стать 106954
    106954.75,  # Должно стать 106955
]

print("Testing float_to_wire function:")
print("=" * 50)

for price in test_prices:
    result = float_to_wire(price, is_price=True)
    # Проверяем делится ли результат на 0.5
    try:
        float_result = float(result)
        divisible = (float_result * 2) == int(float_result * 2)
        status = "OK" if divisible else "FAIL"
        print(f"{status} {price:8.2f} -> {result:8s} (divisible by 0.5: {divisible})")
    except:
        print(f"FAIL {price:8.2f} -> {result:8s} (conversion error)")

print("=" * 50)
print("OK = correctly rounded")
print("FAIL = incorrectly rounded")
