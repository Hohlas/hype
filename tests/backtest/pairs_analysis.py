#!/usr/bin/env python3
"""Диагностика пар: корреляция, соотношение, возврат к среднему.

Только вычислительная часть — без торговли. Отвечает на вопросы:
  1. насколько активы коррелированы (часовые/дневные лог-доходности);
  2. возвращается ли соотношение log(PA) - beta*log(PB) к среднему
     (AR(1), период полураспада, t-статистика Дики-Фуллера);
  3. есть ли вообще смысл строить сетку на паре.

Выход:
  results/pairs_stats.csv      — по каждой паре
  results/pairs_corr_matrix.csv — матрица корреляций дневных доходностей

Запуск: ./venv/bin/python tests/backtest/pairs_analysis.py
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RESULTS = os.path.join(HERE, "results")

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK",
         "DOT", "LTC", "TRX", "TON", "SUI", "NEAR", "ATOM", "BCH", "UNI",
         "AAVE", "XLM", "HBAR"]


def load_closes(coin: str) -> dict[int, float]:
    path = os.path.join(DATA, f"{coin.lower()}_h1_3y.csv")
    out: dict[int, float] = {}
    with open(path, newline="") as fh:
        r = csv.DictReader(fh)
        for row in r:
            ts = dt.datetime.strptime(row["time_utc"], "%Y-%m-%d %H:%M:%S")
            t = int(ts.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            out[t] = float(row["close"])
    return out


def pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def ols(y: list[float], x: list[float]) -> tuple[float, float, float]:
    """Простая линейная регрессия y = a + b*x. Возвращает (b, a, t-стат b)."""
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0:
        return 0.0, 0.0, 0.0
    b = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / sxx
    a = my - b * mx
    resid = [yi - (a + b * xi) for xi, yi in zip(x, y)]
    s2 = sum(e * e for e in resid) / max(1, n - 2)
    se = math.sqrt(s2 / sxx) if sxx > 0 else 0.0
    t = b / se if se > 0 else 0.0
    return b, a, t


def adf_tstat(y: list[float], lags: int = 0) -> tuple[float, float]:
    """Дики-Фуллер (без лагов) для ряда y: регрессия dy_t на y_{t-1}.
    Возвращает (phi, t-стат). phi<1 и очень отрицательный t => возврат к среднему.
    Лаги отброшены ради скорости (210 пар x 26k баров); AR(1)-полураспад —
    основной показатель и считается отдельно.
    """
    dy = [y[i] - y[i - 1] for i in range(1, len(y))]
    # соберём матрицу регрессоров: y_{t-1}, dy_{t-1..t-lags}
    rows_y, rows_X = [], []
    for i in range(lags, len(dy)):
        rows_y.append(dy[i])
        row = [y[i]]  # y_{t-1} (нулевой лаг в индексации dy)
        for L in range(1, lags + 1):
            row.append(dy[i - L])
        rows_X.append(row)
    k = len(rows_X[0])
    n = len(rows_y)
    # МНК y = X beta
    XtX = [[sum(rows_X[r][i] * rows_X[r][j] for r in range(n))
            for j in range(k)] for i in range(k)]
    Xty = [sum(rows_X[r][i] * rows_y[r] for r in range(n)) for i in range(k)]
    beta = _solve(XtX, Xty)
    if beta is None:
        return 1.0, 0.0
    resid = [rows_y[r] - sum(beta[i] * rows_X[r][i] for i in range(k))
             for r in range(n)]
    s2 = sum(e * e for e in resid) / max(1, n - k)
    inv = _inverse(XtX)
    if inv is None:
        return beta[0] + 1.0, 0.0
    se0 = math.sqrt(max(0.0, s2 * inv[0][0]))
    t = beta[0] / se0 if se0 > 0 else 0.0
    return beta[0] + 1.0, t  # phi = 1 + коэффициент при уровне


def _solve(A: list[list[float]], b: list[float]):
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        pv = M[c][c]
        for j in range(c, n + 1):
            M[c][j] /= pv
        for r in range(n):
            if r != c and M[r][c] != 0:
                f = M[r][c]
                for j in range(c, n + 1):
                    M[r][j] -= f * M[c][j]
    return [M[i][n] for i in range(n)]


def _inverse(A: list[list[float]]):
    n = len(A)
    M = [row[:] + [1.0 if i == j else 0.0 for j in range(n)]
         for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        pv = M[c][c]
        for j in range(2 * n):
            M[c][j] /= pv
        for r in range(n):
            if r != c and M[r][c] != 0:
                f = M[r][c]
                for j in range(2 * n):
                    M[r][j] -= f * M[c][j]
    return [row[n:] for row in M]


def main() -> None:
    closes = {c: load_closes(c) for c in COINS}
    os.makedirs(RESULTS, exist_ok=True)

    # --- статистика по каждой паре на её собственном пересечении баров ---
    rows = []
    corr = {a: {b: (1.0 if a == b else 0.0) for b in COINS} for a in COINS}
    total_bars = []
    for i, a in enumerate(COINS):
        for b in COINS[i + 1:]:
            ts_ab = sorted(set(closes[a]) & set(closes[b]))
            total_bars.append(len(ts_ab))
            pa = [closes[a][t] for t in ts_ab]
            pb = [closes[b][t] for t in ts_ab]
            la = [math.log(v) for v in pa]
            lb = [math.log(v) for v in pb]
            dp_a, dp_b = pa[::24], pb[::24]
            dr_a = [math.log(dp_a[k] / dp_a[k - 1]) for k in range(1, len(dp_a))]
            dr_b = [math.log(dp_b[k] / dp_b[k - 1]) for k in range(1, len(dp_b))]
            corr_h = pearson([la[k] - la[k - 1] for k in range(1, len(la))],
                             [lb[k] - lb[k - 1] for k in range(1, len(lb))])
            corr_d = pearson(dr_a, dr_b)
            corr[a][b] = corr[b][a] = round(corr_d, 3)
            beta, _a, _t = ols(la, lb)
            spread = [la[k] - beta * lb[k] for k in range(len(la))]
            phi, tstat = adf_tstat(spread)
            hl_days = (math.log(0.5) / math.log(phi) / 24
                       if 0 < phi < 1 else float("inf"))
            ratio_ret = pa[-1] / pa[0] / (pb[-1] / pb[0]) - 1
            rows.append({
                "a": a, "b": b,
                "bars": len(ts_ab),
                "corr_h1": round(corr_h, 3),
                "corr_daily": round(corr_d, 3),
                "beta": round(beta, 3),
                "phi": round(phi, 4),
                "halflife_days": (round(hl_days, 1) if hl_days != float("inf")
                                  else ""),
                "adf_t": round(tstat, 1),
                "stationary_5pct": "да" if tstat < -2.89 else "нет",
                "ratio_ret_3y_pct": round(ratio_ret * 100, 1),
            })
    print(f"монет: {len(COINS)}, пар: {len(rows)}, "
          f"баров на пару: {min(total_bars)}..{max(total_bars)}")

    with open(os.path.join(RESULTS, "pairs_corr_matrix.csv"), "w",
              newline="") as fh:
        w = csv.writer(fh)
        w.writerow([""] + COINS)
        for a in COINS:
            w.writerow([a] + [corr[a][b] for b in COINS])
    rows.sort(key=lambda r: r["adf_t"])
    with open(os.path.join(RESULTS, "pairs_stats.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("\n=== Самые «возвратные» пары (лучший ADF t, порог 5% = -2.89) ===")
    print(f"{'пара':<12}{'corr_d':>7}{'beta':>7}{'phi':>8}{'HL дн':>8}"
          f"{'ADF t':>8}{'стац':>6}{'ratio 3г':>10}")
    for r in rows[:15]:
        print(f"{r['a']+'/'+r['b']:<12}{r['corr_daily']:>7}{r['beta']:>7}"
              f"{r['phi']:>8}{str(r['halflife_days']):>8}{r['adf_t']:>8}"
              f"{r['stationary_5pct']:>6}{r['ratio_ret_3y_pct']:>10}")

    print("\n=== Самые коррелированные пары (дневные доходности) ===")
    for r in sorted(rows, key=lambda r: -r["corr_daily"])[:15]:
        print(f"{r['a']+'/'+r['b']:<12}{r['corr_daily']:>7}{r['beta']:>7}"
              f"{r['phi']:>8}{str(r['halflife_days']):>8}{r['adf_t']:>8}"
              f"{r['stationary_5pct']:>6}{r['ratio_ret_3y_pct']:>10}")

    # доля стационарных пар
    n_stat = sum(1 for r in rows if r["stationary_5pct"] == "да")
    print(f"\nстационарных пар на 5%: {n_stat} из {len(rows)} "
          f"({100*n_stat/len(rows):.0f}%)")
    print(f"CSV: {RESULTS}/pairs_stats.csv, pairs_corr_matrix.csv")


if __name__ == "__main__":
    main()
