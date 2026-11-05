#!/usr/bin/env python3
"""Оптимизация параметров БОЕВОГО движка GridRB на H1-данных (по умолчанию SOL).

Отличия от gridrb_sweep.py:
  - гоняется не копия логики (tests/backtest/strategies.py), а тот же класс
    modules.gridrb_strategy.GridRBEngine, что работает в бою (через
    minute_replay.py) — так оптимум не «съезжает» из-за дрейфа копии;
  - учитываются ранняя докупка (early_lot) и гистерезис перестановки (rearm_eps);
  - подбор идёт на обучающем окне, проверка — на отдельном тестовом
    (out-of-sample) и на полном окне.

Методика:
  - окно 70/30: train — первые 70 % баров, test — последние 30 %. На тесте
    каждый прогон стартует заново с $10 000;
  - случайный поиск N конфигураций (seed фиксирован) + покоординатное уточнение
    лучших (тоже через пул процессов);
  - относительный критерий (как в spec §4): excess vs hold при
    maxDD_стратегии <= 1.2 * maxDD_hold. Дополнительно считаем Calmar
    (доходность / |просадка|).

ВНИМАНИЕ (общая оговорка бэктеста): модель исполнения считает, что ордер
исполнен, как только бар коснулся его цены. В реальности есть очередь мейкера,
поэтому число исполнений завышено, особенно на волатильных монетах (SOL).
Результаты ниже — верхняя оптимистичная граница, а не гарантия.

Запуск:
    ./venv/bin/python tests/backtest/optimize_live.py --coin SOL --n 600 --workers 3
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import minute_replay as mr  # noqa: E402
from modules.gridrb_strategy import GridRBConfig  # noqa: E402

CASH = 10_000.0
SPLIT = 0.70
COOLDOWN = 300.0
MIN_NOTIONAL = 10.0
SZ = 5  # десятичные знаки размера (USOL: 3, UBTC: 5)
PENT = 0.0  # «протыкание» уровня для исполнения лимита (см. engine.py)

GRID = {
    "f0": [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
    "band": [0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.08],
    "delta": [0.0025, 0.003, 0.004, 0.005, 0.006, 0.007, 0.010],
    "n": [2, 3, 4, 5, 6, 7, 8],
    "micro_lot": [0.0, 0.01, 0.02, 0.03, 0.04],
    "micro_max": [2, 3, 4, 5],
    "early_lot": [0.0, 0.10, 0.15, 0.25],
    "rearm_eps": [0.0, 0.001, 0.002, 0.005],
}

LIVE = {"f0": 0.80, "band": 0.04, "delta": 0.004, "n": 5,
        "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15, "rearm_eps": 0.002}
VARIANTS = [
    ("spec_70_30", {"f0": 0.70, "band": 0.05, "delta": 0.005, "n": 5,
                    "micro_lot": 0.02, "micro_max": 3, "early_lot": 0.15,
                    "rearm_eps": 0.002}),
    ("spec_80_20", dict(LIVE)),
    ("no_micro", {**LIVE, "micro_lot": 0.0}),
    ("no_early", {**LIVE, "early_lot": 0.0}),
    ("passthrough", {**LIVE, "f0": 1.0, "band": 0.0, "early_lot": 0.0,
                     "micro_lot": 0.0}),
]

_G: dict = {}


def _init_worker(path: str, sz: int, pent: float) -> None:
    bars = mr.load_bars(path)
    k = int(len(bars) * SPLIT)
    _G["train"] = bars[:k]
    _G["test"] = bars[k:]
    _G["full"] = bars
    _G["sz"] = sz
    _G["pent"] = pent


def _cfg(p: dict) -> GridRBConfig:
    return GridRBConfig(min_notional=MIN_NOTIONAL, rearm_cooldown=COOLDOWN, **p)


def _work(p: dict):
    """Обучающее окно: возвращает (params, result)."""
    return p, mr.evaluate(_G["train"], _cfg(p), cash=CASH,
                          sz_decimals=_G["sz"], penetration=_G["pent"])


def score(r: mr.ReplayResult) -> float:
    """Скаляр для отбора: excess vs hold, но со штрафом за превышение просадки.

    10 п.п. штрафа за каждый 1 п.п. просадки сверх лимита 1.2*maxDD_hold.
    """
    limit = 1.2 * abs(r.hold_mdd) * 100
    over = max(0.0, abs(r.mdd) * 100 - limit)
    return r.excess_pp - 10.0 * over


def make_candidates(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    out: list[dict] = []
    seen: set = set()

    def add(p: dict) -> None:
        key = tuple(p[k] for k in GRID)
        if key in seen:
            return
        seen.add(key)
        out.append(dict(p))

    for _, p in VARIANTS:
        add(p)
    while len(out) < n:
        add({k: rng.choice(v) for k, v in GRID.items()})
    return out


def refine(ex, base: dict, rounds: int = 3):
    """Покоординатное уточнение на обучающем окне через пул процессов."""
    best = dict(base)
    best_r = _work(best)[1]
    for _ in range(rounds):
        trials = []
        for key, vals in GRID.items():
            for v in vals:
                if v == best[key]:
                    continue
                t = dict(best)
                t[key] = v
                trials.append(t)
        if not trials:
            break
        improved = False
        for t, r in ex.map(_work, trials, chunksize=4):
            if score(r) > score(best_r):
                best, best_r, improved = t, r, True
        if not improved:
            break
    return best, best_r


def fmt(p: dict) -> str:
    return (f"f0={p['f0']:.2f} band={p['band']:.3f} "
            f"delta={p['delta']*1000:.1f}‰ n={p['n']:.0f} "
            f"micro={p['micro_lot']:.2f}×{p['micro_max']:.0f} "
            f"early={p['early_lot']:.2f} eps={p['rearm_eps']*1000:.1f}‰")


def row(name: str, p: dict, tr, te, full) -> dict:
    def cal(r):
        return round(r.ret / abs(r.mdd), 3) if r.mdd else 0.0
    return {
        "variant": name, **p,
        "train_final": round(tr.final, 2), "train_ret_pct": round(tr.ret*100, 2),
        "train_dd_pct": round(tr.mdd*100, 2), "train_excess_pp": round(tr.excess_pp, 2),
        "train_calmar": cal(tr),
        "test_final": round(te.final, 2), "test_ret_pct": round(te.ret*100, 2),
        "test_dd_pct": round(te.mdd*100, 2), "test_excess_pp": round(te.excess_pp, 2),
        "test_calmar": cal(te),
        "full_final": round(full.final, 2), "full_ret_pct": round(full.ret*100, 2),
        "full_dd_pct": round(full.mdd*100, 2), "full_excess_pp": round(full.excess_pp, 2),
        "full_calmar": cal(full), "full_fills": full.fills,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Оптимизация GridRB на боевом движке")
    ap.add_argument("--coin", default="SOL")
    ap.add_argument("--n", type=int, default=600, help="число случайных кандидатов")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--rounds", type=int, default=3, help="раунды уточнения")
    ap.add_argument("--seeds", type=int, default=3, help="сколько лучших уточнять")
    ap.add_argument("--sz-decimals", type=int, default=None)
    ap.add_argument("--penetration", type=float, default=0.0,
                    help="насколько цена должна уйти за уровень лимита (доля)")
    args = ap.parse_args()

    path = os.path.join(HERE, "data", f"{args.coin.lower()}_h1_3y.csv")
    if not os.path.exists(path):
        raise SystemExit(f"нет файла данных: {path}")
    global SZ, PENT
    SZ = args.sz_decimals if args.sz_decimals is not None else (
        3 if args.coin.upper() in ("SOL", "AVAX") else 5)
    PENT = args.penetration
    _init_worker(path, SZ, PENT)  # грузим бары и в родителе (уточнение, финалисты)

    cands = make_candidates(args.n, args.seed)
    print(f"монета={args.coin} sz_decimals={SZ} penetration={PENT*100:.3f}% "
          f"кандидатов={len(cands)} воркеров={args.workers}", flush=True)

    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker,
                             initargs=(path, SZ, PENT)) as ex:
        results = []
        for j, (p, r) in enumerate(ex.map(_work, cands, chunksize=4), 1):
            results.append((p, r))
            if j % 50 == 0:
                print(f"  ... {j}/{len(cands)}", flush=True)

        results.sort(key=lambda pr: score(pr[1]), reverse=True)
        hold_tr = results[0][1]
        print(f"\ntrain: {hold_tr.bars} бар | hold ${hold_tr.hold_final:,.0f} "
              f"({hold_tr.hold_ret*100:+.1f}%, DD {hold_tr.hold_mdd*100:.1f}%)",
              flush=True)

        print("\nТОП-10 (train, по score):")
        print(f"{'ok':>3} {'params':<62}{'final':>9}{'excess':>8}{'DD%':>8}")
        for p, r in results[:10]:
            ok = abs(r.mdd) <= 1.2 * abs(r.hold_mdd)
            print(f"{'+' if ok else 'x':>3} {fmt(p):<62}"
                  f"{r.final:>9,.0f}{r.excess_pp:>8.1f}{r.mdd*100:>8.1f}")

        seeds = [p for p, _ in results[:args.seeds]]
        refined = []
        for s in seeds:
            b, br_ = refine(ex, s, rounds=args.rounds)
            refined.append((b, br_))
            print(f"\nуточнено: {fmt(b)} | train excess {br_.excess_pp:+.1f} "
                  f"DD {br_.mdd*100:.1f}%", flush=True)

    finalists: list[tuple[str, dict]] = []
    seen: set = set()

    def push(name: str, p: dict) -> None:
        key = tuple(round(float(p[k]), 6) for k in GRID)
        if key in seen:
            return
        seen.add(key)
        finalists.append((name, dict(p)))

    for nm, p in VARIANTS:
        push(nm, p)
    for i, (p, _) in enumerate(results[:5]):
        push(f"rand{i+1}", p)
    for i, (p, _) in enumerate(refined):
        push(f"refined{i+1}", p)
    by_cal = sorted(results, key=lambda pr: (pr[1].ret / abs(pr[1].mdd)
                                             if pr[1].mdd else 0), reverse=True)
    for i, (p, _) in enumerate(by_cal[:5]):
        push(f"calmar{i+1}", p)

    rows = []
    print(f"\n{'variant':<12}{'tr_ex':>8}{'te_ex':>8}{'teDD':>8}"
          f"{'full_ex':>9}{'fullDD':>8}{'fullCalm':>10}  params")
    print("-" * 128)
    for name, p in finalists:
        tr = mr.evaluate(_G["train"], _cfg(p), cash=CASH, sz_decimals=SZ,
                         penetration=PENT)
        te = mr.evaluate(_G["test"], _cfg(p), cash=CASH, sz_decimals=SZ,
                         penetration=PENT)
        fu = mr.evaluate(_G["full"], _cfg(p), cash=CASH, sz_decimals=SZ,
                         penetration=PENT)
        rows.append(row(name, p, tr, te, fu))
        print(f"{name:<12}{tr.excess_pp:>8.1f}{te.excess_pp:>8.1f}"
              f"{te.mdd*100:>8.1f}{fu.excess_pp:>9.1f}{fu.mdd*100:>8.1f}"
              f"{(fu.ret/abs(fu.mdd) if fu.mdd else 0):>10.2f}  {fmt(p)}")

    res_dir = os.path.join(HERE, "results")
    os.makedirs(res_dir, exist_ok=True)
    tag = f"{args.coin.lower()}" + (f"_pen{int(round(PENT*1e6))}" if PENT else "")
    all_csv = os.path.join(res_dir, f"optimize_{tag}_train.csv")
    with open(all_csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "score", *GRID.keys(), "final", "ret_pct",
                    "dd_pct", "excess_pp", "calmar", "dd_ok"])
        for i, (p, r) in enumerate(results, 1):
            w.writerow([i, round(score(r), 3), *[p[k] for k in GRID],
                        round(r.final, 2), round(r.ret*100, 2),
                        round(r.mdd*100, 2), round(r.excess_pp, 2),
                        round(r.ret/abs(r.mdd), 3) if r.mdd else 0.0,
                        int(abs(r.mdd) <= 1.2 * abs(r.hold_mdd))])

    best_csv = os.path.join(res_dir, f"optimize_{tag}_best.csv")
    with open(best_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"\nCSV: {all_csv}\nCSV: {best_csv}")


if __name__ == "__main__":
    main()
