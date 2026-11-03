#!/usr/bin/env python3
"""Портфель из двух стратегий: GridRB (80/20) + Trend-Adaptive Grid.

Капитал делится в доле x на GridRB и (1-x) на Trend-Adaptive Grid; каждая
стратегия ведёт свой независимый счёт. Такое разделение корректно, потому что
размеры входов у обеих стратегий пропорциональны капиталу (кривые equity
линейны по стартовому депозиту).

Дополнительно считается вырожденная комбинация GridRB + пассивное ядро
(buy&hold) — чтобы видеть, что даёт именно вторая активная стратегия.

Запуск:
    ./venv/bin/python tests/backtest/combo.py
"""
from __future__ import annotations

import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import buy_and_hold, load_bars  # noqa: E402
from run_backtest import RESULTS, run_instance  # noqa: E402
from strategies import GridRB, TrendAdaptiveGrid  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_2y.csv")
CASH = 10_000.0

GRIDRB_8020 = {"f0": 0.80, "band": 0.04, "delta": 0.004, "n": 5,
               "micro_lot": 0.02, "micro_max": 3}
TREND_DEFAULT = {"core_pct": 0.5, "STEP": 500.0}


def curve(params, cls, bars):
    return run_instance(cls(CASH, **params), bars)


def combined(eq_a, eq_b, x):
    return [x * a + (1 - x) * b for a, b in zip(eq_a, eq_b)]


def stats(eq, hold_final):
    final = eq[-1]
    peak, mdd = -math.inf, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return {
        "final_usd": round(final, 2),
        "excess_pp": round((final - hold_final) / CASH * 100, 2),
        "max_dd_pct": round(mdd * 100, 2),
    }


def corr(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va == 0 or vb == 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


def returns(eq):
    return [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]


def run_segment(bars, h_final, label):
    g = curve(GRIDRB_8020, GridRB, bars)
    t = curve(TREND_DEFAULT, TrendAdaptiveGrid, bars)
    bh = buy_and_hold(bars, CASH)
    rows = []
    for i in range(0, 101, 5):
        x = i / 100
        c = combined(g.equity, t.equity, x)
        st = stats(c, h_final)
        rows.append({
            "segment": label, "x_gridrb": round(x, 2),
            "x_trend": round(1 - x, 2),
            **st,
            "trades": g.trades + t.trades,
        })
    return rows, g, t, bh


def main():
    bars = load_bars(DATA)
    split = int(len(bars) * 0.70)
    segs = {
        "train": bars[:split],
        "test": bars[split:],
        "full": bars,
    }
    all_rows = []
    precalc = {}
    for name, sb in segs.items():
        h = buy_and_hold(sb, CASH)
        rows, g, t, bh = run_segment(sb, h.final, name)
        all_rows.extend(rows)
        precalc[name] = (g, t, bh, h)

    out = os.path.join(RESULTS, "combo_sweep.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)

    # Кривая на полном окне
    print("Полное окно: доля GridRB x -> итог портфеля")
    print(f"{'x_gridrb':>9}{'final':>9}{'excess':>8}{'DD%':>8}")
    for r in [r for r in all_rows if r["segment"] == "full"]:
        print(f"{r['x_gridrb']:>9.2f}{r['final_usd']:>9.0f}"
              f"{r['excess_pp']:>8.2f}{r['max_dd_pct']:>8.1f}")

    # выбор x на train по excess при ограничении просадки
    tr = [r for r in all_rows if r["segment"] == "train"]
    hold_tr = precalc["train"][3]
    ok = [r for r in tr if r["max_dd_pct"] >= hold_tr.mdd * 100 * 1.2]
    best_tr = max(ok or tr, key=lambda r: r["excess_pp"])
    x_best = best_tr["x_gridrb"]
    print(f"\nЛучший x на train: {x_best:.2f} "
          f"(excess {best_tr['excess_pp']:+.2f} п.п., DD {best_tr['max_dd_pct']:.1f}%)")

    full_rows = {round(r["x_gridrb"], 2): r for r in all_rows
                 if r["segment"] == "full"}
    test_rows = {round(r["x_gridrb"], 2): r for r in all_rows
                 if r["segment"] == "test"}
    key = round(x_best, 2)
    print(f"  на test:  excess {test_rows[key]['excess_pp']:+.2f} п.п., "
          f"DD {test_rows[key]['max_dd_pct']:.1f}%, final "
          f"{test_rows[key]['final_usd']:.0f}")
    print(f"  на full:  excess {full_rows[key]['excess_pp']:+.2f} п.п., "
          f"DD {full_rows[key]['max_dd_pct']:.1f}%, final "
          f"{full_rows[key]['final_usd']:.0f}")

    # сравнение ног и равного сплита
    g, t, bh, hold = precalc["full"]
    print("\nНоги и комбинации (полное окно):")
    for label, eq in (("GridRB 80/20", g.equity),
                      ("Trend-Adaptive", t.equity),
                      ("Buy&Hold", bh.equity),
                      ("50/50 combo", combined(g.equity, t.equity, 0.5)),
                      (f"combo x={x_best:.2f}", combined(g.equity, t.equity, x_best))):
        st = stats(eq, hold.final)
        print(f"  {label:<18} final={st['final_usd']:8.0f} "
              f"excess={st['excess_pp']:+6.2f}пп DD={st['max_dd_pct']:6.1f}%")

    c = corr(returns(g.equity), returns(t.equity))
    print(f"\nКорреляция часовых доходностей GridRB и TrendGrid: {c:.3f}")

    # GridRB + пассивное ядро (buy&hold) для сравнения
    print("\nGridRB 80/20 + пассивное ядро (buy&hold):")
    print(f"{'x_gridrb':>9}{'final':>9}{'excess':>8}{'DD%':>8}")
    for i in range(0, 101, 10):
        x = i / 100
        eq = combined(g.equity, bh.equity, x)
        st = stats(eq, hold.final)
        print(f"{x:>9.2f}{st['final_usd']:>9.0f}{st['excess_pp']:>8.2f}"
              f"{st['max_dd_pct']:>8.1f}")

    # --- моё добавление: динамическое распределение по просадке от ATH ---
    # w_trend = clip(0.5 + k*(c/ATH - 1), 0, 1): у хая — 50/50,
    # в просадке — больше GridRB. И симметричный вариант (больше TrendGrid).
    ath = []
    peak = -math.inf
    for b in bars:
        peak = max(peak, b.c)
        ath.append(peak)
    drawdown = [b.c / a - 1 for b, a in zip(bars, ath)]

    dyn_rows = []
    print("\nДинамическое распределение (w_trend по просадке от ATH):")
    print(f"{'rule':<22}{'final':>9}{'excess':>8}{'DD%':>8}")
    for k in (0.5, 1.0, 2.0):
        for sign, label in ((1.0, f"+k={k} (больше GridRB в просадке)"),
                            (-1.0, f"-k={k} (больше Trend в просадке)")):
            w = [min(1.0, max(0.0, 0.5 + sign * k * dd)) for dd in drawdown]
            total = 10_000.0
            eq = [total]
            for j in range(1, len(bars)):
                wr = w[j - 1]
                rg = g.equity[j] / g.equity[j - 1] - 1
                rt = t.equity[j] / t.equity[j - 1] - 1
                total *= (1 + wr * rg + (1 - wr) * rt)
                eq.append(total)
            st = stats(eq, hold.final)
            dyn_rows.append({"rule": label, "k": k, **st})
            print(f"{label:<22}{st['final_usd']:>9.0f}{st['excess_pp']:>8.2f}"
                  f"{st['max_dd_pct']:>8.1f}")
    dyn_out = os.path.join(RESULTS, "combo_dynamic.csv")
    with open(dyn_out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(dyn_rows[0].keys()))
        w.writeheader()
        w.writerows(dyn_rows)

    print(f"\nCSV: {out}")
    print(f"CSV: {dyn_out}")


if __name__ == "__main__":
    main()