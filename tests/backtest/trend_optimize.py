#!/usr/bin/env python3
"""Оптимизация Trend-Adaptive Grid на 3-летних H1-данных BTC и сравнение с GridRB.

Модель исполнения — общая: tests/backtest/engine.py (maker 0.040 %, taker 0.070 %,
slip 2 б.п., лимит исполняется при касании уровня). Стратегии гоняются одним и
тем же драйвером (`run_backtest.run_instance`), поэтому сравнение «яблоки к
яблокам».

Схема:
  - окно 2023-11-01 → 2026-11-01 (26 305 баров H1), старт $10 000;
  - train — первые 70 %, test — последние 30 % (out-of-sample, старт заново);
  - полный перебор по сетке параметров TrendGrid на train, затем финалисты
    проверяются на test и полном окне;
  - критерий (как в spec §4): максимум excess к hold при
    maxDD ≤ 1.2·maxDD_hold; дополнительно Calmar;
  - для сравнения на полном окне считаются GridRB 70/30 и 80/20 (порт из
    strategies.py, тот же движок) и боевой GridRB (minute_replay, 80/20 + early).

Запуск:
    ./venv/bin/python tests/backtest/trend_optimize.py --workers 3
"""
from __future__ import annotations

import argparse
import csv
import itertools
import os
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import engine as bt  # noqa: E402
from run_backtest import run_instance  # noqa: E402
from strategies import GridRB, TrendAdaptiveGrid  # noqa: E402

CASH = 10_000.0
SPLIT = 0.70
DATA = os.path.join(HERE, "data", "btc_h1_3y.csv")

# Сетка параметров TrendAdaptiveGrid (см. strategies.py).
GRID = {
    "STEP": [250.0, 500.0, 750.0, 1000.0, 1500.0, 2000.0],
    "q": [0.03, 0.05, 0.08, 0.10],
    "core_pct": [0.30, 0.40, 0.50, 0.60, 0.70],
    "target": [0.60, 0.70, 0.80],
    "dip_pct": [0.05, 0.10, 0.15],
    "dip_usd_pct": [0.05, 0.10, 0.20],
}
DEFAULT = {"STEP": 500.0, "q": 0.05, "core_pct": 0.50, "target": 0.70,
           "dip_pct": 0.10, "dip_usd_pct": 0.10}

_G: dict = {}


def _init_worker(path: str) -> None:
    bars = bt.load_bars(path)
    k = int(len(bars) * SPLIT)
    _G["train"] = bars[:k]
    _G["test"] = bars[k:]
    _G["full"] = bars
    _G["hold_train"] = bt.buy_and_hold(_G["train"], CASH)
    _G["hold_test"] = bt.buy_and_hold(_G["test"], CASH)
    _G["hold_full"] = bt.buy_and_hold(_G["full"], CASH)


def _metrics(res, hold) -> dict:
    ret = res.final / CASH - 1
    hret = hold.final / CASH - 1
    return {
        "final": res.final, "ret": ret, "mdd": res.mdd,
        "hold_final": hold.final, "hold_ret": hret, "hold_mdd": hold.mdd,
        "excess_pp": (ret - hret) * 100,
        "calmar": ret / abs(res.mdd) if res.mdd else 0.0,
        "trades": res.trades, "fees": res.fees,
    }


def _run(bars, hold, params) -> dict:
    strat = TrendAdaptiveGrid(CASH, **params)
    return _metrics(run_instance(strat, bars), hold)


def _work(params: dict):
    """Перебор на train: возвращает (params, metrics)."""
    return params, _run(_G["train"], _G["hold_train"], params)


def _score(m: dict) -> float:
    limit = 1.2 * abs(m["hold_mdd"]) * 100
    over = max(0.0, abs(m["mdd"]) * 100 - limit)
    return m["excess_pp"] - 10.0 * over


def _candidates() -> list[dict]:
    keys = list(GRID)
    out = [dict(DEFAULT)]
    for vals in itertools.product(*(GRID[k] for k in keys)):
        out.append(dict(zip(keys, vals)))
    return out


def fmt(p: dict) -> str:
    return (f"STEP={p['STEP']:.0f} q={p['q']:.2f} core={p['core_pct']:.2f} "
            f"target={p['target']:.2f} dip={p['dip_pct']:.2f} "
            f"dip$={p['dip_usd_pct']:.2f}")


def _row(name, p, tr, te, fu) -> dict:
    return {
        "variant": name, **p,
        "train_final": round(tr["final"], 2), "train_ret_pct": round(tr["ret"]*100, 2),
        "train_dd_pct": round(tr["mdd"]*100, 2), "train_excess_pp": round(tr["excess_pp"], 2),
        "train_calmar": round(tr["calmar"], 3),
        "test_final": round(te["final"], 2), "test_ret_pct": round(te["ret"]*100, 2),
        "test_dd_pct": round(te["mdd"]*100, 2), "test_excess_pp": round(te["excess_pp"], 2),
        "test_calmar": round(te["calmar"], 3),
        "full_final": round(fu["final"], 2), "full_ret_pct": round(fu["ret"]*100, 2),
        "full_dd_pct": round(fu["mdd"]*100, 2), "full_excess_pp": round(fu["excess_pp"], 2),
        "full_calmar": round(fu["calmar"], 3), "full_trades": fu["trades"],
        "full_fees": round(fu["fees"], 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    cands = _candidates()
    print(f"данные: {DATA}\nкандидатов: {len(cands)}", flush=True)

    _init_worker(DATA)
    hold_tr = _G["hold_train"]
    print(f"train: {len(_G['train'])} бар | hold ${hold_tr.final:,.0f} "
          f"({(hold_tr.final/CASH-1)*100:+.1f}%, DD {hold_tr.mdd*100:.1f}%)",
          flush=True)

    results = []
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker,
                             initargs=(DATA,)) as ex:
        for j, (p, m) in enumerate(ex.map(_work, cands, chunksize=16), 1):
            results.append((p, m))
            if j % 200 == 0:
                print(f"  ... {j}/{len(cands)}", flush=True)

    results.sort(key=lambda pm: _score(pm[1]), reverse=True)
    print("\nТОП-10 (train, по score):")
    print(f"{'ok':>3} {'params':<64}{'final':>9}{'excess':>8}{'DD%':>8}")
    for p, m in results[:10]:
        ok = abs(m["mdd"]) <= 1.2 * abs(m["hold_mdd"])
        print(f"{'+' if ok else 'x':>3} {fmt(p):<64}"
              f"{m['final']:>9,.0f}{m['excess_pp']:>8.1f}{m['mdd']*100:>8.1f}")

    # финалисты: дефолт, топ-8 по score, топ-5 по Calmar
    finalists: list[tuple[str, dict]] = [("default", dict(DEFAULT))]
    seen = {tuple(DEFAULT[k] for k in GRID)}
    for i, (p, _) in enumerate(results[:8]):
        key = tuple(p[k] for k in GRID)
        if key not in seen:
            seen.add(key)
            finalists.append((f"rand{i+1}", p))
    by_cal = sorted(results, key=lambda pm: pm[1]["calmar"], reverse=True)
    for i, (p, _) in enumerate(by_cal[:5]):
        key = tuple(p[k] for k in GRID)
        if key not in seen:
            seen.add(key)
            finalists.append((f"calmar{i+1}", p))

    rows = []
    print(f"\n{'variant':<10}{'tr_ex':>8}{'te_ex':>8}{'teDD':>8}"
          f"{'full_ex':>9}{'fullDD':>8}{'fullCalm':>10}{'fullRet':>9}  params")
    print("-" * 140)
    for name, p in finalists:
        tr = _run(_G["train"], _G["hold_train"], p)
        te = _run(_G["test"], _G["hold_test"], p)
        fu = _run(_G["full"], _G["hold_full"], p)
        rows.append(_row(name, p, tr, te, fu))
        print(f"{name:<10}{tr['excess_pp']:>8.1f}{te['excess_pp']:>8.1f}"
              f"{te['mdd']*100:>8.1f}{fu['excess_pp']:>9.1f}{fu['mdd']*100:>8.1f}"
              f"{fu['calmar']:>10.2f}{fu['ret']*100:>8.1f}%  {fmt(p)}")

    # ---- сравнение с GridRB на полном окне (тот же движок) ----
    print("\n=== Сравнение на полном 3-летнем окне (общий движок) ===")
    hold_full = _G["hold_full"]
    print(f"{'strategy':<34}{'final':>10}{'ret%':>8}{'excess':>8}{'DD%':>8}{'trades':>8}")
    print("-" * 78)

    def line(name, m):
        print(f"{name:<34}{m['final']:>10,.0f}{m['ret']*100:>8.1f}"
              f"{m['excess_pp']:>8.1f}{m['mdd']*100:>8.1f}{m['trades']:>8}")

    line("Buy&Hold", _metrics(hold_full, hold_full))
    for name, p in (("GridRB 70/30 (spec, порт)", {"f0": 0.70, "band": 0.05,
                                                   "delta": 0.005, "n": 5,
                                                   "micro_lot": 0.02,
                                                   "micro_max": 3}),
                    ("GridRB 80/20 (spec, порт)", {"f0": 0.80, "band": 0.04,
                                                   "delta": 0.004, "n": 5,
                                                   "micro_lot": 0.02,
                                                   "micro_max": 3})):
        m = _metrics(run_instance(GridRB(CASH, **p), _G["full"]), hold_full)
        line(name, m)
    for name, p in (("TrendGrid (default)", dict(DEFAULT)),
                    (f"TrendGrid ({finalists[1][0] if len(finalists) > 1 else 'default'}, лучший train)",
                     finalists[1][1] if len(finalists) > 1 else dict(DEFAULT))):
        m = _metrics(run_instance(TrendAdaptiveGrid(CASH, **p), _G["full"]), hold_full)
        line(name, m)

    # боевой GridRB (другой драйвер, та же модель исполнения) — для справки
    try:
        import minute_replay as mr
        from modules.gridrb_strategy import GridRBConfig
        rb = mr.load_bars(DATA)
        cfg = GridRBConfig.from_settings()
        live = mr.evaluate(rb, cfg, cash=CASH, sz_decimals=5)
        print(f"{'GridRB боевой (80/20+early)':<34}{live.final:>10,.0f}"
              f"{live.ret*100:>8.1f}{live.excess_pp:>8.1f}"
              f"{live.mdd*100:>8.1f}{live.fills:>8}")
    except Exception as e:  # noqa: BLE001
        print(f"(боевой GridRB не посчитан: {e})")

    res_dir = os.path.join(HERE, "results")
    os.makedirs(res_dir, exist_ok=True)
    all_csv = os.path.join(res_dir, "trend_grid_train.csv")
    with open(all_csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "score", *GRID.keys(), "final", "ret_pct",
                    "dd_pct", "excess_pp", "calmar", "dd_ok"])
        for i, (p, m) in enumerate(results, 1):
            w.writerow([i, round(_score(m), 3), *[p[k] for k in GRID],
                        round(m["final"], 2), round(m["ret"]*100, 2),
                        round(m["mdd"]*100, 2), round(m["excess_pp"], 2),
                        round(m["calmar"], 3),
                        int(abs(m["mdd"]) <= 1.2 * abs(m["hold_mdd"]))])
    best_csv = os.path.join(res_dir, "trend_grid_best.csv")
    with open(best_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV: {all_csv}\nCSV: {best_csv}")


if __name__ == "__main__":
    main()