#!/usr/bin/env python3
"""Загрузка минутной истории BTCUSDT за 3 года через официальные архивы Binance.

Источник: data.binance.vision (месячные zip-архивы klines), т.е. первоисточник
Binance, а не сторонний сайт. Внутри CSV без заголовка, время в микросекундах:
    open_time, open, high, low, close, volume, close_time, ...

Окно совпадает с btc_h1_3y.csv: 2023-11-01 00:00 -> 2026-11-01 00:00 (не включая).
Файл: data/btc_1m_3y.csv (time_utc, open, high, low, close, volume).

Запуск: ./venv/bin/python tests/backtest/fetch_data_1m.py
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "btc_1m_3y.csv")
START = dt.datetime(2023, 11, 1, tzinfo=dt.timezone.utc)
END = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
START_MS = int(START.timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)
URL = ("https://data.binance.vision/data/spot/monthly/klines/"
       "BTCUSDT/1m/BTCUSDT-1m-{y:04d}-{m:02d}.zip")


def months():
    y, m = 2023, 11
    while (y, m) < (2026, 11):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def fetch_month(y: int, m: int) -> list[tuple[str, str, str, str, str, str]]:
    url = URL.format(y=y, m=m)
    req = urllib.request.Request(url, headers={"User-Agent": "hype-backtest/1.0"})
    raw = urllib.request.urlopen(req, timeout=120).read()
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = z.namelist()[0]
    rows: list[tuple[str, str, str, str, str, str]] = []
    with z.open(name) as fh:
        for line in io.TextIOWrapper(fh, encoding="utf-8"):
            p = line.strip().split(",")
            ts = int(p[0])
            # до 2025 г. время в миллисекундах, позже — в микросекундах
            ts_ms = ts // 1000 if ts > 10 ** 14 else ts
            if ts_ms < START_MS or ts_ms >= END_MS:
                continue
            t = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
            rows.append((t.strftime("%Y-%m-%d %H:%M:%S"),
                         p[1], p[2], p[3], p[4], p[5]))
    return rows


def main() -> None:
    os.makedirs(DATA, exist_ok=True)
    total = 0
    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "open", "high", "low", "close", "volume"])
        for y, m in months():
            rows = fetch_month(y, m)
            w.writerows(rows)
            total += len(rows)
            print(f"{y}-{m:02d}: {len(rows):>6} баров, всего {total}")
    print(f"готово: {OUT} ({total} баров)")


if __name__ == "__main__":
    main()
