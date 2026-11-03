#!/usr/bin/env python3
"""Диагностика стратегий: средний вес BTC, диапазон веса, число сделок.

    ./venv/bin/python tests/backtest/diagnostics.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import load_bars  # noqa: E402
from run_backtest import build_all  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_2y.csv")
CASH = 10_000.0


def diagnose(strat, bars):
    weights = []
    for bar in bars:
        strat.b.execute_bar(bar.i, bar.o, bar.h, bar.l, bar.c, strat.on_fill)
        strat.on_close(bar.i, bar)
        weights.append(strat.b.weight(bar.c))
    buys = sum(1 for f in strat.b.fills if f.side == "buy")
    sells = sum(1 for f in strat.b.fills if f.side == "sell")
    return {
        "strategy": strat.name,
        "avg_weight_pct": round(sum(weights) / len(weights) * 100, 1),
        "max_weight_pct": round(max(weights) * 100, 1),
        "min_weight_pct": round(min(weights) * 100, 1),
        "buys": buys,
        "sells": sells,
    }


def main():
    bars = load_bars(DATA)
    print(f"{'strategy':<22}{'avg_w%':>8}{'max_w%':>8}{'min_w%':>8}"
          f"{'buys':>7}{'sells':>7}")
    print("-" * 60)
    for s in build_all(CASH):
        d = diagnose(s, bars)
        print(f"{d['strategy']:<22}{d['avg_weight_pct']:>8}{d['max_weight_pct']:>8}"
              f"{d['min_weight_pct']:>8}{d['buys']:>7}{d['sells']:>7}")


if __name__ == "__main__":
    main()