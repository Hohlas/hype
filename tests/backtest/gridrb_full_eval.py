#!/usr/bin/env python3
"""Догоняет перебор GridRB: считает test и full для всех вариантов из
results/gridrb_sweep.csv и показывает влияние f0.

Запуск:
    ./venv/bin/python tests/backtest/gridrb_full_eval.py
"""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import buy_and_hold, load_bars  # noqa: E402
from run_backtest import RESULTS, run_instance  # noqa: E402
from strategies import GridRB  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_2y.csv")
CASH = 10_000.0


def ev(params, bars, h_final):
    r = run_instance(GridRB(CASH, **params), bars)
    return (r.final, (r.final - h_final) / CASH * 100, r.mdd * 100, r.trades)


def main():
    bars = load_bars(DATA)
    split = int(len(bars) * 0.70)
    train, test = bars[:split], bars[split:]
    full = bars
    h_tr = buy_and_hold(train, CASH).final
    h_te = buy_and_hold(test, CASH).final
    h_fu = buy_and_hold(full, CASH).final

    src = os.path.join(RESULTS, "gridrb_sweep.csv")
    with open(src, newline="") as fh:
        rows = list(csv.DictReader(fh))

    out_rows = []
    for j, r in enumerate(rows):
        p = {
            "f0": float(r["f0"]), "band": float(r["band"]),
            "delta": float(r["delta"]), "n": int(r["n"]),
            "micro_lot": float(r["micro_lot"]), "micro_max": int(r["micro_max"]),
        }
        te = ev(p, test, h_te)
        fu = ev(p, full, h_fu)
        out_rows.append({
            **p,
            "train_excess_pp": float(r["excess_pp"]),
            "train_dd_pct": float(r["max_dd_pct"]),
            "test_excess_pp": round(te[1], 2),
            "test_dd_pct": round(te[2], 2),
            "full_final_usd": round(fu[0], 2),
            "full_excess_pp": round(fu[1], 2),
            "full_dd_pct": round(fu[2], 2),
            "full_trades": fu[3],
        })
        if (j + 1) % 100 == 0:
            print(f"  {j+1}/{len(rows)}")

    out = os.path.join(RESULTS, "gridrb_sweep_all.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    # влияние f0 (усреднение по остальным параметрам)
    by_f0 = defaultdict(list)
    for r in out_rows:
        by_f0[r["f0"]].append(r)
    print(f"\n{'f0':>5}{'n':>4}{'full_ex_mean':>14}{'full_dd_mean':>14}"
          f"{'test_ex_mean':>14}")
    for f0 in sorted(by_f0):
        v = by_f0[f0]
        print(f"{f0:>5.2f}{len(v):>4}"
              f"{sum(x['full_excess_pp'] for x in v)/len(v):>14.2f}"
              f"{sum(x['full_dd_pct'] for x in v)/len(v):>14.1f}"
              f"{sum(x['test_excess_pp'] for x in v)/len(v):>14.2f}")

    top_full = sorted(out_rows, key=lambda r: r["full_excess_pp"], reverse=True)[:10]
    print("\nТОП-10 по excess на полном окне (in-sample, для ориентира):")
    print(f"{'f0':>5}{'band':>6}{'delta':>7}{'n':>3}{'mlot':>6}{'mmax':>5}"
          f"{'full_ex':>9}{'DD%':>8}{'trades':>7}")
    for r in top_full:
        print(f"{r['f0']:>5.2f}{r['band']:>6.3f}{r['delta']:>7.4f}{r['n']:>3}"
              f"{r['micro_lot']:>6.2f}{r['micro_max']:>5}"
              f"{r['full_excess_pp']:>9.1f}{r['full_dd_pct']:>8.1f}"
              f"{r['full_trades']:>7}")
    print(f"\nCSV: {out}")


if __name__ == "__main__":
    main()