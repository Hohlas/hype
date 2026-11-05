#!/usr/bin/env python3
"""Проверка GridRB на скользящих 180-дневных окнах (SOL по умолчанию).

Идея (как в REPORT_gridrb_3y.md §3–4, но на боевом движке): рынок режется на
независимые окна по 180 дней (4 320 H1-баров) с шагом 14 дней. В каждом окне
стратегия и hold стартуют заново с $10 000. Смотрим excess vs hold в разрезе
режима окна (падение/флэт/рост) и устойчивость: корреляцию «первая часть окна
(выбор)» vs «последняя (проверка)» по набору конфигураций.

Запуск:
    ./venv/bin/python tests/backtest/sol_rolling.py --coin SOL --workers 3
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import minute_replay as mr  # noqa: E402
from modules.gridrb_strategy import GridRBConfig  # noqa: E402

CASH = 10_000.0
WINDOW = 180 * 24      # 4320 H1-баров
STEP = 14 * 24         # 336 H1-баров
WALK = 0.60            # доля окон для «выбора»

# Те же конфигурации, что в переборе + боевая.
CONFIGS: dict[str, dict] = {
    "spec_80_20":  {"f0": 0.80, "band": 0.040, "delta": 0.004, "n": 5,
                    "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15,
                    "rearm_eps": 0.002},
    "spec_70_30":  {"f0": 0.70, "band": 0.050, "delta": 0.005, "n": 5,
                    "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15,
                    "rearm_eps": 0.002},
    "no_micro":    {"f0": 0.80, "band": 0.040, "delta": 0.004, "n": 5,
                    "micro_lot": 0.00, "micro_max": 3, "early_lot": 0.15,
                    "rearm_eps": 0.002},
    "no_early":    {"f0": 0.80, "band": 0.040, "delta": 0.004, "n": 5,
                    "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.00,
                    "rearm_eps": 0.002},
    "rand1":       {"f0": 0.65, "band": 0.040, "delta": 0.006, "n": 3,
                    "micro_lot": 0.02, "micro_max": 5, "early_lot": 0.25,
                    "rearm_eps": 0.000},
    "rand2":       {"f0": 0.80, "band": 0.020, "delta": 0.005, "n": 6,
                    "micro_lot": 0.04, "micro_max": 2, "early_lot": 0.15,
                    "rearm_eps": 0.001},
    "rand3":       {"f0": 0.70, "band": 0.050, "delta": 0.007, "n": 2,
                    "micro_lot": 0.00, "micro_max": 3, "early_lot": 0.25,
                    "rearm_eps": 0.001},
    "rand4":       {"f0": 0.80, "band": 0.030, "delta": 0.006, "n": 5,
                    "micro_lot": 0.02, "micro_max": 4, "early_lot": 0.25,
                    "rearm_eps": 0.005},
    "rand5":       {"f0": 0.90, "band": 0.020, "delta": 0.003, "n": 2,
                    "micro_lot": 0.03, "micro_max": 5, "early_lot": 0.25,
                    "rearm_eps": 0.002},
    "refined3":    {"f0": 0.70, "band": 0.050, "delta": 0.007, "n": 2,
                    "micro_lot": 0.04, "micro_max": 3, "early_lot": 0.25,
                    "rearm_eps": 0.001},
    "calmar3":     {"f0": 0.65, "band": 0.050, "delta": 0.006, "n": 5,
                    "micro_lot": 0.03, "micro_max": 3, "early_lot": 0.25,
                    "rearm_eps": 0.000},
    "calmar5":     {"f0": 0.50, "band": 0.015, "delta": 0.005, "n": 5,
                    "micro_lot": 0.03, "micro_max": 3, "early_lot": 0.25,
                    "rearm_eps": 0.005},
    "low_f0":      {"f0": 0.50, "band": 0.040, "delta": 0.004, "n": 5,
                    "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15,
                    "rearm_eps": 0.002},
    "mid_f0":      {"f0": 0.60, "band": 0.040, "delta": 0.004, "n": 5,
                    "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15,
                    "rearm_eps": 0.002},
    "high_f0":     {"f0": 0.90, "band": 0.040, "delta": 0.004, "n": 5,
                    "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15,
                    "rearm_eps": 0.002},
    "wide_delta":  {"f0": 0.80, "band": 0.040, "delta": 0.010, "n": 5,
                    "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15,
                    "rearm_eps": 0.002},
    "narrow_delta": {"f0": 0.80, "band": 0.040, "delta": 0.0025, "n": 5,
                     "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15,
                     "rearm_eps": 0.002},
    "many_levels": {"f0": 0.80, "band": 0.040, "delta": 0.004, "n": 8,
                    "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15,
                    "rearm_eps": 0.002},
}

_G: dict = {}
_SZ = 3


def _init_worker(path: str, sz: int) -> None:
    global _SZ
    _SZ = sz
    _G["bars"] = mr.load_bars(path)


def _work(task):
    name, params, w0, w1 = task
    seg = _G["bars"][w0:w1]
    cfg = GridRBConfig(rearm_cooldown=300.0, **params)
    r = mr.evaluate(seg, cfg, cash=CASH, sz_decimals=_SZ)
    return (name, w0, r.ret, r.hold_ret, r.mdd, r.hold_mdd, r.excess_pp)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="SOL")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    path = os.path.join(HERE, "data", f"{args.coin.lower()}_h1_3y.csv")
    bars = mr.load_bars(path)
    n_windows = (len(bars) - WINDOW) // STEP + 1
    starts = [k * STEP for k in range(n_windows)]
    print(f"монета={args.coin} баров={len(bars)} окон={n_windows} "
          f"({WINDOW//24} дней, шаг {STEP//24})", flush=True)

    tasks = [(name, p, s, s + WINDOW)
             for name, p in CONFIGS.items() for s in starts]
    sz = 3 if args.coin.upper() in ("SOL", "AVAX") else 5
    _init_worker(path, sz)
    rows = []
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker,
                             initargs=(path, sz)) as ex:
        for j, res in enumerate(ex.map(_work, tasks, chunksize=8), 1):
            rows.append(res)
            if j % 200 == 0:
                print(f"  ... {j}/{len(tasks)}", flush=True)

    # агрегация
    def regime(hret: float) -> str:
        if hret < -0.10:
            return "падение"
        if hret > 0.10:
            return "рост"
        return "флэт"

    idx = {name: [] for name in CONFIGS}
    for name, w0, ret, hret, mdd, hmdd, ex in rows:
        idx[name].append((w0, ret, hret, mdd, hmdd, ex))
    for name in idx:
        idx[name].sort()

    header = (f"{'config':<15}{'mean_ex':>8}{'median':>8}{'win%':>7}{'meanDD':>8}"
              f"{'падение':>9}{'флэт':>8}{'рост':>8}")
    print("\n" + header)
    print("-" * len(header))
    summary = []
    for name in CONFIGS:
        d = idx[name]
        exs = [x[5] for x in d]
        dds = [x[3] * 100 for x in d]
        by = {"падение": [], "флэт": [], "рост": []}
        for x in d:
            by[regime(x[2])].append(x[5])
        mean = statistics.fmean(exs)
        med = statistics.median(exs)
        win = 100.0 * sum(1 for e in exs if e > 0) / len(exs)
        rec = {
            "config": name, "mean_excess": round(mean, 2),
            "median_excess": round(med, 2), "win_pct": round(win, 1),
            "mean_dd_pct": round(statistics.fmean(dds), 1),
            "fall": round(statistics.fmean(by["падение"]), 2) if by["падение"] else 0.0,
            "flat": round(statistics.fmean(by["флэт"]), 2) if by["флэт"] else 0.0,
            "rise": round(statistics.fmean(by["рост"]), 2) if by["рост"] else 0.0,
            "n_fall": len(by["падение"]), "n_flat": len(by["флэт"]),
            "n_rise": len(by["рост"]),
        }
        summary.append(rec)
        print(f"{name:<15}{rec['mean_excess']:>8.2f}{rec['median_excess']:>8.2f}"
              f"{rec['win_pct']:>7.1f}{rec['mean_dd_pct']:>8.1f}"
              f"{rec['fall']:>9.2f}{rec['flat']:>8.2f}{rec['rise']:>8.2f}")

    # walk-forward: корреляция «выбор» vs «проверка» по конфигурациям
    k = int(n_windows * WALK)
    part1 = {name: [x[5] for x in idx[name] if x[0] < starts[k]] for name in CONFIGS}
    part2 = {name: [x[5] for x in idx[name] if x[0] >= starts[k]] for name in CONFIGS}
    xs = [statistics.fmean(part1[n]) for n in CONFIGS]
    ys = [statistics.fmean(part2[n]) for n in CONFIGS]
    corr = _pearson(xs, ys)
    print(f"\nwalk-forward: окон выбора {k}, проверки {n_windows - k}")
    print(f"корреляция mean_excess (выбор vs проверка) по {len(CONFIGS)} "
          f"конфигурациям: {corr:+.3f}")
    b1 = statistics.fmean(xs)
    b2 = statistics.fmean(ys)
    print(f"средний excess: выбор {b1:+.2f} п.п., проверка {b2:+.2f} п.п.")

    res_dir = os.path.join(HERE, "results")
    os.makedirs(res_dir, exist_ok=True)
    win_csv = os.path.join(res_dir, f"{args.coin.lower()}_rolling_windows.csv")
    with open(win_csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["config", "start_bar", "strat_ret_pct", "hold_ret_pct",
                    "strat_dd_pct", "hold_dd_pct", "excess_pp", "regime"])
        for name in CONFIGS:
            for x in idx[name]:
                w.writerow([name, x[0], round(x[1]*100, 2), round(x[2]*100, 2),
                            round(x[3]*100, 2), round(x[4]*100, 2),
                            round(x[5], 2), regime(x[2])])
    sum_csv = os.path.join(res_dir, f"{args.coin.lower()}_rolling_summary.csv")
    with open(sum_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)
    print(f"\nCSV: {win_csv}\nCSV: {sum_csv}")


def _pearson(a: list[float], b: list[float]) -> float:
    if len(a) < 2:
        return 0.0
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


if __name__ == "__main__":
    main()
