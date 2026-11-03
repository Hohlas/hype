"""GridRB 80/20 — боевая реализация для спота Hyperliquid.

Соответствует docs/GridRB_spec.md и бэктесту tests/backtest/strategies.py::GridRB.

Отличия live-версии от бэктеста (осознанные, из-за реальной биржи):
- на старте процесса все висящие ордера по токену снимаются, лестницы строятся
  заново от текущей цены. Позиция BTC сохраняется, поэтому повторного
  бутстрап-маркета не будет, если BTC уже куплен;
- у «замороженной» лестницы объёмы не пересчитываются каждый тик (иначе
  пришлось бы постоянно снимать/переставлять maker-ордера и терять очередь);
- факт исполнения micro-buy определяется по исчезновению oid из openOrders,
  цена тейка считается от цены лимитки (консервативно, не хуже фактической);
- ордера без нашего oid (сироты) снимаются, чтобы не было дублей.

Всё состояние — в памяти процесса. При рестарте оно восстанавливается с биржи:
балансы, открытые ордера, текущая цена. Отдельной персистентности нет.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from loguru import logger

import settings


# ---------------------------------------------------------------------------
# Конфигурация и структуры
# ---------------------------------------------------------------------------

@dataclass
class GridRBConfig:
    f0: float = 0.80
    band: float = 0.04
    delta: float = 0.004
    n: int = 5
    micro_lot: float = 0.02
    micro_max: int = 3
    min_notional: float = 10.0

    @classmethod
    def from_settings(cls) -> "GridRBConfig":
        return cls(
            f0=float(getattr(settings, "GRIDRB_F0", 0.80)),
            band=float(getattr(settings, "GRIDRB_BAND", 0.04)),
            delta=float(getattr(settings, "GRIDRB_DELTA", 0.004)),
            n=int(getattr(settings, "GRIDRB_LEVELS", 5)),
            micro_lot=float(getattr(settings, "GRIDRB_MICRO_LOT", 0.02)),
            micro_max=int(getattr(settings, "GRIDRB_MICRO_MAX", 3)),
            min_notional=float(getattr(settings, "GRIDRB_MIN_NOTIONAL", 10.0)),
        )


@dataclass
class TrackedOrder:
    oid: int | None
    side: str          # "buy" | "sell"
    price: float
    qty: float
    tag: str           # "trim" | "defc" | "micro" | "micro_take"


@dataclass
class Action:
    kind: str          # "cancel" | "place" | "market_buy"
    side: str = ""
    price: float = 0.0
    qty: float = 0.0
    usd: float = 0.0
    tag: str = ""
    oid: int | None = None
    order: TrackedOrder | None = None


# ---------------------------------------------------------------------------
# Чистая логика (тестируется без биржи)
# ---------------------------------------------------------------------------

class GridRBEngine:
    """Один шаг GridRB: принимает снимок счёта, возвращает список действий."""

    def __init__(self, cfg: GridRBConfig, sz_decimals: int = 5):
        self.cfg = cfg
        self.sz_decimals = sz_decimals

        self.first: bool | None = None
        self.trim_anchor: float | None = None
        self.defc_anchor: float | None = None
        self.trim_orders: list[TrackedOrder] = []
        self.defc_orders: list[TrackedOrder] = []
        self.micro_buys: list[TrackedOrder] = []
        self.micro_sells: list[TrackedOrder] = []

    # ---- округления ----
    def _qty(self, qty: float) -> float:
        step = Decimal(1).scaleb(-self.sz_decimals)
        return float(Decimal(str(qty)).quantize(step, rounding=ROUND_DOWN))

    def _px(self, price: float) -> float:
        """5 значащих цифр — требование Hyperliquid к цене ордера."""
        if price <= 0:
            return 0.0
        exp = math.floor(math.log10(price))
        step = 10 ** (exp - 4)
        return round(price / step) * step

    def _ok(self, qty: float, price: float) -> bool:
        return qty > 0 and price > 0 and qty * price >= self.cfg.min_notional

    # ---- учёт ----
    def _all_tracked(self) -> list[TrackedOrder]:
        return (self.trim_orders + self.defc_orders
                + self.micro_buys + self.micro_sells)

    def _reserved_sell(self) -> float:
        return sum(o.qty for o in self.trim_orders + self.micro_sells)

    def _reserved_buy(self) -> float:
        return sum(o.price * o.qty for o in self.defc_orders + self.micro_buys)

    def register(self, action: Action, oid: int | None) -> None:
        """Запоминает oid успешно выставленного ордера."""
        if action.order is not None and oid is not None:
            action.order.oid = oid

    def drop(self, action: Action) -> None:
        """Убирает ордер из учёта, если выставить его не удалось."""
        if action.order is None:
            return
        for lst in (self.trim_orders, self.defc_orders,
                    self.micro_buys, self.micro_sells):
            if action.order in lst:
                lst.remove(action.order)

    # ---- сверка с биржей ----
    def _reconcile(self, open_oids: set[int]) -> list[Action]:
        actions: list[Action] = []
        tracked_oids = {o.oid for o in self._all_tracked() if o.oid is not None}

        # чужие/неотслеживаемые ордера снимаем, чтобы не было дублей
        for oid in sorted(open_oids - tracked_oids):
            actions.append(Action("cancel", oid=oid))

        # отслеживаемый, но уже не висящий ордер = исполнен
        for lst, tag in ((self.trim_orders, "trim"),
                         (self.defc_orders, "defc"),
                         (self.micro_buys, "micro"),
                         (self.micro_sells, "micro_take")):
            for o in list(lst):
                if o.oid is None or o.oid in open_oids:
                    continue
                if tag == "micro":
                    tp = self._px(o.price * (1 + self.cfg.delta))
                    qty = self._qty(o.qty)
                    if self._ok(qty, tp):
                        take = TrackedOrder(None, "sell", tp, qty, "micro_take")
                        self.micro_sells.append(take)
                        actions.append(Action("place", side="SELL", price=tp,
                                              qty=qty, tag="micro_take",
                                              order=take))
                lst.remove(o)
        return actions

    # ---- основной шаг ----
    def plan(self, price: float, usdc: float, btc: float,
             open_oids: set[int]) -> list[Action]:
        actions = self._reconcile(open_oids)

        if self.first is None:
            # Пустой/неудавшийся снимок не должен «залипать» как first=False:
            # если балансов нет вообще, просто ждём следующего тика.
            if usdc <= 0 and btc <= 0:
                return actions
            self.first = btc <= 1e-12 and usdc >= self.cfg.min_notional

        V = usdc + btc * price
        if V <= 0:
            return actions

        # бутстрап: единственный маркет за жизнь стратегии
        if self.first:
            # 0.1% запаса на taker-комиссию, чтобы ордер не отклонили
            usd = min(self.cfg.f0 * V, usdc * 0.999)
            if usd >= self.cfg.min_notional:
                actions.append(Action("market_buy", usd=usd))
                self.first = False
            return actions

        w = btc * price / V
        free_usdc = max(0.0, usdc - self._reserved_buy())
        spent_usd = 0.0

        # ---------- TRIM ----------
        alive = bool(self.trim_orders)
        if w > self.cfg.f0 + self.cfg.band:
            q_tot = min((w - self.cfg.f0) * V / price,
                        max(0.0, btc - self._reserved_sell()))
            if self.trim_anchor is None or price > self.trim_anchor or not alive:
                for o in self.trim_orders:
                    actions.append(Action("cancel", oid=o.oid, order=o))
                self.trim_orders = []
                if q_tot * price >= self.cfg.n * self.cfg.min_notional:
                    per = q_tot / self.cfg.n
                    for k in range(1, self.cfg.n + 1):
                        px = self._px(price * (1 + k * self.cfg.delta))
                        qty = self._qty(per)
                        if self._ok(qty, px):
                            o = TrackedOrder(None, "sell", px, qty, "trim")
                            self.trim_orders.append(o)
                            actions.append(Action("place", side="SELL", price=px,
                                                  qty=qty, tag="trim", order=o))
                    self.trim_anchor = price
                else:
                    self.trim_anchor = None
        else:
            for o in self.trim_orders:
                actions.append(Action("cancel", oid=o.oid, order=o))
            self.trim_orders = []
            self.trim_anchor = None

        # ---------- DEFC ----------
        alive = bool(self.defc_orders)
        if w < self.cfg.f0 - self.cfg.band:
            budget = min((self.cfg.f0 - w) * V, 0.999 * usdc)
            if self.defc_anchor is None or price > self.defc_anchor or not alive:
                for o in self.defc_orders:
                    actions.append(Action("cancel", oid=o.oid, order=o))
                self.defc_orders = []
                if budget >= self.cfg.n * self.cfg.min_notional:
                    per_usd = budget / self.cfg.n
                    for k in range(1, self.cfg.n + 1):
                        px = self._px(price * (1 - k * self.cfg.delta))
                        qty = self._qty(per_usd / px) if px > 0 else 0.0
                        cost = qty * px
                        if self._ok(qty, px) and free_usdc - spent_usd >= cost * 1.001:
                            o = TrackedOrder(None, "buy", px, qty, "defc")
                            self.defc_orders.append(o)
                            actions.append(Action("place", side="BUY", price=px,
                                                  qty=qty, tag="defc", order=o))
                            spent_usd += qty * px
                    self.defc_anchor = price
                else:
                    self.defc_anchor = None
        else:
            for o in self.defc_orders:
                actions.append(Action("cancel", oid=o.oid, order=o))
            self.defc_orders = []
            self.defc_anchor = None

        # ---------- MICRO ----------
        n_micro = len(self.micro_buys) + len(self.micro_sells)
        if n_micro < self.cfg.micro_max and w < self.cfg.f0 + self.cfg.band:
            usd = min(self.cfg.micro_lot * V, free_usdc - spent_usd)
            if usd >= self.cfg.min_notional:
                px = self._px(price * (1 - self.cfg.delta))
                qty = self._qty(usd / px) if px > 0 else 0.0
                if self._ok(qty, px):
                    o = TrackedOrder(None, "buy", px, qty, "micro")
                    self.micro_buys.append(o)
                    actions.append(Action("place", side="BUY", price=px,
                                          qty=qty, tag="micro", order=o))

        return actions


# ---------------------------------------------------------------------------
# Боевой раннер
# ---------------------------------------------------------------------------

def _extract_oid(result: dict) -> int | None:
    try:
        statuses = (result.get("response", {})
                    .get("data", {})
                    .get("statuses", []))
        if not statuses:
            return None
        st = statuses[0]
        for key in ("resting", "filled"):
            if key in st and st[key].get("oid") is not None:
                return int(st[key]["oid"])
    except Exception:
        pass
    return None


def _btc_balance(balances: dict, token: str) -> float:
    for key in (token, f"U{token}", token.upper(), f"U{token.upper()}"):
        if key in balances:
            return float(balances[key])
    return 0.0


def _is_token_order(order: dict, token: str) -> bool:
    coin = str(order.get("coin", "")).upper()
    t = token.upper()
    known = {t, f"U{t}"}
    if t == "BTC":
        known |= {"@142", "10142"}
    return coin in known


async def _snapshot(client, token: str):
    balances = await client.browser.get_all_balances()
    price = float(await client.browser.get_token_price(token))
    orders = await client.browser.get_account_orders()
    ours = [o for o in orders if _is_token_order(o, token)]
    open_oids = {int(o["oid"]) for o in ours if o.get("oid") is not None}
    usdc = float(balances.get("USDC", 0.0))
    btc = _btc_balance(balances, token)
    return price, usdc, btc, open_oids, ours


async def _cancel_all(client, token: str) -> int:
    _, _, _, _, orders = await _snapshot(client, token)
    n = 0
    for o in orders:
        oid = o.get("oid")
        if oid is None:
            continue
        try:
            await client.browser.cancel_order(token, int(oid))
            n += 1
        except Exception as e:
            logger.warning(f"[{client.label}] cancel {oid} failed: {e}")
    return n


async def _execute(client, token: str, engine: GridRBEngine,
                   actions: list[Action], price: float, dry_run: bool) -> list[str]:
    notes: list[str] = []
    # сначала отмены, потом маркет, потом лимитки
    ordered = ([a for a in actions if a.kind == "cancel"]
               + [a for a in actions if a.kind == "market_buy"]
               + [a for a in actions if a.kind == "place"])

    for a in ordered:
        try:
            if a.kind == "cancel":
                if a.oid is not None:
                    if not dry_run:
                        await client.browser.cancel_order(token, int(a.oid))
                    notes.append(f"cancel {a.oid}")
            elif a.kind == "market_buy":
                if dry_run:
                    notes.append(f"market buy ${a.usd:.2f}")
                else:
                    # буфер на проскальзывание, чтобы IOC-маркет точно исполнился
                    slip = float(getattr(settings, "GRIDRB_MARKET_SLIP", 0.01))
                    await client.place_order(
                        token=token, side="BUY", order_type="MARKET",
                        usdc_amount=a.usd, price=price * (1 + slip))
                    notes.append(f"market buy ${a.usd:.2f}")
            elif a.kind == "place":
                if dry_run:
                    notes.append(f"{a.side} {a.qty:.6f} @ {a.price:.2f} [{a.tag}]")
                    engine.drop(a)
                    continue
                res = await client.place_order(
                    token=token, side=a.side, order_type="LIMIT",
                    quantity=a.qty, price=a.price)
                oid = _extract_oid(res)
                if oid is not None:
                    engine.register(a, oid)
                else:
                    # oid не пришёл: не отслеживаем, следующий тик снимет как сироту
                    engine.drop(a)
                    logger.warning(
                        f"[{client.label}] no oid for {a.side} {a.qty} @ {a.price}")
                notes.append(f"{a.side} {a.qty:.6f} @ {a.price:.2f} [{a.tag}]")
        except Exception as e:
            if a.kind == "market_buy":
                engine.first = True  # бутстрап не удался — повторим на следующем тике
            engine.drop(a)
            logger.error(f"[{client.label}] action {a.kind} {a.tag} failed: {e}")
            notes.append(f"ERROR {a.kind} {a.tag}: {e}")
    return notes


async def run_gridrb(client, token: str, max_iterations: int = 0,
                     poll_interval: float | None = None,
                     watch_seconds: float | None = None) -> str:
    """Один цикл GridRB. max_iterations=0 — бесконечно (до Ctrl+C)."""
    cfg = GridRBConfig.from_settings()
    poll = float(poll_interval or getattr(settings, "GRIDRB_POLL_INTERVAL", 60))
    dry_run = bool(getattr(settings, "GRIDRB_DRY_RUN", False))

    await client.browser.start_session()
    _, _, sz_decimals = await client.get_token_limits(token)

    removed = await _cancel_all(client, token)
    if removed:
        logger.info(f"[{client.label}] старт: снято {removed} старых ордеров, "
                    f"лестницы строятся заново")
        await asyncio.sleep(1.0)

    engine = GridRBEngine(cfg, sz_decimals=sz_decimals)
    started = time.monotonic()
    i = 0

    logger.info(
        f"[{client.label}] GridRB {cfg.f0:.2f}/{1-cfg.f0:.2f} | "
        f"band={cfg.band:.2f} delta={cfg.delta*1000:.1f}‰ n={cfg.n} "
        f"micro={cfg.micro_lot*100:.0f}%×{cfg.micro_max} | dry_run={dry_run}")

    sent_start = False
    while True:
        i += 1
        try:
            price, usdc, btc, open_oids, _ = await _snapshot(client, token)
            if not sent_start:
                V = usdc + btc * price
                logger.info(
                    f"[{client.label}] price=${price:,.2f} USDC={usdc:,.2f} "
                    f"BTC={btc:.6f} equity=${V:,.2f} weight={btc*price/V*100 if V else 0:.1f}%")
                sent_start = True

            actions = engine.plan(price, usdc, btc, open_oids)
            notes = await _execute(client, token, engine, actions, price, dry_run)
            if notes:
                logger.info(f"[{client.label}] iter {i}: " + "; ".join(notes))
        except Exception as e:
            logger.error(f"[{client.label}] iteration {i} failed: {e}")

        if max_iterations and i >= max_iterations:
            break
        if watch_seconds and (time.monotonic() - started) >= watch_seconds:
            break
        await asyncio.sleep(poll)

    logger.info(f"[{client.label}] GridRB остановлен после {i} итераций")
    return "Ended"