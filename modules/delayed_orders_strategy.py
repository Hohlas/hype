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
from .utils import round_cut, async_sleep, TgReport, send_warning_notification, send_profit_notification
from .retry import CustomError
import settings

# Кэш для ограничения частоты предупреждающих сообщений
_order_skipped_cache = {}


def normalize_price(price_value: float, decimals: int) -> float:
    """
    Нормализует цену, округляя до указанного количества знаков после запятой.
    Убирает floating point ошибки округления.
    
    Args:
        price_value: Цена для нормализации
        decimals: Количество знаков после запятой (sz_decimals из API для конкретного токена)
        
    Returns:
        Нормализованная цена (float, округленная до указанного количества знаков)
    """
    return round(price_value, decimals)


# async def calculate_real_profit(client, token_name: str, sold_amount: float, sell_price: float) -> float:
#     """
#     Рассчитывает реальную прибыль от сделки с учетом комиссии
#     (ЗАКОММЕНТИРОВАНО: используем расчет через изменение баланса USDC)
#     
#     Args:
#         client: HyperliquidClient instance
#         token_name: Название токена (например, "BTC")
#         sold_amount: Количество проданных токенов
#         sell_price: Цена продажи
#     
#     Returns:
#         float: Реальная прибыль в USDC
#     """
#     try:
#         # Получаем историю сделок для поиска цены покупки
#         trades_history = await client.browser.get_all_orders_history(token_name, limit=50)
#         
#         # Отладочная информация (убрано для чистоты логов)
#         
#         if not trades_history:
#             client.browser.log_message("⚠️ No trade history found for profit calculation", level="INFO")
#             # Используем упрощенный расчет на основе STEP
#             step_profit = sold_amount * float(settings.STEP)
#             commission = sold_amount * sell_price * 0.0002  # 0.02% комиссия
#             estimated_profit = step_profit - commission
#             client.browser.log_message(f"💰 Using STEP-based profit estimation: ${estimated_profit:.2f}", level="INFO")
#             return estimated_profit
#         
#         # Ищем последние BUY сделки для расчета средней цены покупки
#         buy_trades = []
#         total_bought = 0.0
#         
#         for trade in trades_history:
#             side = trade.get('side', '').upper()
#             if side in ['BUY', 'B']:
#                 trade_amount = float(trade.get('sz', 0))
#                 trade_price = float(trade.get('px', 0))
#                 
#                 buy_trades.append({
#                     'amount': trade_amount,
#                     'price': trade_price,
#                     'value': trade_amount * trade_price
#                 })
#                 total_bought += trade_amount
#                 
#                 # Если набрали достаточно для покрытия проданного количества
#                 if total_bought >= sold_amount:
#                     break
#         
#         if not buy_trades or total_bought < sold_amount:
#             client.browser.log_message("⚠️ Insufficient buy history for profit calculation", level="INFO")
#             return 0.0
#         
#         # Рассчитываем средневзвешенную цену покупки
#         weighted_price = 0.0
#         remaining_to_cover = sold_amount
#         
#         for trade in reversed(buy_trades):  # Берем последние сделки
#             if remaining_to_cover <= 0:
#                 break
#                 
#             amount_to_use = min(trade['amount'], remaining_to_cover)
#             weighted_price += amount_to_use * trade['price']
#             remaining_to_cover -= amount_to_use
#         
#         avg_buy_price = weighted_price / sold_amount
#         
#         # Рассчитываем прибыль
#         sell_value = sold_amount * sell_price
#         buy_value = sold_amount * avg_buy_price
#         
#         # Примерная комиссия (обычно 0.02% на Hyperliquid)
#         commission_rate = 0.0002  # 0.02%
#         commission = sell_value * commission_rate
#         
#         real_profit = sell_value - buy_value - commission
#         
#         client.browser.log_message(
#             f"💰 Profit calculation: Sold {sold_amount:.6f}BTC @ ${sell_price:.2f} = ${sell_value:.2f}, "
#             f"Bought @ ${avg_buy_price:.2f} = ${buy_value:.2f}, Commission ${commission:.2f}, "
#             f"Net profit: ${real_profit:.2f}", 
#             level="INFO"
#         )
#         
#         return real_profit
#         
#     except Exception as e:
#         client.browser.log_message(f"Failed to calculate real profit: {e}", level="ERROR")
#         return 0.0


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


async def find_matching_buy_trade(client, token_name: str, tp_order_id: int, sell_size: float, sell_price: float, tp_order_time: int = None):
    """
    Находит связанную покупку для исполненного TP ордера.
    
    Алгоритм:
    1. Получаем историю сделок (от новых к старым)
    2. Используем время выставления TP ордера (tp_order_time) из Open Orders как верхнюю границу
    3. Ищем покупку с таким же объемом, время которой МЕНЬШЕ времени выставления TP ордера
       (это гарантирует, что покупка была до выставления TP ордера)
    
    Важно: Если срабатывает несколько TP ордеров подряд, покупки для более ранних
    TP ордеров будут ближе к продажам в истории. Использование времени ВЫСТАВЛЕНИЯ
    TP ордера (а не времени исполнения) гарантирует правильное сопоставление.
    
    Args:
        client: HyperliquidClient instance
        token_name: Название токена (например, "BTC")
        tp_order_id: Order ID исполненного TP ордера
        sell_size: Размер проданного BTC
        sell_price: Цена продажи
        tp_order_time: Время выставления TP ордера из Open Orders (в миллисекундах), если доступно
    
    Returns:
        dict: Данные покупки с полями px, sz, fee, feeToken, time, oid или None если не найдено
    """
    try:
        # Получаем историю сделок (от новых к старым)
        # Сортировка работает правильно, 500 самых новых сделок достаточно для поиска покупки
        trades_history = await client.browser.get_all_orders_history(token_name, limit=500)
        
        if not trades_history:
            client.browser.log_message("⚠️ No trade history found for matching buy trade", level="INFO")
            return None
        
        
        # ОПИСАНИЕ АЛГОРИТМА ПОИСКА ПОКУПКИ:
        # 1. Используем время СОЗДАНИЯ TP ордера из Open Orders как верхнюю границу
        #    TP ордер создается СРАЗУ ПОСЛЕ покупки, поэтому покупка должна быть ДО времени создания TP
        # 2. Ищем покупки (BUY) в истории сделок, которые:
        #    - Время покупки < время создания TP ордера (trade_time < tp_order_time)
        #    - Размер покупки совпадает с размером продажи (с допуском tolerance = 0.0001)
        #    - Не старше 30 дней от времени TP ордера
        # 3. Если найдено несколько подходящих покупок, берем самую ПОСЛЕДНЮЮ (ближайшую к времени TP)
        #    Это гарантирует правильное сопоставление даже при множественных покупках лесенкой
        # 4. Если точного совпадения нет, ищем ближайшую по размеру покупку
        
        from datetime import datetime
        
        # Определяем верхнюю границу времени для поиска покупки
        time_limit = None
        
        if tp_order_time:
            # Используем время СОЗДАНИЯ TP ордера из Open Orders - это правильная верхняя граница
            time_limit = tp_order_time
            tp_datetime_readable = datetime.fromtimestamp(tp_order_time / 1000).strftime('%Y-%m-%d %H:%M:%S')
        else:
            # Fallback: если время создания TP ордера не найдено, используем время исполнения продажи
            sell_trade = None
            for trade in trades_history:
                trade_oid = trade.get('oid')
                trade_side = trade.get('side', '').upper()
                
                if trade_oid == tp_order_id and trade_side in ['SELL', 'S', 'A']:
                    sell_trade = trade
                    time_limit = trade.get('time', 0)
                    client.browser.log_message(
                        f"⚠️ Using sell trade execution time as fallback (TP order creation time not found)",
                        level="WARNING"
                    )
                    break
            
            if not sell_trade:
                # Берем первую продажу в истории как fallback
                if trades_history:
                    first_trade = trades_history[0]
                    if first_trade.get('side', '').upper() in ['SELL', 'S', 'A']:
                        time_limit = first_trade.get('time', 0)
                        client.browser.log_message(
                            f"⚠️ Using first sell trade time as fallback",
                            level="WARNING"
                        )
        
        if not time_limit:
            client.browser.log_message("⚠️ No time limit found for buy trade search", level="WARNING")
            return None
        
        # Теперь ищем покупку с таким же объемом, время которой МЕНЬШЕ времени выставления TP ордера
        # КРИТИЧНО: TP ордер создается СРАЗУ после покупки (задержка несколько секунд/минут)
        # Поэтому ищем покупку в небольшом временном окне ДО времени создания TP ордера
        tolerance = 0.00001  # Допустимое отклонение размера (строгое сопоставление)
        max_time_diff_minutes = 5  # Максимальная разница между покупкой и созданием TP ордера (в минутах)
        # TP ордер создается СРАЗУ после покупки, задержка обычно несколько секунд
        max_time_diff_ms = max_time_diff_minutes * 60 * 1000  # Преобразуем в миллисекунды
        
        
        # Отладочное логирование: выводим все покупки для анализа
        from datetime import datetime
        tp_datetime_str = datetime.fromtimestamp(time_limit / 1000).strftime('%Y-%m-%d %H:%M:%S')
        buy_trades_for_debug = []
        
        # Собираем все подходящие покупки
        matching_buys = []
        for trade in trades_history:
            trade_side = trade.get('side', '').upper()
            trade_time = trade.get('time', 0)
            
            # Ищем покупку
            if trade_side in ['BUY', 'B']:
                trade_size = float(trade.get('sz', 0))
                trade_price = float(trade.get('px', 0))
                trade_datetime = datetime.fromtimestamp(trade_time / 1000)
                
                # Сохраняем для отладки
                time_diff_ms = time_limit - trade_time
                within_time_window = time_diff_ms <= max_time_diff_ms
                buy_trades_for_debug.append({
                    'time': trade_time,
                    'time_str': trade_datetime.strftime('%Y-%m-%d %H:%M:%S'),
                    'size': trade_size,
                    'price': trade_price,
                    'before_tp': trade_time < time_limit,
                    'within_time_window': within_time_window,
                    'size_match': abs(trade_size - sell_size) <= tolerance
                })
                
                # КРИТИЧНО: Время покупки должно быть МЕНЬШЕ времени СОЗДАНИЯ TP ордера
                # TP ордер создается сразу после покупки (задержка несколько секунд/минут)
                if trade_time >= time_limit:
                    continue  # Пропускаем покупки, которые были после создания TP ордера
                
                # КРИТИЧНО: Покупка должна быть в небольшом временном окне ДО создания TP ордера
                # Разница не должна превышать max_time_diff_minutes минут
                time_diff_ms = time_limit - trade_time
                if time_diff_ms > max_time_diff_ms:
                    continue  # Пропускаем покупки, которые были слишком давно (больше max_time_diff_minutes минут назад)
                
                # Проверяем соответствие размера (с учетом допуска)
                if abs(trade_size - sell_size) <= tolerance:
                    matching_buys.append({
                        'px': float(trade.get('px', 0)),
                        'sz': trade_size,
                        'fee': trade.get('fee', '0'),
                        'feeToken': trade.get('feeToken', ''),
                        'time': trade_time,
                        'oid': trade.get('oid'),
                        'hash': trade.get('hash', '')
                    })
        
        
        # Если нашли подходящие покупки, берем самую ПОСЛЕДНЮЮ (ближайшую к времени TP ордера)
        if matching_buys:
            # Сортируем по времени (от новых к старым) и берем первую (самую последнюю перед TP)
            matching_buys.sort(key=lambda x: x['time'], reverse=True)
            buy_trade = matching_buys[0]
            
            
            # Логируем время покупки и TP ордера в читаемом формате для проверки с веб-терминалом
            buy_datetime = datetime.fromtimestamp(buy_trade['time'] / 1000)
            tp_datetime = datetime.fromtimestamp(time_limit / 1000)
            time_diff_ms = time_limit - buy_trade['time']
            time_diff_sec = time_diff_ms / 1000
            time_diff_min = time_diff_sec / 60
            
            # Проверка: если разница больше нескольких минут, это подозрительно
            if time_diff_min > 5:
                client.browser.log_message(
                    f"⚠️ WARNING: Time difference is {time_diff_min:.1f} minutes! "
                    f"Expected: few seconds/minutes. This may indicate wrong buy trade match!",
                    level="WARNING"
                )
            
            client.browser.log_message(
                f"⏱️ Time comparison: Buy at {buy_datetime.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"TP created at {tp_datetime.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"Difference: {time_diff_sec:.1f} seconds ({time_diff_min:.2f} minutes)",
                level="INFO"
            )
            
            return buy_trade
        
        # Если не нашли точное совпадение, ищем максимально близкую покупку ДО времени выставления TP ордера
        # ВАЖНО: При поиске ближайшей покупки предпочитаем покупки с размером >= размера продажи
        # или очень близким к нему (разница не более 10%)
        from datetime import datetime
        tp_datetime_str = datetime.fromtimestamp(time_limit / 1000).strftime('%Y-%m-%d %H:%M:%S')
        client.browser.log_message(
            f"⚠️ No exact match found for sell size {sell_size:.6f} BTC, searching for closest match before TP order time ({tp_datetime_str})...",
            level="WARNING"
        )
        
        closest_buy = None
        min_diff = float('inf')
        closest_buy_preferred = None  # Предпочтительная покупка (размер >= размера продажи или очень близок)
        min_diff_preferred = float('inf')
        
        # Отладочная информация: сколько покупок найдено в истории
        buy_count = 0
        buy_before_tp_count = 0
        
        for trade in trades_history:
            trade_side = trade.get('side', '').upper()
            trade_time = trade.get('time', 0)
            
            if trade_side in ['BUY', 'B']:
                buy_count += 1
                
                # КРИТИЧНО: Время покупки должно быть МЕНЬШЕ времени СОЗДАНИЯ TP ордера
                # TP ордер создается сразу после покупки (задержка несколько секунд/минут)
                if trade_time >= time_limit:
                    continue  # Пропускаем покупки, которые были после создания TP ордера
                
                buy_before_tp_count += 1
                
                # КРИТИЧНО: Покупка должна быть в небольшом временном окне ДО создания TP ордера
                # Разница не должна превышать max_time_diff_minutes минут
                time_diff_ms = time_limit - trade_time
                if time_diff_ms > max_time_diff_ms:
                    continue  # Пропускаем покупки, которые были слишком давно (больше max_time_diff_minutes минут назад)
                
                trade_size = float(trade.get('sz', 0))
                diff = abs(trade_size - sell_size)
                
                # Предпочитаем покупки с размером >= размера продажи или очень близкие (разница <= 10%)
                size_ratio = trade_size / sell_size if sell_size > 0 else 0
                is_preferred = trade_size >= sell_size or (size_ratio >= 0.9 and size_ratio <= 1.1)
                
                if is_preferred and diff < min_diff_preferred:
                    min_diff_preferred = diff
                    closest_buy_preferred = {
                        'px': float(trade.get('px', 0)),
                        'sz': trade_size,
                        'fee': trade.get('fee', '0'),
                        'feeToken': trade.get('feeToken', ''),
                        'time': trade_time,
                        'oid': trade.get('oid'),
                        'hash': trade.get('hash', '')
                    }
                elif diff < min_diff:
                    min_diff = diff
                    closest_buy = {
                        'px': float(trade.get('px', 0)),
                        'sz': trade_size,
                        'fee': trade.get('fee', '0'),
                        'feeToken': trade.get('feeToken', ''),
                        'time': trade_time,
                        'oid': trade.get('oid'),
                        'hash': trade.get('hash', '')
                    }
        
        # Используем предпочтительную покупку, если она найдена
        if closest_buy_preferred:
            closest_buy = closest_buy_preferred
            min_diff = min_diff_preferred
            client.browser.log_message(
                f"✅ Found preferred buy (size >= sell or within 10%): {closest_buy['sz']:.6f} BTC "
                f"(diff: {min_diff:.6f} BTC, {min_diff/sell_size*100:.1f}%)",
                level="INFO"
            )
        
        # Логируем отладочную информацию
        if buy_count == 0:
            client.browser.log_message(
                f"⚠️ No BUY trades found in trade history (total trades: {len(trades_history)})",
                level="WARNING"
            )
        elif buy_before_tp_count == 0:
            client.browser.log_message(
                f"⚠️ Found {buy_count} BUY trades, but all are after TP order time ({tp_datetime_str})",
                level="WARNING"
            )
        
        if closest_buy:
            from datetime import datetime
            buy_datetime = datetime.fromtimestamp(closest_buy['time'] / 1000)
            tp_datetime = datetime.fromtimestamp(time_limit / 1000)
            size_diff_pct = (min_diff / sell_size * 100) if sell_size > 0 else 0
            
            client.browser.log_message(
                f"⚠️ Using closest buy trade (NO EXACT MATCH): {closest_buy['sz']:.6f} BTC @ ${closest_buy['px']:.2f} | "
                f"Size diff: {min_diff:.6f} BTC ({size_diff_pct:.1f}%)",
                level="WARNING"
            )
            
            # Предупреждение, если размер покупки сильно отличается от размера продажи
            if closest_buy['sz'] < sell_size * 0.9 or closest_buy['sz'] > sell_size * 1.1:
                client.browser.log_message(
                    f"⚠️ WARNING: Buy size ({closest_buy['sz']:.6f} BTC) differs significantly from sell size ({sell_size:.6f} BTC)! "
                    f"Profit calculation may be inaccurate!",
                    level="WARNING"
                )
            
            client.browser.log_message(
                f"📅 Buy trade: {buy_datetime.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"TP order: {tp_datetime.strftime('%Y-%m-%d %H:%M:%S')}",
                level="INFO"
            )
            return closest_buy
        
        from datetime import datetime
        tp_datetime_str = datetime.fromtimestamp(time_limit / 1000).strftime('%Y-%m-%d %H:%M:%S')
        client.browser.log_message(
            f"❌ Could not find matching buy trade for sell size {sell_size:.6f} BTC before TP order time ({tp_datetime_str})",
            level="ERROR"
        )
        return None
        
    except Exception as e:
        client.browser.log_message(f"Failed to find matching buy trade: {e}", level="ERROR")
        return None


def initialize_statistics_file():
    """
    Инициализирует файл статистики с правильной структурой колонок при старте программы.
    Вызывается один раз при запуске стратегии.
    """
    stats_file = "hype_stat.xlsx"
    
    # Определяем список колонок
    expected_columns = [
        'Timestamp', 'Current BTC Price', 'Operation', 'BTC Amount', 
        'Operation Price', 'USDC Balance', 'BTC Balance', 'Limit Profit', 'Total Value'
    ]
    
    # Проверяем, существует ли файл
    if not os.path.exists(stats_file):
        # Создаем новый пустой DataFrame с заголовками и сохраняем
        df = pd.DataFrame(columns=expected_columns)
        df.to_excel(stats_file, index=False)
        logger.info(f"✅ Created statistics file: {stats_file}")
    else:
        # Файл существует - проверяем структуру
        try:
            df = pd.read_excel(stats_file)
            
            # Если столбец "Limit Profit" отсутствует, добавляем его
            if 'Limit Profit' not in df.columns:
                if 'Total Value' in df.columns:
                    total_value_idx = df.columns.get_loc('Total Value')
                    df.insert(total_value_idx, 'Limit Profit', 0.0)
                else:
                    df['Limit Profit'] = 0.0
                df.to_excel(stats_file, index=False)
                logger.info(f"✅ Updated statistics file structure: added 'Limit Profit' column")
            
            # Убеждаемся, что все ожидаемые колонки присутствуют
            missing_columns = [col for col in expected_columns if col not in df.columns]
            if missing_columns:
                for col in missing_columns:
                    df[col] = None
                df.to_excel(stats_file, index=False)
                logger.info(f"✅ Updated statistics file structure: added missing columns")
        except Exception as e:
            logger.warning(f"⚠️ Failed to check statistics file structure: {e}")


async def log_statistics_to_excel(client, operation: str, btc_amount: float, price: float, 
                                 usdc_balance: float, btc_balance: float, total_value: float, 
                                 orders=None, sz_decimals: int = None, token_name: str = "BTC"):
    """
    Записывает статистику операции в Excel файл hype_stat.xlsx
    
    Args:
        client: HyperliquidClient instance
        operation: Тип операции (First Position/set BuyLimit/Pyramiding/set TP/Take Profit)
        btc_amount: Объем BTC операции
        price: Цена по которой проводилась операция
        usdc_balance: Баланс USDC
        btc_balance: Баланс BTC
        total_value: Суммарная стоимость всех активов
        orders: Список открытых ордеров (опционально, если не передан - будет получен внутри функции)
        sz_decimals: Количество знаков после запятой для форматирования цены (из API)
        token_name: Название торгуемого токена (по умолчанию "BTC")
    """
    try:
        # Получаем текущую цену токена
        current_token_price = float(await client.browser.get_token_price(token_name))
        
        # Если ордера не переданы, получаем их
        if orders is None:
            orders = await client.browser.get_account_orders()
        
        # Рассчитываем сумму Order Value для всех лимитных ордеров на продажу
        limit_profit = 0.0
        for order in orders:
            # Проверяем, что это лимитный ордер на продажу
            side = order.get("side", "")
            is_buy = order.get("isBuy", True)
            limit_price = float(order.get("limitPx", 0))
            
            # Ордер на продажу: side in ["SELL", "S", "A"] или isBuy == False
            is_sell = side in ["SELL", "S", "A"] or (not is_buy)
            
            # Проверяем, что это лимитный ордер (есть limitPx > 0)
            is_limit = limit_price > 0
            
            if is_sell and is_limit:
                # Получаем размер ордера
                size = float(order.get("sz", 0))
                
                # Order Value = размер * цена
                if size > 0:
                    limit_profit += size * limit_price
        
        # Создаем запись статистики
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Получаем sz_decimals из клиента, если не передан
        if sz_decimals is None:
            try:
                sz_decimals = client.get_token_sz_decimals(token_name)
            except:
                sz_decimals = 5  # Fallback
        
        # Цена уже нормализована при расчете, но для Excel форматируем как строку без trailing zeros
        def format_price_for_excel(price_value: float, decimals: int) -> str:
            """Форматирует цену для Excel, убирая trailing zeros"""
            price_str = f"{price_value:.{decimals}f}"
            if '.' in price_str:
                return price_str.rstrip('0').rstrip('.')
            return price_str
        
        formatted_price = format_price_for_excel(price, sz_decimals)
        
        # Подготавливаем данные для записи
        new_row = {
            'Timestamp': timestamp,
            'Current BTC Price': current_token_price,
            'Operation': operation,
            'BTC Amount': btc_amount,
            'Operation Price': formatted_price,
            'USDC Balance': usdc_balance,
            'BTC Balance': btc_balance,
            'Limit Profit': limit_profit,
            'Total Value': total_value
        }
        
        # Путь к файлу статистики
        stats_file = "hype_stat.xlsx"
        
        # Определяем список колонок
        expected_columns = [
            'Timestamp', 'Current BTC Price', 'Operation', 'BTC Amount', 
            'Operation Price', 'USDC Balance', 'BTC Balance', 'Limit Profit', 'Total Value'
        ]
        
        # Проверяем, существует ли файл
        if os.path.exists(stats_file):
            # Читаем существующий файл
            df = pd.read_excel(stats_file)
            
            # Если столбец "Limit Profit" отсутствует, добавляем его
            if 'Limit Profit' not in df.columns:
                # Находим позицию столбца "Total Value"
                if 'Total Value' in df.columns:
                    total_value_idx = df.columns.get_loc('Total Value')
                    # Вставляем столбец "Limit Profit" перед "Total Value"
                    df.insert(total_value_idx, 'Limit Profit', 0.0)
                else:
                    # Если "Total Value" нет, добавляем в конец
                    df['Limit Profit'] = 0.0
            
            # Убеждаемся, что все ожидаемые колонки присутствуют
            for col in expected_columns:
                if col not in df.columns:
                    df[col] = None
        else:
            # Создаем новый пустой DataFrame с заголовками
            df = pd.DataFrame(columns=expected_columns)
        
        # Создаем новый DataFrame из строки с явным указанием колонок
        new_df = pd.DataFrame([new_row], columns=expected_columns)
        
        # Убеждаемся, что колонки в обоих DataFrame совпадают и в правильном порядке
        df = df.reindex(columns=expected_columns, fill_value=None)
        
        # Конкатенируем с явным указанием колонок
        # Если df пустой (нет строк), используем new_df напрямую, иначе конкатенируем
        if len(df) == 0:
            df = new_df
        else:
            df = pd.concat([df, new_df], ignore_index=True, sort=False)
        
        # Сохраняем в Excel файл
        df.to_excel(stats_file, index=False)
        
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


async def check_minimum_order_size(client, token_name: str, usdc_amount: float, min_qty: float, min_notional: float, checked_price: float = None) -> tuple[bool, str]:
    """
    Проверяет, соответствует ли размер ордера минимальным требованиям биржи.
    
    Args:
        client: Клиент Hyperliquid
        token_name: Название токена
        usdc_amount: Размер ордера в USDC
        min_qty: Минимальное количество токенов
        min_notional: Минимальная номинальная стоимость
        checked_price: Цена для проверки (если None, используется текущая цена токена)
    """
    try:
        # Получаем цену для проверки (если не передана, используем текущую цену)
        if checked_price is None:
            checked_price = float(await client.browser.get_token_price(token_name))
        else:
            checked_price = float(checked_price)
        
        balance = float(await client.browser.get_balance())
        
        # Рассчитываем количество токенов
        token_qty = usdc_amount / checked_price
        
        # Проверяем минимальное количество
        if token_qty < min_qty:
            return False, f"Quantity {token_qty:.6f} < min {min_qty:.6f} (Free margin: ${balance:.2f}, Order: ${usdc_amount:.2f})"
        
        # Проверяем минимальную номинальную стоимость по цене проверки
        real_notional = token_qty * checked_price
        
        # Добавляем запас 10% для учета возможных изменений цены и округления
        safety_margin = 1.1
        safe_notional = real_notional * safety_margin
        
        if safe_notional < min_notional:
            return False, f"Notional ${safe_notional:.2f} < min ${min_notional:.2f} (Free margin: ${balance:.2f}, Order: ${usdc_amount:.2f}, Price: ${checked_price:.2f})"
        
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
            # Для LIMIT ордеров используем цену лимитного ордера, для остальных - текущую цену
            checked_price = None
            if kwargs.get('order_type') == 'LIMIT' and kwargs.get('price'):
                checked_price = float(kwargs['price'])
            
            is_valid, reason = await check_minimum_order_size(
                client, 
                kwargs.get('token'), 
                float(kwargs['usdc_amount']), 
                float(kwargs['min_qty']), 
                float(kwargs['min_notional']),
                checked_price=checked_price
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
                pass
        
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
        pass


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
        
        if success and order_result:
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
        
        # Тестируем функцию расчета прибыли при запуске (сейчас используем расчет через баланс)
        # client.browser.log_message("🔍 Testing profit calculation function...", level="INFO")
        # test_profit = await calculate_real_profit(client, token_name, 0.001, 110000.0)
        # client.browser.log_message(f"🔍 Test profit calculation result: ${test_profit:.2f}", level="INFO")
        
        # Тест DEBUG лога отключен для чистоты логов
        # client.browser.log_message("🔍 DEBUG: Strategy function started", level="INFO")
        
        # Инициализируем файл статистики при старте (создаем, если не существует)
        initialize_statistics_file()
        
        # Загружаем лимиты токена один раз при старте (кэшируются в client)
        min_qty, min_notional, sz_decimals = await client.load_token_limits(token_name)
        
        # Получаем информацию о последней покупке из Trade History при старте 
        last_buy_price = None
        last_buy_size = None
        try:
            trades_history = await client.browser.get_all_orders_history(token_name, limit=500)
            if trades_history:
                # История уже отсортирована от новых к старым
                for trade in trades_history:
                    trade_side = trade.get('side', '').upper()
                    # Ищем последнюю покупку
                    if trade_side in ['BUY', 'B']:
                        last_buy_price = float(trade.get('px', 0))
                        last_buy_size = float(trade.get('sz', 0))
                        if last_buy_price > 0 and last_buy_size > 0:
                            break
        except Exception as e:
            client.browser.log_message(f"Failed to get last buy info: {e}", level="WARNING")
        
        # Кэш для отслеживания изменений в ордерах и балансах
        previous_orders_state = None
        previous_balance_state = None
        
        # Время последнего статусного сообщения (для дежурных сообщений каждые 10 минут)
        import time
        status_interval = 600  # 10 минут в секундах
        last_status_time = 0  # Установлено в 0, чтобы статус выводился сразу при запуске
        
        while True:
            try:
                # Задержка между итерациями основного цикла
                # Перемещена в начало для того, чтобы после создания ордеров была пауза
                # и они успели появиться в API перед следующей итерацией
                await async_sleep(10)
                
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
                
                # Дежурное статусное сообщение каждые 10 минут
                current_time = time.time()
                if current_time - last_status_time >= status_interval:
                    # Получаем балансы
                    usdc_balance = all_balances.get("USDC", 0.0)
                    btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                    
                    # Подсчитываем количество TP ордеров
                    tp_count = len(tp_orders) if tp_orders else 0
                    
                    # Формируем информацию о последней покупке
                    last_buy_info = f"LastBuy at {last_buy_price:.2f}" if last_buy_price else "LastBuy: N/A"
                    
                    # Выводим статусное сообщение
                    client.browser.log_message(
                        f"💚 Price: ${current_price:.2f}, USDC: ${usdc_balance:.2f}, "
                        f"{token_name}: {btc_balance:.6f}, TP Orders: {tp_count}, {last_buy_info}",
                        level="INFO"
                    )
                    
                    last_status_time = current_time
                
                # Разделяем лимитные ордера на две категории
                # Маппинг токенов для поиска ордеров
                token_mapping = {
                    "BTC": "@142",
                    "ETH": "@2", 
                    "USDC": "@1",
                    "UBTC": "@142"  # UBTC тоже использует @142
                }
                asset_id = token_mapping.get(token_name, token_name)
                
                # current_tp_orders — лимитные ордера на продажу (токен → USDC)
                current_tp_orders = []
                # buy_limit_orders — лимитные ордера на покупку (USDC → токен)
                buy_limit_orders = []
                
                for order in open_orders:
                    coin = order.get("coin", "")
                    side = order.get("side", "")
                    is_buy = order.get("isBuy", True)
                    limit_price = float(order.get("limitPx", 0))
                    
                    # Проверяем, что это ордер для нашего токена и лимитный (есть limitPx > 0)
                    if coin == asset_id and limit_price > 0:
                        # Ордер на продажу: side in ["SELL", "S", "A"] или isBuy == False
                        is_sell = side in ["SELL", "S", "A"] or (not is_buy)
                        
                        if is_sell:
                            current_tp_orders.append(order)
                        else:
                            # Ордер на покупку: side in ["BUY", "B"] или isBuy == True
                            buy_limit_orders.append(order)
                
                # Инициализация отслеживания баланса USDC для расчета прибыли
                if not hasattr(client, '_previous_usdc_balance'):
                    client._previous_usdc_balance = all_balances.get("USDC", 0.0)
                
                # ============================================
                # МОДУЛЬ SET_TP: Выставление TP-ордеров
                # ============================================
                # Этот модуль работает независимо от блоков покупки.
                # Он проверяет историю сделок и выставляет тейк-профит для любых новых покупок.
                try:
                    # Получаем историю торговли (последние 100 сделок)
                    trades_history = await client.browser.get_all_orders_history(token_name, limit=100)
                    
                    if trades_history:
                        # Фильтруем только покупки: USDC → токен
                        buy_trades = []
                        for trade in trades_history:
                            trade_side = trade.get('side', '').upper()
                            
                            # Ищем покупки
                            if trade_side in ['BUY', 'B']:
                                buy_trades.append(trade)
                        
                        # Извлекаем данные последней покупки
                        if buy_trades:
                            # История уже отсортирована от новых к старым, берем первую
                            latest_trade = buy_trades[0]
                            latest_price = float(latest_trade.get('px', 0))
                            latest_amount = float(latest_trade.get('sz', 0))
                            latest_timestamp = latest_trade.get('time', 0)
                            
                            # Проверяем, является ли покупка новой
                            # Используем относительную разницу (процент) вместо абсолютной для корректной работы с любыми ценами
                            # Порог 0.1% (0.001) для избежания ложных срабатываний из-за округления
                            if latest_price > 0 and latest_amount > 0:
                                is_new_buy = False
                                if last_buy_price is None:
                                    is_new_buy = True
                                elif last_buy_price > 0:
                                    # Относительная разница в процентах
                                    price_diff_percent = abs(latest_price - last_buy_price) / last_buy_price
                                    if price_diff_percent > 0.0001:  # 0.1% разница
                                        is_new_buy = True
                                
                                if is_new_buy:
                                    # Обнаружена новая покупка
                                    # Лог убран по запросу пользователя
                                    
                                    # Обновляем last_buy_price для отслеживания следующей новой покупки
                                    last_buy_price = latest_price
                                    
                                    # Рассчитываем цену TP
                                    tp_price = normalize_price(latest_price + float(step), sz_decimals)
                                    
                                    # Размещение TP-ордера на бирже
                                    # Количество: latest_amount токенов
                                    # Лимитная цена: tp_price
                                    try:
                                        tp_order_data = {
                                            "token": token_name,
                                            "side": "SELL",
                                            "order_type": "LIMIT",
                                            "quantity": latest_amount,
                                            "price": tp_price,
                                        }
                                        
                                        tp_result = await client.place_order(**tp_order_data)
                                        
                                        client.browser.log_message(
                                            f"🎯 Set TP: SELL {latest_amount:.6f} {token_name} @ ${tp_price:.2f}",
                                            level="INFO"
                                        )
                                        
                                        # Отправляем уведомление в Telegram
                                        await send_tg_notification(
                                            client,
                                            text=f'🎯 set TP: SELL {latest_amount:.6f} {token_name} @ ${tp_price:.2f}',
                                            save_to_report=False
                                        )
                                        
                                        # Записываем статистику операции Set TP
                                        try:
                                            btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                                            total_value = all_balances.get("USDC", 0.0) + (btc_balance * current_price)
                                            await log_statistics_to_excel(
                                                client=client,
                                                operation="Set TP",
                                                btc_amount=latest_amount,
                                                price=tp_price,
                                                usdc_balance=all_balances.get("USDC", 0.0),
                                                btc_balance=btc_balance,
                                                total_value=total_value,
                                                sz_decimals=sz_decimals,
                                                token_name=token_name
                                            )
                                        except Exception as e:
                                            client.browser.log_message(f"Failed to log Set TP statistics: {e}", level="ERROR")
                                        
                                        # Обновляем min_tp_price немедленно, не дожидаясь следующей итерации
                                        # Это предотвращает повторное размещение Averaging ордера
                                        if min_tp_price == 0 or tp_price < min_tp_price:
                                            min_tp_price = tp_price
                                            # Лог убран по запросу пользователя
                                        
                                        # Прерываем цикл после создания TP ордера
                                        # Это дает время ордеру появиться в API перед следующей итерацией
                                        continue
                                        
                                    except Exception as e:
                                        client.browser.log_message(f"❌ Failed to create TP order: {e}", level="ERROR")
                except Exception as e:
                    client.browser.log_message(f"❌ SET_TP module error: {e}", level="ERROR")
                
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
                    # Инициализируем множество обработанных TP ордеров, если еще не создано
                    if not hasattr(client, '_processed_tp_orders'):
                        client._processed_tp_orders = set()
                    
                    # Фильтруем только новые (еще не обработанные) TP ордера
                    new_executed_tp_ids = executed_tp_ids - client._processed_tp_orders
                    
                    if new_executed_tp_ids:
                        # client.browser.log_message(f"🔍 DEBUG: Found executed TP orders: {new_executed_tp_ids}", level="INFO")
                    
                    # Получаем информацию об исполненном ордере из предыдущего состояния
                        for executed_id in new_executed_tp_ids:
                            # Проверяем, не обработан ли уже этот ордер (защита от дубликатов)
                            if executed_id in client._processed_tp_orders:
                                continue
                            
                            executed_order = client._previous_tp_orders.get(executed_id)
                            
                            if executed_order:
                                # Оборачиваем обработку в try-finally, чтобы гарантировать пометку как обработанного
                                try:
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
                                    
                                    # Получаем размер из сохраненного ордера (fallback)
                                    filled_qty = float(executed_order.get("sz", 0))
                                    tp_price_fallback = normalize_price(float(executed_order.get("limitPx", 0)), sz_decimals)  # Fallback цена
                                    tp_order_id = executed_order.get("oid")
                                    
                                    # Получаем фактический размер исполнения, цену и комиссию из Trade History
                                    # Это важно, так как биржа может округлить размер при исполнении
                                    # И фактическая цена исполнения может отличаться от лимитной цены
                                    trades_history = await client.browser.get_all_orders_history(token_name, limit=500)
                                    sell_trade_from_history = None
                                    tp_price = tp_price_fallback  # Инициализируем fallback значением
                                    
                                    if trades_history:
                                        for trade in trades_history:
                                            if trade.get('oid') == tp_order_id and trade.get('side', '').upper() in ['SELL', 'S', 'A']:
                                                # Используем фактический размер исполнения из Trade History
                                                actual_filled_qty = float(trade.get('sz', filled_qty))
                                                if actual_filled_qty > 0:
                                                    filled_qty = actual_filled_qty
                                                # Используем фактическую цену исполнения из Trade History
                                                actual_price = float(trade.get('px', tp_price_fallback))
                                                if actual_price > 0:
                                                    tp_price = normalize_price(actual_price, sz_decimals)
                                                else:
                                                    tp_price = tp_price_fallback
                                                sell_trade_from_history = trade
                                                break
                                    
                                    # Если сделка не найдена в истории, значит ордер не был исполнен
                                    # Не логируем Take Profit, если нет подтверждения из Trade History
                                    if not sell_trade_from_history:
                                        client.browser.log_message(
                                            f"⚠️ TP order {tp_order_id} not found in trade history. Skipping Take Profit logging.",
                                            level="WARNING"
                                        )
                                        # Выходим из try блока, finally все равно выполнится
                                        # (пропускаем остальной код обработки)
                                    else:
                                        # Проверяем время исполнения сделки продажи - игнорируем старые (> 5 минут)
                                        sell_trade_time = sell_trade_from_history.get('time', 0)
                                        should_process = True
                                        
                                        if sell_trade_time:
                                            import time
                                            current_time_ms = int(time.time() * 1000)
                                            age_minutes = (current_time_ms - sell_trade_time) / (1000 * 60)
                                            
                                            if age_minutes > 5:
                                                client.browser.log_message(
                                                    f"⏰ Skipping old Take Profit trade (executed {age_minutes:.1f} minutes ago)",
                                                    level="INFO"
                                                )
                                                # Пропускаем обработку старой сделки, переходим к finally
                                                should_process = False
                                        
                                        # Обрабатываем только если сделка свежая (< 5 минут) или время не определено
                                        if should_process:
                                            # Получаем время выставления TP ордера из saved order или из Trade History
                                            tp_order_time = executed_order.get('timestamp')
                                            
                                            # Используем время исполнения из Trade History, если доступно
                                            if sell_trade_from_history and sell_trade_from_history.get('time'):
                                                tp_order_time = sell_trade_from_history.get('time')
                                            
                                            if tp_order_time:
                                                # Конвертируем в читаемый формат для проверки
                                                from datetime import datetime
                                                tp_datetime = datetime.fromtimestamp(tp_order_time / 1000)
                                                client.browser.log_message(
                                                    f"📅 TP order executed at: {tp_datetime.strftime('%Y-%m-%d %H:%M:%S')} | "
                                                    f"Actual price: ${tp_price:.2f} (limit was ${tp_price_fallback:.2f})",
                                                    level="INFO"
                                                )
                                            else:
                                                client.browser.log_message(
                                                    f"⚠️ No timestamp found, using current time",
                                                    level="WARNING"
                                                )
                                            
                                            # Находим связанную покупку для расчета прибыли
                                            # Используем время выставления TP ордера, если доступно
                                            
                                            buy_trade = await find_matching_buy_trade(
                                                client, token_name, tp_order_id, filled_qty, tp_price, tp_order_time
                                            )
                                            
                                            # Рассчитываем прибыль на основе конкретных сделок покупки и продажи
                                            if buy_trade:
                                                buy_price = buy_trade['px']
                                                buy_size = buy_trade['sz']
                                                
                                                # Получаем комиссии из API
                                                buy_fee_str = buy_trade.get('fee', '0')
                                                buy_fee_token = buy_trade.get('feeToken', '')
                                                
                                                # Рассчитываем комиссию покупки в USDC
                                                buy_fee_usdc = 0.0
                                                if buy_fee_token == 'USDC':
                                                    buy_fee_usdc = float(buy_fee_str)
                                                elif buy_fee_token == 'UBTC' or buy_fee_token == 'BTC':
                                                    # Если комиссия в BTC, конвертируем в USDC по цене покупки
                                                    buy_fee_usdc = float(buy_fee_str) * buy_price
                                                
                                                # Получаем данные о продаже из Trade History для точной комиссии продажи
                                                # Используем уже полученный sell_trade_from_history
                                                sell_fee_usdc = 0.0
                                                sell_fee_token = 'USDC'  # Значение по умолчанию
                                                
                                                if sell_trade_from_history:
                                                    sell_fee_str = sell_trade_from_history.get('fee', '0')
                                                    sell_fee_token = sell_trade_from_history.get('feeToken', 'USDC')
                                                    
                                                    if sell_fee_token == 'USDC':
                                                        sell_fee_usdc = float(sell_fee_str)
                                                    elif sell_fee_token == 'UBTC' or sell_fee_token == 'BTC':
                                                        # Если комиссия в BTC, конвертируем в USDC по цене продажи
                                                        sell_fee_usdc = float(sell_fee_str) * tp_price
                                                
                                                
                                                # Расчет выручки от продажи
                                                sell_total = filled_qty * tp_price
                                                sell_revenue = sell_total - sell_fee_usdc
                                                
                                                client.browser.log_message(
                                                    f"  💵 SELL: {filled_qty:.6f} BTC × ${tp_price:.2f} = ${sell_total:.2f} "
                                                    f"| Fee: ${sell_fee_usdc:.4f} ({sell_fee_token}) | "
                                                    f"Net revenue: ${sell_revenue:.2f}",
                                                    level="INFO"
                                                )
                                                
                                                # Расчет затрат на покупку
                                                buy_total = buy_size * buy_price
                                                buy_cost = buy_total + buy_fee_usdc
                                                
                                                client.browser.log_message(
                                                    f"  💵 BUY: {buy_size:.6f} BTC × ${buy_price:.2f} = ${buy_total:.2f} "
                                                    f"| Fee: ${buy_fee_usdc:.4f} ({buy_fee_token}) | "
                                                    f"Total cost: ${buy_cost:.2f}",
                                                    level="INFO"
                                                )
                                                
                                                # Чистая прибыль = выручка - затраты
                                                profit = sell_revenue - buy_cost
                                                
                                                client.browser.log_message(
                                                    f"  💰 PROFIT: ${sell_revenue:.2f} (revenue) - ${buy_cost:.2f} (cost) = ${profit:.2f}",
                                                    level="INFO"
                                                )
                                                
                                                # Проверка на аномально большой профит
                                                if profit > filled_qty * float(settings.STEP) * 2:
                                                    client.browser.log_message(
                                                        f"⚠️ WARNING: Profit ${profit:.2f} seems unusually high! "
                                                        f"Expected max ~${filled_qty * float(settings.STEP):.2f} (STEP × size)",
                                                        level="WARNING"
                                                    )
                                            else:
                                                # Если не нашли связанную покупку, используем упрощенный расчет
                                                # (fallback на случай, если не удалось найти покупку)
                                                step_profit = filled_qty * float(settings.STEP)
                                                estimated_commission = filled_qty * tp_price * 0.0002  # 0.02% комиссия
                                                profit = step_profit - estimated_commission
                                                
                                                client.browser.log_message(
                                                    f"⚠️ Buy trade not found, using STEP-based estimation: ${profit:.2f}",
                                                    level="WARNING"
                                                )
                                            
                                            # Обновляем предыдущий баланс для следующей итерации
                                            client._previous_usdc_balance = usdc_balance
                                            
                                            # Записываем статистику операции Take Profit
                                            try:
                                                await log_statistics_to_excel(
                                                    client=client,
                                                    operation="Take Profit",
                                                    btc_amount=filled_qty,
                                                    price=tp_price,
                                                    usdc_balance=usdc_balance,
                                                    btc_balance=btc_balance,
                                                    total_value=total_usd_value,
                                                    orders=open_orders,
                                                    sz_decimals=sz_decimals,
                                                    token_name=token_name
                                                )
                                            except Exception as e:
                                                client.browser.log_message(f"Failed to log Take Profit statistics: {e}", level="ERROR")
                                            
                                            # Формируем сообщение согласно требованиям
                                            # Используем фактический размер исполнения из Trade History
                                            client.browser.log_message(
                                                f"✅ Profit ${profit:.2f} | ${filled_qty:.6f}BTC | ${usdc_balance:.2f}USDC + {btc_balance:.6f}BTC = ${total_usd_value:.2f}",
                                                level="INFO"
                                            )
                                            
                                            # Отправляем уведомление в Telegram согласно требованиям
                                            profit_message = f'🎯 hype: Profit ${profit:.2f} | ${usdc_balance:.2f}USDC + {btc_balance:.6f}BTC = ${total_usd_value:.2f}'
                                            await send_tg_notification(
                                                client,
                                                text=profit_message,
                                                save_to_report=False
                                            )
                                            
                                            # Дублируем сообщение о профите в PROFIT_BOT_TOKEN
                                            await send_profit_notification(profit_message)
                                            
                                            # Устанавливаем флаг, чтобы пропустить вывод списка ордеров после профита
                                            if not hasattr(client, '_skip_orders_log_after_profit'):
                                                client._skip_orders_log_after_profit = False
                                            client._skip_orders_log_after_profit = True
                                
                                except Exception as e:
                                    client.browser.log_message(
                                        f"❌ Error processing Take Profit for order {executed_id}: {e}",
                                        level="ERROR"
                                    )
                                finally:
                                    # Всегда помечаем TP ордер как обработанный, даже при ошибке
                                    # Это предотвращает повторную обработку и дубликаты в Excel
                                    client._processed_tp_orders.add(executed_id)
                
                # Обновляем предыдущее состояние (сохраняем полную информацию об ордерах)
                # В TP ордерах из Open Orders есть поле "timestamp" - время создания ордера
                for order in tp_orders:
                    oid = order.get("oid")
                    if oid:
                        client._previous_tp_orders[oid] = order
                
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
                
                # Выводим логи только при изменении, но пропускаем вывод после профита
                # Сбрасываем флаг пропуска в начале проверки (если он был установлен ранее)
                skip_orders_this_iteration = getattr(client, '_skip_orders_log_after_profit', False)
                if skip_orders_this_iteration:
                    client._skip_orders_log_after_profit = False  # Сбрасываем флаг на следующей итерации
                
                # Обновляем кэш для отслеживания изменений (но не выводим список ордеров)
                if orders_changed or balance_changed:
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
                    
                    # Получаем баланс USDC для проверки
                    usdc_balance = all_balances.get("USDC", 0.0)
                    
                    # Проверяем достаточность средств перед размещением ордера
                    if usdc_balance < usdc_amount:
                        client.browser.log_message(
                            f"⚠️ Insufficient USDC balance: ${usdc_balance:.2f} < ${usdc_amount:.2f} required for first position",
                            level="WARNING"
                        )
                        continue  # Пропускаем этот цикл
                    
                    # Отправляем уведомление о первой позиции (обновим после получения фактической цены)
                    await send_tg_notification(
                        client,
                        f"🚀 <b>First Position</b>\nBUY {token_name} at ${current_price:.2f}",
                        save_to_report=False
                    )
                    
                    order_result = await client.place_order(
                        token=token_name,
                        side="BUY",
                        order_type="MARKET",
                        usdc_amount=usdc_amount,
                        price=current_price
                    )
                    
                    # Обновляем баланс после маркет-ордера на покупку
                    all_balances = await client.browser.get_all_balances()
                    client._previous_usdc_balance = all_balances.get("USDC", 0.0)
                    
                    # Получаем количество токенов из результата ордера
                    token_amount = order_result.get('quantity', usdc_amount / current_price)
                    
                    # Получаем фактическую цену исполнения из API ответа
                    actual_price = normalize_price(current_price, sz_decimals)  # fallback
                    try:
                        response_data = order_result.get('response', {})
                        statuses = response_data.get('data', {}).get('statuses', [])
                        if statuses and len(statuses) > 0:
                            first_status = statuses[0]
                            if 'filled' in first_status:
                                filled_data = first_status['filled']
                                actual_price = normalize_price(float(filled_data.get('avgPx', current_price)), sz_decimals)
                                client.browser.log_message(
                                    f"✅ First Position: Actual fill price ${actual_price:.2f} (expected ${current_price:.2f})", 
                                    level="INFO"
                                )
                            else:
                                client.browser.log_message(
                                    f"⚠️ First Position: Order not filled yet, using current price: ${current_price:.2f}", 
                                    level="INFO"
                                )
                    except Exception as e:
                        client.browser.log_message(f"Failed to extract First Position fill price from API: {e}", level="ERROR")
                    
                    # Отправляем дополнительное уведомление с фактической ценой исполнения
                    if actual_price != current_price:
                        await send_tg_notification(
                            client,
                            f"✅ <b>First Position Executed</b>\n{token_amount:.6f}{token_name} filled at ${actual_price:.2f} (expected ${current_price:.2f})",
                            save_to_report=False
                        )
                    
                    # Записываем статистику операции First Position с фактической ценой
                    try:
                        usdc_balance = all_balances.get("USDC", 0.0)
                        btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                        total_value = usdc_balance + (btc_balance * actual_price)
                        await log_statistics_to_excel(
                            client=client,
                            operation="First Position",
                            btc_amount=token_amount,
                            price=actual_price,
                            usdc_balance=usdc_balance,
                            btc_balance=btc_balance,
                            total_value=total_value,
                            sz_decimals=sz_decimals,
                            token_name=token_name
                        )
                    except Exception as e:
                        client.browser.log_message(f"Failed to log First Position statistics: {e}", level="ERROR")
                    
                    # Прерываем цикл после создания First Position ордера
                    # Это дает время ордеру появиться в API перед следующей итерацией
                    continue
                    
                # 2. Усреднение: лимитный ордер на покупку при current_price < min_TP_price - STEP*2
                if min_tp_price > 0:
                    # Расчет целевой цены для лимитного ордера
                    target_buy_price = normalize_price(min_tp_price - float(step) * 2, sz_decimals)
                    
                    # Используем уже полученный баланс USDC из all_balances
                    usdc_balance = all_balances.get("USDC", 0.0)
                    
                    # Проверяем, есть ли активные Buy Limit ордера
                    active_buy_limits = [order for order in buy_limit_orders if float(order.get("limitPx", 0)) > 0]
                    
                    if not active_buy_limits:
                        # Нет активного Buy Limit - размещаем новый лимитный ордер
                        # Проверка баланса
                        if usdc_balance < usdc_amount:
                            client.browser.log_message(
                                f"⚠️ Insufficient USDC balance: ${usdc_balance:.2f} < ${usdc_amount:.2f} required for averaging",
                                level="WARNING"
                            )
                        else:
                            # Проверка минимального размера ордера перед созданием
                            # Для LIMIT ордера используем цену лимитного ордера для проверки
                            is_valid, reason = await check_minimum_order_size(
                                client,
                                token_name,
                                usdc_amount,
                                min_qty,
                                min_notional,
                                checked_price=target_buy_price
                            )
                            
                            if not is_valid:
                                client.browser.log_message(
                                    f"⚠️ Buy Limit order skipped: {reason}",
                                    level="WARNING"
                                )
                            else:
                                # Размещение лимитного ордера на покупку
                                try:
                                    order_result = await client.place_order(
                                        token=token_name,
                                        side="BUY",
                                        order_type="LIMIT",
                                        usdc_amount=usdc_amount,
                                        price=target_buy_price
                                    )
                                    
                                    if order_result:
                                        client.browser.log_message(
                                            f"📉 set BuyLimit - ${usdc_amount:.2f} at ${target_buy_price:.2f}",
                                            level="INFO"
                                        )
                                        
                                        # Отправляем уведомление в Telegram
                                        await send_tg_notification(
                                            client,
                                            f"📉 <b>set BuyLimit</b>\n${usdc_amount:.2f} at ${target_buy_price:.2f}",
                                            save_to_report=False
                                        )
                                        
                                        # Записываем статистику операции Averaging (лимитный ордер, еще не исполнен)
                                        try:
                                            btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                                            total_value = usdc_balance + (btc_balance * current_price)
                                            # Для лимитного ордера используем целевую цену и расчетное количество
                                            token_amount = usdc_amount / target_buy_price
                                            await log_statistics_to_excel(
                                                client=client,
                                                operation="set BuyLimit",
                                                btc_amount=token_amount,
                                                price=target_buy_price,
                                                usdc_balance=usdc_balance,
                                                btc_balance=btc_balance,
                                                total_value=total_value,
                                                orders=open_orders,
                                                sz_decimals=sz_decimals,
                                                token_name=token_name
                                            )
                                        except Exception as e:
                                            client.browser.log_message(f"Failed to log Averaging statistics: {e}", level="ERROR")
                                        
                                        # Прерываем цикл после создания Averaging ордера
                                        # Это дает время ордеру появиться в API перед следующей итерацией
                                        continue
                                        
                                except Exception as e:
                                    client.browser.log_message(f"❌ Failed to create Buy Limit order: {e}", level="ERROR")
                    else:
                        # Есть активные Buy Limit ордера - проверяем их актуальность
                        threshold = min_tp_price - float(step) * 2.5
                        
                        for order in active_buy_limits:
                            order_price = float(order.get("limitPx", 0))
                            order_id = order.get("oid")
                            
                            if order_price < threshold:
                                # Ордер устарел - отменяем его
                                try:
                                    await client.browser.cancel_order(token_name, order_id)
                                    client.browser.log_message(
                                        f"❌ Cancelled old Buy Limit at ${order_price:.2f} (threshold: ${threshold:.2f})",
                                        level="INFO"
                                    )
                                except Exception as e:
                                    client.browser.log_message(f"❌ Failed to cancel old Buy Limit order: {e}", level="ERROR")
                
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
                        
                        # Проверяем достаточность средств перед размещением ордера
                        if usdc_balance < usdc_amount:
                            client.browser.log_message(
                                f"⚠️ Insufficient USDC balance: ${usdc_balance:.2f} < ${usdc_amount:.2f} required for pyramiding",
                                level="WARNING"
                            )
                            continue  # Пропускаем этот цикл
                        
                        # Объединенный лог для пирамидинга
                        client.browser.log_message(
                            f"🚀 Pyramiding at ${current_price:.2f} | Balance ${usdc_balance:.2f}",
                            level="INFO"
                        )
                        
                        order_result = await client.place_order(
                            token=token_name,
                            side="BUY",
                            order_type="MARKET",
                            usdc_amount=usdc_amount,
                            price=current_price
                        )
                        
                        # Обновляем баланс после маркет-ордера на покупку
                        all_balances = await client.browser.get_all_balances()
                        client._previous_usdc_balance = all_balances.get("USDC", 0.0)
                        
                        # Получаем количество токенов из результата ордера
                        token_amount = order_result.get('quantity', usdc_amount / current_price)
                        
                        # Получаем фактическую цену исполнения из API ответа
                        actual_price = normalize_price(current_price, sz_decimals)  # fallback
                        try:
                            response_data = order_result.get('response', {})
                            statuses = response_data.get('data', {}).get('statuses', [])
                            if statuses and len(statuses) > 0:
                                first_status = statuses[0]
                                if 'filled' in first_status:
                                    filled_data = first_status['filled']
                                    actual_price = normalize_price(float(filled_data.get('avgPx', current_price)), sz_decimals)
                                    client.browser.log_message(
                                        f"✅ Pyramiding: Actual fill price ${actual_price:.2f} (expected ${current_price:.2f})", 
                                        level="INFO"
                                    )
                                else:
                                    client.browser.log_message(
                                        f"⚠️ Pyramiding: Order not filled yet, using current price: ${current_price:.2f}", 
                                        level="INFO"
                                    )
                        except Exception as e:
                            client.browser.log_message(f"Failed to extract Pyramiding fill price from API: {e}", level="ERROR")
                        
                        # Записываем статистику операции Pyramiding с фактической ценой
                        try:
                            btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                            total_value = usdc_balance + (btc_balance * actual_price)
                            await log_statistics_to_excel(
                                client=client,
                                operation="Pyramiding",
                                btc_amount=token_amount,
                                price=actual_price,
                                usdc_balance=usdc_balance,
                                btc_balance=btc_balance,
                                total_value=total_value,
                                sz_decimals=sz_decimals,
                                token_name=token_name
                            )
                        except Exception as e:
                            client.browser.log_message(f"Failed to log Pyramiding statistics: {e}", level="ERROR")
                        
                        # Обновляем лог с фактической ценой исполнения
                        client.browser.log_message(
                            f"🚀 Pyramiding {token_amount:.6f}{token_name} / ${actual_price:.2f} | Bal: ${usdc_balance:.2f}",
                            level="INFO"
                        )
                        
                        # Отправляем дополнительное уведомление с фактической ценой исполнения
                        if actual_price != current_price:
                            await send_tg_notification(
                                client,
                                f"✅ <b>Pyramiding Executed</b>\n{token_amount:.6f}{token_name} filled at ${actual_price:.2f} (expected ${current_price:.2f})",
                                save_to_report=False
                            )
                        
                        # Обновляем уведомление с фактической ценой исполнения
                        await send_tg_notification(
                            client,
                            f"📈 <b>Pyramiding</b>\n{token_amount:.6f}{token_name} / ${actual_price:.2f}",
                            save_to_report=False
                        )
                
            except Exception as e:
                error_str = str(e)
                # Если получили 429 ошибку, увеличиваем задержку
                if '429' in error_str or 'Rate limit' in error_str:
                    client.browser.log_message(f"⚠️ Rate limit detected, waiting 30 seconds...", level="WARNING")
                    await async_sleep(30)
                else:
                    client.browser.log_message(f"❌ Trading error: {e}", level="ERROR")
                    await async_sleep(10)
                
    except Exception as e:
        client.browser.log_message(f"❌ Strategy error: {e}", level="ERROR")
        return False
    
    # Функция должна работать бесконечно, но если по какой-то причине завершилась
    return True
