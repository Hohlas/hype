#!/usr/bin/env python3
"""Скользящие окна 180 дней по 3 годам: сравнение гипотез GridRB.

Окно 180 дней (4320 H1-баров), шаг 14 дней (336 баров), старт каждого окна с
$10 000. Считаются excess vs buy&hold, maxDD, сделки; окна делятся по режиму
(hold за окно): падение < -10%, флэт -10..+10%, рост > +10%.

Выход:
  results/rolling_windows.csv — сырые строки (конфиг x окно)
  results/rolling_summary.csv — агрегаты по конфигам + per-regime
  results/rolling_walkforward.csv — выбор на первых 60% окон, проверка на 40%
"""
from __future__ import annotations

import csv
import math
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import buy_and_hold, load_bars  # noqa: E402
from run_backtest import RESULTS  # noqa: E402
from strategies import GridRB  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_3y.csv")
CASH = 10_000.0
WIN = 180 * 24        # 4320 баров
STEP = 14 * 24        # 336 баров


def window_mdd(eq):
    peak, mdd = -math.inf, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd


def run_window(params, bars):
    s = GridRB(CASH, **params)
    eq = []
    for b in bars:
        s.b.execute_bar(b.i, b.o, b.h, b.l, b.c, s.on_fill)
        s.on_close(b.i, b)
        eq.append(s.b.equity(b.c))
    return eq[-1], window_mdd(eq) * 100, len(s.b.fills)


def configs():
    out = []
    f0s = [0.80, 0.75, 0.70, 0.65]
    base = dict(delta=0.004, n=5, micro_lot=0.02, micro_max=3)
    for f0 in f0s:
        for band in (0.02, 0.03, 0.04, 0.05, 0.06):
            out.append((f"H0 band={band}", {**base, "f0": f0, "band": band}))
    pairs = [(0.02, 0.02), (0.02, 0.04), (0.04, 0.02), (0.04, 0.04),
             (0.03, 0.05), (0.05, 0.03), (0.02, 0.06), (0.06, 0.02)]
    for f0 in f0s:
        for db, tb in pairs:
            out.append((f"H1 defc={db}/trim={tb}",
                        {**base, "f0": f0, "band": 0.04,
                         "defc_band": db, "trim_band": tb}))
    for f0 in f0s:
        for el in (0.05, 0.10, 0.15, 0.20):
            out.append((f"H2 early={el}",
                        {**base, "f0": f0, "band": 0.04, "early_lot": el}))
        for dyn in (-0.4, -0.2, 0.2, 0.4):
            out.append((f"H3 dyn={dyn}",
                        {**base, "f0": f0, "band": 0.04, "dyn_band": dyn}))
        for el in (0.10, 0.20):
            for dyn in (-0.2, -0.4):
                out.append((f"H2+H3 early={el},dyn={dyn}",
                            {**base, "f0": f0, "band": 0.04,
                             "early_lot": el, "dyn_band": dyn}))
    # добавляем f0 в человекочитаемый label
    labeled = []
    for label, p in out:
        labeled.append((f"f0={p['f0']:.2f} {label}", p))
    return labeled


def regime_of(ret_pct):
    if ret_pct < -10:
        return "fall"
    if ret_pct > 10:
        return "rise"
    return "flat"


def main():
    bars = load_bars(DATA)
    windows = []
    i = 0
    while i + WIN <= len(bars):
        w = bars[i:i + WIN]
        hold = buy_and_hold(w, CASH)
        hret = (hold.final / CASH - 1) * 100
        windows.append({
            "idx": len(windows),
            "start": w[0].i,
            "end": w[-1].i,
            "hold_final": hold.final,
            "hold_ret": hret,
            "hold_dd": hold.mdd * 100,
            "regime": regime_of(hret),
            "bars": w,
        })
        i += STEP
    print(f"окон: {len(windows)} | режимы: "
          f"fall={sum(1 for w in windows if w['regime']=='fall')}, "
          f"flat={sum(1 for w in windows if w['regime']=='flat')}, "
          f"rise={sum(1 for w in windows if w['regime']=='rise')}")
    print("диапазон hold за окно: "
          f"{min(w['hold_ret'] for w in windows):+.1f}% .. "
          f"{max(w['hold_ret'] for w in windows):+.1f}%")

    cfgs = configs()
    print(f"конфигов: {len(cfgs)}")

    raw = []
    summary = []
    for ci, (label, params) in enumerate(cfgs):
        exc, dds, trs = [], [], []
        by_reg = {"fall": [], "flat": [], "rise": []}
        for w in windows:
            final, dd, trades = run_window(params, w["bars"])
            e = (final - w["hold_final"]) / CASH * 100
            exc.append(e); dds.append(dd); trs.append(trades)
            by_reg[w["regime"]].append(e)
            raw.append({
                "config": label, "f0": params["f0"],
                "band": params.get("band", ""),
                "trim_band": params.get("trim_band", ""),
                "defc_band": params.get("defc_band", ""),
                "early_lot": params.get("early_lot", ""),
                "dyn_band": params.get("dyn_band", ""),
                "window": w["idx"], "regime": w["regime"],
                "hold_ret_pct": round(w["hold_ret"], 2),
                "excess_pp": round(e, 2), "dd_pct": round(dd, 2),
                "trades": trades,
            })
        reg_mean = {k: (sum(v) / len(v) if v else float("nan"))
                    for k, v in by_reg.items()}
        summary.append({
            "config": label, "f0": params["f0"],
            "band": params.get("band", ""),
            "trim_band": params.get("trim_band", ""),
            "defc_band": params.get("defc_band", ""),
            "early_lot": params.get("early_lot", ""),
            "dyn_band": params.get("dyn_band", ""),
            "mean_excess": round(sum(exc) / len(exc), 2),
            "median_excess": round(st.median(exc), 2),
            "win_rate_pct": round(100 * sum(1 for x in exc if x > 0) / len(exc), 1),
            "mean_dd": round(sum(dds) / len(dds), 2),
            "worst_window_excess": round(min(exc), 2),
            "mean_fall": round(reg_mean["fall"], 2) if by_reg["fall"] else "",
            "mean_flat": round(reg_mean["flat"], 2) if by_reg["flat"] else "",
            "mean_rise": round(reg_mean["rise"], 2) if by_reg["rise"] else "",
            "min_regime": round(min(v for v in reg_mean.values()
                                    if not math.isnan(v)), 2),
            "mean_trades": round(sum(trs) / len(trs), 0),
        })
        if (ci + 1) % 10 == 0:
            print(f"  {ci+1}/{len(cfgs)}")

    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "rolling_windows.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(raw[0].keys()))
        w.writeheader(); w.writerows(raw)
    with open(os.path.join(RESULTS, "rolling_summary.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary[0].keys()))
        w.writeheader(); w.writerows(summary)

    # --- лучшие по разным критериям ---
    def top(key, n=8):
        return sorted(summary, key=lambda r: r[key], reverse=True)[:n]

    def show(title, rows):
        print(f"\n=== {title} ===")
        print(f"{'config':<40}{'mean':>7}{'median':>8}{'win%':>6}{'DD':>7}"
              f"{'fall':>7}{'flat':>7}{'rise':>7}{'minReg':>7}")
        for r in rows:
            print(f"{r['config']:<40}{r['mean_excess']:>7.2f}{r['median_excess']:>8.2f}"
                  f"{r['win_rate_pct']:>6.1f}{r['mean_dd']:>7.1f}"
                  f"{str(r['mean_fall']):>7}{str(r['mean_flat']):>7}"
                  f"{str(r['mean_rise']):>7}{r['min_regime']:>7.2f}")

    show("ТОП по среднему excess", top("mean_excess"))
    show("ТОП по медиане excess", top("median_excess"))
    show("ТОП по maximin (худший режим)", top("min_regime"))
    show("ТОП по win-rate", top("win_rate_pct"))
    show("Лучшие в падении", top("mean_fall", 5))
    show("Лучшие в росте", top("mean_rise", 5))

    # --- walk-forward: выбор на первых 60% окон, проверка на 40% ---
    nwf = int(len(windows) * 0.6)
    train_idx = {w["idx"] for w in windows[:nwf]}
    test_idx = {w["idx"] for w in windows[nwf:]}
    wf = []
    for label, params in cfgs:
        te = [r["excess_pp"] for r in raw
              if r["config"] == label and r["window"] in train_idx]
        tr = [r["excess_pp"] for r in raw
              if r["config"] == label and r["window"] in test_idx]
        wf.append({
            "config": label,
            "train_mean": round(sum(te) / len(te), 2),
            "train_median": round(st.median(te), 2),
            "test_mean": round(sum(tr) / len(tr), 2),
            "test_median": round(st.median(tr), 2),
            "test_win_pct": round(100 * sum(1 for x in tr if x > 0) / len(tr), 1),
        })
    with open(os.path.join(RESULTS, "rolling_walkforward.csv"), "w",
              newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(wf[0].keys()))
        w.writeheader(); w.writerows(wf)

    wf.sort(key=lambda r: r["train_mean"], reverse=True)
    print(f"\n=== Walk-forward: выбрано на {nwf} окнах, проверка на "
          f"{len(windows)-nwf} ===")
    print(f"{'config':<40}{'train':>8}{'test':>8}{'testMed':>9}{'testWin%':>9}")
    for r in wf[:10]:
        print(f"{r['config']:<40}{r['train_mean']:>8.2f}{r['test_mean']:>8.2f}"
              f"{r['test_median']:>9.2f}{r['test_win_pct']:>9.1f}")
    # корреляция train/test
    xs = [r["train_mean"] for r in wf]
    ys = [r["test_mean"] for r in wf]
    mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
    cov = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    vx = sum((x-mx)**2 for x in xs); vy = sum((y-my)**2 for y in ys)
    print(f"\nкорреляция train/test по конфигам: {cov/math.sqrt(vx*vy):.3f}")
    print(f"CSV: {RESULTS}")


if __name__ == "__main__":
    main()