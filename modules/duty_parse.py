"""
Одна итерация разбора для дежурного слоя (лестница §10, шаги 1–2).

Строго чтение: балансы + открытые ордера + цена BTC по каждому кошельку.
Постановок ордеров нет — сверка перед каждой постановкой (§5) вступит позже,
на этапе торговой сшивки. Ошибка одного кошелька не отменяет остальные;
падают все — исключение наружу (запуск ведущим выглядеть не должен).
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger

from modules.hyperliquid_browser import HyperliquidBrowser
from modules.wallet_loader import get_wallet_modules

TOKEN_NAME = "BTC"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _parse_one_wallet(module_data: dict) -> str:
    browser = HyperliquidBrowser(
        proxy=module_data.get("proxy"),
        wallet_address=module_data["wallet_address"],
        private_key=module_data["private_key"],
        label=module_data.get("label", "Unknown"),
        db=None,
    )
    try:
        await browser.start_session()
        balances, orders, price = await asyncio.gather(
            browser.get_all_balances(),
            browser.get_account_orders(),
            browser.get_token_price(TOKEN_NAME),
        )
        usdc = float(balances.get("USDC", 0.0) or 0.0)
        btc = float(balances.get("UBTC", 0.0) or balances.get("BTC", 0.0) or 0.0)
        total = usdc + btc * float(price)
        line = (
            f"{module_data.get('label')}: {usdc:.0f}USDC + {btc:.5f}BTC = "
            f"${total:.0f} @ ${float(price):.0f}, orders {len(orders)}"
        )
        logger.info(line)
        return line
    finally:
        try:
            await browser.close_sessions()
        except Exception:
            pass


async def run_parse_iteration(state: dict) -> int:
    """Одна итерация разбора по всем кошелькам. Возвращает число успешных.

    В CI кошельки приходят из WALLETS_DATA (база не нужна); локально/VPS —
    из зашифрованной базы, для чего строится DataBase с паролем из окружения
    или интерактивом.
    """
    from modules.database import DataBase

    modules = get_wallet_modules(db=DataBase())
    if not modules or modules == "No more accounts left":
        raise RuntimeError("No wallets available for parse iteration")

    ok = 0
    for module_data in modules:
        label = module_data.get("label", "Unknown")
        try:
            await _parse_one_wallet(module_data)
            state.setdefault("marks", {})[f"parse:{label}"] = _utc_stamp()
            ok += 1
        except Exception as e:
            logger.error(f"Parse failed for {label}: {e}")

    state["last_run_utc"] = _utc_stamp()
    if ok == 0:
        raise RuntimeError("Parse iteration failed for all wallets")
    return ok
