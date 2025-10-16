"""
Parse Mode Module for Hyperliquid Bot
"""

from loguru import logger
from modules.utils import async_sleep


async def parse_mode(client):
    """
    Режим парсинга данных аккаунта.
    """
    try:
        client.browser.log_message("🔍 Starting parse mode...", level="INFO")
        
        # Получаем информацию об аккаунте
        account_info = await client.browser.get_account_info()
        
        client.browser.log_message("📊 Account Information:", level="INFO")
        client.browser.log_message(f"   Wallet Address: {client.wallet_address}", level="INFO")
        
        # Показываем балансы
        balance = await client.browser.get_balance()
        client.browser.log_message(f"   USDC Balance: ${balance:.2f}", level="INFO")
        
        # Показываем позиции
        positions = await client.browser.get_account_positions()
        if positions:
            client.browser.log_message(f"📈 Active Positions ({len(positions)}):", level="INFO")
            for pos in positions:
                coin = pos["position"]["coin"]
                size = float(pos["position"]["szi"])
                entry_price = float(pos["position"]["entryPx"]) if pos["position"]["entryPx"] else 0.0
                client.browser.log_message(f"   {coin}: {size:.6f} @ ${entry_price:.2f}", level="INFO")
        else:
            client.browser.log_message("📈 No active positions", level="INFO")
        
        # Показываем активные ордера
        orders = await client.browser.get_account_orders()
        if orders:
            client.browser.log_message(f"📋 Active Orders ({len(orders)}):", level="INFO")
            for order in orders:
                coin = order.get("coin", "Unknown")
                side = "BUY" if order.get("isBuy") else "SELL"
                size = float(order.get("sz", 0))
                price = float(order.get("px", 0))
                client.browser.log_message(f"   {coin} {side}: {size:.6f} @ ${price:.2f}", level="INFO")
        else:
            client.browser.log_message("📋 No active orders", level="INFO")
        
        # Получаем цены поддерживаемых токенов
        client.browser.log_message("💰 Token Prices:", level="INFO")
        supported_tokens = ["ETH", "BTC", "USDC", "USDT"]
        
        for token in supported_tokens:
            try:
                price = await client.browser.get_token_price(token)
                client.browser.log_message(f"   {token}: ${price:.2f}", level="INFO")
                await async_sleep(0.1)  # Небольшая пауза между запросами
            except Exception as e:
                client.browser.log_message(f"   {token}: Price not available", level="WARNING")
        
        client.browser.log_message("✅ Parse mode completed", level="INFO")
        return True
        
    except Exception as e:
        client.browser.log_message(f"❌ Parse mode failed: {e}", level="ERROR")
        return False
