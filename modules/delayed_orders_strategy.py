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

from .utils import round_cut, async_sleep, TgReport, send_warning_notification
from .retry import CustomError
import settings

# Кэш для ограничения частоты предупреждающих сообщений
_order_skipped_cache = {}


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
        
        # Фильтруем только SELL ордера для данного токена
        tp_orders = []
        for order in orders:
            coin = order.get("coin", "")
            is_buy = order.get("isBuy", True)
            
            # Проверяем, что это SELL ордер для нашего токена
            if coin == token_name and not is_buy:
                tp_orders.append(order)
        
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
    Проверяет, сработал ли какой-то TP ордер
    """
    try:
        current_price = float(await client.browser.get_token_price(token_name))
        balance = float(await client.browser.get_balance())
        
        for tp_order in tp_orders:
            if tp_order.get("status") == "FILLED":
                # TP ордер сработал
                filled_qty = float(tp_order.get("sz", 0))
                tp_price = float(tp_order.get("px", 0))
                
                # Рассчитываем прибыль
                profit = (tp_price - current_price) * filled_qty
                
                # Формируем сообщение согласно требованиям
                client.browser.log_message(
                    f"✅ Take Profit +${profit:.2f} | Eq=${balance:.1f}",
                    level="INFO"
                )
                
                # Отправляем уведомление в Telegram согласно требованиям
                await send_tg_notification(
                    client,
                    text=f'🎯 Take Profit +${profit:.2f} | Eq=${balance:.1f}',
                    save_to_report=False
                )
                
                return True, tp_order
        
        return False, None
        
    except Exception as e:
        client.browser.log_message(f"Failed to check TP execution: {e}", level="ERROR")
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
    - Пирамидинг: маркет покупка при current_price > max_TP_price - STEP/2
    - Для каждого BUY сразу ставим лимитный ордер на продажу (TP)
    """
    try:
        step = Decimal(str(settings.STEP))
        client.browser.log_message(f"📊 New Strategy: STEP ${step}", level="INFO")
        
        # Получаем лимиты токена
        min_qty, min_notional = await client.get_token_limits(token_name)
        
        while True:
            try:
                # Получаем текущую цену
                current_price = float(await client.browser.get_token_price(token_name))
                
                # Получаем все открытые ордера
                open_orders = await client.browser.get_account_orders()
                
                # Логируем сырой ответ API
                client.browser.log_message(f"🔍 RAW API Response: {open_orders}", level="DEBUG")
                
                # Получаем историю сделок для анализа
                trade_history = await client.browser.get_all_orders_history(token_name, limit=10)
                if trade_history:
                    client.browser.log_message(f"📈 Recent Trade History ({len(trade_history)} trades):", level="INFO")
                    for i, trade in enumerate(trade_history[-4:]):  # Показываем последние 4 сделки
                        side = trade.get("side", "UNKNOWN")
                        price = trade.get("px", "N/A")
                        size = trade.get("sz", "N/A")
                        timestamp = trade.get("time", "N/A")
                        client.browser.log_message(
                            f"  {i+1}. {side} | Price: ${price} | Size: {size} | Time: {timestamp}",
                            level="INFO"
                        )
                
                buy_orders = [order for order in open_orders if order.get("side") in ["BUY", "B"]]
                sell_orders = [order for order in open_orders if order.get("side") in ["SELL", "S", "A"]]
                
                # Логируем все открытые ордера
                client.browser.log_message(f"📋 Open Orders ({len(open_orders)} total):", level="INFO")
                for i, order in enumerate(open_orders):
                    side_raw = order.get("side", "UNKNOWN")
                    side_display = "BUY" if side_raw in ["BUY", "B"] else "SELL" if side_raw in ["SELL", "S", "A"] else side_raw
                    price = order.get("limitPx", "N/A")
                    size = order.get("sz", "N/A")
                    coin = order.get("coin", "N/A")
                    client.browser.log_message(
                        f"  {i+1}. {side_display} {coin} | Price: ${price} | Size: {size}",
                        level="INFO"
                    )
                
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
                
                # Отладочная информация
                client.browser.log_message(
                    f"🔍 DEBUG: Current=${current_price:.2f}, has_order_below={has_order_below}, min_TP=${min_tp_price:.2f}, max_TP=${max_tp_price:.2f}",
                    level="DEBUG"
                )
                
                # Анализ текущей ситуации
                if min_tp_price > 0:
                    trigger_level = min_tp_price - float(step) * 2
                    client.browser.log_message(
                        f"📊 Strategy Analysis: min_TP=${min_tp_price:.2f}, trigger_level=${trigger_level:.2f}, current=${current_price:.2f}",
                        level="INFO"
                    )
                    if current_price < trigger_level:
                        client.browser.log_message("🚀 AVERAGING TRIGGERED: Price below trigger level!", level="INFO")
                    else:
                        client.browser.log_message("⏳ Waiting for averaging trigger...", level="INFO")
                else:
                    client.browser.log_message("🔍 No TP orders found - first run or no positions", level="INFO")
                
                # Детальная информация о BUY ордерах
                if buy_orders:
                    client.browser.log_message(f"🔍 BUY Orders Analysis:", level="DEBUG")
                    for order in buy_orders:
                        order_price = float(order.get("limitPx", 0))
                        is_below = order_price < current_price
                        client.browser.log_message(
                            f"  BUY ${order_price:.2f} {'BELOW' if is_below else 'ABOVE'} current ${current_price:.2f}",
                            level="DEBUG"
                        )
                else:
                    client.browser.log_message("🔍 No BUY orders found", level="DEBUG")
                
                # 1. При отсутствии TP ордеров: маркет покупка + TP ордер
                if min_tp_price == 0 and max_tp_price == 0:
                    client.browser.log_message(
                        f"🚀 No TP Orders: creating MARKET BUY at current price ${current_price:.2f}",
                        level="INFO"
                    )
                    await client.place_order(
                        token=token_name,
                        side="BUY",
                        order_type="MARKET",
                        usdc_amount=usdc_amount
                    )
                    
                    # Сразу ставим TP ордер на продажу
                    tp_price = current_price + float(step)
                    client.browser.log_message(
                        f"🎯 Setting TP: SELL at ${tp_price:.2f}",
                        level="INFO"
                    )
                    await client.place_order(
                        token=token_name,
                        side="SELL",
                        order_type="LIMIT",
                        price=tp_price,
                        usdc_amount=usdc_amount
                    )
                
                # 2. Усреднение: маркет покупка при current_price < min_TP_price - STEP*2
                elif min_tp_price > 0:
                    trigger_level = min_tp_price - float(step) * 2
                    if current_price < trigger_level:
                        client.browser.log_message(
                            f"🔍 Averaging: current ${current_price:.2f} < trigger ${trigger_level:.2f}, creating MARKET BUY",
                            level="INFO"
                        )
                        await client.place_order(
                            token=token_name,
                            side="BUY",
                            order_type="MARKET",
                            usdc_amount=usdc_amount
                        )
                        
                        # Сразу ставим TP ордер на продажу
                        tp_price = current_price + float(step)
                        client.browser.log_message(
                            f"🎯 Setting TP: SELL at ${tp_price:.2f}",
                            level="INFO"
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
                    trigger_level = max_tp_price - float(step) / 2
                    if current_price > trigger_level:
                        client.browser.log_message(
                            f"🚀 Pyramiding: current ${current_price:.2f} > trigger ${trigger_level:.2f}, creating MARKET BUY",
                            level="INFO"
                        )
                        await client.place_order(
                            token=token_name,
                            side="BUY",
                            order_type="MARKET",
                            usdc_amount=usdc_amount
                        )
                        
                        # Сразу ставим TP ордер на продажу
                        tp_price = current_price + float(step)
                        client.browser.log_message(
                            f"🎯 Setting TP: SELL at ${tp_price:.2f}",
                            level="INFO"
                        )
                        await client.place_order(
                            token=token_name,
                            side="SELL",
                            order_type="LIMIT",
                            price=tp_price,
                            usdc_amount=usdc_amount
                        )
                
                await async_sleep(15)
                
            except Exception as e:
                client.browser.log_message(f"❌ Trading error: {e}", level="ERROR")
                await async_sleep(5)
                
    except Exception as e:
        client.browser.log_message(f"❌ Strategy error: {e}", level="ERROR")
