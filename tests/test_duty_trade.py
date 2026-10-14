"""Тесты торговой сшивки дежурства (лестница §10, ступень 3).

Проверяем: (1) выключатель ``settings.DUTY_MODE`` fail-safe; (2) диспетчер
``duty.run_trading_iteration`` выбирает разбор или торговлю; (3) стратегия
в режиме одного прохода возвращается и НЕ ставит ордеров без позиции.

Сеть не трогаем: клиент/браузер — заглушки.
"""

import asyncio

import settings

from modules.utils import modes


# --- Выключатель режима: безопасный дефолт ---

def test_duty_mode_default_is_parse(monkeypatch):
    monkeypatch.setattr(settings, "DUTY_MODE", "parse", raising=False)
    assert modes.get_duty_mode() == "parse"


def test_duty_mode_averaging(monkeypatch):
    monkeypatch.setattr(settings, "DUTY_MODE", "averaging", raising=False)
    assert modes.get_duty_mode() == "averaging"


def test_duty_mode_unknown_fails_safe_to_parse(monkeypatch):
    # Опечатка не должна включать торговлю.
    monkeypatch.setattr(settings, "DUTY_MODE", "trading", raising=False)
    assert modes.get_duty_mode() == "parse"


def test_duty_mode_missing_attr_defaults_parse(monkeypatch):
    monkeypatch.delattr(settings, "DUTY_MODE", raising=False)
    assert modes.get_duty_mode() == "parse"


def test_duty_mode_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "DUTY_MODE", "  AVERAGING  ", raising=False)
    assert modes.get_duty_mode() == "averaging"


# --- Диспетчер итерации: parse vs averaging ---

def test_iteration_dispatches_parse(monkeypatch):
    from modules import duty

    called = {}

    async def fake_parse(state):
        called["parse"] = True

    monkeypatch.setattr("modules.utils.modes.get_duty_mode", lambda: "parse")
    monkeypatch.setattr("modules.duty_parse.run_parse_iteration", fake_parse)
    asyncio.run(duty.run_trading_iteration({}))
    assert called.get("parse") is True


def test_iteration_dispatches_averaging(monkeypatch):
    from modules import duty

    called = {}

    async def fake_trade(state):
        called["trade"] = True

    monkeypatch.setattr("modules.utils.modes.get_duty_mode", lambda: "averaging")
    monkeypatch.setattr("modules.duty_trade.run_trade_iteration", fake_trade)
    asyncio.run(duty.run_trading_iteration({}))
    assert called.get("trade") is True


# --- Один проход стратегии: возвращается, ордеров не ставит ---

class FakeBrowser:
    def __init__(self):
        self.logs = []

    def log_message(self, msg, level="INFO"):
        self.logs.append((level, msg))

    async def get_token_price(self, token):
        return 100000.0

    async def get_account_orders(self):
        return []

    async def get_all_orders_history(self, token, limit=100):
        return []

    async def get_all_balances(self):
        return {"USDC": 0.0, "UBTC": 0.0}

    async def get_available_token_balance(self, token):
        return 0.0


class FakeClient:
    def __init__(self):
        self.browser = FakeBrowser()
        self.label = "Acc Test"
        self.wallet_address = "0x0"
        self.db = None

    async def load_token_limits(self, token):
        return (0.0001, 10.0, 5)

    async def calculate_usdc_amount_for_order(self, token):
        return (100.0, 1)

    async def place_order(self, **kw):
        raise AssertionError(f"place_order must not be called: {kw}")


def test_strategy_single_pass_returns_without_orders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WATCH_SECONDS", "0")
    monkeypatch.setenv("BOT_POLL_INTERVAL", "60")

    from modules.delayed_orders_strategy import trade_delayed_orders_strategy

    client = FakeClient()
    result = asyncio.run(trade_delayed_orders_strategy(
        client, "BTC", max_iterations=1, poll_interval_override=0.0,
    ))

    assert result is True
    assert any("Single-pass mode" in msg for _, msg in client.browser.logs)
    assert not any("place_order" in msg for _, msg in client.browser.logs)
