#!/usr/bin/env python3
"""
Скрипт для проверки соответствия приватного ключа и адреса кошелька
"""

from eth_account import Account

def check_wallet_match(private_key: str, expected_address: str):
    """
    Проверяет соответствие приватного ключа и адреса кошелька
    """
    print(f"🔍 Проверка соответствия приватного ключа и адреса...")
    print(f"🔑 Приватный ключ: {private_key[:10]}...")
    print(f"📍 Ожидаемый адрес: {expected_address}")
    
    try:
        # Создаем аккаунт из приватного ключа
        account = Account.from_key(private_key)
        actual_address = account.address
        
        print(f"✅ Адрес из приватного ключа: {actual_address}")
        
        # Проверяем совпадение
        if actual_address.lower() == expected_address.lower():
            print(f"✅ SUCCESS: Приватный ключ соответствует адресу!")
            return True
        else:
            print(f"❌ MISMATCH: Адреса не совпадают!")
            print(f"   Ожидаемый: {expected_address}")
            print(f"   Фактический: {actual_address}")
            return False
            
    except Exception as e:
        print(f"❌ Ошибка при создании аккаунта: {e}")
        return False

if __name__ == "__main__":
    # Замените на ваш реальный приватный ключ
    PRIVATE_KEY = "YOUR_PRIVATE_KEY_HERE"
    EXPECTED_ADDRESS = "0x9D5aa857a554F842d668D7Dd0120F2fB450dD248"
    
    print("=" * 60)
    print("🔍 ПРОВЕРКА СООТВЕТСТВИЯ ПРИВАТНОГО КЛЮЧА И АДРЕСА")
    print("=" * 60)
    
    if PRIVATE_KEY == "YOUR_PRIVATE_KEY_HERE":
        print("❌ Замените YOUR_PRIVATE_KEY_HERE на ваш реальный приватный ключ!")
        print("   Пример: PRIVATE_KEY = \"0x1234567890abcdef...\"")
    else:
        check_wallet_match(PRIVATE_KEY, EXPECTED_ADDRESS)
    
    print("=" * 60)
