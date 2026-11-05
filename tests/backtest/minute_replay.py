#!/usr/bin/env python3
"""Прогон БОЕВОГО движка GridRB по барам (H1 или 1m).

Зачем: в tests/backtest/strategies.py лежит ПОРТ боевой логики, и он успел
разойтись с оригиналом (гистерезис перестановки, ранняя докупка, заморозка
объёмов лестницы). Здесь вызывается ровно тот же класс
modules.gridrb_strategy.GridRBEngine, что работает в бою. Отличается только
источник баров, конфигурация (по умолчанию — из settings.py) и модель
исполнения (tests/backtest/engine.py).

Модель исполнения (как в engine.py): лимитный buy исполняется на баре i,
активен с i+1, если low <= цена, по цене min(open, цена); sell — если
high >= цена, по max(open, цена); комиссия maker 0.04 %; нотионал < $10 не
исполняется. Часы бота идут по реальным меткам баров, поэтому кулдаун
перестановки (GRIDRB_REARM_COOLDOWN) воспроизводится честно: на 1m это 5
баров, на H1 он не связывает.

Модуль даёт переиспользуемые функции (load_bars / run_replay / summarize /
evaluate), которыми пользуется перебор параметров (sol_optimize.py).

Запуск:
    ./venv/bin/python tests/backtest/minute_replay.py tests/backtest/data/btc_h1_3y.csv
    ./venv/bin/python tests/backtest/minute_replay.py tests/backtest/data/btc_1m_3y.csv
"""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from dataclasses import dataclass, field

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
    """Подмена модуля time внутри gridrb_strategy: время = секунды от старта."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class ReplayBroker(bt.Broker):
    """Broker с постановкой по точному qty и с синтетическими oid."""

    def __init__(self, cash: float, penetration: float = 0.0) -> None:
        super().__init__(cash, penetration=penetration)
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


def load_bars(path: str) -> list[tuple[float, float, float, float, float]]:
    """Читает CSV (time_utc,open,high,low,close,...) -> список баров.

    Возвращает [(epoch, open, high, low, close)]. Читается один раз: перебор
    параметров гоняет движок по уже загруженным барам.
    """
    bars: list[tuple[float, float, float, float, float]] = []
    with open(path, newline="") as fh:
        fh.readline()  # заголовок
        for line in fh:
            p = line.rstrip("\n").split(",")
            bars.append((_epoch(p[0]), float(p[1]), float(p[2]),
                         float(p[3]), float(p[4])))
    return bars


@dataclass
class ReplayResult:
    params: str
    bars: int
    final: float
    ret: float
    mdd: float
    hold_final: float
    hold_ret: float
    hold_mdd: float
    excess_pp: float
    excess_per_dd: float
    fills: int
    tags: dict = field(default_factory=dict)
    fees: float = 0.0
    turnover: float = 0.0
    placed: int = 0
    cancelled: int = 0


def run_replay(bars, cfg: GridRBConfig, cash: float = 10_000.0,
               sz_decimals: int = 5, progress: int = 0, penetration: float = 0.0):
    """Прогон движка по барам. Возвращает (broker, equity, hold_equity)."""
    eng = GridRBEngine(cfg, sz_decimals=sz_decimals)
    br = ReplayBroker(cash, penetration=penetration)
    hold = bt.Broker(cash)
    clock = _Clock()
    gs.time = clock  # type: ignore[assignment]
    equity: list[float] = []
    hold_eq: list[float] = []
    t0 = bars[0][0] if bars else 0.0
    first_close = bars[0][4] if bars else 0.0
    hold.market_buy(cash, first_close, 0)
    for i, (t, o, h, l, c) in enumerate(bars):
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
    return br, equity, hold_eq


def summarize(br, equity, hold_eq, cfg: GridRBConfig,
              cash: float = 10_000.0) -> ReplayResult:
    """Сводка по уже выполненному прогону."""
    ret = equity[-1] / cash - 1
    hret = hold_eq[-1] / cash - 1
    mdd = bt.max_drawdown(equity)
    hmdd = bt.max_drawdown(hold_eq)
    excess = (ret - hret) * 100
    p = (f"f0={cfg.f0:.2f} band={cfg.band:.3f} delta={cfg.delta*1000:.1f}‰ "
         f"n={cfg.n} micro={cfg.micro_lot:.2f}×{cfg.micro_max} "
         f"early={cfg.early_lot:.2f} eps={cfg.rearm_eps*1000:.1f}‰ "
         f"cd={cfg.rearm_cooldown:.0f}s")
    return ReplayResult(
        params=p, bars=len(equity),
        final=equity[-1], ret=ret, mdd=mdd,
        hold_final=hold_eq[-1], hold_ret=hret, hold_mdd=hmdd,
        excess_pp=excess,
        excess_per_dd=excess / max(1e-9, abs(mdd * 100)),
        fills=len(br.fills), tags=dict(Counter(f.tag for f in br.fills)),
        fees=br.fees_paid, turnover=br.turnover,
        placed=br.placed, cancelled=br.cancelled)


def evaluate(bars, cfg: GridRBConfig, cash: float = 10_000.0,
             sz_decimals: int = 5, penetration: float = 0.0) -> ReplayResult:
    """Прогон движка по барам + сводка (финал, доходность, просадка, hold)."""
    br, equity, hold_eq = run_replay(bars, cfg, cash=cash,
                                     sz_decimals=sz_decimals,
                                     penetration=penetration)
    return summarize(br, equity, hold_eq, cfg, cash)


def print_result(path: str, cfg: GridRBConfig, r: ReplayResult) -> None:
    print(f"\nфайл: {os.path.basename(path)}")
    print(f"параметры: f0={cfg.f0:.2f} band={cfg.band:.2f} "
          f"delta={cfg.delta*1000:.1f}‰ n={cfg.n} micro={cfg.micro_lot*100:.0f}%"
          f"×{cfg.micro_max} early={cfg.early_lot:.2f} "
          f"rearm_eps={cfg.rearm_eps*100:.1f}% cooldown={cfg.rearm_cooldown:.0f}s")
    print(f"баров: {r.bars}")
    print(f"  GridRB : финал ${r.final:,.0f} ({r.ret*100:+.1f}%)  "
          f"maxDD {r.mdd*100:,.1f}%")
    print(f"  Hold   : финал ${r.hold_final:,.0f} ({r.hold_ret*100:+.1f}%)  "
          f"maxDD {r.hold_mdd*100:,.1f}%")
    print(f"  разница: {r.excess_pp:+.1f} п.п.")
    print(f"  сделок {r.fills}  {r.tags}")
    print(f"  постановок {r.placed}  отмен {r.cancelled}  "
          f"комиссии ${r.fees:,.2f}  оборот ${r.turnover:,.2f}")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Прогон боевого GridRB по барам")
    ap.add_argument("path", nargs="?",
                    default=os.path.join(HERE, "data", "btc_h1_3y.csv"),
                    help="CSV с барами (time_utc,open,high,low,close)")
    ap.add_argument("--sz-decimals", type=int, default=5,
                    help="точность размера (USOL=3, UBTC=5; см. spotMeta)")
    ap.add_argument("--penetration", type=float, default=0.0,
                    help="насколько цена должна уйти за уровень лимита (доля)")
    args = ap.parse_args()
    cfg = GridRBConfig.from_settings()
    big = os.path.getsize(args.path) > 50_000_000
    bars = load_bars(args.path)
    br, equity, hold_eq = run_replay(
        bars, cfg, progress=200_000 if big else 0,
        sz_decimals=args.sz_decimals, penetration=args.penetration)
    r = summarize(br, equity, hold_eq, cfg)
    print_result(args.path, cfg, r)
    print(f"  модель: sz_decimals={args.sz_decimals} "
          f"penetration={args.penetration*100:.3f}%")


if __name__ == "__main__":
    main()
