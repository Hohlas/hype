#!/usr/bin/env python3
"""Прогон трёх стратегий на 2-летней истории BTC H1 и выгрузка результатов в CSV.

Запуск:
    ./venv/bin/python tests/backtest/run_backtest.py

Выход (tests/backtest/results/):
    equity_full.csv        — кривые equity по барам (все стратегии + hold)
    summary.csv            — итоговые метрики (базовые издержки)
    summary_stress2.csv    — итоговые метрики при издержках x2
"""
from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Broker, Result, buy_and_hold, load_bars, max_drawdown  # noqa: E402
from strategies import GridRB, TrendAdaptiveGrid, TwoModes7030  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_2y.csv")
RESULTS = os.path.join(HERE, "results")
START_CASH = 10_000.0


def run_instance(strat, bars) -> Result:
    res = Result(strat.name)
    for bar in bars:
        strat.b.execute_bar(bar.i, bar.o, bar.h, bar.l, bar.c, strat.on_fill)
        strat.on_close(bar.i, bar)
        res.equity.append(strat.b.equity(bar.c))
    res.trades = len(strat.b.fills)
    res.fees = strat.b.fees_paid
    res.turnover = strat.b.turnover
    return res


def build_all(cash, stress=1.0):
    return [
        GridRB(cash, stress=stress),
        TwoModes7030(cash, stress=stress),
        TrendAdaptiveGrid(cash, stress=stress),
    ]


def summarize(results, hold):
    rows = []
    h_final = hold.final
    for r in [hold] + results:
        rows.append({
            "strategy": r.name,
            "final_usd": round(r.final, 2),
            "return_pct": round((r.final / START_CASH - 1) * 100, 2),
            "excess_vs_hold_usd": round(r.final - h_final, 2),
            "excess_vs_hold_pp": round((r.final - h_final) / START_CASH * 100, 2),
            "max_dd_pct": round(r.mdd * 100, 2),
            "trades": r.trades,
            "fees_usd": round(r.fees, 2),
            "turnover_usd": round(r.turnover, 2),
        })
    return rows


def write_csv(path, rows, fields=None):
    if not rows:
        return
    fields = fields or list(rows[0].keys())
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def print_table(rows):
    hdr = f"{'strategy':<22}{'final':>10}{'ret%':>8}{'excess$':>10}" \
          f"{'excesspp':>10}{'maxDD%':>9}{'trades':>8}{'fees$':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['strategy']:<22}{r['final_usd']:>10.0f}{r['return_pct']:>8.1f}"
              f"{r['excess_vs_hold_usd']:>10.0f}{r['excess_vs_hold_pp']:>10.2f}"
              f"{r['max_dd_pct']:>9.1f}{r['trades']:>8}{r['fees_usd']:>9.0f}")


def write_fills(name, fills):
    import re
    mapping = {
        "GridRB 70/30": "gridrb",
        "Два режима 70/30": "two_modes_70_30",
        "Trend-Adaptive Grid": "trend_adaptive_grid",
    }
    safe = mapping.get(name) or re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_")
    path = os.path.join(RESULTS, f"fills_{safe}.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["bar", "side", "price", "qty", "notional", "tag", "fee"])
        for f in fills:
            w.writerow([f.i, f.side, round(f.price, 4), round(f.qty, 8),
                        round(f.price * f.qty, 4), f.tag, round(f.fee, 6)])


def main():
    os.makedirs(RESULTS, exist_ok=True)
    bars = load_bars(DATA)
    print(f"баров: {len(bars)}  ({bars[0].c:.0f} -> {bars[-1].c:.0f})")

    hold = buy_and_hold(bars, START_CASH)
    strats = build_all(START_CASH)
    results = [run_instance(s, bars) for s in strats]
    rows = summarize(results, hold)
    print("\n=== Базовые издержки (maker 0.040%, taker 0.070%, slip 2 б.п.) ===")
    print_table(rows)
    write_csv(os.path.join(RESULTS, "summary.csv"), rows)
    for s in strats:
        write_fills(s.name, s.b.fills)

    # кривые equity
    with open(os.path.join(RESULTS, "equity_full.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["bar", "time_utc", "close", "Buy&Hold"]
                   + [r.name for r in results])
        import datetime as dt
        for j, bar in enumerate(bars):
            ts = dt.datetime.fromtimestamp(bar.t / 1000, tz=dt.timezone.utc)
            w.writerow([bar.i, ts.strftime("%Y-%m-%d %H:%M:%S"), bar.c,
                        round(hold.equity[j], 4)]
                       + [round(r.equity[j], 4) for r in results])

    # стресс x2
    hold2 = buy_and_hold(bars, START_CASH, stress=2.0)
    res2 = [run_instance(s, bars) for s in build_all(START_CASH, stress=2.0)]
    rows2 = summarize(res2, hold2)
    print("\n=== Стресс x2 по издержкам ===")
    print_table(rows2)
    write_csv(os.path.join(RESULTS, "summary_stress2.csv"), rows2)

    print(f"\nCSV сохранены в {RESULTS}")


if __name__ == "__main__":
    main()