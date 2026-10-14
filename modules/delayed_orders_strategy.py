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
import csv
from datetime import datetime
import os
import json
import math
import asyncio
from .utils import round_cut, async_sleep, TgReport, send_warning_notification, send_profit_notification
from .retry import CustomError
import settings

# Кэш для ограничения частоты предупреждающих сообщений
_order_skipped_cache = {}

# Глобальный флаг остановки бота (выставляется при KeyboardInterrupt в main.py).
# Проверяется вверху цикла, после сна и перед выставлением ордеров —
# стратегия завершает цикл сама вместо обрыва посреди операций.
_bot_stop_flag = False


def set_bot_stop_flag(value: bool = True):
    """Устанавливает флаг остановки бота"""
    global _bot_stop_flag
    _bot_stop_flag = value


def get_bot_stop_flag() -> bool:
    """Возвращает текущее значение флага остановки бота"""
    return _bot_stop_flag


def get_watch_seconds() -> float:
    """Лимит вотча в секундах из WATCH_SECONDS (§9.п5, §2.2: 19800 в дежурстве).

    0 или мусор — без лимита (текущее локальное поведение без изменений).
    """
    try:
        return max(0.0, float(os.getenv("WATCH_SECONDS", "0")))
    except (TypeError, ValueError):
        return 0.0


def get_poll_interval(default: float = 10.0) -> float:
    """Пауза между итерациями из BOT_POLL_INTERVAL.

    Дефолт — текущая локальная пауза (без изменений); workflow дежурства
    передаёт 60 явно (§5: шаг опроса против 429/бана).
    """
    try:
        interval = float(os.getenv("BOT_POLL_INTERVAL", str(default)))
        return interval if interval > 0 else default
    except (TypeError, ValueError):
        return default


# Кулдаун повторной заливки одного BuyLimit-уровня, сек.
# Фолбэк: если TP fill-а так и не подтвердился висящим ордером
# (не выставился / исполнился мгновенно / снят), уровень разблокируется сам.
BUYLIMIT_LEVEL_COOLDOWN_SEC = 600


def _buylimit_level_allowed(client, target_buy_price: float, current_tp_orders) -> bool:
    """
    Правило refill BuyLimit-уровня: тот же уровень — только после того, как TP
    предыдущего fill-а подтвержден висящим sell-ордером. Иначе на проливе бот
    заливает один уровень дважды (кейс 84447 + 84439 с одного $84509).

    - TP подтвержден (висящий sell ~ OWисполненной цене входа + STEP) → можно, wait снят.
    - Уровень сменился (другая ступень лесенки) → можно сразу.
    - Тот же уровень без подтвержденного TP → ждать, но не дольше кулдауна.
    """
    wait = getattr(client, "_buylimit_wait_tp", None)
    if not wait:
        return True
    try:
        expect_px = float(wait.get("expect_tp_px", 0) or 0)
        if expect_px > 0:
            for o in (current_tp_orders or []):
                try:
                    if abs(float(o.get("limitPx", 0)) - expect_px) <= 1.0:
                        client._buylimit_wait_tp = None
                        return True
                except Exception:
                    continue
        if abs(float(target_buy_price) - float(wait.get("level", 0) or 0)) > 1.0:
            return True
        since = float(wait.get("since", 0) or 0)
        if datetime.now().timestamp() - since >= BUYLIMIT_LEVEL_COOLDOWN_SEC:
            client.browser.log_message(
                f"⏳ BuyLimit ${target_buy_price:.2f}: TP not confirmed within "
                f"{BUYLIMIT_LEVEL_COOLDOWN_SEC}s — level cooldown expired, allowing refill",
                level="WARNING"
            )
            client._buylimit_wait_tp = None
            return True
        client.browser.log_message(
            f"⏳ BuyLimit ${target_buy_price:.2f}: waiting TP confirmation "
            f"for fill (TP ${expect_px:.2f} expected) — refill paused",
            level="INFO"
        )
        return False
    except Exception:
        return True


# Сторожевой heartbeat цикла: как часто проверять прогресс и какая тишина
# считается подвисанием. Метка client._watchdog_last_mark обновляется каждую
# итерацию; фоновая задача _cycle_watchdog ругается в лог+терминал.
WATCHDOG_INTERVAL_SEC = 60
WATCHDOG_STALL_SEC = 300


async def _cycle_watchdog(client):
    """
    Фоновая задача: раз в WATCHDOG_INTERVAL_SEC проверяет метку прогресса
    цикла. Тишина дольше WATCHDOG_STALL_SEC — WARNING (дедуп 10 мин).
    Ничего не перезапускает и не отменяет — только диагностика.
    """
    import time
    try:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SEC)
            if get_bot_stop_flag():
                return
            last = getattr(client, "_watchdog_last_mark", None)
            if last is None:
                continue
            stalled = time.monotonic() - last
            if stalled > WATCHDOG_STALL_SEC:
                if can_log_warning_message(client.label, "watchdog_stall", cooldown_minutes=10):
                    client.browser.log_message(
                        f"🐶 Watchdog: no cycle progress for {stalled:.0f}s "
                        f"(threshold {WATCHDOG_STALL_SEC}s) — loop possibly stuck",
                        level="WARNING",
                    )
                    # TG-дубль: лог/терминал могут быть не под рукой. Если сеть
                    # жива (подвис не интернет) — уведомление дойдёт сюда.
                    # Ошибка отправки не должна убивать сторожа.
                    try:
                        await send_tg_notification(
                            client,
                            f"🐶 <b>Watchdog</b>\n{client.label}: no cycle progress "
                            f"for {stalled:.0f}s — loop possibly stuck",
                            save_to_report=False,
                        )
                    except Exception as tg_err:
                        client.browser.log_message(
                            f"Watchdog TG failed: {tg_err}", level="ERROR"
                        )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        try:
            client.browser.log_message(f"Watchdog error: {e}", level="ERROR")
        except Exception:
            pass


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



async def find_matching_buy_trade(client, token_name: str, tp_order_id: int, sell_size: float, sell_price: float, tp_order_time: int = None, sz_decimals: int = None):
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
        sz_decimals: Количество знаков после запятой для размера (если не передан, получается из кэша)
    
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
        #    TP ордер создается СРАЗУ ПОСЛЕ покупки (~15 секунд), поэтому покупка должна быть ДО времени создания TP
        # 2. Ищем покупки (BUY) в истории сделок, которые:
        #    - Время покупки < время создания TP ордера (trade_time < tp_order_time)
        #    - Размер покупки совпадает с размером продажи (с динамическим допуском tolerance = 10^(-sz_decimals))
        #    - Не старше 1 минуты от времени создания TP ордера (окно поиска с запасом)
        # 3. Если найдено несколько подходящих покупок, берем самую ПОСЛЕДНЮЮ (ближайшую к времени TP)
        #    Это гарантирует правильное сопоставление даже при множественных покупках лесенкой
        # 4. Если точного совпадения нет, ищем ближайшую по размеру покупку
        
        # Определяем верхнюю границу времени для поиска покупки
        
        if tp_order_time:
            time_limit = tp_order_time # Используем время СОЗДАНИЯ TP ордера из Open Orders - это правильная верхняя граница
        else:
            time_limit = None 
            client.browser.log_message("⚠️ No time limit found for buy trade search", level="WARNING")
            return None
        
        
        # Теперь ищем покупку с таким же объемом, время которой МЕНЬШЕ времени выставления TP ордера
        # КРИТИЧНО: TP ордер создается СРАЗУ после покупки (задержка несколько секунд/минут)
        # Поэтому ищем покупку в небольшом временном окне ДО времени создания TP ордера
        
        # Получаем sz_decimals для динамического расчета tolerance (если не передан как параметр)
        if sz_decimals is None:
            try:
                sz_decimals = client.get_token_sz_decimals(token_name)
            except:
                sz_decimals = 5  # Fallback
        
        # Динамический tolerance на основе sz_decimals: минимальная единица измерения размера
        # Например, для sz_decimals=5 tolerance будет 0.00001, для sz_decimals=3 - 0.001
        tolerance = 10 ** (-sz_decimals)
        
        max_time_diff_minutes = 1  # Максимальная разница между покупкой и созданием TP ордера (в минутах)
        # TP ордер создается СРАЗУ после покупки, задержка обычно ~15 секунд
        # 1 минута - достаточный запас для учета возможных задержек
        max_time_diff_ms = max_time_diff_minutes * 60 * 1000  # Преобразуем в миллисекунды
        
        
        # Отладочное логирование: выводим все покупки для анализа
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
            
            # Проверка: если разница больше max_time_diff_minutes, это подозрительно
            if time_diff_min > max_time_diff_minutes:
                client.browser.log_message(
                    f"⚠️ WARNING: Time difference is {time_diff_min:.1f} minutes! "
                    f"Expected: ~15 seconds (max {max_time_diff_minutes} min). This may indicate wrong buy trade match!",
                    level="WARNING"
                )
            
            client.browser.log_message(
                f"⏱️ Time comparison: Buy at {buy_datetime.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"TP created at {tp_datetime.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"Difference: {time_diff_sec:.1f} seconds ({time_diff_min:.2f} minutes)",
                level="INFO"
            )
            
            return buy_trade
        
        # Если не нашли точное совпадение, возвращаем None
        return None
        
    except Exception as e:
        client.browser.log_message(f"Failed to find matching buy trade: {e}", level="ERROR")
        return None


# Файл персистентности якорей hold_benchmark (депо_старт / цена_старт).
# Якорь фиксируется ОДИН РАЗ при первой операции First Position и больше
# никогда не перезаписывается — иначе сравнение Total Value vs hold_benchmark
# при перезапусках теряет смысл. Хранится per-label, чтобы несколько
# аккаунтов не затирали друг друга.
STATS_DB_FILE = "databases/stats.json"
HOLD_ANCHOR_KEY = "hold_benchmark_anchor"


def load_hold_anchor(label: str):
    """
    Читает якорь hold_benchmark для label из databases/stats.json.

    Returns:
        dict {depo_start, price_start, timestamp} или None если якоря нет.
        Существующие ключи файла не трогаются.
    """
    try:
        if not os.path.exists(STATS_DB_FILE):
            return None
        with open(STATS_DB_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        anchors = data.get(HOLD_ANCHOR_KEY)
        if not isinstance(anchors, dict):
            return None
        # Backward compat: старый плоский формат {depo_start, price_start, ...}
        if "depo_start" in anchors and "price_start" in anchors:
            return anchors
        entry = anchors.get(label)
        if isinstance(entry, dict) and entry.get("depo_start") and entry.get("price_start"):
            return entry
        return None
    except Exception:
        return None


def save_hold_anchor(label: str, depo_start: float, price_start: float):
    """
    Записывает якорь hold_benchmark для label в databases/stats.json.
    Пишет ТОЛЬКО если якоря для этого label еще нет (повторная запись запрещена).
    Остальное содержимое файла сохраняется как есть.

    Returns:
        True если якорь записан (или уже был и совпадает по смыслу — т.е. якорь есть),
        актуальный якорь dict.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = {}
    try:
        if os.path.exists(STATS_DB_FILE):
            with open(STATS_DB_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
    except Exception:
        data = {}

    anchors = data.get(HOLD_ANCHOR_KEY)
    if not isinstance(anchors, dict):
        anchors = {}

    # Плоский legacy-формат в per-label не мигрируем молча — просто не трогаем,
    # новый якорь кладем рядом под label.
    if "depo_start" in anchors and "price_start" in anchors and label not in anchors:
        pass

    existing = anchors.get(label)
    if isinstance(existing, dict) and existing.get("depo_start") and existing.get("price_start"):
        return existing  # уже зафиксирован — не перетираем

    entry = {
        "depo_start": depo_start,
        "price_start": price_start,
        "timestamp": timestamp,
    }
    anchors[label] = entry
    data[HOLD_ANCHOR_KEY] = anchors
    os.makedirs(os.path.dirname(STATS_DB_FILE) or ".", exist_ok=True)
    with open(STATS_DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return entry


def ensure_hold_anchor(client, depo_start: float = None, price_start: float = None):
    """
    Гарантирует наличие якоря hold_benchmark в памяти client.

    Логика (приоритет сверху вниз):
    1. Если якорь уже есть в памяти client — ничего не делать (рестарты
       в рамках одного запуска не перетирают значения).
    2. Иначе прочитать из databases/stats.json — если есть, взять оттуда.
    3. Иначе (только First Position передает depo_start/price_start) —
       зафиксировать переданные значения в памяти + записать на диск.

    Returns:
        dict якоря или None.
    """
    if getattr(client, '_hold_depo_start', None) and getattr(client, '_hold_price_start', None):
        return {
            "depo_start": client._hold_depo_start,
            "price_start": client._hold_price_start,
        }
    label = getattr(client, 'label', 'default')
    anchor = load_hold_anchor(label)
    if anchor is None and depo_start and price_start:
        anchor = save_hold_anchor(label, depo_start, price_start)
    if anchor:
        client._hold_depo_start = float(anchor["depo_start"])
        client._hold_price_start = float(anchor["price_start"])
        return anchor
    return None


def calc_hold_benchmark(client, current_price: float):
    """
    hold_benchmark = депо_старт / цена_старт × цена_сейчас.
    Стоимость депозита, если бы на старте весь депозит ушел в BTC.

    Returns:
        float или None если якорь еще не зафиксирован.
    """
    depo_start = getattr(client, '_hold_depo_start', None)
    price_start = getattr(client, '_hold_price_start', None)
    if not depo_start or not price_start or not current_price:
        return None
    return depo_start / price_start * float(current_price)


def floor_size(amount: float, decimals: int) -> float:
    """
    Округление размера ВНИЗ до decimals знаков. Для SELL важно именно вниз:
    биржа реджектит продажу, если размер хоть на пыль выше свободного остатка.
    """
    factor = 10 ** decimals
    return math.floor(float(amount) * factor) / factor


def fee_to_usdc(fee_str, fee_token: str, ref_price: float, token_name: str) -> float:
    """
    Конвертирует комиссию из API в USDC.
    Важно: биржа возвращает feeToken с U-префиксом (UBTC), а token_name
    в стратегии без него (BTC) — прямое сравнение их не матчит, и комиссия
    молча обнулялась (все BUY-комиссии считались $0.0000). Нормализуем.
    """
    try:
        fee = float(fee_str or 0)
    except Exception:
        return 0.0
    t = (fee_token or "").upper()
    base = (token_name or "").upper()
    if t == "USDC":
        return fee
    if t == base or t == f"U{base}" or (t.startswith("U") and t[1:] == base):
        return fee * float(ref_price)
    return 0.0


def aggregate_buys_by_oid(trades_history):
    """
    Группирует fills покупок по oid ордера: биржа разбивает исполнение одного
    ордера на несколько записей (сплиты вида 0.00002 + 0.00022). Без группировки
    записи-крошки выглядят «покупками без TP» и плодят дубли TP.
    Returns:
        dict oid -> {oid, price (VWAP), amount (сумма), timestamp (max)}.
    """
    grouped = {}
    for trade in (trades_history or []):
        if trade.get("side", "").upper() not in ["BUY", "B"]:
            continue
        oid = trade.get("oid")
        try:
            px = float(trade.get("px", 0))
            sz = float(trade.get("sz", 0))
            ts = trade.get("time", 0)
        except Exception:
            continue
        if not (px > 0 and sz > 0 and oid):
            continue
        g = grouped.get(oid)
        if g is None:
            grouped[oid] = {"oid": oid, "price": px, "amount": sz, "timestamp": ts}
        else:
            total = g["amount"] + sz
            g["price"] = (g["price"] * g["amount"] + px * sz) / total
            g["amount"] = total
            g["timestamp"] = max(g["timestamp"], ts)
    return grouped


async def find_orphan_buys(client, token_name: str, trades_history, open_orders,
                           sz_decimals: int, last_buy_timestamp=None,
                           max_age_days: int = 7, min_qty: float = 0.0):
    """
    Стартовое сканирование сирот: покупки из истории, у которых нет ни
    висящего TP SELL, ни более поздней продажи. Такие покупки обычному SET_TP
    уже не видны (их timestamp не новее last_buy_timestamp), без этого скана
    они остались бы без TP навсегда.

    Покрытие ЖАДНОЕ и ПОШТУЧНОЕ: один висящий sell покрывает одну покупку
    (старые первыми), совпадение — по размеру И по цене (ожидаемый TP =
    entry + STEP). Иначе одинаковые размеры разных входов дают ложные
    пропуски, а «близнецы» (размер+цена совпали) — ложных сирот.
    Fills одного ордера предварительно группируются по oid
    (см. aggregate_buys_by_oid).

    Суммарный размер сирот ограничивается свободным балансом (от новых к старым),
    чтобы не ставить TP на пыль сверх имеющихся средств.

    Returns:
        list[{oid, price, amount, timestamp}] от старых к новым.
    """
    orphans = []
    try:
        if not trades_history:
            return orphans

        tolerance = 10 ** (-sz_decimals)
        price_tol = 1.0  # TP-цены целочисленные, STEP=200 >> 1.0 — чужие уровни не зацепит
        step_val = float(settings.STEP)
        now_ms = datetime.now().timestamp() * 1000
        max_age_ms = max_age_days * 24 * 3600 * 1000

        # Висящие продажи нашего токена: список (oid, sz, px), каждый покрывает одну покупку
        resting_sells = []
        for order in (open_orders or []):
            try:
                is_buy = order.get("isBuy", True)
                side = order.get("side", "")
                is_sell = side in ["SELL", "S", "A"] or (not is_buy)
                if is_sell:
                    sz = float(order.get("sz", 0))
                    px = float(order.get("limitPx", 0))
                    if sz > 0 and px > 0:
                        resting_sells.append({"oid": order.get("oid"), "sz": sz, "px": px})
            except Exception:
                continue

        sells = [
            (t.get("time", 0), float(t.get("sz", 0)))
            for t in trades_history
            if t.get("side", "").upper() in ["SELL", "S", "A"]
        ]

        # Кандидаты oldest-first; покрытие — жадное, один sell = один buy
        candidates = []
        for buy_oid, grp in aggregate_buys_by_oid(trades_history).items():
            buy_time = grp["timestamp"]
            buy_price = grp["price"]
            buy_amount = grp["amount"]
            # Крошки меньше биржевого минимума TP не получат никогда —
            # не ставим в очередь (пыль копим, не гоняем)
            if buy_amount < min_qty:
                continue
            # Свежие покупки разберет обычный поток SET_TP — скану они не нужны
            if last_buy_timestamp is None or buy_time > last_buy_timestamp:
                continue
            if now_ms - buy_time > max_age_ms:
                continue
            candidates.append({
                "oid": buy_oid,
                "price": buy_price,
                "amount": buy_amount,
                "timestamp": buy_time,
            })
        candidates.sort(key=lambda x: x["timestamp"])

        used_rest = set()
        for cand in candidates:
            expect_px = cand["price"] + step_val
            hit_idx = None
            for idx, r in enumerate(resting_sells):
                if idx in used_rest:
                    continue
                if abs(r["sz"] - cand["amount"]) <= tolerance and abs(r["px"] - expect_px) <= price_tol:
                    hit_idx = idx
                    break
            if hit_idx is not None:
                used_rest.add(hit_idx)
                continue  # защищена висящим TP
            # Есть более поздняя продажа того же размера → позиция закрыта
            if any(st > cand["timestamp"] and abs(ssz - cand["amount"]) <= tolerance for st, ssz in sells):
                continue
            orphans.append(cand)

        if not orphans:
            return orphans

        # Ограничиваем суммарный размер свободным балансом (от новых к старым)
        try:
            free_btc = float(await client.browser.get_available_token_balance(token_name))
        except Exception:
            free_btc = 0.0
        orphans.sort(key=lambda x: x["timestamp"], reverse=True)
        picked, cum = [], 0.0
        for o in orphans:
            if cum + o["amount"] <= free_btc + tolerance:
                picked.append(o)
                cum += o["amount"]
        picked.sort(key=lambda x: x["timestamp"])
        return picked

    except Exception as e:
        try:
            client.browser.log_message(f"Failed to scan orphan buys: {e}", level="ERROR")
        except Exception:
            pass
        return []


def _statistics_columns(token_name: str) -> list:
    """Единый список колонок файла статистики (имена зависят от токена)."""
    return [
        'Timestamp', f'Current {token_name} Price', 'Operation', f'{token_name} Amount',
        'Operation Price', 'USDC Balance', f'{token_name} Balance', 'Limit Profit', 'Total Value',
        'Hold Benchmark', 'Profit'
    ]


def _stats_value_to_str(value) -> str:
    """CSV не хранит типы: None пишем пустой строкой, прочее — как есть."""
    if value is None:
        return ""
    return str(value)


def _migrate_stats_rows(header: list, rows: list, expected_columns: list) -> tuple:
    """
    Приводит старый файл к актуальной структуре, не теряя строки:
    колонку 'Limit Profit' вставляет перед 'Total Value' (как делал pandas),
    остальные недостающие — в конец. Возвращает (new_header, new_rows).
    """
    new_header = list(header)
    if 'Limit Profit' not in new_header and 'Total Value' in new_header:
        idx = new_header.index('Total Value')
        new_header.insert(idx, 'Limit Profit')
    for col in expected_columns:
        if col not in new_header:
            new_header.append(col)

    new_rows = [new_header]
    for row in rows:
        # Выравниваем строку по старому заголовку, недостающее — 0.0.
        old_by_name = {col: (row[i] if i < len(row) else None) for i, col in enumerate(header)}
        new_rows.append([old_by_name.get(col) if old_by_name.get(col) is not None else '0.0'
                         for col in new_header])
    return new_header, new_rows


def initialize_statistics_file(token_name: str = "BTC"):
    """
    Инициализирует файл статистики (CSV) с правильной структурой колонок при старте.
    Вызывается один раз при запуске стратегии.

    Args:
        token_name: Название торгуемого токена (по умолчанию "BTC")
    """
    stats_file = "hype_stat.csv"
    expected_columns = _statistics_columns(token_name)

    if not os.path.exists(stats_file):
        with open(stats_file, 'w', encoding='utf-8', newline='') as f:
            csv.writer(f).writerow(expected_columns)
        logger.info(f"✅ Created statistics file: {stats_file}")
        return

    # Файл существует — дополняем недостающие колонки, не теряя данные.
    try:
        with open(stats_file, 'r', encoding='utf-8', newline='') as f:
            rows = list(csv.reader(f))

        if not rows:
            with open(stats_file, 'w', encoding='utf-8', newline='') as f:
                csv.writer(f).writerow(expected_columns)
            logger.info(f"✅ Updated statistics file structure: empty file initialized")
            return

        header = rows[0]
        missing = [col for col in expected_columns if col not in header]
        if missing:
            new_header, new_rows = _migrate_stats_rows(header, rows[1:], expected_columns)
            with open(stats_file, 'w', encoding='utf-8', newline='') as f:
                csv.writer(f).writerows(new_rows)
            logger.info(f"✅ Updated statistics file structure: added {len(missing)} column(s)")
    except Exception as e:
        logger.warning(f"⚠️ Failed to check statistics file structure: {e}")


async def log_statistics_to_csv(client, operation: str, btc_amount: float, price: float,
                                usdc_balance: float, btc_balance: float, total_value: float,
                                orders=None, sz_decimals: int = None, token_name: str = "BTC",
                                hold_benchmark: float = None, profit: float = None):
    """
    Записывает статистику операции в CSV файл hype_stat.csv

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
        hold_benchmark: Эталон buy-and-hold (опционально; если не передан —
            считается внутри из якоря client: депо_старт / цена_старт × цена_сейчас)
        profit: Профит сделки в USDC (то же значение, что уходит в Telegram).
            Заполняется только для Take Profit, иначе None.
    """
    try:
        # Получаем текущую цену токена
        current_token_price = float(await client.browser.get_token_price(token_name))

        # Если ордера не переданы, получаем их
        if orders is None:
            orders = await client.browser.get_account_orders()

        # Рассчитываем сумму Order Value для всех лимитных ордеров на продажу.
        # Это ВАЛОВАЯ стоимость висящих sell (notional), а не чистая прибыль:
        # себестоимость позиций не вычитается. Имя колонки 'Limit Profit'
        # сохранено для совместимости со старым форматом статистики.
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

        # Округляем btc_amount до sz_decimals для соответствия формату биржи
        btc_amount = round(btc_amount, sz_decimals)

        # Hold Benchmark: если явно не передан — считаем из якоря client.
        # Якорь фиксируется один раз при First Position (см. ensure_hold_anchor),
        # до этого момента колонка остается пустой (None).
        if hold_benchmark is None:
            ensure_hold_anchor(client)
            hold_benchmark = calc_hold_benchmark(client, current_token_price)

        # Profit=None (не Take Profit) -> 0.0: так заведены все неторговые строки.
        if profit is None:
            profit = 0.0

        expected_columns = _statistics_columns(token_name)
        new_row = {
            'Timestamp': timestamp,
            f'Current {token_name} Price': current_token_price,
            'Operation': operation,
            f'{token_name} Amount': btc_amount,
            'Operation Price': price,
            'USDC Balance': usdc_balance,
            f'{token_name} Balance': btc_balance,
            'Limit Profit': limit_profit,
            'Total Value': total_value,
            'Hold Benchmark': hold_benchmark,
            'Profit': profit
        }

        stats_file = "hype_stat.csv"

        # Гарантируем наличие файла и корректный заголовок перед дозаписью.
        initialize_statistics_file(token_name)

        # Читаем существующий заголовок, чтобы упорядочить значения.
        with open(stats_file, 'r', encoding='utf-8', newline='') as f:
            reader = csv.reader(f)
            header = next(reader, expected_columns)

        with open(stats_file, 'a', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([_stats_value_to_str(new_row.get(col)) for col in header])

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


async def send_tg_notification(client, text: str, save_to_report: bool = True):
    """
    Отправляет уведомление в Telegram немедленно (без ожидания окончания работы модуля).
    При выставленном stop-флаге уведомления глушатся (защита от фантомного процесса).
    """
    try:
        if get_bot_stop_flag():
            logger.warning("⛔ Bot stop flag is set, skipping Telegram notification")
            return
        await TgReport().send_log(logs=text)
        # PostgreSQL-отчёты удалены (I2): персистентность — JSON/Excel/TG.
        # save_to_report оставлен в сигнатуре для совместимости вызовов.
    except Exception as e:
        pass


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


async def _stop_watchdog(watchdog_task):
    """Аккуратно гасит задачу-сторожа (None-safe, без warnings event loop)."""
    if watchdog_task is None:
        return
    try:
        watchdog_task.cancel()
        await watchdog_task
    except (asyncio.CancelledError, Exception):
        pass


async def trade_delayed_orders_strategy(client, token_name: str,
                                        max_iterations: int = 0,
                                        poll_interval_override: float = None):
    """
    Новая стратегия:
    - При отсутствии TP ордеров: маркет покупка по текущей цене + TP ордер
    - Усреднение: маркет покупка при current_price < min_TP_price - STEP*2
    - Пирамидинг: маркет покупка при current_price > max_TP_price - STEP*AGGR (AGGR по умолчанию 0.5)
    - Для каждого BUY сразу ставим лимитный ордер на продажу (TP)

    max_iterations=0 — прежний бесконечный цикл (VPS). Число >0 — отработать
    ровно столько итераций и вернуться: дежурство (§5) вызывает одну итерацию
    на тик, повторяя тот же боевой код, а не его копию.
    poll_interval_override — пауза между итерациями вместо BOT_POLL_INTERVAL
    (нужна дежурству: его цикл уже спит сам, двойной сон не нужен).
    """
    try:
        watchdog_task = None  # задача-сторож, создаётся перед циклом; остановка None-safe
        step = Decimal(str(settings.STEP))
        client.browser.log_message(f"📊 New Strategy: STEP ${step}", level="INFO")
        
        # Тестируем функцию расчета прибыли при запуске (сейчас используем расчет через баланс)
        # client.browser.log_message("🔍 Testing profit calculation function...", level="INFO")
        # test_profit = await calculate_real_profit(client, token_name, 0.001, 110000.0)
        # client.browser.log_message(f"🔍 Test profit calculation result: ${test_profit:.2f}", level="INFO")
        
        # Тест DEBUG лога отключен для чистоты логов
        # client.browser.log_message("🔍 DEBUG: Strategy function started", level="INFO")
        
        # Инициализируем файл статистики при старте (создаем, если не существует)
        initialize_statistics_file(token_name)

        # Восстанавливаем якорь hold_benchmark (депо_старт / цена_старт) с диска.
        # Только чтение: если якорь уже зафиксирован ранее — используем его,
        # повторная запись при перезапусках запрещена.
        ensure_hold_anchor(client)
        
        # Загружаем лимиты токена один раз при старте (кэшируются в client)
        min_qty, min_notional, sz_decimals = await client.load_token_limits(token_name)
        
        # Получаем информацию о последней покупке из Trade History при старте 
        last_buy_price = None
        last_buy_size = None
        last_buy_timestamp = None
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
                        last_buy_timestamp = trade.get('time', 0)
                        if last_buy_price > 0 and last_buy_size > 0:
                            break
        except Exception as e:
            client.browser.log_message(f"Failed to get last buy info: {e}", level="WARNING")

        # Сбрасываем stop-флаг при старте стратегии (порт из lighter):
        # прошлый запуск мог завершиться по Ctrl+C с выставленным флагом.
        set_bot_stop_flag(False)

        # Добираем персистентные timestamps покупок с диска (порт из lighter):
        # переживают рестарты, дополняют in-memory last_buy_timestamp.
        try:
            if getattr(client, 'db', None) is not None:
                persisted = client.db.get_last_buy_timestamps(client.wallet_address, token_name) or []
                if persisted:
                    persisted_max = max(persisted)
                    if last_buy_timestamp is None or persisted_max > last_buy_timestamp:
                        last_buy_timestamp = persisted_max
                    client.browser.log_message(
                        f"💾 Restored {len(persisted)} buy timestamp(s) from DB "
                        f"(latest: {datetime.fromtimestamp(persisted_max / 1000).strftime('%Y-%m-%d %H:%M:%S')})",
                        level="INFO"
                    )
        except Exception as e:
            client.browser.log_message(f"Failed to restore buy timestamps: {e}", level="WARNING")
        
        # Структуры незащищенных покупок (фиксы 2-4):
        # _retry_tp_buys: oid -> buy info, TP не выставлен — ретраить вне зависимости от timestamp
        # _pending_tp_oids: oids покупок без TP — блокируют First Position (фикс 3)
        if not hasattr(client, '_retry_tp_buys'):
            client._retry_tp_buys = {}
        if not hasattr(client, '_pending_tp_oids'):
            client._pending_tp_oids = set()
        # Трекинг BuyLimit-ордеров для правила refill (один уровень — один fill
        # до подтвержденного TP): _buylimit_open: order oid -> {level},
        # _buylimit_wait_tp: {level, expect_tp_px, since} | None
        if not hasattr(client, '_buylimit_open'):
            client._buylimit_open = {}
        if not hasattr(client, '_buylimit_wait_tp'):
            client._buylimit_wait_tp = None
        
        # Стартовое сканирование сирот (фикс 4): покупки из истории без TP.
        # Только чтение + постановка в retry-очередь, торговую логику не меняет.
        try:
            startup_orders = await client.browser.get_account_orders()
            orphans = await find_orphan_buys(
                client, token_name, trades_history, startup_orders,
                sz_decimals, last_buy_timestamp=last_buy_timestamp,
                min_qty=min_qty,
            )
            for orphan in orphans:
                client._retry_tp_buys[orphan["oid"]] = orphan
                client._pending_tp_oids.add(orphan["oid"])
                client.browser.log_message(
                    f"🔍 Orphan buy without TP: {orphan['amount']:.6f} {token_name} "
                    f"at ${orphan['price']:.2f} (oid: {orphan['oid']}) — queued for TP",
                    level="INFO"
                )
        except Exception as e:
            client.browser.log_message(f"Failed orphan TP scan: {e}", level="WARNING")
        
        # Кэш для отслеживания изменений в ордерах и балансах
        previous_orders_state = None
        previous_balance_state = None
        
        # Время последнего статусного сообщения (для дежурных сообщений каждые 30 минут)
        import time
        status_interval = 1800  # 30 минут в секундах
        last_status_time = 0  # Установлено в 0, чтобы статус выводился сразу при запуске

        # Счетчик итераций для throttled heartbeat (CYCLE пишется каждую 120-ю, ~раз в 22 мин)
        cycle_count = 0
        cycle_log_every = 120

        # Сторожевой heartbeat: метка прогресса + фоновая задача-сторож.
        client._watchdog_last_mark = time.monotonic()
        watchdog_task = asyncio.create_task(_cycle_watchdog(client))

        # Лимит вотча (§9.п5): дедлайн проверяется вверху каждой итерации,
        # стартовавшая итерация всегда дорабатывает целиком (хвост §2.2).
        watch_seconds = get_watch_seconds()
        poll_interval = (float(poll_interval_override)
                         if poll_interval_override is not None
                         else get_poll_interval())
        watch_start = time.monotonic()
        if watch_seconds > 0:
            client.browser.log_message(
                f"⏱️ Watch limit: {watch_seconds:.0f}s, poll: {poll_interval:.0f}s",
                level="INFO",
            )

        while True:
            try:
                # Чистое завершение по stop-флагу (порт из lighter)
                if get_bot_stop_flag():
                    client.browser.log_message(
                        "🛑 [STOP_FLAG] Bot stop flag detected, exiting strategy loop",
                        level="WARNING"
                    )
                    break
                # Выход по вотчу — до любых запросов/постановок
                if watch_seconds > 0 and (time.monotonic() - watch_start) > watch_seconds:
                    client.browser.log_message(
                        "retiring: watch over, yielding to next run",
                        level="INFO",
                    )
                    break
                cycle_count += 1
                client._watchdog_last_mark = time.monotonic()
                # Single-pass mode (дежурство §5): отработать ровно max_iterations
                # проходов и вернуться вызывающему. Проверка вверху — пути с
                # `continue` тоже завершаются после одного полного прохода.
                if max_iterations and cycle_count > max_iterations:
                    client.browser.log_message(
                        f"🛑 Single-pass mode: {max_iterations} iteration(s) done — exiting",
                        level="INFO")
                    break
                if cycle_count % cycle_log_every == 0:
                    client.browser.log_message(f"🔄 CYCLE #{cycle_count}", level="INFO")
                # Задержка между итерациями основного цикла
                # Перемещена в начало для того, чтобы после создания ордеров была пауза
                # и они успели появиться в API перед следующей итерацией
                await async_sleep(poll_interval)
                # Повторная проверка после сна — выходим до любых запросов/ордеров
                if get_bot_stop_flag():
                    client.browser.log_message(
                        "🛑 [STOP_FLAG] Bot stop flag detected after sleep, exiting strategy loop",
                        level="WARNING"
                    )
                    break
                
                # DEBUG лог отключен для чистоты логов
                # client.browser.log_message("🔍 DEBUG: Starting strategy iteration", level="INFO")
                
                # Получаем данные параллельно для оптимизации
                try: # Отдельная обработка для получения данных
                    tasks = [
                        client.browser.get_token_price(token_name),
                        client.browser.get_account_orders(),
                        get_active_tp_orders(client, token_name),
                        client.browser.get_all_balances()
                    ]
                    current_price_raw, open_orders, tp_orders, all_balances = await asyncio.gather(*tasks)
                    current_price = float(current_price_raw)
                except Exception as gather_error:
                    client.browser.log_message(f"⚠️ Failed to fetch data: {gather_error}", level="ERROR")
                    await async_sleep(5)
                    continue  # Пропускаем итерацию, НО внешний try не прерывается

                # Дежурное статусное сообщение каждые 30 минут
                current_time = time.time()
                if current_time - last_status_time >= status_interval:
                    # Получаем балансы
                    usdc_balance = all_balances.get("USDC", 0.0)
                    btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                    
                    # Подсчитываем количество TP ордеров
                    tp_count = len(tp_orders) if tp_orders else 0
                    
                    # Формируем информацию о последней покупке
                    if last_buy_price and last_buy_timestamp:
                        # Преобразуем timestamp из миллисекунд в читаемый формат
                        buy_datetime = datetime.fromtimestamp(last_buy_timestamp / 1000)
                        buy_time_str = buy_datetime.strftime('%H:%M:%S')
                        last_buy_info = f"LastBuy: {last_buy_price:.2f} at {buy_time_str}"
                    else:
                        last_buy_info = "LastBuy: N/A"
                    
                    # Выводим статусное сообщение
                    client.browser.log_message(
                        f"💚 Price: {current_price:.2f}, USDC:{usdc_balance:.2f}, "
                        f"{token_name}:{btc_balance:.6f}, {last_buy_info}",
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
                # Он проверяет историю сделок и выставляет тейк-профит для всех новых покупок.
                try:
                    # Инициализируем множество обработанных покупок (если еще не создано)
                    if not hasattr(client, '_processed_buy_trades'):
                        client._processed_buy_trades = set()
                    if not hasattr(client, '_retry_tp_buys'):
                        client._retry_tp_buys = {}
                    if not hasattr(client, '_pending_tp_oids'):
                        client._pending_tp_oids = set()
                    if not hasattr(client, '_buylimit_open'):
                        client._buylimit_open = {}
                    if not hasattr(client, '_buylimit_wait_tp'):
                        client._buylimit_wait_tp = None
                    # Получаем историю торговли (последние 100 сделок)
                    trades_history = await client.browser.get_all_orders_history(token_name, limit=100)
                    # Трекинг fills выставленных нами BuyLimit-ордеров: fill уровня ->
                    # ждем подтверждения его TP перед refill того же уровня.
                    # (oid fill-а совпадает с oid ордера.)
                    try:
                        for trade in (trades_history or []):
                            if trade.get('side', '').upper() not in ['BUY', 'B']:
                                continue
                            foid = trade.get('oid')
                            if foid is not None and foid in client._buylimit_open:
                                lvl = client._buylimit_open.pop(foid).get("level", 0.0)
                                try:
                                    fpx = float(trade.get('px', 0))
                                except Exception:
                                    fpx = 0.0
                                if fpx > 0:
                                    client._buylimit_wait_tp = {
                                        "level": float(lvl),
                                        "expect_tp_px": fpx + float(step),
                                        "since": datetime.now().timestamp(),
                                    }
                    except Exception:
                        pass
                    # Собираем новые покупки: fills группируем по oid ордера, т.к.
                    # биржа разбивает одно исполнение на несколько записей (сплиты).
                    # Без группировки каждая запись выглядела бы отдельной покупкой
                    # и получала бы свой TP (дубли). Новой считается группа с
                    # max timestamp > last_buy_timestamp.
                    new_buys = []
                    if trades_history:
                        for trade_oid, grp in aggregate_buys_by_oid(trades_history).items():
                            # Проверяем, не обработана ли уже эта покупка
                            if trade_oid in client._processed_buy_trades:
                                continue
                            # Проверяем, является ли покупка новой
                            is_new_buy = False
                            if last_buy_timestamp is None:
                                # Первая покупка - всегда новая
                                is_new_buy = True
                            elif grp["timestamp"] > last_buy_timestamp:
                                # Покупка с более поздним timestamp - новая покупка
                                is_new_buy = True

                            if is_new_buy:
                                new_buys.append({
                                    'oid': trade_oid,
                                    'price': grp["price"],
                                    'amount': grp["amount"],
                                    'timestamp': grp["timestamp"],
                                    # Свежая покупка: ее TP заведомо еще не выставлен,
                                    # dedupe-guard ниже ее не касается
                                    'retry': False,
                                })
                    # Добираем ретраи (фикс 2): покупки, чей TP не выставился ранее.
                    # Они обрабатываются ВНЕ зависимости от timestamp — иначе после
                    # сдвига last_buy_timestamp вперед они не были бы «новыми»
                    # и остались бы без TP навсегда (так потерялся первый вход).
                    # Данные обновляем из АГРЕГИРОВАННОЙ по oid истории (сплиты!),
                    # иначе refresh хватала бы первую (частичную) запись.
                    buys_by_oid = aggregate_buys_by_oid(trades_history)
                    for retry_oid, retry_buy in list(client._retry_tp_buys.items()):
                        if retry_oid in client._processed_buy_trades:
                            client._retry_tp_buys.pop(retry_oid, None)
                            client._pending_tp_oids.discard(retry_oid)
                            continue
                        # Обновляем данные из свежей истории, если покупка там есть.
                        # ВАЖНО: amount только УМЕНЬШАЕМ (min) — в записи может лежать
                        # непокрытый остаток частичного TP, полный размер из истории
                        # его бы затер и вернул перекрытие.
                        grp = buys_by_oid.get(retry_oid)
                        if grp is not None:
                            kept_amount = retry_buy.get("amount", grp["amount"])
                            retry_buy = {
                                'oid': retry_oid,
                                'price': grp["price"],
                                'amount': min(kept_amount, grp["amount"]),
                                'timestamp': grp["timestamp"],
                                'retry': True,
                            }
                            client._retry_tp_buys[retry_oid] = retry_buy
                        if not any(b['oid'] == retry_oid for b in new_buys):
                            entry = dict(retry_buy)
                            entry['retry'] = True
                            new_buys.append(entry)
                    # Dedupe-guard ТОЛЬКО для retry/orphan (свежие покупки его не
                    # проходят: их TP заведомо еще не выставлен). Связка размер+цена
                    # (ожидаемый TP = entry + STEP) + поштучный учет: один висящий
                    # sell покрывает одну покупку, старые первыми. Иначе одинаковые
                    # размеры разных входов дают ложные пропуски, а «близнецы»
                    # (размер+цена совпали у разных входов) — ложных сирот.
                    if new_buys:
                        tp_tol = 10 ** (-sz_decimals)
                        rested = []
                        for o in current_tp_orders:
                            try:
                                rsz = float(o.get("sz", 0))
                                rpx = float(o.get("limitPx", 0))
                                if rsz > 0 and rpx > 0:
                                    rested.append({"oid": o.get("oid"), "sz": rsz, "px": rpx})
                            except Exception:
                                continue
                        used_rest = set()
                        kept = []
                        for b in sorted(new_buys, key=lambda x: x["timestamp"]):
                            if not b.get("retry"):
                                kept.append(b)
                                continue
                            expect_px = b["price"] + float(step)
                            hit_idx = None
                            for idx, r in enumerate(rested):
                                if idx in used_rest:
                                    continue
                                if abs(r["sz"] - b["amount"]) <= tp_tol and abs(r["px"] - expect_px) <= 1.0:
                                    hit_idx = idx
                                    break
                            if hit_idx is not None:
                                used_rest.add(hit_idx)
                                client.browser.log_message(
                                    f"🔍 TP already resting for buy oid {b['oid']} "
                                    f"({b['amount']:.6f} {token_name} -> ${expect_px:.2f}) — skipping duplicate",
                                    level="INFO"
                                )
                                client._processed_buy_trades.add(b["oid"])
                                client._retry_tp_buys.pop(b["oid"], None)
                                client._pending_tp_oids.discard(b["oid"])
                            else:
                                kept.append(b)
                        new_buys = sorted(kept, key=lambda x: x["timestamp"], reverse=True)
                    # Обрабатываем все новые покупки (от новых к старым)
                    if new_buys:
                        # Сортируем по timestamp (от новых к старым)
                        new_buys.sort(key=lambda x: x['timestamp'], reverse=True)

                        # Свободный остаток для корректного размера TP (фикс 1):
                        # биржа реджектит продажу, если размер хоть на пыль выше
                        # доступного (зачисление после fill меньше на комиссию).
                        try:
                            free_btc = float(await client.browser.get_available_token_balance(token_name))
                        except Exception:
                            free_btc = 0.0

                        # NOTE: last_buy_timestamp и _processed_buy_trades обновляются
                        # ТОЛЬКО после успешного выставления TP (фикс 2). Неуспех —
                        # в retry-очередь, повторная попытка на следующих итерациях.

                        # Обрабатываем каждую новую покупку
                        for buy in new_buys:
                            trade_oid = buy['oid']
                            buy_price = buy['price']
                            buy_amount = buy['amount']

                            # Размер TP: min(fill, свободный остаток), округление ВНИЗ.
                            # Обычно равен fill; чуть меньше — только на величину комиссии.
                            tp_qty = floor_size(min(buy_amount, free_btc), sz_decimals)

                            if tp_qty < min_qty:
                                client.browser.log_message(
                                    f"⚠️ TP skipped for buy oid {trade_oid}: "
                                    f"available {free_btc:.6f} {token_name} < min {min_qty} "
                                    f"(will retry when balance grows)",
                                    level="WARNING"
                                )
                                client._retry_tp_buys[trade_oid] = buy
                                client._pending_tp_oids.add(trade_oid)
                                continue

                            # Рассчитываем цену TP
                            tp_price = normalize_price(buy_price + float(step), sz_decimals)

                            # Размещение TP-ордера на бирже
                            try:
                                tp_order_data = {
                                    "token": token_name,
                                    "side": "SELL",
                                    "order_type": "LIMIT",
                                    "quantity": tp_qty,
                                    "price": tp_price,
                                }

                                tp_result = await client.place_order(**tp_order_data)

                                # Успех: только теперь помечаем обработанной (фикс 2)
                                client._processed_buy_trades.add(trade_oid)
                                # Частичный TP: непокрытый остаток (>= min_qty) остается
                                # в retry — иначе он без защиты навсегда. Пыль (< min_qty)
                                # не queued: копится на балансе до ручной продажи.
                                remainder = buy_amount - tp_qty
                                if remainder >= min_qty:
                                    client._retry_tp_buys[trade_oid] = {
                                        'oid': trade_oid,
                                        'price': buy_price,
                                        'amount': remainder,
                                        'timestamp': buy['timestamp'],
                                        'retry': True,
                                    }
                                    client._pending_tp_oids.add(trade_oid)
                                else:
                                    client._retry_tp_buys.pop(trade_oid, None)
                                    client._pending_tp_oids.discard(trade_oid)
                                if last_buy_timestamp is None or buy['timestamp'] > last_buy_timestamp:
                                    last_buy_timestamp = buy['timestamp']
                                # Дублируем timestamp на диск (порт из lighter) — переживет рестарт.
                                # Ошибки БД не роняют торговлю.
                                try:
                                    if getattr(client, 'db', None) is not None:
                                        client.db.add_last_buy_timestamp(
                                            client.wallet_address, token_name, buy['timestamp']
                                        )
                                except Exception:
                                    pass
                                free_btc = max(0.0, free_btc - tp_qty)

                                client.browser.log_message(
                                    f"📉 Set TP: {tp_qty:.6f} {token_name} at ${tp_price:.2f} (buy oid: {trade_oid})",
                                    level="INFO"
                                )

                                # Отправляем уведомление в Telegram
                                await send_tg_notification(
                                    client,
                                    text=f'📉 Set TP: {tp_qty:.6f} {token_name} at ${tp_price:.2f}',
                                    save_to_report=False
                                )

                                # Записываем статистику операции Set TP
                                try:
                                    btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                                    total_value = all_balances.get("USDC", 0.0) + (btc_balance * current_price)
                                    await log_statistics_to_csv(
                                        client=client,
                                        operation="Set TP",
                                        btc_amount=tp_qty,
                                        price=tp_price,
                                        usdc_balance=all_balances.get("USDC", 0.0),
                                        btc_balance=btc_balance,
                                        total_value=total_value,
                                        sz_decimals=sz_decimals,
                                        token_name=token_name
                                    )
                                except Exception as e:
                                    client.browser.log_message(f"Failed to log Set TP statistics: {e}", level="ERROR")

                                # Обновляем last_buy_price на цену последней обработанной покупки
                                last_buy_price = buy_price

                            except Exception as e:
                                client.browser.log_message(f"❌ Failed to create TP order for buy oid {trade_oid}: {e}", level="ERROR")
                                # В retry-очередь и pending — повторная попытка на
                                # следующих итерациях (фикс 2). Из _processed_buy_trades
                                # НЕ удаляем, а просто туда не добавляем.
                                client._retry_tp_buys[trade_oid] = buy
                                client._pending_tp_oids.add(trade_oid)

                        # Прерываем цикл после обработки TP
                        # Это дает время ордерам появиться в API перед следующей итерацией.
                        # Продолжаем и при частичном неуспехе: First Position ниже
                        # дополнительно защищен проверкой _pending_tp_oids (фикс 3).
                        if new_buys:
                            continue
                                                
                except Exception as e:
                    client.browser.log_message(f"❌ SET_TP module error: {e}", level="ERROR")
                
                # Отладочная информация отключена для чистоты логов
                # client.browser.log_message(f"🔍 DEBUG: get_active_tp_orders returned {len(tp_orders)} orders", level="INFO")
                # if tp_orders:
                #     for tp_order in tp_orders:
                #         client.browser.log_message(f"🔍 DEBUG: TP order: {tp_order}", level="INFO")
                
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
                                    btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                                    
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
                                            # Получаем время СОЗДАНИЯ TP ордера из saved order
                                            # ВАЖНО: используем время создания, а НЕ время исполнения!
                                            # Если времени создания нет, используем STEP-based estimation
                                            tp_order_time = executed_order.get('timestamp')
                                            
                                            if tp_order_time:
                                                # Конвертируем в читаемый формат для проверки
                                                tp_datetime = datetime.fromtimestamp(tp_order_time / 1000)
                                                
                                                # Логируем время создания TP ордера
                                                log_msg = f"📅 TP order, created at: {tp_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
                                                
                                                # Если есть время исполнения, добавляем его для сравнения
                                                if sell_trade_from_history and sell_trade_from_history.get('time'):
                                                    exec_time = sell_trade_from_history.get('time')
                                                    exec_datetime = datetime.fromtimestamp(exec_time / 1000)
                                                    time_diff_minutes = (exec_time - tp_order_time) / (1000 * 60)
                                                    log_msg += f", executed at: {exec_datetime.strftime('%Y-%m-%d %H:%M:%S')} ({time_diff_minutes:.1f} min later)"
                                                
                                                log_msg += f" | Executed price: ${tp_price:.2f} (limit was ${tp_price_fallback:.2f})"
                                                
                                                client.browser.log_message(log_msg, level="INFO")
                                            else:
                                                client.browser.log_message(
                                                    f"⚠️ No timestamp found, using current time",
                                                    level="WARNING"
                                                )
                                            
                                            # Находим связанную покупку для расчета прибыли
                                            # Используем время выставления TP ордера, если доступно
                                            
                                            buy_trade = await find_matching_buy_trade(
                                                client, token_name, tp_order_id, filled_qty, tp_price, tp_order_time, sz_decimals
                                            )

                                            # Рассчитываем прибыль на основе конкретных сделок покупки и продажи
                                            if buy_trade:
                                                buy_price = buy_trade['px']
                                                buy_size = buy_trade['sz']
                                                
                                                # Получаем комиссии из API
                                                buy_fee_str = buy_trade.get('fee', '0')
                                                buy_fee_token = buy_trade.get('feeToken', '')
                                                
                                                # Рассчитываем комиссию покупки в USDC
                                                # (fee_to_usdc нормализует UBTC->BTC, иначе комиссия обнуляется)
                                                buy_fee_usdc = fee_to_usdc(buy_fee_str, buy_fee_token, buy_price, token_name)
                                                
                                                # Получаем данные о продаже из Trade History для точной комиссии продажи
                                                # Используем уже полученный sell_trade_from_history
                                                sell_fee_usdc = 0.0
                                                sell_fee_token = 'USDC'  # Значение по умолчанию
                                                
                                                if sell_trade_from_history:
                                                    sell_fee_str = sell_trade_from_history.get('fee', '0')
                                                    sell_fee_token = sell_trade_from_history.get('feeToken', 'USDC')
                                                    sell_fee_usdc = fee_to_usdc(sell_fee_str, sell_fee_token, tp_price, token_name)
                                                
                                                
                                                # Расчет выручки от продажи
                                                sell_total = filled_qty * tp_price
                                                sell_revenue = sell_total - sell_fee_usdc
                                                
                                                client.browser.log_message(
                                                    f"  💵 SELL: {filled_qty:.6f} {token_name} × ${tp_price:.2f} = ${sell_total:.2f} "
                                                    f"| Fee: ${sell_fee_usdc:.4f} ({sell_fee_token}) | "
                                                    f"Net revenue: ${sell_revenue:.2f}",
                                                    level="INFO"
                                                )
                                                
                                                # Расчет затрат на покупку ПРОПОРЦИОНАЛЬНО проданной доле:
                                                # TP может продать меньше входа (размер срезан до свободного
                                                # остатка), остаток-пыль остается на балансе и в себестоимость
                                                # этой продажи не входит (иначе каждая частичная продажа
                                                # показывала бы ложный убыток).
                                                qty_ratio = filled_qty / buy_size if buy_size > 0 else 1.0
                                                buy_total = filled_qty * buy_price
                                                buy_cost = buy_total + buy_fee_usdc * qty_ratio

                                                client.browser.log_message(
                                                    f"  💵 BUY: {filled_qty:.6f}/{buy_size:.6f} {token_name} × ${buy_price:.2f} = ${buy_total:.2f} "
                                                    f"| Fee: ${buy_fee_usdc * qty_ratio:.4f} ({buy_fee_token}) | "
                                                    f"Total cost: ${buy_cost:.2f}",
                                                    level="INFO"
                                                )
                                                
                                                # Чистая прибыль = выручка - затраты
                                                profit = sell_revenue - buy_cost
                                                
                                                # client.browser.log_message(f"  💰 PROFIT: ${sell_revenue:.2f} (revenue) - ${buy_cost:.2f} (cost) = ${profit:.2f}", level="INFO")
                                                
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
                                                # Предполагаем, что покупка была на STEP ниже
                                                buy_price = tp_price - float(settings.STEP)
                                                
                                                # Учитываем комиссию И при покупке, И при продаже (0.0144% = 0.000144)
                                                buy_commission = filled_qty * buy_price * 0.000144  # Комиссия при покупке
                                                sell_commission = filled_qty * tp_price * 0.000144  # Комиссия при продаже
                                                
                                                # Чистая прибыль = STEP - обе комиссии
                                                step_profit = filled_qty * float(settings.STEP)
                                                profit = step_profit - buy_commission - sell_commission
                                                
                                                client.browser.log_message(
                                                    f"⚠️ Buy trade not found, using STEP-based estimation: ${profit:.2f} "
                                                    f"(STEP profit ${step_profit:.2f} - buy fee ${buy_commission:.2f} - sell fee ${sell_commission:.2f})",
                                                    level="WARNING"
                                                )
                                            
                                            # Обновляем предыдущий баланс для следующей итерации
                                            client._previous_usdc_balance = usdc_balance
                                            
                                            # Записываем статистику операции Take Profit
                                            # (Profit — то же значение, что уходит в Telegram)
                                            try:
                                                await log_statistics_to_csv(
                                                    client=client,
                                                    operation="Take Profit",
                                                    btc_amount=filled_qty,
                                                    price=tp_price,
                                                    usdc_balance=usdc_balance,
                                                    btc_balance=btc_balance,
                                                    total_value=total_usd_value,
                                                    orders=open_orders,
                                                    sz_decimals=sz_decimals,
                                                    token_name=token_name,
                                                    profit=profit
                                                )
                                            except Exception as e:
                                                client.browser.log_message(f"Failed to log Take Profit statistics: {e}", level="ERROR")
                                            
                                            # Формируем сообщение согласно требованиям
                                            # Используем фактический размер исполнения из Trade History
                                            client.browser.log_message(
                                                f"✅ Profit ${profit:.2f}, Eq = {usdc_balance:.2f}USDC + {btc_balance:.6f}{token_name} = ${total_usd_value:.2f}",
                                                level="INFO"
                                            )
                                            
                                            # Отправляем уведомление в Telegram согласно требованиям
                                            profit_message = f'🎯 Hype: PROFIT ${profit:.2f} | {usdc_balance:.2f}USDC + {btc_balance:.6f}{token_name} = ${total_usd_value:.2f}'
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

                # Чистим исполненные: иначе словарь растёт весь прогон
                for executed_id in executed_tp_ids:
                    client._previous_tp_orders.pop(executed_id, None)
                
                # Логируем сырой ответ API (отключено для чистоты логов)
                # client.browser.log_message(f"🔍 RAW API Response: {open_orders}", level="DEBUG")
                
        
                
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
                        elif coin == token_name:
                            try:
                                # Используем уже полученную цену current_price для расчета стоимости токена
                                usd_value = balance * current_price
                                total_usd_value += usd_value
                                token_balances.append(f"{balance:.6f}{token_name}")
                            except Exception as e:
                                token_balances.append(f"{balance:.6f}{token_name}")
                    
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
                    # Фикс 3: не стреляем второй маркет, пока есть покупки без TP
                    # (TP не выставился/отклонила биржа). Ждем и даем SET_TP
                    # ретраить, иначе получаем дублирующиеся входы по одной цене.
                    pending_tp = getattr(client, '_pending_tp_oids', set()) or set()
                    retry_tp = getattr(client, '_retry_tp_buys', {}) or {}
                    if pending_tp or retry_tp:
                        client.browser.log_message(
                            f"⏳ TP pending for {len(pending_tp | set(retry_tp))} buy(s) — "
                            f"waiting for TP retry, skipping First Position",
                            level="INFO"
                        )
                        continue

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

                    # Не открываем новые позиции во время остановки
                    if get_bot_stop_flag():
                        client.browser.log_message(
                            "🛑 [STOP_FLAG] Bot stop flag detected, skipping First Position order",
                            level="WARNING"
                        )
                        continue

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
                        # Фиксируем якорь hold_benchmark ОДИН РАЗ: депо_старт = total
                        # после первой покупки, цена_старт = фактическая цена исполнения.
                        # Если якорь уже есть (память/диск) — не перетираем.
                        ensure_hold_anchor(
                            client,
                            depo_start=total_value,
                            price_start=actual_price,
                        )
                        await log_statistics_to_csv(
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
                        # Правило refill: тот же уровень — только после подтвержденного TP
                        # предыдущего fill-а (иначе двойные заливки как 84447 + 84439).
                        if not _buylimit_level_allowed(client, target_buy_price, current_tp_orders):
                            continue
                        # Проверка баланса
                        if usdc_balance < usdc_amount:
                            if can_log_warning_message(client.label, "insufficient_usdc_averaging", cooldown_minutes=10):
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
                                # Не открываем новые позиции во время остановки
                                if get_bot_stop_flag():
                                    client.browser.log_message(
                                        "🛑 [STOP_FLAG] Bot stop flag detected, skipping BuyLimit order",
                                        level="WARNING"
                                    )
                                    continue
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
                                        response_data = dict(order_result.get('response', {}))
                                        # Получаем фактическую цену исполнения из API ответа (если ордер был исполнен сразу)
                                        actual_price = normalize_price(target_buy_price, sz_decimals)  # fallback
                                        token_amount = usdc_amount / target_buy_price
                                        is_filled = False
                                        try:
                                            statuses = response_data.get('data', {}).get('statuses', [])
                                            if statuses and len(statuses) > 0:
                                                first_status = statuses[0]
                                                if 'filled' in first_status:
                                                    filled_data = first_status['filled']
                                                    actual_price = normalize_price(float(filled_data.get('avgPx', target_buy_price)), sz_decimals)
                                                    # Получаем фактическое количество из ответа, если доступно
                                                    filled_sz = filled_data.get('totalSz', None)
                                                    if filled_sz:
                                                        token_amount = float(filled_sz)
                                                    is_filled = True
                                                    client.browser.log_message(
                                                        f"✅ BuyLimit: Order filled immediately at ${actual_price:.2f} (limit price ${target_buy_price:.2f})", 
                                                        level="INFO"
                                                    )
                                        except Exception as e:
                                            client.browser.log_message(f"Failed to extract BuyLimit fill price from API: {e}", level="ERROR")
                                        
                                        # Трекинг для правила refill: запоминаем выставленный
                                        # BuyLimit (oid -> уровень) либо сразу ставим wait,
                                        # если ордер исполнился мгновенно.
                                        try:
                                            if not hasattr(client, '_buylimit_open'):
                                                client._buylimit_open = {}
                                            if is_filled:
                                                client._buylimit_wait_tp = {
                                                    "level": float(target_buy_price),
                                                    "expect_tp_px": float(actual_price) + float(step),
                                                    "since": datetime.now().timestamp(),
                                                }
                                            else:
                                                resting_oid = (statuses[0].get("resting", {}) or {}).get("oid")
                                                if resting_oid is not None:
                                                    client._buylimit_open[resting_oid] = {
                                                        "level": float(target_buy_price),
                                                    }
                                        except Exception:
                                            pass

                                        # Обновляем баланс после исполнения ордера (если был исполнен)
                                        if is_filled:
                                            all_balances = await client.browser.get_all_balances()
                                            usdc_balance = all_balances.get("USDC", 0.0)
                                        
                                        # Логируем с фактической или расчетной ценой
                                        if is_filled:
                                            client.browser.log_message(
                                                f"📉 BuyLimit filled - ${usdc_amount:.2f} at ${actual_price:.2f}",
                                                level="INFO"
                                            )
                                        else:
                                            client.browser.log_message(
                                                f"📉 set BuyLimit ${usdc_amount:.2f} at ${target_buy_price:.2f}",
                                                level="INFO"
                                            )
                                        
                                        # Отправляем уведомление в Telegram
                                        if is_filled:
                                            await send_tg_notification(
                                                client,
                                                f"📉 <b>BuyLimit filled</b>\n${usdc_amount:.2f} at ${actual_price:.2f}",
                                                save_to_report=False
                                            )
                                        else:
                                            await send_tg_notification(
                                                client,
                                                f"📉 <b>set BuyLimit</b>\n${usdc_amount:.2f} at ${target_buy_price:.2f}",
                                                save_to_report=False
                                            )
                                        
                                        # Записываем статистику операции с фактической ценой исполнения (если ордер был исполнен)
                                        try:
                                            btc_balance = all_balances.get(token_name, 0.0) or all_balances.get("UBTC", 0.0)
                                            total_value = usdc_balance + (btc_balance * current_price)
                                            # Для лимитного ордера используем целевую цену и расчетное количество
                                            token_amount = usdc_amount / target_buy_price
                                            await log_statistics_to_csv(
                                                client=client,
                                                operation="set BuyLimit",
                                                btc_amount=token_amount,
                                                price=actual_price,  # Используем фактическую цену исполнения, если ордер был исполнен
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
                                        # continue
                                        await async_sleep(5)
                                        
                                except Exception as e:
                                    client.browser.log_message(f"❌ Failed to create Buy Limit order: {e}", level="ERROR")
                    else:
                        # Есть активные Buy Limit ордера - проверяем их актуальность
                        threshold = min_tp_price - float(step) * 2.5
                        
                        for order in active_buy_limits:
                            order_price = float(order.get("limitPx", 0))
                            order_id = order.get("oid")
                            
                            if order_price < threshold:
                                # Ордер слишком далеко - отменяем его
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

                        # Не открываем новые позиции во время остановки
                        if get_bot_stop_flag():
                            client.browser.log_message(
                                "🛑 [STOP_FLAG] Bot stop flag detected, skipping Pyramiding order",
                                level="WARNING"
                            )
                            continue

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
                            await log_statistics_to_csv(
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
                client.browser.log_message(f"⚠️ GLOBAL EXCEPTION: {e}", level="ERROR")
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
        await _stop_watchdog(watchdog_task)
        return False
    
    await _stop_watchdog(watchdog_task)
    # Функция должна работать бесконечно, но если по какой-то причине завершилась
    return True
