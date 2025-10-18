#!/usr/bin/env python3
"""
Тестовый скрипт для проверки исправленной EIP-712 подписи для Hyperliquid API
"""

import asyncio
import json
import time
from modules.hyperliquid_browser import HyperliquidBrowser

async def test_signature():
    """Тестируем исправленную подпись"""
    
    # Замените на ваши реальные данные
    WALLET_ADDRESS = "0x9D5aa857a554F842d668D7Dd0120F2fB450dD248"  # Ваш адрес
    PRIVATE_KEY = "your_private_key_here"  # Замените на ваш приватный ключ
    
    print("🔍 Тестирование исправленной EIP-712 подписи...")
    
    # Создаем клиент
    client = HyperliquidBrowser(
        wallet_address=WALLET_ADDRESS,
        private_key=PRIVATE_KEY,
        label="TEST"
    )
    
    try:
        # Тестовый action для spot ордера
        test_action = {
            "type": "order",
            "orders": [{
                "a": 10142,  # BTC-SPOT asset ID
                "b": True,   # покупка
                "p": "106875.50",  # цена
                "s": "0.00105",     # размер
                "r": False,         # не reduce-only
                "t": {"limit": {"tif": "Gtc"}}  # лимитный ордер
            }],
            "grouping": "na"
        }
        
        # Создаем nonce
        nonce = int(time.time() * 1000)
        
        print(f"🔍 Тестовый action: {json.dumps(test_action, indent=2)}")
        print(f"🔍 Nonce: {nonce}")
        
        # Тестируем подпись
        signature = client.sign_l1_action(test_action, nonce, is_mainnet=True)
        
        print(f"✅ Подпись создана успешно!")
        print(f"🔍 Signature: {json.dumps(signature, indent=2)}")
        
        # Формируем полный payload
        payload = {
            "action": test_action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None
        }
        
        print(f"🔍 Полный payload: {json.dumps(payload, indent=2)}")
        
        # Теперь можно отправить на API для проверки
        print("\n💡 Теперь вы можете отправить этот payload на Hyperliquid API")
        print("💡 Ожидаемый результат: успешное создание ордера")
        print("💡 Если все еще получаете ошибку 'User or API Wallet does not exist',")
        print("   проверьте правильность приватного ключа и адреса кошелька")
        
    except Exception as e:
        print(f"❌ Ошибка при тестировании: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_signature())
