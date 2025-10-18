#!/usr/bin/env python3
"""
Скрипт для проверки баланса на Hyperliquid
"""

import asyncio
import sys
import os

# Добавляем путь к модулям
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from modules.hyperliquid_browser import HyperliquidBrowser

async def check_balance():
    """Проверяем баланс на Hyperliquid"""
    
    # Замените на ваши данные
    WALLET_ADDRESS = "0x9D5aa857a554F842d668D7Dd0120F2fB450dD248"
    PRIVATE_KEY = "your_private_key_here"  # Замените на ваш приватный ключ
    
    print("🔍 Проверка баланса на Hyperliquid...")
    
    # Создаем клиент
    client = HyperliquidBrowser(
        wallet_address=WALLET_ADDRESS,
        private_key=PRIVATE_KEY,
        label="BALANCE_CHECK"
    )
    
    try:
        # Проверяем spot баланс
        spot_balance = await client.get_balance()
        print(f"💰 USDC Spot Balance: ${spot_balance:.2f}")
        
        # Проверяем spot account info
        spot_info = await client.get_spot_account_info()
        balances = spot_info.get("balances", [])
        
        print(f"\n📊 Все Spot балансы:")
        total_spot_value = 0
        for balance in balances:
            coin = balance.get("coin", "")
            total = float(balance.get("total", 0))
            if total > 0:
                print(f"  {coin}: {total:.6f}")
                if coin == "USDC":
                    total_spot_value += total
        
        # Проверяем открытые ордера
        orders = await client.get_account_orders()
        if orders:
            print(f"\n📋 Открытые ордера ({len(orders)}):")
            total_reserved_usdc = 0
            
            for order in orders:
                coin = order.get("coin", "")
                side = "BUY" if order.get("isBuy", False) else "SELL"
                size = float(order.get("sz", 0))
                price = float(order.get("limitPx", 0))
                oid = order.get("oid", "N/A")
                
                # Рассчитываем зарезервированную сумму для BUY ордеров
                if side == "BUY" and price > 0:
                    reserved_usdc = size * price
                    total_reserved_usdc += reserved_usdc
                    print(f"  {coin} {side}: {size:.6f} @ ${price:.2f} (Reserved: ${reserved_usdc:.2f}) [ID: {oid}]")
                else:
                    print(f"  {coin} {side}: {size:.6f} @ ${price:.2f} [ID: {oid}]")
            
            print(f"\n💸 Общая зарезервированная сумма для BUY ордеров: ${total_reserved_usdc:.2f}")
            print(f"💵 Доступный баланс для новых ордеров: ${spot_balance - total_reserved_usdc:.2f}")
        else:
            print(f"\n📋 Нет открытых ордеров")
        
        # Проверяем futures баланс
        futures_info = await client.get_account_info()
        account_value = float(futures_info.get('marginSummary', {}).get('accountValue', '0.0'))
        print(f"\n🚀 Futures Account Value: ${account_value:.2f}")
        
        # Проверяем позиции
        positions = await client.get_account_positions()
        if positions:
            print(f"\n📈 Активные позиции ({len(positions)}):")
            for pos in positions:
                coin = pos["position"]["coin"]
                size = float(pos["position"]["szi"])
                entry_price = float(pos["position"]["entryPx"]) if pos["position"]["entryPx"] else 0.0
                print(f"  {coin}: {size:.6f} @ ${entry_price:.2f}")
        else:
            print(f"\n📈 Нет активных позиций")
            
        # Анализ проблемы с балансом
        if spot_balance < 100:
            print(f"\n⚠️  ВНИМАНИЕ: Низкий баланс USDC (${spot_balance:.2f})")
            print("   Рекомендуется пополнить баланс для корректной работы бота")
        
        if orders and total_reserved_usdc > spot_balance * 0.8:
            print(f"\n⚠️  ВНИМАНИЕ: Большая часть баланса зарезервирована в ордерах")
            print(f"   Зарезервировано: ${total_reserved_usdc:.2f} из ${spot_balance:.2f}")
            print("   Рассмотрите отмену части ордеров для освобождения средств")
            
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(check_balance())
