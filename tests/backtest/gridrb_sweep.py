#!/usr/bin/env python3
"""Перебор параметров GridRB на обучающем окне + проверка на тесте.

Обучающее окно — первые 70 % баров, тест — последние 30 % (чистый out-of-sample:
на тесте каждая стратегия стартует заново с $10 000).

Критерий отбора (как в docs/spec.md §4): максимум excess vs hold при
maxDD_стратегии <= 1.2 * maxDD_hold. Дополнительно сохраняются лучшие по
абсолютному excess и по «эксцесс на единицу просадки».

Запуск:
    ./venv/bin/python tests/backtest/gridrb_sweep.py
"""
from __future__ import annotations

import csv
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import buy_and_hold, load_bars  # noqa: E402
from run_backtest import RESULTS, run_instance  # noqa: E402
from strategies import GridRB  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_2y.csv")
CASH = 10_000.0
N_SAMPLE = 600
SEED = 42

GRID = {
    "f0": [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
    "band": [0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.08],
    "delta": [0.0025, 0.003, 0.004, 0.005, 0.006, 0.007, 0.010],
    "n": [2, 3, 4, 5, 6, 7, 8],
    "micro_lot": [0.0, 0.01, 0.02, 0.03, 0.04],
    "micro_max": [2, 3, 4, 5, 6],
}

# явно добавляем два варианта из docs/GridRB_spec.md
SPEC_VARIANTS = [
    {"f0": 0.70, "band": 0.05, "delta": 0.005, "n": 5,
     "micro_lot": 0.02, "micro_max": 3},
    {"f0": 0.80, "band": 0.04, "delta": 0.004, "n": 5,
     "micro_lot": 0.02, "micro_max": 3},
]


def make_candidates():
    rng = random.Random(SEED)
    seen = set()
    out = []
    for v in SPEC_VARIANTS:
        key = tuple(v[k] for k in GRID)
        seen.add(key)
        out.append(v)
    while len(out) < N_SAMPLE:
        v = {k: rng.choice(vals) for k, vals in GRID.items()}
        key = tuple(v[k] for k in GRID)
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def run(params, bars):
    s = GridRB(CASH, **params)
    return run_instance(s, bars)


def evaluate(params, bars, h_final):
    r = run(params, bars)
    excess = (r.final - h_final) / CASH * 100
    return {
        "final_usd": round(r.final, 2),
        "excess_pp": round(excess, 2),
        "max_dd_pct": round(r.mdd * 100, 2),
        "trades": r.trades,
    }


def main():
    os.makedirs(RESULTS, exist_ok=True)
    bars = load_bars(DATA)
    split = int(len(bars) * 0.70)
    train, test = bars[:split], bars[split:]
    hold_tr = buy_and_hold(train, CASH)
    hold_te = buy_and_hold(test, CASH)
    print(f"train: {len(train)} бар, hold {hold_tr.final:.0f} "
          f"(DD {hold_tr.mdd*100:.1f}%)")
    print(f"test:  {len(test)} бар, hold {hold_te.final:.0f} "
          f"(DD {hold_te.mdd*100:.1f}%)")

    cands = make_candidates()
    rows = []
    for j, p in enumerate(cands):
        m = evaluate(p, train, hold_tr.final)
        constraint = m["max_dd_pct"] >= hold_tr.mdd * 100 * 1.2  # mdd отрицательный
        rows.append({
            "f0": p["f0"], "band": p["band"], "delta": p["delta"],
            "n": p["n"], "micro_lot": p["micro_lot"],
            "micro_max": p["micro_max"],
            **m,
            "dd_within_limit": int(constraint),
            "excess_per_dd": round(m["excess_pp"] / max(1e-9, abs(m["max_dd_pct"])), 4),
        })
        if (j + 1) % 100 == 0:
            print(f"  перебрано {j+1}/{len(cands)}")

    out = os.path.join(RESULTS, "gridrb_sweep.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if r["dd_within_limit"]]
    by_excess = sorted(ok or rows, key=lambda r: r["excess_pp"], reverse=True)
    by_ratio = sorted(ok or rows, key=lambda r: r["excess_per_dd"], reverse=True)

    print(f"\nвсего вариантов: {len(rows)}, прошли ограничение по просадке: {len(ok)}")
    print("\nТОП-10 по excess (в пределах лимита просадки):")
    print_table(by_excess[:10])
    print("\nТОП-10 по excess/|DD|:")
    print_table(by_ratio[:10])

    # лучший на train + контроль на test и на полном окне
    bests = []
    for label, sel in (("best_excess", by_excess[0]),
                       ("best_ratio", by_ratio[0])):
        p = {k: sel[k] for k in GRID}
        # приводим типы
        p["n"] = int(p["n"])
        p["micro_max"] = int(p["micro_max"])
        te = evaluate(p, test, hold_te.final)
        full = evaluate(p, bars, buy_and_hold(bars, CASH).final)
        bests.append({
            "variant": label, **{k: sel[k] for k in GRID},
            "train_excess_pp": sel["excess_pp"],
            "train_dd_pct": sel["max_dd_pct"],
            "test_excess_pp": te["excess_pp"],
            "test_dd_pct": te["max_dd_pct"],
            "test_final_usd": te["final_usd"],
            "full_excess_pp": full["excess_pp"],
            "full_dd_pct": full["max_dd_pct"],
            "full_final_usd": full["final_usd"],
            "full_trades": full["trades"],
        })

    # контроль spec-вариантов на train/test/full
    for label, p in (("spec_70_30", SPEC_VARIANTS[0]),
                     ("spec_80_20", SPEC_VARIANTS[1])):
        tr = evaluate(p, train, hold_tr.final)
        te = evaluate(p, test, hold_te.final)
        full = evaluate(p, bars, buy_and_hold(bars, CASH).final)
        bests.append({
            "variant": label, **p,
            "train_excess_pp": tr["excess_pp"],
            "train_dd_pct": tr["max_dd_pct"],
            "test_excess_pp": te["excess_pp"],
            "test_dd_pct": te["max_dd_pct"],
            "test_final_usd": te["final_usd"],
            "full_excess_pp": full["excess_pp"],
            "full_dd_pct": full["max_dd_pct"],
            "full_final_usd": full["final_usd"],
            "full_trades": full["trades"],
        })

    out2 = os.path.join(RESULTS, "gridrb_best.csv")
    with open(out2, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(bests[0].keys()))
        w.writeheader()
        w.writerows(bests)
    print("\nИтоговые кандидаты (train -> test -> full):")
    print(f"{'variant':<16}{'f0':>5}{'band':>6}{'delta':>7}{'n':>3}"
          f"{'mlot':>6}{'mmax':>5}{'tr_ex':>7}{'te_ex':>7}{'full_ex':>8}"
          f"{'fullDD':>8}{'trades':>7}")
    print("-" * 90)
    for b in bests:
        print(f"{b['variant']:<16}{b['f0']:>5.2f}{b['band']:>6.3f}{b['delta']:>7.4f}"
              f"{int(b['n']):>3}{b['micro_lot']:>6.2f}{int(b['micro_max']):>5}"
              f"{b['train_excess_pp']:>7.1f}{b['test_excess_pp']:>7.1f}"
              f"{b['full_excess_pp']:>8.1f}{b['full_dd_pct']:>8.1f}"
              f"{b['full_trades']:>7}")
    print(f"\nCSV: {out}\nCSV: {out2}")


def print_table(rows):
    print(f"{'f0':>5}{'band':>6}{'delta':>7}{'n':>3}{'mlot':>6}{'mmax':>5}"
          f"{'final':>9}{'excess':>8}{'DD%':>8}{'trades':>7}")
    for r in rows:
        print(f"{r['f0']:>5.2f}{r['band']:>6.3f}{r['delta']:>7.4f}{r['n']:>3}"
              f"{r['micro_lot']:>6.2f}{r['micro_max']:>5}{r['final_usd']:>9.0f}"
              f"{r['excess_pp']:>8.1f}{r['max_dd_pct']:>8.1f}{r['trades']:>7}")


if __name__ == "__main__":
    main()