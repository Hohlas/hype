#!/usr/bin/env python3
"""Прогон БОЕВОГО движка GridRB по барам (H1 или 1m).

Зачем: в tests/backtest/strategies.py лежит ПОРТ боевой логики, и он успел
разойтись с оригиналом (гистерезис перестановки, ранняя докупка, заморозка
объёмов лестницы). Здесь вызывается ровно тот же класс
modules/gridrb_strategy.GridRBEngine, что работает в бою, и та же конфигурация
(GridRBConfig.from_settings()). Отличается только источник баров и модель
исполнения (tests/backtest/engine.py).

Модель исполнения (как в engine.py): лимитный buy исполняется на баре i,
активен с i+1, если low <= цена, по цене min(open, цена); sell — если
high >= цена, по max(open, цена); комиссия maker 0.04 %; нотионал < $10 не
исполняется. Часы бота сымитированы: один бар = 60 с, поэтому
GRIDRB_REARM_COOLDOWN=300 превращается в 5 баров.

Запуск:
    ./venv/bin/python tests/backtest/minute_replay.py tests/backtest/data/btc_h1_3y.csv
    ./venv/bin/python tests/backtest/minute_replay.py tests/backtest/data/btc_1m_3y.csv
"""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))            # tests/backtest
ROOT = os.path.dirname(os.path.dirname(HERE))                # корень репозитория
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from loguru import logger  # noqa: E402

logger.remove()

import engine as bt  # noqa: E402
import modules.gridrb_strategy as gs  # noqa: E402
from modules.gridrb_strategy import GridRBConfig, GridRBEngine  # noqa: E402

RESULTS = os.path.join(HERE, "results")


def _noop(*_a) -> None:
    return None


def _epoch(s: str) -> float:
    """'YYYY-MM-DD HH:MM:SS' (UTC) -> секунды эпохи. Быстрее strptime."""
    import datetime as dt
    return dt.datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]),
                       int(s[11:13]), int(s[14:16]), int(s[17:19]),
                       tzinfo=dt.timezone.utc).timestamp()


class _Clock:
    """Подмена модуля time внутри gridrb_strategy: время = номер бара * 60 с."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class ReplayBroker(bt.Broker):
    """Broker с постановкой по точному qty и с синтетическими oid."""

    def __init__(self, cash: float) -> None:
        super().__init__(cash)
        self._next_oid = 0
        self._by_oid: dict[int, bt.Order] = {}
        self.placed = 0
        self.cancelled = 0

    def place(self, side: str, price: float, qty: float, tag: str,
              active_from: int) -> int | None:
        if qty <= 0 or price <= 0 or qty * price < bt.MIN_NOTIONAL:
            return None
        self._next_oid += 1
        o = bt.Order(side, price, qty, tag, active_from)
        o.oid = self._next_oid  # type: ignore[attr-defined]
        self.orders.append(o)
        self._by_oid[o.oid] = o  # type: ignore[attr-defined]
        self.placed += 1
        return o.oid  # type: ignore[attr-defined]

    def open_oids(self) -> set[int]:
        return {o.oid for o in self.orders if o.alive}  # type: ignore[attr-defined]

    def cancel_oid(self, oid: int) -> None:
        o = self._by_oid.get(oid)
        if o is not None and o.alive:
            o.alive = False
            self.cancelled += 1


def replay(path: str, cfg: GridRBConfig, cash: float = 10_000.0,
           sz_decimals: int = 5, progress: int = 0):
    eng = GridRBEngine(cfg, sz_decimals=sz_decimals)
    br = ReplayBroker(cash)
    hold = bt.Broker(cash)
    clock = _Clock()
    gs.time = clock  # type: ignore[assignment]
    equity: list[float] = []
    hold_eq: list[float] = []
    i = -1
    t0: float | None = None
    started_hold = False
    with open(path, newline="") as fh:
        fh.readline()  # заголовок
        for line in fh:
            i += 1
            p = line.rstrip("\n").split(",")
            o, h, l, c = float(p[1]), float(p[2]), float(p[3]), float(p[4])
            t = _epoch(p[0])
            if t0 is None:
                t0 = t
            if not started_hold:
                hold.market_buy(cash, c, 0)
                started_hold = True
            br.execute_bar(i, o, h, l, c, _noop)
            clock.now = t - t0  # реальные секунды от старта -> кулдаун честный
            for a in eng.plan(c, br.U, br.B, br.open_oids()):
                if a.kind == "place":
                    oid = br.place(a.side.lower(), a.price, a.qty, a.tag, i + 1)
                    if oid is None:
                        eng.drop(a)
                    else:
                        eng.register(a, oid)
                elif a.kind == "cancel":
                    if a.oid is not None:
                        br.cancel_oid(a.oid)
                elif a.kind == "market_buy":
                    br.market_buy(a.usd, c, i)
            equity.append(br.equity(c))
            hold_eq.append(hold.equity(c))
            if progress and i % progress == 0:
                print(f"  ... {i} баров, equity ${br.equity(c):,.0f}", flush=True)
    return br, hold, equity, hold_eq


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "data", "btc_h1_3y.csv")
    cfg = GridRBConfig.from_settings()
    big = os.path.getsize(path) > 50_000_000
    br, hold, equity, hold_eq = replay(path, cfg, cash=10_000.0,
                                       progress=200_000 if big else 0)
    tags = Counter(f.tag for f in br.fills)
    ret = equity[-1] / 10_000.0 - 1
    hret = hold_eq[-1] / 10_000.0 - 1
    print(f"\nфайл: {os.path.basename(path)}")
    print(f"параметры: f0={cfg.f0:.2f} band={cfg.band:.2f} "
          f"delta={cfg.delta*1000:.1f}‰ n={cfg.n} micro={cfg.micro_lot*100:.0f}%"
          f"×{cfg.micro_max} early={cfg.early_lot:.2f} "
          f"rearm_eps={cfg.rearm_eps*100:.1f}% cooldown={cfg.rearm_cooldown:.0f}s")
    print(f"баров: {len(equity)}")
    print(f"  GridRB : финал ${equity[-1]:,.0f} ({ret*100:+.1f}%)  "
          f"maxDD {bt.max_drawdown(equity)*100:,.1f}%")
    print(f"  Hold   : финал ${hold_eq[-1]:,.0f} ({hret*100:+.1f}%)  "
          f"maxDD {bt.max_drawdown(hold_eq)*100:,.1f}%")
    print(f"  разница: {(ret-hret)*100:+.1f} п.п.")
    print(f"  сделок {len(br.fills)}  {dict(tags)}")
    print(f"  постановок {br.placed}  отмен {br.cancelled}  "
          f"комиссии ${br.fees_paid:,.2f}  оборот ${br.turnover:,.2f}")


if __name__ == "__main__":
    main()
