"""
Торговая итерация дежурства (лестница §10, ступень 3).

Одна итерация §5 на кошелёк: разбор → мягкий сброс → сверка усреднения.
Не копия стратегии: вызывается ТОТ ЖЕ боевой код
(``trade_delayed_orders_strategy``) в режиме одного прохода
(``max_iterations=1``). Это исключает расхождение двух реализаций.

Источник истины — биржа (§4): клиент переживает тики внутри одной вахты
(кэш ниже), а при ротации поднимается заново и состояние восстанавливается
разбором (история сделок + скан сирот). Потеря памяти между процессами
допустима как деградация (§4).

Единственное исключение — якорь hold_benchmark: он НЕ берётся с биржи (снимок
депо и цены в момент первой позиции), поэтому ходит через ветку ``state``
(``_restore_anchors``/``_capture_anchors``).

Модуль импортируется лениво и только когда ``DUTY_MODE="averaging"``
(``duty.run_trading_iteration``): по умолчанию дежурство остаётся на разборе
и сюда не заходит.
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger

from modules.delayed_orders_strategy import trade_delayed_orders_strategy
from modules.hyperliquid_browser import HyperliquidBrowser
from modules.hyperliquid_client import HyperliquidClient
from modules.wallet_loader import get_wallet_modules

TOKEN_NAME = "BTC"

# Пауза перед единственным проходом внутри стратегии. Собственный цикл
# дежурства уже спит (BOT_POLL_INTERVAL), двойной длинный сон не нужен;
# небольшая пауза даёт ордерам появиться в API перед следующим тиком.
SINGLE_PASS_POLL_SEC = 1.0

# Клиенты и база переживают тики внутри одной вахты: так сохраняется
# in-memory состояние стратегии (_buylimit_wait_tp и прочее), которое из
# биржи не восстанавливается. Сервер сам сбрасывает его при ротации (§4).
_clients = {}
_db = None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_db():
    """Одна DataBase на процесс дежурства (в CI — пароль из окружения)."""
    global _db
    if _db is None:
        from modules.database import DataBase

        _db = DataBase()
    return _db


async def _get_client(module_data: dict, db):
    """Клиент на кошелёк из кэша процесса; создаётся и поднимает сессию лениво."""
    key = module_data["wallet_address"]
    client = _clients.get(key)
    if client is not None:
        return client
    browser = HyperliquidBrowser(
        proxy=module_data.get("proxy"),
        wallet_address=module_data["wallet_address"],
        private_key=module_data["private_key"],
        label=module_data.get("label", "Unknown"),
        db=db,
    )
    client = HyperliquidClient(
        browser=browser,
        wallet_address=module_data["wallet_address"],
        private_key=module_data["private_key"],
        label=module_data.get("label", "Unknown"),
        proxy=module_data.get("proxy"),
        db=db,
    )
    await browser.start_session()
    _clients[key] = client
    return client


def _drop_client(module_data: dict) -> None:
    _clients.pop(module_data.get("wallet_address"), None)


async def close_trade_clients() -> None:
    """Закрыть все сессии и очистить кэш (зовётся на выходе вахты)."""
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        try:
            await client.browser.close_sessions()
        except Exception:
            pass


def _restore_anchors(state: dict) -> int:
    """Осесть якоря hold_benchmark из state в эфемерный ``databases/stats.json``.

    Якорь не восстанавливается с биржи (§4), поэтому ветка ``state`` — его
    единственная память между вахтами. Кладём его в файл ДО стратегии, чтобы
    ``ensure_hold_anchor``/``load_hold_anchor`` нашли его при старте.
    """
    anchors = state.get("hold_anchor")
    if not isinstance(anchors, dict):
        return 0
    from modules.delayed_orders_strategy import load_hold_anchor, save_hold_anchor

    restored = 0
    for label, entry in anchors.items():
        if not isinstance(entry, dict) or load_hold_anchor(label) is not None:
            continue
        depo, price = entry.get("depo_start"), entry.get("price_start")
        if not depo or not price:
            continue
        save_hold_anchor(label, depo, price, timestamp=entry.get("timestamp"))
        restored += 1
    return restored


def _capture_anchors(state: dict, labels) -> int:
    """Снять зафиксированные якоря из файла в state (для ветки §4).

    Стратегия пишет якорь один раз при первой позиции; если он появился в этой
    вахте — вернём его в state, чтобы пережил смерть процесса. Повторно
    существующий якорь не трогаем (write-once, §1.3).
    """
    from modules.delayed_orders_strategy import load_hold_anchor

    captured = 0
    for label in labels:
        if not label:
            continue
        entry = load_hold_anchor(label)
        if not isinstance(entry, dict):
            continue
        depo, price = entry.get("depo_start"), entry.get("price_start")
        if not depo or not price:
            continue
        anchors = state.setdefault("hold_anchor", {})
        if label in anchors:
            continue
        anchors[label] = {
            "depo_start": depo,
            "price_start": price,
            "timestamp": entry.get("timestamp", ""),
        }
        captured += 1
    return captured


def _restore_buy_ts(state: dict, db) -> int:
    """Осесть карту ``last_buy_timestamps`` из state в эфемерную базу (§4).

    Биржа отдаёт лишь последние ~500 сделок, поэтому персистентная карта
    (окно для скана сирот) в CI теряется вместе с ``databases/``. Восстанавливаем
    её до стратегии, чтобы стартовый скан сирот видел более глубокую историю.
    """
    ts_map = state.get("last_buy_ts")
    if not isinstance(ts_map, dict) or not ts_map:
        return 0
    return db.merge_last_buy_timestamps(ts_map)


def _capture_buy_ts(state: dict, db) -> int:
    """Снять карту ``last_buy_timestamps`` из базы в state (для ветки §4)."""
    from modules.duty import merge_buy_ts

    remote = db.get_all_last_buy_timestamps()
    if not remote:
        return 0
    state["last_buy_ts"] = merge_buy_ts(remote, state.get("last_buy_ts"))
    return len(remote)


async def run_trade_iteration(state: dict) -> int:
    """Одна торговая итерация по всем кошелькам. Возвращает число успешных.

    Ошибка одного кошелька не отменяет остальные; падают все — исключение
    наружу (ведущим запуск выглядеть не должен).
    """
    db = _get_db()
    modules = get_wallet_modules(db=db)
    if not modules or modules == "No more accounts left":
        raise RuntimeError("No wallets available for trade iteration")

    restored = _restore_anchors(state)
    if restored:
        logger.info(f"Restored {restored} hold anchor(s) from state branch")
    restored_ts = _restore_buy_ts(state, db)
    if restored_ts:
        logger.info(f"Restored buy timestamps for {restored_ts} key(s) from state branch")

    ok = 0
    labels = []
    for module_data in modules:
        label = module_data.get("label", "Unknown")
        labels.append(label)
        try:
            client = await _get_client(module_data, db)
            await trade_delayed_orders_strategy(
                client, TOKEN_NAME,
                max_iterations=1,
                poll_interval_override=SINGLE_PASS_POLL_SEC,
            )
            state.setdefault("marks", {})[f"trade:{label}"] = _utc_stamp()
            ok += 1
        except Exception as e:
            # Сброшенный клиент пересоберётся на следующем тике (свежий разбор).
            _drop_client(module_data)
            logger.error(f"Trade iteration failed for {label}: {e}")

    captured = _capture_anchors(state, labels)
    if captured:
        logger.info(f"Captured {captured} hold anchor(s) into state")
    captured_ts = _capture_buy_ts(state, db)
    if captured_ts:
        logger.info(f"Captured buy timestamps for {captured_ts} key(s) into state")

    state["last_run_utc"] = _utc_stamp()
    if ok == 0:
        raise RuntimeError("Trade iteration failed for all wallets")
    return ok
