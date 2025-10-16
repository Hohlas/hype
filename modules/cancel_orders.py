"""
Cancel Orders Module for Hyperliquid Bot
"""

from loguru import logger
from modules.utils import async_sleep


async def cancel_all_orders(client):
    """
    Отмена всех активных ордеров для всех токенов.
    """
    try:
        client.browser.log_message("🔄 Starting to cancel all orders...", level="INFO")
        
        # Получаем все активные ордера
        orders = await client.browser.get_account_orders()
        
        if not orders:
            client.browser.log_message("✅ No active orders found", level="INFO")
            return True
            
        client.browser.log_message(f"📋 Found {len(orders)} active orders", level="INFO")
        
        # Группируем ордера по токенам
        orders_by_token = {}
        for order in orders:
            token = order.get("coin")
            if token:
                if token not in orders_by_token:
                    orders_by_token[token] = []
                orders_by_token[token].append(order)
        
        # Отменяем ордера по токенам
        total_cancelled = 0
        for token, token_orders in orders_by_token.items():
            client.browser.log_message(f"🔄 Cancelling {len(token_orders)} orders for {token}...", level="INFO")
            
            for order in token_orders:
                try:
                    order_id = order.get("oid")
                    if order_id:
                        await client.browser.cancel_order(token, order_id)
                        total_cancelled += 1
                        client.browser.log_message(f"✅ Cancelled order {order_id} for {token}", level="INFO")
                        await async_sleep(0.1)  # Небольшая пауза между отменами
                        
                except Exception as e:
                    client.browser.log_message(f"❌ Failed to cancel order {order.get('oid')} for {token}: {e}", level="ERROR")
        
        client.browser.log_message(f"✅ Successfully cancelled {total_cancelled} orders", level="INFO")
        return True
        
    except Exception as e:
        client.browser.log_message(f"❌ Failed to cancel orders: {e}", level="ERROR")
        return False
