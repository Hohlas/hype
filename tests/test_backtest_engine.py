"""Тесты движка бэктеста и инвариантов стратегий.

Запуск: ./venv/bin/python -m pytest tests/test_backtest_engine.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "backtest"))

from engine import Bar, Broker, buy_and_hold, max_drawdown  # noqa: E402
from strategies import GridRB, TrendAdaptiveGrid, TwoModes7030  # noqa: E402

DATA = os.path.join(HERE, "backtest", "data", "btc_h1_2y.csv")


def test_market_buy_never_overspends():
    b = Broker(10_000)
    b.market_buy(10_000, 100.0, 0)
    assert b.U >= 0
    assert b.B > 0
    # с учётом taker+slip капитал чуть ниже стартового
    assert 9_990 < b.equity(100.0) <= 10_000


def test_limit_gap_fill_uses_open():
    b = Broker(10_000)
    od = b.place_limit_buy(100.0, 1_000.0, "t", 1)
    assert od is not None
    fills = []
    # гэп вниз: open=90 < уровня 100 -> исполнение по 90 (дешевле)
    b.execute_bar(1, 90.0, 95.0, 85.0, 92.0,
                  lambda i, o, px, q: fills.append(px))
    assert fills == [90.0]


def test_limit_sell_gap_fill_uses_open():
    b = Broker(0.0)
    b.B = 1.0
    od = b.place_limit_sell(100.0, 0.5, "t", 1)
    assert od is not None
    fills = []
    b.execute_bar(1, 110.0, 115.0, 105.0, 112.0,
                  lambda i, o, px, q: fills.append(px))
    assert fills == [110.0]
    assert b.B == pytest.approx(0.5)


def test_orders_are_not_active_on_placement_bar():
    b = Broker(10_000)
    b.place_limit_buy(100.0, 1_000.0, "t", 5)  # активен с бара 5
    fills = []
    cb = lambda i, o, px, q: fills.append(px)
    b.execute_bar(4, 100.0, 100.0, 50.0, 100.0, cb)  # бар выставления
    assert fills == []
    b.execute_bar(5, 100.0, 100.0, 50.0, 100.0, cb)  # следующий бар
    assert fills == [100.0]


def test_max_drawdown():
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(-0.25)


@pytest.mark.parametrize("cls", [GridRB, TwoModes7030, TrendAdaptiveGrid])
def test_strategy_accounting_stays_positive(cls):
    """Ни касса, ни BTC не уходят в минус на реальной истории."""
    if not os.path.exists(DATA):
        pytest.skip("нет данных")
    from engine import load_bars
    bars = load_bars(DATA)[:3000]
    s = cls(10_000)
    for bar in bars:
        s.b.execute_bar(bar.i, bar.o, bar.h, bar.l, bar.c, s.on_fill)
        s.on_close(bar.i, bar)
        assert s.b.U >= -1e-6, (cls.__name__, bar.i, s.b.U)
        assert s.b.B >= -1e-12, (cls.__name__, bar.i, s.b.B)
        assert s.b.equity(bar.c) > 0


def test_buy_and_hold_tracks_price():
    bars = [Bar(0, 0, 100, 100, 100, 100),
            Bar(1, 1, 110, 110, 110, 110),
            Bar(2, 2, 120, 120, 120, 120)]
    r = buy_and_hold(bars, 10_000)
    expected = 10_000 * 1.2 / ((1 + 0.0007) * (1 + 0.0002))
    assert r.final == pytest.approx(expected, rel=1e-9)