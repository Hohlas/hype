#!/usr/bin/env python3
"""Загрузка часовой истории BTCUSDT за 2 года.

Источник: data-api.binance.vision (публичный дата-API Binance без гео-ограничений).
Гео-ограниченный api.binance.com из этого окружения недоступен.

Данные сохраняются в tests/backtest/data/btc_h1_2y.csv со столбцами:
    time_utc, open, high, low, close, volume

Окно совпадает со справкой docs/GridRB_spec.md §9:
    2024-11-02 11:00 UTC -> 2026-11-02 11:00 UTC (закрытие последнего бара).

Свечи Binance (BTCUSDT) не тождественны Hyperliquid spot (UBTC/USDC): другая
площадка, другие микроструктурные детали. Для сравнения стратегий между собой
и с buy&hold этого достаточно; абсолютные значения могут слегка отличаться от
справки GridRB_spec.md.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import urllib.request

DATA_API = "https://data-api.binance.vision/api/v3/klines"
PAIR = "BTCUSDT"
INTERVAL = "1h"

START_MS = int(dt.datetime(2024, 11, 2, 11, tzinfo=dt.timezone.utc).timestamp() * 1000)
END_MS = int(dt.datetime(2026, 11, 2, 11, tzinfo=dt.timezone.utc).timestamp() * 1000)

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT_PATH = os.path.join(OUT_DIR, "btc_h1_2y.csv")


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "hype-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch() -> list[list]:
    rows: list[list] = []
    start = START_MS
    while start < END_MS:
        url = (f"{DATA_API}?symbol={PAIR}&interval={INTERVAL}"
               f"&startTime={start}&endTime={END_MS}&limit=1000")
        batch = _get(url)
        if not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        if last_open <= start:
            break
        start = last_open + 3600_000
    # дедупликация + сортировка по времени открытия
    uniq = {int(r[0]): r for r in rows}
    return [uniq[k] for k in sorted(uniq)]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = fetch()
    if not rows:
        raise SystemExit("нет данных от источника")
    with open(OUT_PATH, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "open", "high", "low", "close", "volume"])
        for r in rows:
            ts = dt.datetime.fromtimestamp(int(r[0]) / 1000, tz=dt.timezone.utc)
            w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"),
                        r[1], r[2], r[3], r[4], r[5]])
    first = dt.datetime.fromtimestamp(int(rows[0][0]) / 1000, tz=dt.timezone.utc)
    last = dt.datetime.fromtimestamp(int(rows[-1][0]) / 1000, tz=dt.timezone.utc)
    print(f"сохранено {len(rows)} свечей -> {OUT_PATH}")
    print(f"первый бар: {first}  последний бар: {last}")
    print(f"цена: first open={rows[0][1]}  last close={rows[-1][4]}")


if __name__ == "__main__":
    main()
