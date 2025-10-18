"""
Hyperliquid Grid Trading Strategy Module
========================================

Этот модуль реализует сеточную стратегию торговли для spot торговли на Hyperliquid.

Основные принципы:
- Бот использует только покупки, основываясь на том, что торгуемый актив будет только расти
- Основной параметр STEP - шаг расстановки ордеров
- При падении цены задействуется тактика Усреднения
- При росте цены задействуется тактика Пирамидинга (модифицированная)

Логика открытия сделок:
1. Если нет открытых позиций, открывается маркет ордер на покупку (Логируется как "First Position")
2. Объем всех ордеров на покупку определяется в настройках как "POSITION_SIZE_PERCENT" в % от баланса
3. Для каждой покупки сразу же выставляется лимитный ордер на продажу с тем же объемом по цене TP_price = entry_price + STEP
4. При падении цены: Покупаем если current_price < min_TP_price - STEP*2
5. При росте цены: Покупаем если current_price > max_TP_price - STEP/2

Использование:
    from modules.grid_strategy import trade_grid_strategy
    
    await trade_grid_strategy(
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
    
    Args:
        account_label: Лейбл аккаунта
        message_type: Тип сообщения (например, "order_skipped", "insufficient_margin")
        cooldown_minutes: Интервал между сообщениями в минутах
    
    Returns:
        True если можно выводить сообщение, False если нужно подождать
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
    
    Returns:
        (is_valid: bool, reason: str)
    """
    try:
        # Получаем текущую цену токена и баланс
        token_price = float(await client.browser.get_token_price(token_name))
        balance = float(await client.browser.get_balance())
        
        # Рассчитываем количество токенов
        token_qty = usdc_amount / token_price
        
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
                # Проверяем, можно ли выводить сообщение "Order skipped" (не чаще раза в 5 минут)
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
            # Проверяем, можно ли выводить сообщение "Insufficient margin" (не чаще раза в 5 минут)
            if can_log_warning_message(client.label, "insufficient_margin"):
                client.browser.log_message(
                    f"⚠️ Insufficient margin, waiting for positions to close...",
                    level="WARNING"
                )
            return False, None
        
        # Количество меньше минимального - пропускаем
        if 'quantity' in error_msg.lower() or 'size' in error_msg.lower():
            # Проверяем, можно ли выводить сообщение "Order quantity below minimum" (не чаще раза в 5 минут)
            if can_log_warning_message(client.label, "quantity_below_minimum"):
                client.browser.log_message(
                    f"⚠️ Order quantity below minimum (free margin too low), skipping...",
                    level="WARNING"
                )
            return False, None
        
        # Объем ордера меньше минимального - пропускаем
        if 'notional' in error_msg.lower() or 'amount' in error_msg.lower():
            # Проверяем, можно ли выводить сообщение "Order size < $5" (не чаще раза в 5 минут)
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
    Создает TP ордер для сеточной стратегии.
    TP_price = entry_price + STEP
    """
    try:
        # Рассчитываем цену TP согласно сеточной стратегии
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


async def calculate_grid_levels(client, token_name: str, tp_orders: list, step: Decimal):
    """
    Рассчитывает уровни сетки на основе активных TP ордеров
    """
    try:
        if not tp_orders:
            return None, None
        
        # Получаем цены TP ордеров
        tp_prices = []
        for order in tp_orders:
            price = float(order.get("limitPx", 0))
            if price > 0:
                tp_prices.append(price)
        
        if not tp_prices:
            return None, None
        
        # Находим минимальную и максимальную цены TP
        min_tp_price = min(tp_prices)
        max_tp_price = max(tp_prices)
        
        return min_tp_price, max_tp_price
        
    except Exception as e:
        client.browser.log_message(f"Failed to calculate grid levels: {e}", level="ERROR")
        return None, None


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


async def trade_grid_strategy(client, token_name: str):
    """
    Основная функция сеточной стратегии торговли для spot торговли на Hyperliquid.
    """
    try:
        # Получаем настройки торговли
        step = Decimal(str(settings.STEP))
        max_orders = settings.MAX_GRID_ORDERS
        
        client.browser.log_message(f"📊 Grid Strategy mode: STEP ${step}, Max orders: {max_orders}", level="INFO")
        
        # Получаем лимиты токена
        min_qty, min_notional = await client.get_token_limits(token_name)
        client.browser.log_message(f"📏 Min limits for {token_name}: Qty={min_qty}, Notional=${min_notional}", level="INFO")
        
        # Основной цикл торговли
        while True:
            try:
                # Получаем текущую цену и баланс
                current_price = float(await client.browser.get_token_price(token_name))
                balance = float(await client.browser.get_balance())
                
                # Получаем активные TP ордера
                tp_orders = await get_active_tp_orders(client, token_name)
                
                # Проверяем, сработал ли какой-то TP ордер
                tp_executed, executed_order = await check_take_profit_execution(client, token_name, tp_orders)
                
                if tp_executed:
                    # Удаляем исполненный ордер из списка
                    tp_orders.remove(executed_order)
                
                # Рассчитываем уровни сетки
                min_tp_price, max_tp_price = await calculate_grid_levels(client, token_name, tp_orders, step)
                
                # Определяем, нужно ли открывать новую позицию
                should_open_position = False
                operation_type = ""
                
                if len(tp_orders) == 0:
                    # Нет открытых позиций - открываем первую позицию
                    should_open_position = True
                    operation_type = "First Position"
                    client.browser.log_message(f"🔍 No open positions, opening first position", level="INFO")
                    
                elif len(tp_orders) < max_orders:
                    # Есть позиции, но не достигли максимума
                    if min_tp_price and max_tp_price:
                        # Логика усреднения при падении цены
                        if current_price < min_tp_price - float(step) * 2:
                            should_open_position = True
                            operation_type = "Averaging"
                            client.browser.log_message(f"🔍 Averaging: current_price ${current_price:.2f} < min_TP_price - STEP*2 = ${min_tp_price - float(step) * 2:.2f}", level="INFO")
                        
                        # Логика пирамидинга при росте цены
                        elif current_price > max_tp_price - float(step) / 2:
                            should_open_position = True
                            operation_type = "Pyramiding"
                            client.browser.log_message(f"🔍 Pyramiding: current_price ${current_price:.2f} > max_TP_price - STEP/2 = ${max_tp_price - float(step) / 2:.2f}", level="INFO")
                
                # Открываем позицию если нужно
                if should_open_position:
                    # Рассчитываем размер ордера
                    usdc_amount, leverage = await client.calculate_usdc_amount_for_order(token_name)
                    client.browser.log_message(f"🔍 DEBUG: Calculated order size: ${usdc_amount:.2f}, Min required: ${min_notional:.2f}", level="DEBUG")
                    
                    if usdc_amount >= min_notional:
                        # Пересчитываем баланс для корректного логирования
                        current_calculation_balance = float(await client.browser.get_balance())
                        
                        success, order_result = await safe_place_order(
                            client,
                            token=token_name,
                            side="BUY",
                            order_type="MARKET",
                            usdc_amount=usdc_amount,
                            calculation_balance=current_calculation_balance,
                            min_qty=min_qty,
                            min_notional=min_notional,
                            operation_type=operation_type
                        )
                        
                        if success and order_result:
                            # Создаем TP ордер
                            entry_price = Decimal(str(order_result.get("avgPrice", current_price)))
                            order_qty = Decimal(str(order_result.get("executedQty", usdc_amount / current_price)))
                            
                            await create_tp_order(
                                client,
                                token_name=token_name,
                                entry_price=entry_price,
                                order_qty=order_qty,
                                step=step
                            )
                
                await async_sleep(15)
                
            except Exception as e:
                client.browser.log_message(f"❌ Trading error: {e}", level="ERROR")
                await async_sleep(15)
                
    except Exception as e:
        client.browser.log_message(f"❌ Fatal trading error: {e}", level="ERROR")
        raise
