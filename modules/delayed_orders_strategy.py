"""
Hyperliquid Delayed Orders Strategy Module
==========================================

Этот модуль реализует стратегию с отложенными ордерами для spot торговли на Hyperliquid.

Основные принципы:
- Стоп-маркет ордера на пробой (сверху) с шагом STEP/2
- Лимитные ордера на откат (снизу) с шагом STEP*2 от min_TP_price
- Автоматические TP ордера для каждого BUY ордера
- Всегда только по одному ордеру сверху и снизу

Логика работы:
1. Если нет BUY ордеров выше текущей цены → стоп-маркет BUY на current_price + STEP/2
2. Если нет BUY ордеров ниже текущей цены → лимитный BUY на min_TP_price - STEP*2
3. Для каждого BUY ордера сразу создается TP SELL на entry_price + STEP
4. Ордера не отменяются до исполнения

Использование:
    from modules.delayed_orders_strategy import trade_delayed_orders_strategy
    
    await trade_delayed_orders_strategy(
        client=your_hyperliquid_client_instance,
        token_name="BTC"
    )
"""

from random import uniform, randint
from decimal import Decimal
from loguru import logger
import pandas as pd
from datetime import datetime
import os
import asyncio
from .utils import round_cut, async_sleep, TgReport, send_warning_notification
from .retry import CustomError
import settings

# Кэш для ограничения частоты предупреждающих сообщений
_order_skipped_cache = {}


async def calculate_real_profit(client, token_name: str, sold_amount: float, sell_price: float) -> float:
    """
    Рассчитывает реальную прибыль от сделки с учетом комиссии
    
    Args:
        client: HyperliquidClient instance
        token_name: Название токена (например, "BTC")
        sold_amount: Количество проданных токенов
        sell_price: Цена продажи
    
    Returns:
        float: Реальная прибыль в USDC
    """
    try:
        # Получаем историю сделок для поиска цены покупки
        trades_history = await client.browser.get_all_orders_history(token_name, limit=50)
        
        # Отладочная информация
        client.browser.log_message(f"🔍 DEBUG: Retrieved {len(trades_history) if trades_history else 0} trades from history", level="DEBUG")
        if trades_history:
            client.browser.log_message(f"🔍 DEBUG: First trade sample: {trades_history[0] if trades_history else 'None'}", level="DEBUG")
        
        if not trades_history:
            client.browser.log_message("⚠️ No trade history found for profit calculation", level="INFO")
            # Используем упрощенный расчет на основе STEP
            step_profit = sold_amount * float(settings.STEP)
            commission = sold_amount * sell_price * 0.0002  # 0.02% комиссия
            estimated_profit = step_profit - commission
            client.browser.log_message(f"💰 Using STEP-based profit estimation: ${estimated_profit:.2f}", level="INFO")
            return estimated_profit
        
        # Ищем последние BUY сделки для расчета средней цены покупки
        buy_trades = []
        total_bought = 0.0
        
        for trade in trades_history:
            side = trade.get('side', '').upper()
            if side in ['BUY', 'B']:
                trade_amount = float(trade.get('sz', 0))
                trade_price = float(trade.get('px', 0))
                
                buy_trades.append({
                    'amount': trade_amount,
                    'price': trade_price,
                    'value': trade_amount * trade_price
                })
                total_bought += trade_amount
                
                # Если набрали достаточно для покрытия проданного количества
                if total_bought >= sold_amount:
                    break
        
        if not buy_trades or total_bought < sold_amount:
            client.browser.log_message("⚠️ Insufficient buy history for profit calculation", level="INFO")
            return 0.0
        
        # Рассчитываем средневзвешенную цену покупки
        weighted_price = 0.0
        remaining_to_cover = sold_amount
        
        for trade in reversed(buy_trades):  # Берем последние сделки
            if remaining_to_cover <= 0:
                break
                
            amount_to_use = min(trade['amount'], remaining_to_cover)
            weighted_price += amount_to_use * trade['price']
            remaining_to_cover -= amount_to_use
        
        avg_buy_price = weighted_price / sold_amount
        
        # Рассчитываем прибыль
        sell_value = sold_amount * sell_price
        buy_value = sold_amount * avg_buy_price
        
        # Примерная комиссия (обычно 0.02% на Hyperliquid)
        commission_rate = 0.0002  # 0.02%
        commission = sell_value * commission_rate
        
        real_profit = sell_value - buy_value - commission
        
        client.browser.log_message(
            f"💰 Profit calculation: Sold {sold_amount:.6f}BTC @ ${sell_price:.2f} = ${sell_value:.2f}, "
            f"Bought @ ${avg_buy_price:.2f} = ${buy_value:.2f}, Commission ${commission:.2f}, "
            f"Net profit: ${real_profit:.2f}", 
            level="INFO"
        )
        
        return real_profit
        
    except Exception as e:
        client.browser.log_message(f"Failed to calculate real profit: {e}", level="ERROR")
        return 0.0


async def get_actual_fill_price_from_history(client, token_name: str, order_time: float, timeout: int = 10):
    """
    Получает фактическую цену исполнения из истории сделок
    
    Args:
        client: HyperliquidClient instance
        token_name: Название токена (например, "BTC")
        order_time: Время размещения ордера (timestamp)
        timeout: Таймаут ожидания в секундах
    
    Returns:
        tuple: (actual_price, actual_quantity) или (None, None) если не найдено
    """
    try:
        import time
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            # Получаем историю сделок
            trades_history = await client.browser.get_all_orders_history(token_name, limit=10)
            
            if trades_history:
                # Ищем самую свежую сделку BUY после времени размещения ордера
                for trade in trades_history:
                    trade_time = trade.get('time', 0) / 1000  # Конвертируем из миллисекунд
                    
                    # Проверяем, что это сделка после нашего ордера
                    if trade_time > order_time:
                        side = trade.get('side', '').upper()
                        if side in ['BUY', 'B']:
                            actual_price = float(trade.get('px', 0))
                            actual_quantity = float(trade.get('sz', 0))
                            
                            if actual_price > 0 and actual_quantity > 0:
                                client.browser.log_message(
                                    f"✅ Found actual fill price from history: ${actual_price:.2f}, qty: {actual_quantity:.6f}", 
                                    level="INFO"
                                )
                                return actual_price, actual_quantity
            
            # Ждем 1 секунду перед следующей попыткой
            await asyncio.sleep(1)
        
        client.browser.log_message(
            f"⚠️ Could not find actual fill price in history after {timeout}s timeout", 
            level="INFO"
        )
        return None, None
        
    except Exception as e:
        client.browser.log_message(f"Failed to get actual fill price from history: {e}", level="ERROR")
        return None, None


async def log_statistics_to_excel(client, operation: str, btc_amount: float, price: float, 
                                 usdc_balance: float, btc_balance: float, total_value: float):
    """
    Записывает статистику операции в Excel файл hype_stat.xlsx
    
    Args:
        client: HyperliquidClient instance
        operation: Тип операции (First Position/Averaging/Pyramiding/set TP/Take Profit)
        btc_amount: Объем BTC операции
        price: Цена по которой проводилась операция
        usdc_balance: Баланс USDC
        btc_balance: Баланс BTC
        total_value: Суммарная стоимость всех активов
    """
    try:
        # Получаем текущую цену BTC
        current_btc_price = float(await client.browser.get_token_price("BTC"))
        
        # Создаем запись статистики
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Подготавливаем данные для записи
        new_row = {
            'Timestamp': timestamp,
            'Current BTC Price': current_btc_price,
            'Operation': operation,
            'BTC Amount': btc_amount,
            'Operation Price': price,
            'USDC Balance': usdc_balance,
            'BTC Balance': btc_balance,
            'Total Value': total_value
        }
        
        # Путь к файлу статистики
        stats_file = "hype_stat.xlsx"
        
        # Проверяем, существует ли файл
        if os.path.exists(stats_file):
            # Читаем существующий файл
            df = pd.read_excel(stats_file)
        else:
            # Создаем новый DataFrame с заголовками
            df = pd.DataFrame(columns=[
                'Timestamp', 'Current BTC Price', 'Operation', 'BTC Amount', 
                'Operation Price', 'USDC Balance', 'BTC Balance', 'Total Value'
            ])
        
        # Добавляем новую строку (исправлено для избежания предупреждения pandas)
        new_df = pd.DataFrame([new_row])
        df = pd.concat([df, new_df], ignore_index=True)
        
        # Сохраняем в Excel файл
        df.to_excel(stats_file, index=False)
        
        client.browser.log_message(f"📊 Statistics logged: {operation} | {btc_amount:.6f}BTC @ ${price:.2f}", level="DEBUG")
        
    except Exception as e:
        client.browser.log_message(f"Failed to log statistics: {e}", level="ERROR")


def can_log_warning_message(account_label: str, message_type: str, cooldown_minutes: int = 5) -> bool:
    """
    Проверяет, можно ли выводить предупреждающее сообщение для аккаунта.
    """
    global _order_skipped_cache
    from time import time
    
    current_time = time()
    cache_key = f"{account_label}_{message_type}"
    last_log_time = _order_skipped_cache.get(cache_key, 0)
    cooldown_seconds = cooldown_minutes * 60
    
    if current_time - last_log_time >= cooldown_seconds:
        _order_skipped_cache[cache_key] = current_time
        return True
    
    return False


async def check_minimum_order_size(client, token_name: str, usdc_amount: float, min_qty: float, min_notional: float) -> tuple[bool, str]:
    """
    Проверяет, соответствует ли размер ордера минимальным требованиям биржи.
    """
    try:
        # Получаем текущую цену токена и баланс
        token_price = float(await client.browser.get_token_price(token_name))
        balance = float(await client.browser.get_balance())
        
        # Рассчитываем количество токенов
        token_qty = usdc_amount / token_price
        
        # Логируем детали расчета для отладки
        client.browser.log_message(
            f"🔍 Order size check: Price=${token_price:.2f}, Amount=${usdc_amount:.2f}, "
            f"Qty={token_qty:.6f}, MinQty={min_qty:.6f}, MinNotional=${min_notional:.2f}",
            level="DEBUG"
        )
        
        # Проверяем минимальное количество
        if token_qty < min_qty:
            return False, f"Quantity {token_qty:.6f} < min {min_qty:.6f} (Free margin: ${balance:.2f}, Order: ${usdc_amount:.2f})"
        
        # Проверяем минимальную номинальную стоимость
        real_notional = token_qty * token_price
        
        # Добавляем запас 10% для учета возможных изменений цены и округления
        safety_margin = 1.1
        safe_notional = real_notional * safety_margin
        
        if safe_notional < min_notional:
            return False, f"Notional ${safe_notional:.2f} < min ${min_notional:.2f} (Free margin: ${balance:.2f}, Order: ${usdc_amount:.2f}, Price: ${token_price:.2f})"
        
        return True, "OK"
        
    except Exception as e:
        return False, f"Check failed: {e}"


async def safe_place_order(client, **kwargs):
    """
    Безопасное открытие ордера с обработкой ошибок.
    Возвращает (success: bool, result: dict или None)
    """
    try:
        # Проверяем минимальный размер ордера, если переданы лимиты
        if kwargs.get('min_qty') and kwargs.get('min_notional') and kwargs.get('usdc_amount'):
            is_valid, reason = await check_minimum_order_size(
                client, 
                kwargs.get('token'), 
                float(kwargs['usdc_amount']), 
                float(kwargs['min_qty']), 
                float(kwargs['min_notional'])
            )
            
            if not is_valid:
                if can_log_warning_message(client.label, "order_skipped"):
                    client.browser.log_message(
                        f"⚠️ Order skipped: {reason}",
                        level="WARNING"
                    )
                return False, None
        
        # Логируем детали расчета позиции перед открытием
        if kwargs.get('usdc_amount'):
            try:
                balance = float(await client.browser.get_balance())
                token_price = float(await client.browser.get_token_price(kwargs.get('token')))
                usdc_amt = float(kwargs['usdc_amount'])
                token_amount = usdc_amt / token_price
                notional = token_amount * token_price
                
                # Показываем реальное соотношение
                calculation_balance = kwargs.get('calculation_balance', balance)
                percent = (usdc_amt / calculation_balance * 100) if calculation_balance > 0 else 0
                
                # Определяем тип операции
                operation_type = kwargs.get('operation_type', 'Trading')
                
                # Объединенное сообщение для всех деталей ордера согласно требованиям
                client.browser.log_message(
                    f"💸 {operation_type} at ${token_price:.2f} | "
                    f"{token_amount:.3f}'{kwargs.get('token')}' (${notional:.2f}) | "
                    f"Free=${balance:.2f}, Trying=${usdc_amt:.2f} ({percent:.1f}%), "
                    f"Notional=${notional:.2f}",
                    level="INFO"
                )
                
            except Exception as log_err:
                client.browser.log_message(f"Failed to log order details: {log_err}", level="DEBUG")
        
        # Удаляем дополнительные параметры перед передачей в place_order
        order_kwargs = kwargs.copy()
        order_kwargs.pop('calculation_balance', None)
        order_kwargs.pop('min_qty', None)
        order_kwargs.pop('min_notional', None)
        order_kwargs.pop('operation_type', None)
        
        # Размещаем ордер
        result = await client.place_order(**order_kwargs)
        
        return True, result
        
    except Exception as e:
        # Ловим ВСЕ ошибки для отладки
        error_msg = str(e)
        error_type = type(e).__name__
        
        # Логируем саму ошибку для отладки (не чаще раза в 5 минут)
        if can_log_warning_message(client.label, "order_error_debug"):
            client.browser.log_message(f"🔍 Order error caught ({error_type}): {error_msg}", level="DEBUG")
        
        # Недостаточно средств - просто ждём освобождения средств
        if 'insufficient' in error_msg.lower() or 'balance' in error_msg.lower():
            if can_log_warning_message(client.label, "insufficient_margin"):
                client.browser.log_message(
                    f"⚠️ Insufficient margin, waiting for positions to close...",
                    level="WARNING"
                )
            return False, None
        
        # Количество меньше минимального - пропускаем
        if 'quantity' in error_msg.lower() or 'size' in error_msg.lower():
            if can_log_warning_message(client.label, "quantity_below_minimum"):
                client.browser.log_message(
                    f"⚠️ Order quantity below minimum (free margin too low), skipping...",
                    level="WARNING"
                )
            return False, None
        
        # Объем ордера меньше минимального - пропускаем
        if 'notional' in error_msg.lower() or 'amount' in error_msg.lower():
            if can_log_warning_message(client.label, "order_size_small"):
                client.browser.log_message(
                    f"⚠️ Order size < $5, waiting for positions to close...",
                    level="WARNING"
                )
            return False, None
        
        # Другие ошибки - логируем и пропускаем
        client.browser.log_message(f"⚠️ Order failed: {error_msg}", level="WARNING")
        return False, None


async def send_tg_notification(client, text: str, save_to_report: bool = True):
    """
    Отправляет уведомление в Telegram немедленно (без ожидания окончания работы модуля).
    """
    try:
        await TgReport().send_log(logs=text)
        
        if save_to_report:
            await client.db.append_report(
                key=client.wallet_address,
                text=text,
                success=True
            )
    except Exception as e:
        client.browser.log_message(f"Failed to send Telegram notification: {e}", level="DEBUG")


async def create_tp_order(client, token_name: str, entry_price: Decimal, order_qty: Decimal, step: Decimal):
    """
    Создает TP ордер для стратегии с отложенными ордерами.
    TP_price = entry_price + STEP
    """
    try:
        # Рассчитываем цену TP согласно стратегии
        tp_price = entry_price + step
        
        # Создаем TP ордер (продажа)
        tp_order_data = {
            "token": token_name,
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": float(order_qty),
            "price": float(tp_price),
        }
        
        tp_result = await client.place_order(**tp_order_data)
        
        client.browser.log_message(
            f"🎯 set TP: SELL {order_qty:.6f} {token_name} @ ${tp_price:.2f}",
            level="INFO"
        )
        
        # Отправляем уведомление в Telegram
        await send_tg_notification(
            client,
            text=f'🎯 set TP: SELL {order_qty:.6f} {token_name} @ ${tp_price:.2f}',
            save_to_report=False
        )
        
    except Exception as e:
        client.browser.log_message(f"❌ Failed to create TP order: {e}", level="ERROR")


async def get_active_buy_orders(client, token_name: str):
    """
    Получает активные BUY ордера для токена
    """
    try:
        orders = await client.browser.get_account_orders()
        
        # Фильтруем только BUY ордера для данного токена
        buy_orders = []
        for order in orders:
            coin = order.get("coin", "")
            is_buy = order.get("isBuy", True)
            
            # Проверяем, что это BUY ордер для нашего токена
            if coin == token_name and is_buy:
                buy_orders.append(order)
        
        return buy_orders
        
    except Exception as e:
        client.browser.log_message(f"Failed to get active BUY orders: {e}", level="ERROR")
        return []


async def get_active_tp_orders(client, token_name: str):
    """
    Получает активные TP ордера (продажи) для токена
    """
    try:
        orders = await client.browser.get_account_orders()
        
        # Отладочная информация отключена для чистоты логов
        # client.browser.log_message(f"🔍 DEBUG: get_active_tp_orders - token_name: {token_name}", level="INFO")
        # client.browser.log_message(f"🔍 DEBUG: get_active_tp_orders - total orders: {len(orders)}", level="INFO")
        
        # Маппинг токенов для поиска ордеров
        token_mapping = {
            "BTC": "@142",
            "ETH": "@2", 
            "USDC": "@1"
        }
        
        # Получаем asset ID для токена
        asset_id = token_mapping.get(token_name, token_name)
        # client.browser.log_message(f"🔍 DEBUG: get_active_tp_orders - looking for asset_id: {asset_id}", level="INFO")
        
        # Фильтруем только SELL ордера для данного токена
        tp_orders = []
        for order in orders:
            coin = order.get("coin", "")
            side = order.get("side", "")
            
            # client.browser.log_message(f"🔍 DEBUG: Order - coin: {coin}, side: {side}", level="INFO")
            
            # Проверяем, что это SELL ордер для нашего токена (по asset ID)
            if coin == asset_id and side in ["SELL", "S", "A"]:
                tp_orders.append(order)
                # client.browser.log_message(f"🔍 DEBUG: Added TP order: {order}", level="INFO")
        
        # client.browser.log_message(f"🔍 DEBUG: get_active_tp_orders - found {len(tp_orders)} TP orders", level="INFO")
        return tp_orders
        
    except Exception as e:
        client.browser.log_message(f"Failed to get active TP orders: {e}", level="ERROR")
        return []


async def calculate_min_tp_price(tp_orders: list) -> float:
    """
    Рассчитывает минимальную цену TP ордеров
    """
    try:
        if not tp_orders:
            return 0.0
        
        tp_prices = []
        for order in tp_orders:
            price = float(order.get("limitPx", 0))
            if price > 0:
                tp_prices.append(price)
        
        if not tp_prices:
            return 0.0
        
        return min(tp_prices)
        
    except Exception as e:
        return 0.0


async def check_take_profit_execution(client, token_name: str, tp_orders: list):
    """
    Проверяет, сработал ли какой-то TP ордер (отключено - используем альтернативный метод)
    """
    # Эта функция больше не используется, так как мы используем альтернативный метод
    # отслеживания через сравнение состояний активных ордеров
    return False, None


async def create_stop_market_order(client, token_name: str, stop_price: float, usdc_amount: float, min_qty: float, min_notional: float, step: Decimal):
    """
    Создает стоп-маркет ордер на пробой
    """
    try:
        current_calculation_balance = float(await client.browser.get_balance())
        
        # Логируем попытку создания стоп-маркет ордера
        client.browser.log_message(
            f"🔍 DEBUG: Attempting to create STOP_MARKET order at ${stop_price:.2f}",
            level="DEBUG"
        )
        
        success, order_result = await safe_place_order(
            client,
            token=token_name,
            side="BUY",
            order_type="STOP_MARKET",  # Стоп-маркет ордер
            usdc_amount=usdc_amount,
            calculation_balance=current_calculation_balance,
            min_qty=min_qty,
            min_notional=min_notional,
            operation_type="Stop Market Breakout",
            stop_price=stop_price  # Цена активации стоп-ордера
        )
        
        # Логируем результат
        client.browser.log_message(
            f"🔍 DEBUG: STOP_MARKET order result: success={success}, result={order_result}",
            level="DEBUG"
        )
        
        if success and order_result:
            # Создаем TP ордер
            entry_price = Decimal(str(order_result.get("avgPrice", stop_price)))
            order_qty = Decimal(str(order_result.get("executedQty", usdc_amount / stop_price)))
            
            await create_tp_order(
                client,
                token_name=token_name,
                entry_price=entry_price,
                order_qty=order_qty,
                step=step
            )
            
            return True
        
        return False
        
    except Exception as e:
        client.browser.log_message(f"❌ Failed to create stop market order: {e}", level="ERROR")
        return False


async def create_limit_order(client, token_name: str, limit_price: float, usdc_amount: float, min_qty: float, min_notional: float, step: Decimal):
    """
    Создает лимитный ордер на откат
    """
    try:
        current_calculation_balance = float(await client.browser.get_balance())
        
        success, order_result = await safe_place_order(
            client,
            token=token_name,
            side="BUY",
            order_type="LIMIT",
            usdc_amount=usdc_amount,
            calculation_balance=current_calculation_balance,
            min_qty=min_qty,
            min_notional=min_notional,
            operation_type="Limit Pullback",
            price=limit_price
        )
        
        if success and order_result:
            # Создаем TP ордер
            entry_price = Decimal(str(order_result.get("avgPrice", limit_price)))
            order_qty = Decimal(str(order_result.get("executedQty", usdc_amount / limit_price)))
            
            await create_tp_order(
                client,
                token_name=token_name,
                entry_price=entry_price,
                order_qty=order_qty,
                step=step
            )
            
            return True
        
        return False
        
    except Exception as e:
        client.browser.log_message(f"❌ Failed to create limit order: {e}", level="ERROR")
        return False


async def trade_delayed_orders_strategy(client, token_name: str):
    """
    Новая стратегия:
    - При отсутствии TP ордеров: маркет покупка по текущей цене + TP ордер
    - Усреднение: маркет покупка при current_price < min_TP_price - STEP*2
    - Пирамидинг: маркет покупка при current_price > max_TP_price - STEP*AGGR (AGGR по умолчанию 0.5)
    - Для каждого BUY сразу ставим лимитный ордер на продажу (TP)
    """
    try:
        step = Decimal(str(settings.STEP))
        client.browser.log_message(f"📊 New Strategy: STEP ${step}", level="INFO")
        
        # Тест DEBUG лога отключен для чистоты логов
        # client.browser.log_message("🔍 DEBUG: Strategy function started", level="INFO")
        
        # Получаем лимиты токена (отключено для чистоты логов)
        # min_qty, min_notional = await client.get_token_limits(token_name)
        min_qty, min_notional = 0.0001, 5.0  # Используем фиксированные значения
        
        # Кэш для отслеживания изменений в ордерах и балансах
        previous_orders_state = None
        previous_balance_state = None
        
        while True:
            try:
                # DEBUG лог отключен для чистоты логов
                # client.browser.log_message("🔍 DEBUG: Starting strategy iteration", level="INFO")
                
                # Получаем данные параллельно для оптимизации
                import asyncio
                tasks = [
                    client.browser.get_token_price(token_name),
                    client.browser.get_account_orders(),
                    get_active_tp_orders(client, token_name),
                    client.browser.get_all_balances()
                ]
                current_price_raw, open_orders, tp_orders, all_balances = await asyncio.gather(*tasks)
                current_price = float(current_price_raw)
                
                # Отладочная информация отключена для чистоты логов
                # client.browser.log_message(f"🔍 DEBUG: get_active_tp_orders returned {len(tp_orders)} orders", level="INFO")
                # if tp_orders:
                #     for tp_order in tp_orders:
                #         client.browser.log_message(f"🔍 DEBUG: TP order: {tp_order}", level="INFO")
                
                tp_executed, executed_order = await check_take_profit_execution(client, token_name, tp_orders)
                
                # Альтернативная проверка через сравнение с предыдущим состоянием
                if not hasattr(client, '_previous_tp_orders'):
                    client._previous_tp_orders = {}
                
                current_tp_order_ids = {order.get("oid") for order in tp_orders if order.get("oid")}
                previous_tp_order_ids = set(client._previous_tp_orders.keys())
                
                # Отладочная информация отключена для чистоты логов
                # client.browser.log_message(f"🔍 DEBUG: Current TP orders: {current_tp_order_ids}", level="INFO")
                # client.browser.log_message(f"🔍 DEBUG: Previous TP orders: {previous_tp_order_ids}", level="INFO")
                
                # Если TP ордер исчез из списка активных, значит он исполнился
                executed_tp_ids = previous_tp_order_ids - current_tp_order_ids
                if executed_tp_ids:
                    # client.browser.log_message(f"🔍 DEBUG: Found executed TP orders: {executed_tp_ids}", level="INFO")
                    
                    # Получаем информацию об исполненном ордере из предыдущего состояния
                    for executed_id in executed_tp_ids:
                        executed_order = client._previous_tp_orders.get(executed_id)
                        
                    if executed_order:
                        # Используем уже полученные балансы из основного цикла
                        # all_balances уже получены выше в цикле
                        
                        # Извлекаем балансы
                        usdc_balance = all_balances.get("USDC", 0.0)
                        btc_balance = all_balances.get("UBTC", 0.0)
                        
                        # Рассчитываем общую стоимость
                        total_usd_value = usdc_balance
                        if btc_balance > 0:
                            # Используем уже полученную цену current_price для расчета стоимости BTC
                            total_usd_value += btc_balance * current_price
                        
                        filled_qty = float(executed_order.get("sz", 0))
                        tp_price = float(executed_order.get("limitPx", 0))
                        
                        # Рассчитываем реальную прибыль от сделки
                        real_profit = await calculate_real_profit(client, token_name, filled_qty, tp_price)
                        
                        # Используем реальную прибыль, если удалось рассчитать, иначе сумму продажи
                        profit = real_profit if real_profit != 0.0 else filled_qty * tp_price
                        
                        # Записываем статистику операции Take Profit
                        try:
                            await log_statistics_to_excel(
                                client=client,
                                operation="Take Profit",
                                btc_amount=filled_qty,
                                price=tp_price,
                                usdc_balance=usdc_balance,
                                btc_balance=btc_balance,
                                total_value=total_usd_value
                            )
                        except Exception as e:
                            client.browser.log_message(f"Failed to log Take Profit statistics: {e}", level="ERROR")
                        
                        # Формируем сообщение согласно требованиям
                        client.browser.log_message(
                            f"✅ Profit ${profit:.2f} | ${usdc_balance:.2f}USDC + {btc_balance:.6f}BTC = ${total_usd_value:.2f}",
                            level="INFO"
                        )
                        
                        # Отправляем уведомление в Telegram согласно требованиям
                        await send_tg_notification(
                            client,
                            text=f'🎯 Profit ${profit:.2f} | ${usdc_balance:.2f}USDC + {btc_balance:.6f}BTC = ${total_usd_value:.2f}',
                            save_to_report=False
                        )
                
                # Обновляем предыдущее состояние (сохраняем полную информацию об ордерах)
                client._previous_tp_orders = {order.get("oid"): order for order in tp_orders if order.get("oid")}
                
                # Логируем сырой ответ API (отключено для чистоты логов)
                # client.browser.log_message(f"🔍 RAW API Response: {open_orders}", level="DEBUG")
                
        # Получаем историю сделок для анализа (отключено для чистоты логов)
        # trade_history = await client.browser.get_all_orders_history(token_name, limit=10)
        # if trade_history:
        #     client.browser.log_message(f"📈 Recent Trade History ({len(trade_history)} trades):", level="INFO")
        #     for i, trade in enumerate(trade_history[-4:]):  # Показываем последние 4 сделки
        #         side = trade.get("side", "UNKNOWN")
        #         price = trade.get("px", "N/A")
        #         size = trade.get("sz", "N/A")
        #         timestamp = trade.get("time", "N/A")
        #         client.browser.log_message(
        #             f"  {i+1}. {side} | Price: ${price} | Size: {size} | Time: {timestamp}",
        #             level="INFO"
        #         )
                
                buy_orders = [order for order in open_orders if order.get("side") in ["BUY", "B"]]
                sell_orders = [order for order in open_orders if order.get("side") in ["SELL", "S", "A"]]
                
                # Функция для преобразования asset ID в читаемое название
                def get_readable_coin_name(coin_id):
                    asset_mapping = {
                        "@142": "BTC",
                        "@1": "USDC", 
                        "@197": "UBTC",
                        "@2": "ETH"
                    }
                    return asset_mapping.get(coin_id, coin_id)
                
                # Создаем текущее состояние ордеров для сравнения
                current_orders_state = []
                for order in open_orders:
                    side_raw = order.get("side", "UNKNOWN")
                    side_display = "BUY" if side_raw in ["BUY", "B"] else "SELL" if side_raw in ["SELL", "S", "A"] else side_raw
                    price = order.get("limitPx", "N/A")
                    size = order.get("sz", "N/A")
                    coin = order.get("coin", "N/A")
                    readable_coin = get_readable_coin_name(coin)
                    current_orders_state.append(f"{side_display} {readable_coin} | Price: ${price} | Size: {size}")
                
                # Создаем текущее состояние балансов для сравнения (только количества токенов, без общей стоимости)
                try:
                    # Собираем только балансы токенов без расчета общей стоимости
                    token_balances = []
                    total_usd_value = 0.0
                    
                    for coin, balance in all_balances.items():
                        if coin == "USDC":
                            token_balances.append(f"{balance:.2f}USD")
                            total_usd_value += balance
                        elif coin == token_name or coin == "UBTC":  # BTC токен (может быть BTC или UBTC)
                            try:
                                # Используем уже полученную цену current_price для расчета стоимости BTC
                                usd_value = balance * current_price
                                total_usd_value += usd_value
                                token_balances.append(f"{balance:.6f}BTC")
                            except Exception as e:
                                token_balances.append(f"{balance:.6f}BTC")
                    
                    # Формируем строку балансов
                    balance_str = " + ".join(token_balances)
                    # Для сравнения используем только балансы токенов, без общей стоимости
                    current_balance_state = f"Balance: {balance_str}"
                    
                except Exception as e:
                    current_balance_state = f"Failed to get balances: {e}"
                
                # Проверяем, изменились ли ордера или балансы
                orders_changed = (previous_orders_state is None or 
                                previous_orders_state != current_orders_state or 
                                len(previous_orders_state) != len(current_orders_state))
                
                balance_changed = (previous_balance_state is None or 
                                 previous_balance_state != current_balance_state)
                
                # Выводим логи только при изменении
                if orders_changed or balance_changed:
                    orders_info = []
                    orders_info.append(f"📋 Open Orders ({len(open_orders)} total):")
                    
                    for i, order_info in enumerate(current_orders_state):
                        orders_info.append(f"  {i+1}. {order_info}")
                    
                    # Добавляем баланс с общей стоимостью для отображения (но не для сравнения)
                    try:
                        balance_str = " + ".join(token_balances)
                        orders_info.append(f"Balance: {balance_str} = ${total_usd_value:.2f}")
                    except:
                        orders_info.append(current_balance_state)
                    
                    # Отправляем все в одном сообщении
                    client.browser.log_message("\n".join(orders_info), level="INFO")
                    
                    # Обновляем кэш
                    previous_orders_state = current_orders_state.copy()
                    previous_balance_state = current_balance_state
                
                # Рассчитываем размер ордера
                usdc_amount, leverage = await client.calculate_usdc_amount_for_order(token_name)
                
                # Проверяем, есть ли ордера снизу
                has_order_below = any(float(order.get("limitPx", 0)) < current_price for order in buy_orders)
                
                # Находим минимальную и максимальную цены TP ордеров
                min_tp_price = 0.0
                max_tp_price = 0.0
                if sell_orders:
                    tp_prices = [float(order.get("limitPx", 0)) for order in sell_orders]
                    min_tp_price = min(tp_prices)
                    max_tp_price = max(tp_prices)
                    # Сбрасываем флаг, если есть TP ордера
                    if hasattr(client, '_no_tp_logged'):
                        client._no_tp_logged = False
                    # client.browser.log_message(
                    #     f"🔍 TP Analysis: {len(sell_orders)} orders, min_TP=${min_tp_price:.2f}, max_TP=${max_tp_price:.2f}",
                    #     level="INFO"
                    # )
                
                # Отладочная информация (отключено для чистоты логов)
                # client.browser.log_message(
                #     f"🔍 DEBUG: Current=${current_price:.2f}, has_order_below={has_order_below}, min_TP=${min_tp_price:.2f}, max_TP=${max_tp_price:.2f}",
                #     level="DEBUG"
                # )
                
                # Анализ текущей ситуации
                if min_tp_price > 0:
                    trigger_level = min_tp_price - float(step) * 2
                    # client.browser.log_message(
                    #     f"📊 Strategy Analysis: min_TP=${min_tp_price:.2f}, trigger_level=${trigger_level:.2f}, current=${current_price:.2f}",
                    #     level="INFO"
                    # )
                    if current_price < trigger_level:
                        # Лог "AVERAGING TRIGGERED" убран - будет показан в объединенном логе
                        pass
                    else:
                        # Лог "Waiting for averaging trigger" убран для чистоты логов
                        pass
                else:
                    # Выводим сообщение только один раз, если нет TP ордеров
                    if not hasattr(client, '_no_tp_logged') or not client._no_tp_logged:
                        client.browser.log_message("🔍 No TP orders found - first run or no positions", level="INFO")
                        client._no_tp_logged = True
                
                # Детальная информация о BUY ордерах (отключено для чистоты логов)
                # if buy_orders:
                #     client.browser.log_message(f"🔍 BUY Orders Analysis:", level="DEBUG")
                #     for order in buy_orders:
                #         order_price = float(order.get("limitPx", 0))
                #         is_below = order_price < current_price
                #         client.browser.log_message(
                #             f"  BUY ${order_price:.2f} {'BELOW' if is_below else 'ABOVE'} current ${current_price:.2f}",
                #             level="DEBUG"
                #         )
                # else:
                #     client.browser.log_message("🔍 No BUY orders found", level="DEBUG")
                
                # 1. При отсутствии TP ордеров: маркет покупка + TP ордер
                if min_tp_price == 0 and max_tp_price == 0:
                    client.browser.log_message(
                        f"🚀 No TP Orders: creating MARKET BUY at current price ${current_price:.2f}",
                        level="INFO"
                    )
                    
                    # Отправляем уведомление о первой позиции
                    await send_tg_notification(
                        client,
                        f"🚀 <b>First Position</b>\nBUY {token_name} at ${current_price:.2f}",
                        save_to_report=False
                    )
                    
                    order_result = await client.place_order(
                        token=token_name,
                        side="BUY",
                        order_type="MARKET",
                        usdc_amount=usdc_amount
                    )
                    
                    # Получаем количество токенов из результата ордера
                    token_amount = order_result.get('quantity', usdc_amount / current_price)
                    
                    # Записываем статистику операции First Position
                    try:
                        usdc_balance = all_balances.get("USDC", 0.0)
                        btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                        total_value = usdc_balance + (btc_balance * current_price)
                        await log_statistics_to_excel(
                            client=client,
                            operation="First Position",
                            btc_amount=token_amount,
                            price=current_price,
                            usdc_balance=usdc_balance,
                            btc_balance=btc_balance,
                            total_value=total_value
                        )
                    except Exception as e:
                        client.browser.log_message(f"Failed to log First Position statistics: {e}", level="ERROR")
                    
                    # Сразу ставим TP ордер на продажу
                    tp_price = current_price + float(step)
                    client.browser.log_message(
                        f"🎯 Set TP ${tp_price:.2f}",
                        level="INFO"
                    )
                    
                    # Отправляем уведомление о TP ордере
                    await send_tg_notification(
                        client,
                        f"🎯 <b>Set TP</b>\n{token_amount:.6f}{token_name} / ${tp_price:.2f}",
                        save_to_report=False
                    )
                    
                    # Записываем статистику операции Set TP
                    try:
                        usdc_balance = all_balances.get("USDC", 0.0)
                        btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                        total_value = usdc_balance + (btc_balance * current_price)
                        await log_statistics_to_excel(
                            client=client,
                            operation="set TP",
                            btc_amount=token_amount,
                            price=tp_price,
                            usdc_balance=usdc_balance,
                            btc_balance=btc_balance,
                            total_value=total_value
                        )
                    except Exception as e:
                        client.browser.log_message(f"Failed to log Set TP statistics: {e}", level="ERROR")
                    
                    await client.place_order(
                        token=token_name,
                        side="SELL",
                        order_type="LIMIT",
                        price=tp_price,
                        usdc_amount=usdc_amount
                    )
                
                # 2. Усреднение: маркет покупка при current_price < min_TP_price - STEP*2
                if min_tp_price > 0:
                    trigger_level = min_tp_price - float(step) * 2
                    if current_price < trigger_level:
                        # Используем уже полученный баланс USDC из all_balances
                        usdc_balance = all_balances.get("USDC", 0.0)
                        
                        order_result = await client.place_order(
                            token=token_name,
                            side="BUY",
                            order_type="MARKET",
                            usdc_amount=usdc_amount
                        )
                        
                        # Получаем количество токенов из результата ордера
                        token_amount = order_result.get('quantity', usdc_amount / current_price)
                        
                        # Логируем детали размещенного ордера (убрано для чистоты логов)
                        
                        # Получаем фактическую цену исполнения из истории сделок
                        import time
                        order_timestamp = time.time()
                        
                        # Извлекаем фактическую цену исполнения из API ответа
                        api_price = current_price  # fallback
                        try:
                            response_data = order_result.get('response', {})
                            statuses = response_data.get('data', {}).get('statuses', [])
                            if statuses and len(statuses) > 0:
                                first_status = statuses[0]
                                if 'filled' in first_status:
                                    filled_data = first_status['filled']
                                    api_price = float(filled_data.get('avgPx', current_price))
                                    client.browser.log_message(
                                        f"✅ Found actual fill price in API: ${api_price:.2f}", 
                                        level="INFO"
                                    )
                                else:
                                    client.browser.log_message(
                                        f"⚠️ Order not filled yet, using current price: ${current_price:.2f}", 
                                        level="INFO"
                                    )
                        except Exception as e:
                            client.browser.log_message(f"Failed to extract fill price from API: {e}", level="ERROR")
                        
                        # Используем фактическую цену из API (история сделок не нужна)
                        actual_price = api_price
                        client.browser.log_message(
                            f"✅ Используем фактическую цену из API: ${actual_price:.2f}", 
                            level="INFO"
                        )
                        
                        # Логируем разницу между ожидаемой и фактической ценой
                        if actual_price != current_price:
                            client.browser.log_message(
                                f"📊 Price difference: Expected ${current_price:.2f}, Actual ${actual_price:.2f}", 
                                level="INFO"
                            )
                        
                        # Лог с фактической ценой исполнения
                        client.browser.log_message(
                            f"🔍 Averaging {token_amount:.6f}{token_name} / ${actual_price:.2f} | Bal: ${usdc_balance:.2f}",
                            level="INFO"
                        )
                        
                        # Обновляем уведомление с фактической ценой исполнения
                        await send_tg_notification(
                            client,
                            f"📉 <b>Averaging</b>\n{token_amount:.6f}{token_name} / ${actual_price:.2f}",
                            save_to_report=False
                        )
                        
                        # Записываем статистику операции Averaging с фактической ценой
                        try:
                            btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                            total_value = usdc_balance + (btc_balance * actual_price)
                            await log_statistics_to_excel(
                                client=client,
                                operation="Averaging",
                                btc_amount=token_amount,
                                price=actual_price,
                                usdc_balance=usdc_balance,
                                btc_balance=btc_balance,
                                total_value=total_value
                            )
                        except Exception as e:
                            client.browser.log_message(f"Failed to log Averaging statistics: {e}", level="ERROR")
                        
                        # Сразу ставим TP ордер на продажу с расчетом от фактической цены
                        tp_price = actual_price + float(step)
                        client.browser.log_message(
                            f"🎯 Set TP ${tp_price:.2f}",
                            level="INFO"
                        )
                        
                        # Отправляем уведомление о TP ордере
                        await send_tg_notification(
                            client,
                            f"🎯 <b>Set TP</b>\n{token_amount:.6f}{token_name} / ${tp_price:.2f}",
                            save_to_report=False
                        )
                        
                        await client.place_order(
                            token=token_name,
                            side="SELL",
                            order_type="LIMIT",
                            price=tp_price,
                            usdc_amount=usdc_amount
                        )
                    else:
                        client.browser.log_message(
                            f"⚠️ No averaging needed - current ${current_price:.2f} >= trigger ${trigger_level:.2f}",
                            level="DEBUG"
                        )
                
                # 3. Пирамидинг: маркет покупка при current_price > max_TP_price - STEP/2
                if max_tp_price > 0:
                    # Агрессивность пирамидинга: current_price > max_TP_price - STEP * AGGR
                    # По умолчанию AGGR=0.5, если нет в settings
                    aggr = getattr(settings, 'AGGR', 0.5)
                    trigger_level = max_tp_price - float(step) * float(aggr)
                    
                    # Отладочная информация для пирамидинга отключена для чистоты логов
                    # client.browser.log_message(f"🔍 DEBUG: Pyramiding check - current: ${current_price:.2f}, max_TP: ${max_tp_price:.2f}, trigger: ${trigger_level:.2f}", level="INFO")
                    
                    if current_price > trigger_level:
                        # client.browser.log_message(f"🔍 DEBUG: Pyramiding triggered! current ${current_price:.2f} > trigger ${trigger_level:.2f}", level="INFO")
                        # Используем уже полученный баланс USDC из all_balances
                        usdc_balance = all_balances.get("USDC", 0.0)
                        
                        # Объединенный лог для пирамидинга
                        client.browser.log_message(
                            f"🚀 Pyramiding at ${current_price:.2f} | Balance ${usdc_balance:.2f}",
                            level="INFO"
                        )
                        
                        order_result = await client.place_order(
                            token=token_name,
                            side="BUY",
                            order_type="MARKET",
                            usdc_amount=usdc_amount
                        )
                        
                        # Получаем количество токенов из результата ордера
                        token_amount = order_result.get('quantity', usdc_amount / current_price)
                        
                        # Записываем статистику операции Pyramiding
                        try:
                            btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                            total_value = usdc_balance + (btc_balance * current_price)
                            await log_statistics_to_excel(
                                client=client,
                                operation="Pyramiding",
                                btc_amount=token_amount,
                                price=current_price,
                                usdc_balance=usdc_balance,
                                btc_balance=btc_balance,
                                total_value=total_value
                            )
                        except Exception as e:
                            client.browser.log_message(f"Failed to log Pyramiding statistics: {e}", level="ERROR")
                        
                        # Обновляем лог с точным количеством токенов
                        client.browser.log_message(
                            f"🚀 Pyramiding {token_amount:.6f}{token_name} / ${current_price:.2f} | Bal: ${usdc_balance:.2f}",
                            level="INFO"
                        )
                        
                        # Обновляем уведомление с точным количеством токенов
                        await send_tg_notification(
                            client,
                            f"📈 <b>Pyramiding</b>\n{token_amount:.6f}{token_name} / ${current_price:.2f}",
                            save_to_report=False
                        )
                        
                        # Сразу ставим TP ордер на продажу
                        tp_price = current_price + float(step)
                        client.browser.log_message(
                            f"🎯 Set TP ${tp_price:.2f}",
                            level="INFO"
                        )
                        
                        # Отправляем уведомление о TP ордере
                        await send_tg_notification(
                            client,
                            f"🎯 <b>Set TP</b>\n{token_amount:.6f}{token_name} / ${tp_price:.2f}",
                            save_to_report=False
                        )
                        
                        await client.place_order(
                            token=token_name,
                            side="SELL",
                            order_type="LIMIT",
                            price=tp_price,
                            usdc_amount=usdc_amount
                        )
                
                await async_sleep(10)
                
            except Exception as e:
                client.browser.log_message(f"❌ Trading error: {e}", level="ERROR")
                await async_sleep(5)
                
    except Exception as e:
        client.browser.log_message(f"❌ Strategy error: {e}", level="ERROR")
        return False
    
    # Функция должна работать бесконечно, но если по какой-то причине завершилась
    return True
