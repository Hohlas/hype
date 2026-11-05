#!/usr/bin/env python3
"""Загрузка часовой истории нескольких монет (USDT) за 3 года.

Источник: data-api.binance.vision (тот же дата-API, что и для BTC).
Окно: 2023-11-01 -> 2026-11-01 UTC. Файл: data/<coin>_h1_3y.csv.

Запуск:
    ./venv/bin/python tests/backtest/fetch_data_multi.py
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import urllib.request

DATA_API = "https://data-api.binance.vision/api/v3/klines"
START = dt.datetime(2023, 11, 1, tzinfo=dt.timezone.utc)
END = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
START_MS = int(START.timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK",
         "DOT", "LTC", "TRX", "TON", "SUI", "NEAR", "ATOM", "BCH", "UNI",
         "AAVE", "XLM", "HBAR", "INJ", "APT", "FIL"]


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "hype-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch(symbol: str) -> dict[int, list]:
    rows, start = {}, START_MS
    while start < END_MS:
        url = (f"{DATA_API}?symbol={symbol}&interval=1h"
               f"&startTime={start}&endTime={END_MS}&limit=1000")
        batch = _get(url)
        if not batch:
            break
        for r in batch:
            rows[int(r[0])] = r
        last = int(batch[-1][0])
        if last <= start:
            break
        start = last + 3600_000
    return rows


def main() -> None:
    os.makedirs(DATA, exist_ok=True)
    for coin in COINS:
        sym = f"{coin}USDT"
        try:
            rows = fetch(sym)
        except Exception as e:  # noqa: BLE001
            print(f"{coin:5} ОШИБКА: {e}")
            continue
        if not rows:
            print(f"{coin:5} пусто")
            continue
        keys = sorted(rows)
        out = os.path.join(DATA, f"{coin.lower()}_h1_3y.csv")
        with open(out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time_utc", "open", "high", "low", "close", "volume"])
            for k in keys:
                r = rows[k]
                ts = dt.datetime.fromtimestamp(k / 1000, tz=dt.timezone.utc)
                w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"),
                            r[1], r[2], r[3], r[4], r[5]])
        first = dt.datetime.fromtimestamp(keys[0] / 1000, tz=dt.timezone.utc)
        last = dt.datetime.fromtimestamp(keys[-1] / 1000, tz=dt.timezone.utc)
        print(f"{coin:5} {len(keys):>6} баров  {first:%Y-%m-%d} -> {last:%Y-%m-%d}")


if __name__ == "__main__":
    main()
