#!/usr/bin/env python3
"""Хеджированная (дельта-нейтральная) пара: лонг A / шорт B на перпах.

На споте шортить нельзя, поэтому «не входить в глубокие просадки» достижимо
только так. Считаем:
  - статическую нейтральную позицию;
  - нейтральную позицию с ребалансировкой к равным ногам (аналог сетки);
  - перенос (фандинг) из реальной истории Hyperliquid;
  - просадку отношения.

Фандинг: API Hyperliquid, data/funding_<coin>_h1_3y.csv (лонг платит, шорт
получает). Издержки перпа: taker 0.045 % (допущение, из документации).

Запуск: ./venv/bin/python tests/backtest/pairs_neutral.py
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CASH = 10_000.0
TAKER = 0.00045     # перп Hyperliquid, допущение


def load_closes(coin: str) -> dict[int, float]:
    path = os.path.join(DATA, f"{coin.lower()}_h1_3y.csv")
    out = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            ts = dt.datetime.strptime(row["time_utc"], "%Y-%m-%d %H:%M:%S")
            t = int(ts.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            out[t] = float(row["close"])
    return out


def load_funding(coin: str) -> dict[int, float]:
    path = os.path.join(DATA, f"funding_{coin.lower()}_h1_3y.csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            ts = dt.datetime.strptime(row["time_utc"], "%Y-%m-%d %H:%M:%S")
            t = int(ts.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            out[t] = float(row["fundingRate"])
    return out


def mdd(eq: list[float]) -> float:
    peak, m = -math.inf, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            m = min(m, v / peak - 1)
    return m * 100


def neutral(coin_a, coin_b, ts, pa, pb, fa, fb, E_frac, band, rebal):
    """E_frac — gross на ногу как доля капитала. band — порог ребаланса."""
    E = E_frac * CASH
    na = E / pa[0]     # лонг A (единиц)
    nb = E / pb[0]     # шорт B (единиц, отрицательное владение)
    cash = CASH
    carry = 0.0
    eq = []
    rebs = 0
    for i in range(len(ts)):
        # фандинг за час: лонг A платит fa, шорт B получает fb
        f_a = fa.get(ts[i], 0.0) if fa else 0.0
        f_b = fb.get(ts[i], 0.0) if fb else 0.0
        carry += -na * pa[i] * f_a + nb * pb[i] * f_b
        v = cash + na * pa[i] - nb * pb[i] + carry
        eq.append(v)
        if rebal and i + 1 < len(ts):
            long_notional = na * pa[i]
            short_notional = nb * pb[i]
            if E > 0 and abs(long_notional - short_notional) / E > band:
                tgt = 0.5 * (long_notional + short_notional)
                d = tgt - long_notional
                na += d / pa[i]
                nb += -d / pb[i]
                cost = abs(d) * 2 * TAKER
                carry -= cost
                rebs += 1
    return eq, rebs


def main() -> None:
    out_rows = []
    for ca, cb in (("ETH", "SOL"), ("BTC", "ETH"), ("BTC", "SOL"),
                   ("AVAX", "LINK")):
        da, db = load_closes(ca), load_closes(cb)
        fa, fb = load_funding(ca), load_funding(cb)
        ts = sorted(set(da) & set(db))
        pa = [da[t] for t in ts]
        pb = [db[t] for t in ts]
        # просадка самого отношения (только для контекста)
        ratio = [pa[i] / pb[i] for i in range(len(ts))]
        ratio_dd = mdd(ratio)
        print(f"\n=== {ca} / {cb} (лонг {ca}, шорт {cb}) ===")
        print(f"  отношение {ca}/{cb}: итог {ratio[-1]/ratio[0]-1:+.0%}, "
              f"просадка отношения {ratio_dd:.1f}%")
        for E_frac, label in ((0.5, "gross 1x"), (1.0, "gross 2x")):
            for band, rebal, tag in ((0.0, False, "статическая"),
                                     (0.04, True, "ребаланс 4%")):
                eq, reb = neutral(ca, cb, ts, pa, pb, fa, fb, E_frac,
                                  band, rebal)
                out_rows.append({
                    "pair": f"{ca}/{cb}", "gross": label, "mode": tag,
                    "final": round(eq[-1], 0),
                    "ret_pct": round((eq[-1] / CASH - 1) * 100, 1),
                    "mdd_pct": (round(mdd(eq), 1) if eq[-1] > 0 else ""),
                    "rebalances": reb})
                print(f"  {label:<9}{tag:<14} финал {eq[-1]:>9.0f} "
                      f"({eq[-1]/CASH-1:+.1%})  maxDD {mdd(eq):>7.1f}%  "
                      f"ребалансов {reb}")
        # вклад фандинга отдельно
        eq_no_f, _ = neutral(ca, cb, ts, pa, pb, {}, {}, 1.0, 0.0, False)
        however = neutral(ca, cb, ts, pa, pb, fa, fb, 1.0, 0.0, False)[0][-1]
        print(f"  фандинг (gross 2x, без ребаланса): "
              f"итог {eq_no_f[-1]:.0f}, против {however:.0f} с фандингом "
              f"(вклад {however-eq_no_f[-1]:+.0f})")

    path = os.path.join(HERE, "results", "pairs_neutral.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nCSV: {path}")


if __name__ == "__main__":
    main()
