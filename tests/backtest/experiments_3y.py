#!/usr/bin/env python3
"""Эксперименты по GridRB на 3 годах (2023-11 -> 2026-11), без train/test.

Проверяются:
  H0 — базовая симметричная полоса (для ориентира);
  H1 — асимметричные полосы: defc_band (покупки) != trim_band (продажи);
  H2 — ранняя докупка внутри полосы (early_lot);
  H3 — динамическая покупательная полоса от просадки (dyn_band).
Всё при f0 из {0.65, 0.70, 0.75, 0.80}. Все 3 года идут в оценку.

Запуск:
    ./venv/bin/python tests/backtest/experiments_3y.py
"""
from __future__ import annotations

import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import buy_and_hold, load_bars  # noqa: E402
from run_backtest import RESULTS  # noqa: E402
from strategies import GridRB  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_3y.csv")
CASH = 10_000.0


def run_metrics(strat: GridRB, bars):
    equity, weights = [], []
    for bar in bars:
        strat.b.execute_bar(bar.i, bar.o, bar.h, bar.l, bar.c, strat.on_fill)
        strat.on_close(bar.i, bar)
        equity.append(strat.b.equity(bar.c))
        weights.append(strat.b.weight(bar.c))

    tags = {}
    for f in strat.b.fills:
        tags[f.tag] = tags.get(f.tag, 0) + 1

    # время в зонах (по базовым полосам; для dyn — приблизительно)
    n = len(weights)
    defc_z = sum(1 for w in weights if w < strat.f0 - strat.defc_band)
    trim_z = sum(1 for w in weights if w > strat.f0 + strat.trim_band)

    peak, mdd = -math.inf, 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)

    return {
        "final_usd": equity[-1],
        "max_dd_pct": mdd * 100,
        "trades": len(strat.b.fills),
        "fees": strat.b.fees_paid,
        "avg_weight_pct": sum(weights) / n * 100,
        "time_defc_pct": defc_z / n * 100,
        "time_trim_pct": trim_z / n * 100,
        "defc_fills": tags.get("defc", 0),
        "trim_fills": tags.get("trim", 0),
        "early_fills": tags.get("early", 0),
        "micro_fills": tags.get("micro", 0),
        "micro_take_fills": tags.get("micro_take", 0),
    }


def make_row(experiment, params, m, hold_final):
    return {
        "experiment": experiment,
        "f0": params["f0"],
        "band": params.get("band", ""),
        "trim_band": params.get("trim_band", ""),
        "defc_band": params.get("defc_band", ""),
        "early_lot": params.get("early_lot", ""),
        "dyn_band": params.get("dyn_band", ""),
        "final_usd": round(m["final_usd"], 2),
        "excess_pp": round((m["final_usd"] - hold_final) / CASH * 100, 2),
        "max_dd_pct": round(m["max_dd_pct"], 2),
        "trades": m["trades"],
        "fees": round(m["fees"], 2),
        "avg_weight_pct": round(m["avg_weight_pct"], 1),
        "time_defc_pct": round(m["time_defc_pct"], 1),
        "time_trim_pct": round(m["time_trim_pct"], 1),
        "defc_fills": m["defc_fills"],
        "trim_fills": m["trim_fills"],
        "early_fills": m["early_fills"],
        "micro_fills": m["micro_fills"],
        "micro_take_fills": m["micro_take_fills"],
    }


def main():
    bars = load_bars(DATA)
    hold = buy_and_hold(bars, CASH)
    print(f"баров: {len(bars)} | {bars[0].c:.0f} -> {bars[-1].c:.0f} | "
          f"hold=${hold.final:.0f} ({(hold.final/CASH-1)*100:+.1f}%, "
          f"DD={hold.mdd*100:.1f}%)")

    f0s = [0.80, 0.75, 0.70, 0.65]
    rows = []

    def add(experiment, params):
        m = run_metrics(GridRB(CASH, **params), bars)
        rows.append(make_row(experiment, params, m, hold.final))

    # H0 — базовая симметричная полоса
    for f0 in f0s:
        for band in (0.02, 0.03, 0.04, 0.05, 0.06):
            add("H0_base", {"f0": f0, "band": band, "delta": 0.004, "n": 5,
                            "micro_lot": 0.02, "micro_max": 3})

    # H1 — асимметричные полосы
    pairs = [(0.02, 0.02), (0.02, 0.04), (0.04, 0.02), (0.04, 0.04),
             (0.03, 0.05), (0.05, 0.03), (0.02, 0.06), (0.06, 0.02)]
    for f0 in f0s:
        for defc_b, trim_b in pairs:
            add("H1_asym", {"f0": f0, "band": 0.04, "delta": 0.004, "n": 5,
                            "micro_lot": 0.02, "micro_max": 3,
                            "defc_band": defc_b, "trim_band": trim_b})

    # H2 — ранняя докупка
    for f0 in f0s:
        for el in (0.0, 0.05, 0.10, 0.20):
            add("H2_early", {"f0": f0, "band": 0.04, "delta": 0.004, "n": 5,
                             "micro_lot": 0.02, "micro_max": 3,
                             "early_lot": el})

    # H3 — динамическая полоса от просадки
    for f0 in f0s:
        for dyn in (-0.4, -0.2, 0.0, 0.2, 0.4):
            add("H3_dyn", {"f0": f0, "band": 0.04, "delta": 0.004, "n": 5,
                           "micro_lot": 0.02, "micro_max": 3,
                           "dyn_band": dyn})

    # H2+H3 — комбинация лучших (ранняя докупка + сужение полосы в просадке)
    for f0 in f0s:
        for el in (0.05, 0.10, 0.20):
            for dyn in (-0.2, -0.4):
                add("H2H3_combo", {"f0": f0, "band": 0.04, "delta": 0.004,
                                   "n": 5, "micro_lot": 0.02, "micro_max": 3,
                                   "early_lot": el, "dyn_band": dyn})

    out = os.path.join(RESULTS, "experiments_3y.csv")
    os.makedirs(RESULTS, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # лучшие по каждой гипотезе
    for exp in ("H0_base", "H1_asym", "H2_early", "H3_dyn", "H2H3_combo"):
        grp = [r for r in rows if r["experiment"] == exp]
        grp.sort(key=lambda r: r["excess_pp"], reverse=True)
        print(f"\n=== {exp}: топ-5 по excess ===")
        print(f"{'f0':>5}{'band':>6}{'defc_b':>7}{'trim_b':>7}{'early':>7}"
              f"{'dyn':>6}{'excess':>8}{'DD%':>8}{'w%':>6}{'trd':>6}"
              f"{'defcF':>7}{'trimF':>7}{'earF':>6}")
        for r in grp[:5]:
            print(f"{r['f0']:>5.2f}{str(r['band']):>6}{str(r['defc_band']):>7}"
                  f"{str(r['trim_band']):>7}{str(r['early_lot']):>7}"
                  f"{str(r['dyn_band']):>6}{r['excess_pp']:>8.2f}"
                  f"{r['max_dd_pct']:>8.1f}{r['avg_weight_pct']:>6.1f}"
                  f"{r['trades']:>6}{r['defc_fills']:>7}{r['trim_fills']:>7}"
                  f"{r['early_fills']:>6}")

    print(f"\nCSV: {out}")


if __name__ == "__main__":
    main()