"""Тесты live-движка GridRB (без биржи).

Запуск: ./venv/bin/python -m pytest tests/test_gridrb_live.py -q
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from modules.gridrb_strategy import GridRBConfig, GridRBEngine  # noqa: E402


def make_engine(**kw):
    cfg = GridRBConfig(**kw)
    return GridRBEngine(cfg, sz_decimals=5)


def test_bootstrap_buys_to_f0_once():
    e = make_engine()
    acts = e.plan(price=100_000, usdc=10_000, btc=0.0, open_oids=set())
    assert len(acts) == 1
    assert acts[0].kind == "market_buy"
    assert acts[0].usd == 8_000

    acts2 = e.plan(price=100_000, usdc=2_000, btc=0.08, open_oids=set())
    assert not any(a.kind == "market_buy" for a in acts2)


def test_no_bootstrap_when_btc_exists():
    e = make_engine()
    acts = e.plan(price=100_000, usdc=1_000, btc=0.01, open_oids=set())
    assert not any(a.kind == "market_buy" for a in acts)


def test_trim_ladder_when_overweight():
    e = make_engine()
    e.first = False
    # w = 0.905 -> выше f0+band=0.84
    acts = e.plan(price=100_000, usdc=100, btc=0.0095, open_oids=set())
    sells = [a for a in acts if a.kind == "place" and a.side == "SELL"]
    assert len(sells) == 5
    assert all(a.tag == "trim" for a in sells)
    prices = sorted(a.price for a in sells)
    assert prices[0] == 100_400.0  # 100000 * (1 + 0.004)
    assert prices[-1] == 102_000.0  # 100000 * (1 + 5*0.004)


def test_defc_ladder_when_underweight():
    e = make_engine()
    e.first = False
    acts = e.plan(price=100_000, usdc=1_000, btc=0.001, open_oids=set())
    buys = [a for a in acts if a.kind == "place" and a.side == "BUY" and a.tag == "defc"]
    assert len(buys) == 5
    prices = sorted(a.price for a in buys)
    assert prices[0] == 98_000.0   # 100000 * (1 - 5*0.004)
    assert prices[-1] == 99_600.0  # 100000 * (1 - 0.004)


def test_micro_one_per_step_and_max():
    e = make_engine()
    e.first = False
    # w = 0.80 — внутри полосы
    acts = e.plan(price=100_000, usdc=1_000, btc=0.04, open_oids=set())
    micro = [a for a in acts if a.tag == "micro"]
    assert len(micro) == 1
    assert micro[0].price == 99_600.0

    # зарегистрировали 3 микро-ордера -> новый не ставится
    for a in micro:
        e.register(a, 1)
    for oid in (2, 3):
        e.micro_buys.append(type(e.micro_buys[0])(oid, "buy", 99_500.0, 0.001, "micro"))
    acts2 = e.plan(price=100_000, usdc=1_000, btc=0.04,
                   open_oids={1, 2, 3})
    assert not any(a.tag == "micro" for a in acts2)


def test_micro_fill_creates_take_profit():
    e = make_engine()
    e.first = False
    from modules.gridrb_strategy import TrackedOrder
    e.micro_buys.append(TrackedOrder(7, "buy", 99_600.0, 0.001, "micro"))
    acts = e.plan(price=100_000, usdc=1_000, btc=0.04, open_oids=set())
    fills = [a for a in acts if a.kind == "fill" and a.tag == "micro"]
    assert len(fills) == 1  # исполнение микро-покупки зафиксировано
    takes = [a for a in acts if a.tag == "micro_take"]
    assert len(takes) == 1
    assert takes[0].side == "SELL"
    assert takes[0].price == 99_998.0  # 99600 * 1.004 -> 5 значащих цифр
    assert len(e.micro_sells) == 1  # тейк поставлен на учёт

    # после регистрации oid тейк не должен сниматься как сирота
    e.register(takes[0], 77)
    acts2 = e.plan(price=100_000, usdc=1_000, btc=0.04, open_oids={77})
    assert not any(a.kind == "cancel" and a.oid == 77 for a in acts2)

    # тейк исполнился -> фиксируется fill с прибылью
    acts3 = e.plan(price=101_000, usdc=1_000, btc=0.04, open_oids=set())
    tp_fills = [a for a in acts3 if a.kind == "fill" and a.tag == "micro_take"]
    assert len(tp_fills) == 1
    assert tp_fills[0].profit > 0


def test_orphan_order_is_cancelled():
    e = make_engine()
    e.first = False
    acts = e.plan(price=100_000, usdc=1_000, btc=0.04, open_oids={42})
    assert any(a.kind == "cancel" and a.oid == 42 for a in acts)


def test_frozen_trim_ladder_is_not_replaced():
    e = make_engine()
    e.first = False
    acts = e.plan(price=100_000, usdc=100, btc=0.0095, open_oids=set())
    for i, a in enumerate(acts):
        e.register(a, 100 + i)
    oids = {100 + i for i in range(len(acts))}
    # цена не обновила якорь -> лестница стоит
    acts2 = e.plan(price=99_900, usdc=100, btc=0.0095, open_oids=oids)
    assert not any(a.kind == "place" and a.tag == "trim" for a in acts2)
    assert not any(a.kind == "cancel" for a in acts2)


def test_no_rearm_on_tiny_move_and_cooldown():
    e = make_engine()
    e.first = False
    acts = e.plan(price=100_000, usdc=1_000, btc=0.001, open_oids=set())
    defc = [a for a in acts if a.kind == "place" and a.tag == "defc"]
    assert len(defc) == 5
    for i, a in enumerate(defc):
        e.register(a, 200 + i)
    oids = {200 + i for i in range(len(defc))}

    # +0.1% выше якоря — порог 0.2% не пройден, перестановки нет
    acts2 = e.plan(price=100_100, usdc=1_000, btc=0.001, open_oids=oids)
    assert not any(a.kind == "cancel" for a in acts2)
    assert not any(a.kind == "place" and a.tag == "defc" for a in acts2)

    # +0.3% выше, но cooldown ещё не прошёл
    acts3 = e.plan(price=100_300, usdc=1_000, btc=0.001, open_oids=oids)
    assert not any(a.kind == "cancel" for a in acts3)

    # cooldown истёк — лестница переставляется
    e.last_defc_rearm -= 1_000
    acts4 = e.plan(price=100_300, usdc=1_000, btc=0.001, open_oids=oids)
    assert any(a.kind == "cancel" for a in acts4)
    assert any(a.kind == "place" and a.tag == "defc" for a in acts4)


def test_tiny_budget_produces_no_orders():
    e = make_engine(min_notional=10.0)
    e.first = False
    # капитал $50, недобор до f0 даёт меньше $10 на уровень
    acts = e.plan(price=100_000, usdc=50, btc=0.000001, open_oids=set())
    assert not any(a.kind == "place" and a.tag == "defc" for a in acts)


def test_main_has_no_old_runtime():
    path = os.path.join(os.path.dirname(HERE), "main.py")
    text = open(path, encoding="utf-8").read()
    assert "run_gridrb" in text
    assert "delayed_orders_strategy" not in text
    assert "choose_mode" not in text
    assert "modules.duty" not in text


def test_event_labels_split_rebalance_and_grid():
    from modules.gridrb_strategy import event_label
    reb_buy = event_label({"event": "place", "tag": "defc", "side": "BUY",
                           "price": 100.0, "qty": 0.1})
    reb_sell = event_label({"event": "fill", "tag": "trim", "side": "SELL",
                            "price": 100.0, "qty": 0.1})
    grid_buy = event_label({"event": "place", "tag": "micro", "side": "BUY",
                            "price": 100.0, "qty": 0.1})
    grid_tp = event_label({"event": "fill", "tag": "micro_take", "side": "SELL",
                           "price": 100.0, "qty": 0.1})
    assert "Ребаланс" in reb_buy and "Ребаланс" in reb_sell
    assert "Сетка" in grid_buy and "Сетка" in grid_tp


def test_new_csv_logs_only_own_files(tmp_path):
    from modules.gridrb_strategy import log_events, log_state, event_label

    class C:
        label = "acc"

    events_csv = tmp_path / "gridrb_events.csv"
    state_csv = tmp_path / "gridrb_state.csv"
    ev = {"event": "place", "tag": "defc", "side": "BUY", "price": 100.0,
          "qty": 0.1, "notional": 10.0, "oid": 1, "note": ""}
    log_events(C(), str(events_csv), [ev], 100.0, 1000.0, 0.01)
    log_events(C(), str(events_csv), [{"event": "fill", "tag": "micro",
                                       "side": "BUY", "price": 99.0, "qty": 0.1,
                                       "notional": 9.9, "oid": None, "note": ""}],
               100.0, 1000.0, 0.01)
    lines = events_csv.read_text().strip().splitlines()
    assert lines[0].startswith("Timestamp,")
    assert len(lines) == 3  # заголовок + два события
    assert "defc" in lines[1] and "micro" in lines[2]

    e = make_engine()
    e.first = False
    log_state(C(), str(state_csv), 100.0, 1000.0, 0.01, e)
    assert state_csv.read_text().splitlines()[0].startswith("Timestamp,")
    assert event_label(ev).startswith("📥 Ребаланс")