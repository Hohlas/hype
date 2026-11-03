#!/usr/bin/env python3
"""Сценарные окна из docs/ask.md на реальных H1-блоках (со склейкой).

Сценарии (старт всегда $100 000, горизонт 90 дней = 2160 H1-баров):
    S1  просадка -40%                     100k -> 60k
    S2  просадка -40% и возврат в точку   100k -> 60k -> 100k
    S3  просадка -40%, затем +50% от входа 100k -> 60k -> 150k
    S4  сразу рост +50%                   100k -> 150k

Метод: берутся реальные H1-блоки (падение и рост) из загруженной истории и
умножаются на экспоненциальный дрейф так, чтобы попасть в целевую точку.
Внутрибаровые пропорции (o/h/l/c) и волатильность реального блока
сохраняются; меняется только общий наклон. Это синтетика на реальной
микроструктуре, а не точная историческая выборка — помечено явно.

Запуск:
    ./venv/bin/python tests/backtest/scenarios.py
"""
from __future__ import annotations

import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Bar, buy_and_hold, load_bars  # noqa: E402
from run_backtest import build_all, run_instance, summarize  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc_h1_2y.csv")
RESULTS = os.path.join(HERE, "results")
CASH = 10_000.0
START_PRICE = 100_000.0

# реальные блоки-источники (по времени открытия бара, UTC)
DOWN_BLOCK = ("2025-10-06 00:00:00", "2026-01-04 00:00:00")   # падение
UP_BLOCK = ("2026-06-20 00:00:00", "2026-09-18 00:00:00")     # рост


def _ts(s: str) -> int:
    import datetime as dt
    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def slice_bars(bars, start: str, end: str):
    a, b = _ts(start), _ts(end)
    out = [x for x in bars if a <= x.t < b]
    if not out:
        raise SystemExit(f"пустой блок {start}..{end}")
    return out


def transformed(block, start_price: float, end_price: float,
                i0: int = 0):
    """Масштабирует реальный блок так, чтобы open[0]*f(0)=start_price,
    close[-1]*f(L-1)=end_price. f(t)=exp(a+d*t)."""
    L = len(block)
    a = math.log(start_price / block[0].o)
    d = (math.log(end_price / block[-1].c) - a) / (L - 1)
    out = []
    for j, b in enumerate(block):
        f = math.exp(a + d * j)
        out.append(Bar(i0 + j, i0 + j, b.o * f, b.h * f, b.l * f, b.c * f))
    return out


def build_scenarios(bars):
    down = slice_bars(bars, *DOWN_BLOCK)
    up = slice_bars(bars, *UP_BLOCK)
    half = 1080  # 45 дней
    scenarios = {}

    # S1: -40% за 90 дней на блоке падения
    d90 = (down * 2)[:2160]
    scenarios["S1_drop40"] = transformed(d90, START_PRICE, START_PRICE * 0.60)

    # S2: -40% (45д), затем возврат к входу (45д)
    leg1 = transformed(down[:half], START_PRICE, START_PRICE * 0.60)
    leg2 = transformed(up[:half], START_PRICE * 0.60, START_PRICE)
    scenarios["S2_drop40_return"] = leg1 + _reindex(leg2, len(leg1))

    # S3: -40% (45д), затем +50% от входа (45д)
    leg1 = transformed(down[:half], START_PRICE, START_PRICE * 0.60)
    leg2 = transformed(up[:half], START_PRICE * 0.60, START_PRICE * 1.50)
    scenarios["S3_drop40_up50"] = leg1 + _reindex(leg2, len(leg1))

    # S4: +50% за 90 дней на блоке роста
    u90 = (up * 2)[:2160]
    scenarios["S4_up50"] = transformed(u90, START_PRICE, START_PRICE * 1.50)
    return scenarios


def _reindex(bars, offset):
    return [Bar(offset + j, offset + j, b.o, b.h, b.l, b.c)
            for j, b in enumerate(bars)]


def main():
    os.makedirs(RESULTS, exist_ok=True)
    bars = load_bars(DATA)
    scenarios = build_scenarios(bars)
    rows = []
    for sid, sbar in scenarios.items():
        hold = buy_and_hold(sbar, CASH)
        res = [run_instance(s, sbar) for s in build_all(CASH)]
        real_ret = sbar[-1].c / sbar[0].o - 1
        print(f"\n=== {sid}  (факт. доходность hold {real_ret*100:+.1f}%) ===")
        for r in summarize(res, hold):
            r2 = {"scenario": sid, **r}
            rows.append(r2)
            print(f"{r['strategy']:<22} final={r['final_usd']:8.0f} "
                  f"excess={r['excess_vs_hold_pp']:+6.2f}pp "
                  f"maxDD={r['max_dd_pct']:6.1f}% trades={r['trades']}")
    out = os.path.join(RESULTS, "scenarios.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV сохранён: {out}")


if __name__ == "__main__":
    main()