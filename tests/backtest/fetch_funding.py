#!/usr/bin/env python3
"""Загрузка часового фандинга перпов Hyperliquid за 3 года (первоисточник).

API: POST https://api.hyperliquid.xyz/info {"type":"fundingHistory", ...}
Отдаёт страницами по 500 записей -> идём вперёд по времени.

Файл: data/funding_<coin>_h1_3y.csv (time_utc, fundingRate).
Запуск: ./venv/bin/python tests/backtest/fetch_funding.py
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
START = int(dt.datetime(2023, 11, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
END = int(dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
COINS = ["ETH", "SOL", "BTC", "AVAX", "LINK"]


def post(body):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch(coin: str) -> dict[int, float]:
    out, start = {}, START
    while start < END:
        for attempt in range(6):
            try:
                batch = post({"type": "fundingHistory", "coin": coin,
                              "startTime": start, "endTime": END})
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 5:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise
        else:
            break
        if not batch:
            break
        for x in batch:
            out[int(x["time"])] = float(x["fundingRate"])
        last = int(batch[-1]["time"])
        if last <= start:
            break
        start = last + 1
        time.sleep(0.35)
    return out


def main() -> None:
    os.makedirs(DATA, exist_ok=True)
    for coin in COINS:
        out = os.path.join(DATA, f"funding_{coin.lower()}_h1_3y.csv")
        if os.path.exists(out):
            print(f"{coin:5} уже скачано, пропуск")
            continue
        try:
            rows = fetch(coin)
        except Exception as e:  # noqa: BLE001
            print(f"{coin}: ОШИБКА {e}")
            continue
        keys = sorted(rows)
        with open(out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time_utc", "fundingRate"])
            for k in keys:
                ts = dt.datetime.fromtimestamp(k / 1000, tz=dt.timezone.utc)
                w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), rows[k]])
        mean = sum(rows[k] for k in keys) / len(keys)
        yrs = (keys[-1] - keys[0]) / 1000 / 3600 / 24 / 365
        print(f"{coin:5} {len(keys):>6} точек, {yrs:.2f} г, "
              f"средний фандинг/час={mean*100:.5f}%, "
              f"лонг платит {mean*24*365*100:+.1f}%/год, "
              f"шорт получает {mean*24*365*100:+.1f}%/год")


if __name__ == "__main__":
    main()
