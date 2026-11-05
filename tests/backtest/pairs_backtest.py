#!/usr/bin/env python3
"""Прямой бэктест «сетки на паре» (продал один -> купил другой).

Модель: пара A/B рассматривается как актив A, котируемый в B. Сетка GridRB
работает по отношению p = PA/PB; «кэш» — это монеты B. Каждое исполнение в
отношении соответствует РЕАЛЬНОЙ сделке из двух ног (продать A за USDC,
купить B за USDC, или наоборот), поэтому издержки удваиваются
(stress=2: maker/taker/слippage x2).

Итоговый капитал в USD = (B-единицы) * P_B.

Бенчмарки: держать A, держать B, статическая смесь 50/50, а также обычная
одноактивная сетка GridRB на A и на B.

ОГРАНИЧЕНИЯ (важно):
  - OHLC отношения собран из OHLC обеих монет: h = max(o,c,hA/hB),
    l = min(o,c,lA/lB). Внутрибарное взаимное расположение ног не моделируется.
  - Шорта нет (спот): портфель всегда на ~100 % в двух монетах, поэтому
    абсолютная просадка — это просадка корзины, а не хеджа.
  - Фандинг перпов не моделируется (тут только спот-логика).

Запуск: ./venv/bin/python tests/backtest/pairs_backtest.py
"""
from __future__ import annotations

import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import strategies  # noqa: E402
from engine import Broker, load_bars, max_drawdown  # noqa: E402

# мини-нотионал движка задан в единицах котируемой монеты; для отношения это
# бессмысленно, поэтому считаем нотионал в USD отдельно, а движку разрешаем
# исполнять всё (проверка $10 делается ниже по USD).
engine.MIN_NOTIONAL = 0.0
strategies.MIN_NOTIONAL = 0.0

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RESULTS = os.path.join(HERE, "results")
CASH = 10_000.0


class Bar:
    __slots__ = ("i", "t", "o", "h", "l", "c")

    def __init__(self, i, t, o, h, l, c):
        self.i, self.t, self.o, self.h, self.l, self.c = i, t, o, h, l, c


def load(coin: str) -> dict[int, tuple]:
    path = os.path.join(DATA, f"{coin.lower()}_h1_3y.csv")
    out = {}
    with open(path, newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            import datetime as dt
            ts = dt.datetime.strptime(row["time_utc"], "%Y-%m-%d %H:%M:%S")
            t = int(ts.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            out[t] = (float(row["open"]), float(row["high"]),
                      float(row["low"]), float(row["close"]))
    return out


def equity_mdd(eq: list[float]) -> float:
    return max_drawdown(eq) * 100


def run_single_grid(barsA, f0, band, stress=1.0):
    s = strategies.GridRB(CASH, f0=f0, band=band, delta=0.004, n=5,
                          micro_lot=0.02, micro_max=3, stress=stress)
    eq = []
    for b in barsA:
        s.b.execute_bar(b.i, b.o, b.h, b.l, b.c, s.on_fill)
        s.on_close(b.i, b)
        eq.append(s.b.equity(b.c))
    return eq, len(s.b.fills), s.b.fees_paid


def run_pair_grid(barsA, barsB, f0, band, stress=2.0):
    """Сетка на отношении PA/PB. «Кэш» — монеты B."""
    n = len(barsA)
    rb = []
    for i in range(n):
        a, b = barsA[i], barsB[i]
        oo, co = a.o / b.o, a.c / b.c
        lo = min(oo, co, a.l / b.l)
        hi = max(oo, co, a.h / b.h)
        rb.append(Bar(i, a.t, oo, hi, lo, co))
    PB0 = barsB[0].c
    Wb0 = CASH / PB0
    s = strategies.GridRB(Wb0, f0=f0, band=band, delta=0.004, n=5,
                          micro_lot=0.02, micro_max=3, stress=stress)
    eq, bad = [], 0
    for i in range(n):
        s.b.execute_bar(i, rb[i].o, rb[i].h, rb[i].l, rb[i].c, s.on_fill)
        s.on_close(i, rb[i])
        eq.append(s.b.equity(rb[i].c) * barsB[i].c)
    # проверка: каждое исполнение должно иметь USD-нотионал >= $10
    for f in s.b.fills:
        usd = f.price * f.qty * PB0
        if usd < 10.0:
            bad += 1
    return eq, len(s.b.fills), s.b.fees_paid, bad


def hold(prices: list[float], coin: str):
    eq = [CASH * p / prices[0] for p in prices]
    return eq


def rebalanced_5050(barsA, barsB, band=0.04):
    """Пассивные 50/50, возвращаемые к 50/50 при уходе доли > band.
    Каждый обмен — две ноги (продал один, купил другой), издержки удвоены.
    """
    taker, slip = engine.TAKER, engine.SLIP
    qa = 0.5 * CASH / barsA[0].c
    qb = 0.5 * CASH / barsB[0].c
    eq = []
    for i in range(len(barsA)):
        pa, pb = barsA[i].c, barsB[i].c
        va, vb = qa * pa, qb * pb
        v = va + vb
        eq.append(v)
        if v <= 0:
            continue
        share = va / v
        if abs(share - 0.5) > band:
            delta = 0.5 * v - va          # USD, +: переложить из B в A
            cost = abs(delta) * (2 * taker + 2 * slip)
            # уменьшаем на издержки: тратим cost на обмен
            if delta > 0:
                sell_b = delta / pb       # продаём B ~delta USD
                buy_a = (delta - cost) / pa
                qb -= sell_b
                qa += buy_a
            else:
                sell_a = -delta / pa
                buy_b = (-delta - cost) / pb
                qa -= sell_a
                qb += buy_b
    return eq


def main() -> None:
    pairs = [("BTC", "ETH"), ("ETH", "SOL"), ("AVAX", "LINK"),
             ("BTC", "SOL"), ("ETH", "AVAX"), ("ADA", "LINK")]
    f0s = [0.5, 0.7, 0.8]
    bands = [0.04]
    rows = []
    for ca, cb in pairs:
        da, db = load(ca), load(cb)
        common = sorted(set(da) & set(db))
        barsA = [Bar(i, t, *da[t]) for i, t in enumerate(common)]
        barsB = [Bar(i, t, *db[t]) for i, t in enumerate(common)]
        PA = [b.c for b in barsA]
        PB = [b.c for b in barsB]
        ra = PA[-1] / PA[0] - 1
        rb = PB[-1] / PB[0] - 1
        hA = hold(PA, ca)
        hB = hold(PB, cb)
        h5050 = [0.5 * hA[i] + 0.5 * hB[i] for i in range(len(PA))]
        h_reb = rebalanced_5050(barsA, barsB, 0.04)
        best_single = max(hA[-1], hB[-1])
        # одиночные сетки
        gA, tA, fA = run_single_grid(barsA, 0.8, 0.04)
        gB, tB, fB = run_single_grid(barsB, 0.8, 0.04)
        print(f"\n=== {ca}/{cb}  ({len(common)} баров) "
              f"hold {ca} {ra*100:+.0f}% | hold {cb} {rb*100:+.0f}% ===")
        print(f"{'вариант':<26}{'финал$':>10}{'excess vs holdA':>16}"
              f"{'maxDD%':>9}{'сделок':>8}{'min<10':>8}")
        print(f"{'Hold '+ca:<26}{hA[-1]:>10.0f}{0:>16.1f}"
              f"{equity_mdd(hA):>9.1f}{1:>8}{0:>8}")
        print(f"{'Hold '+cb:<26}{hB[-1]:>10.0f}"
              f"{(hB[-1]-hA[-1])/CASH*100:>16.1f}{equity_mdd(hB):>9.1f}{1:>8}{0:>8}")
        print(f"{'Hold 50/50':<26}{h5050[-1]:>10.0f}"
              f"{(h5050[-1]-hA[-1])/CASH*100:>16.1f}"
              f"{equity_mdd(h5050):>9.1f}{2:>8}{0:>8}")
        print(f"{'Rebal 50/50 band4%':<26}{h_reb[-1]:>10.0f}"
              f"{(h_reb[-1]-hA[-1])/CASH*100:>16.1f}"
              f"{equity_mdd(h_reb):>9.1f}{'':>8}{0:>8}")
        print(f"{'GridRB '+ca+' 80/20':<26}{gA[-1]:>10.0f}"
              f"{(gA[-1]-hA[-1])/CASH*100:>16.1f}{equity_mdd(gA):>9.1f}"
              f"{tA:>8}{0:>8}")
        print(f"{'GridRB '+cb+' 80/20':<26}{gB[-1]:>10.0f}"
              f"{(gB[-1]-hA[-1])/CASH*100:>16.1f}{equity_mdd(gB):>9.1f}"
              f"{tB:>8}{0:>8}")
        for f0 in f0s:
            for band in bands:
                eq, tr, fee, bad = run_pair_grid(barsA, barsB, f0, band)
                rows.append({"pair": f"{ca}/{cb}", "f0": f0, "band": band,
                             "final": round(eq[-1], 0),
                             "excess_vs_A_pp": round(
                                 (eq[-1] - hA[-1]) / CASH * 100, 1),
                             "excess_vs_rebal50_pp": round(
                                 (eq[-1] - h_reb[-1]) / CASH * 100, 1),
                             "excess_vs_best_pp": round(
                                 (eq[-1] - best_single) / CASH * 100, 1),
                             "mdd_pct": round(equity_mdd(eq), 1),
                             "trades": tr, "fees": round(fee, 0),
                             "fills_lt_10usd": bad})
                print(f"{'Пара-сетка '+str(f0)+' band='+str(band):<26}"
                      f"{eq[-1]:>10.0f}{rows[-1]['excess_vs_A_pp']:>16.1f}"
                      f"  (vs rebal50 {rows[-1]['excess_vs_rebal50_pp']:+.1f}, "
                      f"vs лучш. {rows[-1]['excess_vs_best_pp']:+.1f}, "
                      f"DD {rows[-1]['mdd_pct']:.0f}%, сделок {tr})")

    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "pairs_grid_backtest.csv")
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV: {path}")


if __name__ == "__main__":
    main()
