#!/usr/bin/env python3
"""
Скрипт для отмены всех открытых ордеров на Hyperliquid
"""

import asyncio
import sys
import os

# Добавляем путь к модулям
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from modules.hyperliquid_browser import HyperliquidBrowser

async def cancel_all_orders():
    """Отменяем все открытые ордера"""
    
    # Замените на ваши данные
    WALLET_ADDRESS = "0x9D5aa857a554F842d668D7Dd0120F2fB450dD248"
    PRIVATE_KEY = "your_private_key_here"  # Замените на ваш приватный ключ
    
    print("🚫 Отмена всех открытых ордеров на Hyperliquid...")
    
    # Создаем клиент
    client = HyperliquidBrowser(
        wallet_address=WALLET_ADDRESS,
        private_key=PRIVATE_KEY,
        label="CANCEL_ORDERS"
    )
    
    try:
        # Получаем все открытые ордера
        orders = await client.get_account_orders()
        
        if not orders:
            print("✅ Нет открытых ордеров для отмены")
            return
        
        print(f"📋 Найдено {len(orders)} открытых ордеров:")
        
        # Показываем ордера перед отменой
        for i, order in enumerate(orders, 1):
            coin = order.get("coin", "")
            side = "BUY" if order.get("isBuy", False) else "SELL"
            size = float(order.get("sz", 0))
            price = float(order.get("limitPx", 0))
            oid = order.get("oid", "N/A")
            print(f"  {i}. {coin} {side}: {size:.6f} @ ${price:.2f} [ID: {oid}]")
        
        # Подтверждение от пользователя
        confirm = input(f"\n❓ Отменить все {len(orders)} ордеров? (y/N): ").strip().lower()
        
        if confirm != 'y':
            print("❌ Отмена операции")
            return
        
        # Отменяем ордера
        cancelled_count = 0
        failed_count = 0
        
        for order in orders:
            try:
                coin = order.get("coin", "")
                oid = order.get("oid")
                
                if oid:
                    await client.cancel_order(coin, oid)
                    cancelled_count += 1
                    print(f"✅ Отменен ордер {coin} [ID: {oid}]")
                    await asyncio.sleep(0.1)  # Небольшая пауза между отменами
                else:
                    print(f"⚠️  Пропущен ордер {coin} (нет ID)")
                    failed_count += 1
                    
            except Exception as e:
                print(f"❌ Ошибка отмены ордера {coin}: {e}")
                failed_count += 1
        
        print(f"\n📊 Результат:")
        print(f"  ✅ Успешно отменено: {cancelled_count}")
        print(f"  ❌ Ошибок: {failed_count}")
        
        # Проверяем баланс после отмены
        print(f"\n🔍 Проверка баланса после отмены...")
        balance = await client.get_balance()
        print(f"💰 Доступный USDC баланс: ${balance:.2f}")
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(cancel_all_orders())
