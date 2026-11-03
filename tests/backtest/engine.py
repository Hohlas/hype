#!/usr/bin/env python3
"""Единый движок бэктеста для сравнения спотовых стратегий на BTC.

Модель исполнения взята 1-в-1 из docs/GridRB_spec.md §2 (исполнительная среда),
чтобы все стратегии сравнивались на ОДНИХ правилах:

- время дискретно: H1-бары i = 0,1,2,...; бар i -> (o,h,l,c);
- счёт: U — свободный USDC, B — количество BTC; V = U + B*c;
- лимитный buy L активен с бара p+1; на баре i > p исполняется, если l <= L,
  по цене min(o, L) (гэп вниз — дешевле уровня);
- лимитный sell S активен с бара p+1; на баре i > p исполняется, если h >= S,
  по цене max(o, S) (гэп вверх — дороже уровня);
- внутри бара сначала исполняются покупки, потом продажи (худший порядок для лонга);
- buy не исполняется, если q*px > U (ордер остаётся висеть);
- sell исполняется на min(q, B);
- сделки/ордера с нотиналом < MIN_NOTIONAL ($10) не исполняются;
- маркет: цена c, slip 0.020%, комиссия taker 0.070%;
- лимит: комиссия maker 0.040%.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field


MAKER = 0.00040
TAKER = 0.00070
SLIP = 0.00020
MIN_NOTIONAL = 10.0


@dataclass
class Order:
    side: str          # "buy" | "sell"
    price: float
    qty: float         # объём в BTC
    tag: str
    active_from: int   # индекс бара, с которого ордер может исполниться
    alive: bool = True

    def notional(self) -> float:
        return self.price * self.qty


@dataclass
class Fill:
    i: int
    side: str
    price: float
    qty: float
    tag: str
    fee: float


class Broker:
    """Счёт + исполнение ордеров по модели docs/GridRB_spec.md §2."""

    def __init__(self, cash: float, maker: float = MAKER, taker: float = TAKER,
                 slip: float = SLIP, stress: float = 1.0):
        self.U = float(cash)
        self.B = 0.0
        self.maker = maker * stress
        self.taker = taker * stress
        self.slip = slip * stress
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.fees_paid = 0.0
        self.turnover = 0.0

    # ---- учёт ----
    def equity(self, c: float) -> float:
        return self.U + self.B * c

    def weight(self, c: float) -> float:
        v = self.equity(c)
        return (self.B * c / v) if v > 0 else 0.0

    def free_usdc(self) -> float:
        return self.U - sum(o.notional() for o in self.orders
                            if o.alive and o.side == "buy")

    def reserved_sell(self) -> float:
        return sum(o.qty for o in self.orders if o.alive and o.side == "sell")

    # ---- постановка ----
    def place_limit_buy(self, price: float, usd: float, tag: str,
                        active_from: int) -> Order | None:
        qty = usd / price if price > 0 else 0.0
        if qty * price < MIN_NOTIONAL or qty <= 0:
            return None
        o = Order("buy", price, qty, tag, active_from)
        self.orders.append(o)
        return o

    def place_limit_sell(self, price: float, qty: float, tag: str,
                         active_from: int) -> Order | None:
        if price <= 0 or qty * price < MIN_NOTIONAL or qty <= 0:
            return None
        o = Order("sell", price, qty, tag, active_from)
        self.orders.append(o)
        return o

    def cancel(self, o: Order) -> None:
        o.alive = False

    def cancel_all(self, orders) -> None:
        for o in orders:
            o.alive = False

    # ---- маркет ----
    def market_buy(self, usd: float, close: float, i: int) -> float:
        """Покупка на notional=usd по цене close; taker + slip. Возвращает qty."""
        if usd < MIN_NOTIONAL or usd <= 0:
            return 0.0
        # бюджет включает комиссию: notional*(1+taker) <= U
        usd = min(usd, self.U / (1 + self.taker))
        if usd < MIN_NOTIONAL:
            return 0.0
        px = close * (1 + self.slip)
        qty = usd / px
        fee = usd * self.taker
        self.U -= usd + fee
        self.B += qty
        self.fees_paid += fee
        self.turnover += usd
        self.fills.append(Fill(i, "buy", px, qty, "market", fee))
        return qty

    def market_sell(self, qty: float, close: float, i: int) -> float:
        if qty <= 0 or self.B <= 0:
            return 0.0
        qty = min(qty, self.B)
        px = close * (1 - self.slip)
        if qty * px < MIN_NOTIONAL:
            return 0.0
        gross = qty * px
        fee = gross * self.taker
        self.U += gross - fee
        self.B -= qty
        self.fees_paid += fee
        self.turnover += gross
        self.fills.append(Fill(i, "sell", px, qty, "market", fee))
        return qty

    # ---- исполнение на баре ----
    def execute_bar(self, i: int, o: float, h: float, l: float, c: float,
                    on_fill) -> None:
        # сначала покупки, затем продажи (худший порядок для лонга)
        for side in ("buy", "sell"):
            for od in list(self.orders):
                if not od.alive or od.side != side or i <= od.active_from - 1:
                    continue
                if i < od.active_from:
                    continue
                if side == "buy":
                    if l > od.price:
                        continue
                    px = min(o, od.price)
                    cost = od.qty * px
                    if cost < MIN_NOTIONAL or cost > self.U + 1e-9:
                        continue
                    fee = cost * self.maker
                    self.U -= cost + fee
                    self.B += od.qty
                    od.alive = False
                    self.fees_paid += fee
                    self.turnover += cost
                    self.fills.append(Fill(i, "buy", px, od.qty, od.tag, fee))
                    on_fill(i, od, px, od.qty)
                else:
                    if h < od.price:
                        continue
                    px = max(o, od.price)
                    qty = min(od.qty, self.B)
                    if qty * px < MIN_NOTIONAL:
                        continue
                    # частичное исполнение возможно только из-за нехватки BTC
                    gross = qty * px
                    fee = gross * self.maker
                    self.U += gross - fee
                    self.B -= qty
                    od.alive = False
                    self.fees_paid += fee
                    self.turnover += gross
                    self.fills.append(Fill(i, "sell", px, qty, od.tag, fee))
                    on_fill(i, od, px, qty)
        self.orders = [o_ for o_ in self.orders if o_.alive]


# ---------------------------------------------------------------------------
# Загрузка баров
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    i: int
    t: int
    o: float
    h: float
    l: float
    c: float


def load_bars(path: str) -> list[Bar]:
    import datetime as dt
    bars: list[Bar] = []
    with open(path, newline="") as fh:
        r = csv.DictReader(fh)
        for i, row in enumerate(r):
            ts = dt.datetime.strptime(row["time_utc"], "%Y-%m-%d %H:%M:%S")
            t = int(ts.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            bars.append(Bar(i, t, float(row["open"]), float(row["high"]),
                            float(row["low"]), float(row["close"])))
    return bars


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------

def max_drawdown(equity: list[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak
            if dd < mdd:
                mdd = dd
    return mdd


@dataclass
class Result:
    name: str
    equity: list[float] = field(default_factory=list)
    trades: int = 0
    fees: float = 0.0
    turnover: float = 0.0

    @property
    def final(self) -> float:
        return self.equity[-1]

    @property
    def mdd(self) -> float:
        return max_drawdown(self.equity)


def buy_and_hold(bars: list[Bar], cash: float, stress: float = 1.0) -> Result:
    b = Broker(cash, stress=stress)
    b.market_buy(cash, bars[0].c, bars[0].i)
    res = Result("Buy&Hold")
    for bar in bars:
        res.equity.append(b.equity(bar.c))
    res.trades = 1
    res.fees = b.fees_paid
    res.turnover = b.turnover
    return res
