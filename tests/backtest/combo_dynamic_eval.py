#!/usr/bin/env python3
"""Проверка динамического распределения GridRB/TrendGrid на train/test.

Правило: w_trend = clip(0.5 + k*(c/ATH - 1), 0, 1).
    k > 0  — в просадке больше веса GridRB, у хая 50/50;
    k < 0  — наоборот.
k выбирается на train, затем проверяется на test и полном окне.

Оговорка: перекладывание капитала между «ногами» считается без комиссий
(внутри одного счёта это технически возможно, но не бесплатно при смене
актива). Это оптимистичное допущение.
"""
from __future__ import annotations

import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import buy_and_hold, load_bars  # noqa: E402
from run_backtest import RESULTS  # noqa: E402
from combo import GRIDRB_8020, TREND_DEFAULT, curve, stats  # noqa: E402
from strategies import GridRB, TrendAdaptiveGrid  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_2y.csv")
CASH = 10_000.0


def dynamic(eq_g, eq_t, drawdown, k, sign=1.0):
    total = CASH
    eq = [total]
    for j in range(1, len(eq_g)):
        w = min(1.0, max(0.0, 0.5 + sign * k * drawdown[j - 1]))
        rg = eq_g[j] / eq_g[j - 1] - 1
        rt = eq_t[j] / eq_t[j - 1] - 1
        total *= 1 + w * rg + (1 - w) * rt
        eq.append(total)
    return eq


def dynamic_threshold(eq_g, eq_t, drawdown, k, threshold=0.10, cost=0.0010):
    """Как dynamic, но вес меняется только при отклонении >= threshold,
    и за перекладывание берётся cost * |dw| (10 б.п. на переложенную долю)."""
    total = CASH
    eq = [total]
    w_act = 0.5
    for j in range(1, len(eq_g)):
        target = min(1.0, max(0.0, 0.5 + k * drawdown[j - 1]))
        if abs(target - w_act) >= threshold:
            total *= 1 - cost * abs(target - w_act)
            w_act = target
        rg = eq_g[j] / eq_g[j - 1] - 1
        rt = eq_t[j] / eq_t[j - 1] - 1
        total *= 1 + w_act * rg + (1 - w_act) * rt
        eq.append(total)
    return eq


def drawdowns(bars):
    out, peak = [], -math.inf
    for b in bars:
        peak = max(peak, b.c)
        out.append(b.c / peak - 1)
    return out


def main():
    bars = load_bars(DATA)
    split = int(len(bars) * 0.70)
    segs = {"train": bars[:split], "test": bars[split:], "full": bars}

    cache = {}
    for name, sb in segs.items():
        g = curve(GRIDRB_8020, GridRB, sb)
        t = curve(TREND_DEFAULT, TrendAdaptiveGrid, sb)
        h = buy_and_hold(sb, CASH)
        cache[name] = (g, t, h, drawdowns(sb))

    ks = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    rows = []
    for name, (g, t, h, dd) in cache.items():
        for k in ks:
            for mode, fn in (("hourly_frictionless", dynamic),
                             ("threshold10pp_cost10bp", dynamic_threshold)):
                eq = fn(g.equity, t.equity, dd, k)
                st = stats(eq, h.final)
                rows.append({"segment": name, "k": k, "mode": mode, **st})

    out = os.path.join(RESULTS, "combo_dynamic.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    tr = [r for r in rows if r["segment"] == "train"
          and r["mode"] == "threshold10pp_cost10bp"]
    hold_dd_tr = cache["train"][2].mdd * 100
    ok = [r for r in tr if r["max_dd_pct"] >= hold_dd_tr * 1.2]
    best = max(ok or tr, key=lambda r: r["excess_pp"])
    k = best["k"]
    print(f"Лучший k на train (с порогом и издержками): {k} "
          f"(excess {best['excess_pp']:+.2f} п.п., DD {best['max_dd_pct']:.1f}%)")
    for seg in ("train", "test", "full"):
        for mode in ("hourly_frictionless", "threshold10pp_cost10bp"):
            r = next(r for r in rows if r["segment"] == seg
                     and r["k"] == k and r["mode"] == mode)
            print(f"  {seg:<6} {mode:<24} excess={r['excess_pp']:+6.2f} п.п. "
                  f"DD={r['max_dd_pct']:6.1f}% final={r['final_usd']:.0f}")

    print("\nВсе k (full, с порогом и издержками):")
    for r in [r for r in rows if r["segment"] == "full"
              and r["mode"] == "threshold10pp_cost10bp"]:
        print(f"  k={r['k']:.1f}  excess={r['excess_pp']:+6.2f}  "
              f"DD={r['max_dd_pct']:6.1f}%  final={r['final_usd']:.0f}")
    print(f"\nCSV: {out}")


if __name__ == "__main__":
    main()