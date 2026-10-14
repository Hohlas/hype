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


async def run_trade_iteration(state: dict) -> int:
    """Одна торговая итерация по всем кошелькам. Возвращает число успешных.

    Ошибка одного кошелька не отменяет остальные; падают все — исключение
    наружу (ведущим запуск выглядеть не должен).
    """
    db = _get_db()
    modules = get_wallet_modules(db=db)
    if not modules or modules == "No more accounts left":
        raise RuntimeError("No wallets available for trade iteration")

    ok = 0
    for module_data in modules:
        label = module_data.get("label", "Unknown")
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

    state["last_run_utc"] = _utc_stamp()
    if ok == 0:
        raise RuntimeError("Trade iteration failed for all wallets")
    return ok
