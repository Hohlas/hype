#!/usr/bin/env python3
"""Чувствительность результатов к неоднозначным местам спецификации.

docs/spec.md описывает «Два режима 70/30» и Trend-Adaptive Grid словами, поэтому
одна и та же формулировка допускает разные реализации. Здесь прогоняются
варианты, чтобы показать разброс, а не выдать один «красивый» результат.

Запуск:
    ./venv/bin/python tests/backtest/sensitivity.py
"""
from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import buy_and_hold, load_bars  # noqa: E402
from run_backtest import RESULTS, run_instance  # noqa: E402
from strategies import TrendAdaptiveGrid, TwoModes7030  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_2y.csv")
CASH = 10_000.0


def metrics(name, params, r, h_final):
    return {
        "strategy": name,
        "params": params,
        "final_usd": round(r.final, 2),
        "return_pct": round((r.final / CASH - 1) * 100, 2),
        "excess_vs_hold_pp": round((r.final - h_final) / CASH * 100, 2),
        "max_dd_pct": round(r.mdd * 100, 2),
        "trades": r.trades,
    }


def main():
    bars = load_bars(DATA)
    hold = buy_and_hold(bars, CASH)
    h = hold.final
    rows = []
    for reb in ("none", "gated", "always"):
        for S in (300, 500, 700):
            s = TwoModes7030(CASH, S=S, rebalance=reb)
            r = run_instance(s, bars)
            rows.append(metrics("Два режима 70/30", f"rebalance={reb},S={S}", r, h))
    for core in (0.3, 0.5, 0.7):
        for step in (300, 500, 700):
            s = TrendAdaptiveGrid(CASH, STEP=step, core_pct=core)
            r = run_instance(s, bars)
            rows.append(metrics("Trend-Adaptive Grid",
                                f"core={core},STEP={step}", r, h))

    out = os.path.join(RESULTS, "sensitivity.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"{'strategy':<20}{'params':<28}{'final':>8}{'excesspp':>9}"
          f"{'maxDD%':>8}{'trades':>7}")
    print("-" * 82)
    for r in rows:
        print(f"{r['strategy']:<20}{r['params']:<28}{r['final_usd']:>8.0f}"
              f"{r['excess_vs_hold_pp']:>9.2f}{r['max_dd_pct']:>8.1f}"
              f"{r['trades']:>7}")
    print(f"\nCSV сохранён: {out}")


if __name__ == "__main__":
    main()