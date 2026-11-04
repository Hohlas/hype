#!/usr/bin/env python3
"""Загрузка часовой истории BTCUSDT за 3 года (2023-11-01 -> 2026-11-01).

Источник: data-api.binance.vision. Сохранение: tests/backtest/data/btc_h1_3y.csv.

Запуск:
    ./venv/bin/python tests/backtest/fetch_data_3y.py
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
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "btc_h1_3y.csv")


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "hype-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    rows, start = {}, START_MS
    while start < END_MS:
        url = (f"{DATA_API}?symbol=BTCUSDT&interval=1h"
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
    keys = sorted(rows)
    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "open", "high", "low", "close", "volume"])
        for k in keys:
            r = rows[k]
            ts = dt.datetime.fromtimestamp(k / 1000, tz=dt.timezone.utc)
            w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"),
                        r[1], r[2], r[3], r[4], r[5]])
    first = dt.datetime.fromtimestamp(keys[0] / 1000, tz=dt.timezone.utc)
    last = dt.datetime.fromtimestamp(keys[-1] / 1000, tz=dt.timezone.utc)
    print(f"сохранено {len(keys)} свечей -> {OUT}")
    print(f"первый: {first}  open={rows[keys[0]][1]}")
    print(f"последний: {last}  close={rows[keys[-1]][4]}")


if __name__ == "__main__":
    main()