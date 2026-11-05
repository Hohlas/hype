#!/usr/bin/env python3
"""Реализации стратегий GridRB, «Два режима 70/30» и Trend-Adaptive Grid.

Каждая стратегия — класс с методами:
    on_close(i, bar)            — вызывается после исполнения ордеров бара i;
    on_fill(i, order, px, qty)  — вызывается на каждое исполнение.

Все они торгуют через один и тот же Broker (tests/backtest/engine.py), поэтому
правила исполнения и комиссии у них идентичны.

ВАЖНО: исторические спецификации стратегий местами описаны словами и допускают
трактовки. Ниже в docstring каждого класса перечислены принятые допущения.
"""
from __future__ import annotations

from collections import deque

from engine import Broker, MIN_NOTIONAL, SLIP


# ===========================================================================
# GridRB — точная формализация из docs/GridRB_spec.md
# ===========================================================================

class GridRB:
    """GridRB по docs/GridRB_spec.md §5, §7 с расширениями для экспериментов.

    Базовые параметры: f0, band, delta, n, micro_lot, micro_max.
    Расширения (по умолчанию выключены = исходный GridRB):
      - trim_band / defc_band — асимметричные полосы продаж и покупок;
      - early_lot — маленькая «ранняя» покупка ВНУТРИ полосы (f0-band <= w < f0),
        размером early_lot*V, до того как вес дойдёт до зоны defc;
      - dyn_band — динамика покупательной полосы от просадки:
        defc_band_eff = defc_band + dyn_band * (ATH-P)/ATH.
    """

    def __init__(self, cash: float, f0: float = 0.70, band: float = 0.05,
                 delta: float = 0.005, n: int = 5, micro_lot: float = 0.02,
                 micro_max: int = 3, stress: float = 1.0,
                 trim_band: float | None = None,
                 defc_band: float | None = None,
                 early_lot: float = 0.0,
                 dyn_band: float = 0.0):
        self.b = Broker(cash, stress=stress)
        self.f0 = f0
        self.band = band
        self.trim_band = band if trim_band is None else trim_band
        self.defc_band = band if defc_band is None else defc_band
        self.delta = delta
        self.n = n
        self.micro_lot = micro_lot
        self.micro_max = micro_max
        self.early_lot = early_lot
        self.dyn_band = dyn_band

        self.name = (f"GridRB {int(round(f0 * 100))}/"
                     f"{int(round((1 - f0) * 100))}")
        self.first = True
        self.ath: float | None = None
        self.trim_anchor: float | None = None
        self.defc_anchor: float | None = None
        self.trim_ods: list = []
        self.defc_ods: list = []
        self.early_ods: list = []

    # ---- реакция на исполнения ----
    def on_fill(self, i: int, od, px: float, qty: float) -> None:
        if od.side == "buy" and od.tag == "micro":
            self.b.place_limit_sell(px * (1 + self.delta), qty, "micro_take", i + 1)

    # ---- основной шаг ----
    def on_close(self, i: int, bar) -> None:
        c = bar.c
        self.ath = c if self.ath is None else max(self.ath, c)

        if self.first:
            self.b.market_buy(self.f0 * self.b.equity(c), c, i)
            self.first = False

        V = self.b.equity(c)
        w = self.b.weight(c)

        # динамика покупательной полосы от просадки
        dd = (self.ath - c) / self.ath if self.ath > 0 else 0.0
        defc_band_eff = self.defc_band
        if self.dyn_band:
            defc_band_eff = max(0.005, self.defc_band + self.dyn_band * dd)

        # ---- TRIM ----
        alive = any(o.alive for o in self.trim_ods)
        if w > self.f0 + self.trim_band:
            q_tot = min((w - self.f0) * V / c, self.b.B - self.b.reserved_sell())
            if self.trim_anchor is None or c > self.trim_anchor or not alive:
                self.b.cancel_all(self.trim_ods)
                self.trim_ods = []
                if q_tot * c >= self.n * MIN_NOTIONAL:
                    per = q_tot / self.n
                    for k in range(1, self.n + 1):
                        od = self.b.place_limit_sell(
                            c * (1 + k * self.delta), per, "trim", i + 1)
                        if od:
                            self.trim_ods.append(od)
                    self.trim_anchor = c
                else:
                    self.trim_anchor = None
            else:
                per = q_tot / self.n
                for od in self.trim_ods:
                    od.qty = per
        else:
            self.b.cancel_all(self.trim_ods)
            self.trim_ods = []
            self.trim_anchor = None

        # ---- DEFC ----
        alive = any(o.alive for o in self.defc_ods)
        if w < self.f0 - defc_band_eff:
            budget = min((self.f0 - w) * V, 0.999 * self.b.U)
            if self.defc_anchor is None or c > self.defc_anchor or not alive:
                self.b.cancel_all(self.defc_ods)
                self.defc_ods = []
                if budget >= self.n * MIN_NOTIONAL:
                    per_usd = budget / self.n
                    for k in range(1, self.n + 1):
                        level = c * (1 - k * self.delta)
                        od = self.b.place_limit_buy(
                            level, per_usd, "defc", i + 1)
                        if od:
                            self.defc_ods.append(od)
                    self.defc_anchor = c
                else:
                    self.defc_anchor = None
            else:
                per_usd = budget / self.n
                for od in self.defc_ods:
                    od.qty = per_usd / od.price
        else:
            self.b.cancel_all(self.defc_ods)
            self.defc_ods = []
            self.defc_anchor = None

        # ---- EARLY (ранняя докупка внутри полосы) ----
        alive_early = any(o.alive for o in self.early_ods)
        in_window = (self.f0 - defc_band_eff) <= w < self.f0
        if self.early_lot > 0 and in_window:
            budget = min(self.early_lot * V, self.b.free_usdc())
            if budget >= MIN_NOTIONAL and not alive_early:
                self.b.cancel_all(self.early_ods)
                self.early_ods = []
                od = self.b.place_limit_buy(
                    c * (1 - self.delta), budget, "early", i + 1)
                if od:
                    self.early_ods.append(od)
        else:
            self.b.cancel_all(self.early_ods)
            self.early_ods = []

        # ---- MICRO ----
        n_micro = (sum(1 for o in self.b.orders
                       if o.alive and o.side == "buy" and o.tag == "micro")
                   + sum(1 for o in self.b.orders
                         if o.alive and o.side == "sell" and o.tag == "micro_take"))
        if n_micro < self.micro_max and w < self.f0 + self.trim_band:
            usd = min(self.micro_lot * V, self.b.free_usdc())
            if usd >= MIN_NOTIONAL:
                self.b.place_limit_buy(
                    c * (1 - self.delta), usd, "micro", i + 1)


# ===========================================================================
# «Два режима 70/30» — историческая спецификация
# ===========================================================================

class TwoModes7030:
    """Два режима (рост/просадка) + ребалансировка доли BTC.

    Принятые допущения (спецификация неоднозначна):
    - H_L — максимум high за последние N=168 баров (неделя). N в спеке не задан.
    - Режим инициализируется как B (просадка), чтобы разрешить первый вход.
    - Параметры по умолчанию: S=500, q=5%, AGGR=0.5, alpha=1.0, beta=0.25,
      eps=0.10, k=2, TARGET=0.70, MAX=0.85, k_up=0.97, k_down=0.85.
    - В режиме A лимит на покупку ставится только если ATH-k*S < текущей цены.
    - Продажа на тейке в режиме A: qty*(1+eps), но не больше свободного BTC.
    - В режиме B размер входа = min(q_eff*free_usdc, дефицит до TARGET*E/c).
    - «Перестановка устаревшего усреднения» — при c < min_TP - 2.5*S_eff.
    - Вдогонку в режиме B — маркет, если c > max_TP - AGGR*S_eff.
    - Релокация в режиме A при c > Base+3S: снять старые тейки, Base := c.
    """

    name = "Два режима 70/30"

    def __init__(self, cash: float, S: float = 500.0, q: float = 0.05,
                 AGGR: float = 0.5, alpha: float = 1.0, beta: float = 0.25,
                 eps: float = 0.10, k: float = 2.0, target: float = 0.70,
                 max_btc: float = 0.85, k_up: float = 0.97,
                 k_down: float = 0.85, N: int = 168, stress: float = 1.0,
                 rebalance: str = "always"):
        self.b = Broker(cash, stress=stress)
        self.S = S
        self.q = q
        self.AGGR = AGGR
        self.alpha = alpha
        self.beta = beta
        self.eps = eps
        self.k = k
        self.target = target
        self.max_btc = max_btc
        self.k_up = k_up
        self.k_down = k_down
        self.N = N
        # "always"  — докупка до целевой доли на каждом шаге (ядро 70/30);
        # "gated"   — то же, но только когда нет висящего усреднения;
        # "none"    — только три типа входов из «Режим B — правила».
        self.rebalance = rebalance

        self.mode = "B"
        self.ath: float | None = None
        self.base: float | None = None
        self.positions: list[dict] = []   # {'qty','entry','order'}
        self.avg_order = None
        self._highs: deque[float] = deque(maxlen=N)

    # ---- вспомогательное ----
    def _hl(self) -> float:
        return max(self._highs) if self._highs else 0.0

    def _deficit_usd(self, c: float) -> float:
        E = self.b.equity(c)
        w = self.b.weight(c)
        return max(0.0, (self.target - w) * E)

    def _add_position(self, i: int, entry_px: float, qty: float,
                      step: float, sell_factor: float) -> None:
        avail = max(0.0, self.b.B - self.b.reserved_sell())
        sell_qty = min(qty * sell_factor, avail)
        od = self.b.place_limit_sell(entry_px + step, sell_qty, "tp", i + 1)
        self.positions.append({"qty": qty, "entry": entry_px, "order": od})

    def _market_entry(self, i: int, c: float, usd: float,
                      step: float, sell_factor: float) -> None:
        qty = self.b.market_buy(usd, c, i)
        if qty > 0:
            self._add_position(i, c * (1 + self.b.slip), qty, step, sell_factor)

    def _cancel_avg(self) -> None:
        if self.avg_order is not None and self.avg_order.alive:
            self.b.cancel(self.avg_order)
        self.avg_order = None

    # ---- исполнения ----
    def on_fill(self, i: int, od, px: float, qty: float) -> None:
        if od.side == "sell" and od.tag == "tp":
            self.positions = [p for p in self.positions if p["order"] is not od]
            if getattr(od, "base_after", None) is not None:
                self.base = od.base_after
        elif od.side == "buy" and od.tag in ("avg", "limitA"):
            self.avg_order = None
            self._add_position(i, px, qty, od.extra_step, od.extra_factor)

    # ---- основной шаг ----
    def on_close(self, i: int, bar) -> None:
        c = bar.c
        self._highs.append(bar.h)
        if self.ath is None:
            self.ath = c
            self.base = c
        self.ath = max(self.ath, c)

        HL = self._hl()
        E = self.b.equity(c)
        w = self.b.weight(c)

        # --- режим ---
        if self.mode == "A":
            if c < 0.90 * HL or c < self.base - self.S:
                self.mode = "B"
        else:
            if c > self.k_up * HL and c > self.base + 0.5 * self.S:
                self.mode = "A"

        if self.mode == "A":
            self._step_A(i, bar, c, E, w)
        else:
            self._step_B(i, bar, c, E, w, HL)

    def _step_A(self, i, bar, c, E, w) -> None:
        self._cancel_avg()
        S = self.S
        # релокация: цена ушла выше Base+3S без новых входов
        if c > self.base + 3 * S and self.positions:
            for p in self.positions:
                if p["order"] is not None and p["order"].alive:
                    self.b.cancel(p["order"])
            self.positions = []
            self.base = c

        # лимитная покупка на откате от ATH
        already = any(o.alive and o.tag == "limitA" for o in self.b.orders)
        level = self.ath - self.k * S
        if not already and level < c:
            usd = min(self.q * self.b.free_usdc(), self.b.free_usdc())
            od = self.b.place_limit_buy(level, usd, "limitA", i + 1)
            if od is not None:
                od.extra_step = S
                od.extra_factor = 1 + self.eps
                od.base_after = level

    def _step_B(self, i, bar, c, E, w, HL) -> None:
        S = self.S
        depth = max(0.0, (HL - c) / HL) if HL > 0 else 0.0
        q_eff = self.q * (1 + self.alpha * depth)
        S_eff = S * (1 - self.beta * depth)

        can_buy = w < self.max_btc
        free = self.b.free_usdc()

        # ребалансировка к целевой доле (ядро 70/30 из спеки): пока недобор,
        # докупаем; размер ограничен q_eff*free и дефицитом до TARGET.
        if self.rebalance == "always" and can_buy:
            usd = min(q_eff * free, self._deficit_usd(c))
            if usd >= MIN_NOTIONAL:
                self._market_entry(i, c, usd, S_eff, 1.0)
        elif self.rebalance == "gated" and can_buy and self.avg_order is None:
            usd = min(q_eff * free, self._deficit_usd(c))
            if usd >= MIN_NOTIONAL:
                self._market_entry(i, c, usd, S_eff, 1.0)
        elif self.rebalance == "none" and can_buy:
            if not self.positions and (self.avg_order is None
                                       or not self.avg_order.alive):
                usd = min(q_eff * free, self._deficit_usd(c))
                if usd >= MIN_NOTIONAL:
                    self._market_entry(i, c, usd, S_eff, 1.0)

        # усреднение
        if self.positions and can_buy:
            min_tp = min((p["entry"] + S_eff) for p in self.positions)
            level = min_tp - 2 * S_eff
            stale_level = min_tp - 2.5 * S_eff
            if self.avg_order is not None and not self.avg_order.alive:
                self.avg_order = None
            if self.avg_order is None:
                usd = min(q_eff * free, self._deficit_usd(c), free)
                od = self.b.place_limit_buy(level, usd, "avg", i + 1)
                if od is not None:
                    od.extra_step = S_eff
                    od.extra_factor = 1.0
                    self.avg_order = od
            elif c < stale_level:
                self.b.cancel(self.avg_order)
                self.avg_order = None

        # вдогонку (низкий приоритет — только если нет усреднения)
        if self.positions and can_buy and self.avg_order is None:
            max_tp = max(p["entry"] + S_eff for p in self.positions)
            if c > max_tp - self.AGGR * S_eff:
                usd = min(q_eff * free, self._deficit_usd(c), free)
                if usd >= MIN_NOTIONAL:
                    self._market_entry(i, c, usd, S_eff, 1.0)


# ===========================================================================
# Trend-Adaptive Grid — точное описание: docs/Trend-Adaptive_Grid.md
# ===========================================================================

class TrendAdaptiveGrid:
    """Ядро (50%) + Спутник (сетка).

    Принятые допущения (спецификация неоднозначна):
    - Старт: 50% капитала — маркет в Ядро, остаток — USDC под сетку.
    - Спутник: первый вход — 5% свободного USDC маркетом, когда нет открытых
      сеточных позиций; усреднение — лимит на `последний вход - STEP`, размер
      5% свободного USDC; вдогонку — маркет, если цена выше max(TP)-0.5*STEP и
      доля BTC < TARGET(70%), размер убывает линейно: q*USDC*(1-w/0.7).
    - Ядро докупается только при первом пересечении вниз уровня 0.9*ATH
      (триггер взводится заново на новом ATH), размер — 10% свободного USDC.
    - Защёлка: на новом ATH половина BTC Спутника переносится в Ядро; тейки
      оставшейся половины сохраняются.
    - STEP=500, размер сетки 5% (как в ask.md).
    - Исправленный дефект порта: при пустом списке позиций живая avg-заявка
      блокировала вход; теперь устаревшая avg снимается
      (см. docs/Trend-Adaptive_Grid.md §6).
    """

    name = "Trend-Adaptive Grid"

    def __init__(self, cash: float, STEP: float = 500.0, q: float = 0.05,
                 core_pct: float = 0.50, target: float = 0.70,
                 dip_pct: float = 0.10, dip_usd_pct: float = 0.10,
                 stress: float = 1.0):
        self.b = Broker(cash, stress=stress)
        self.STEP = STEP
        self.q = q
        self.core_pct = core_pct
        self.target = target
        self.dip_pct = dip_pct
        self.dip_usd_pct = dip_usd_pct

        self.core_qty = 0.0
        self.ath: float | None = None
        self.positions: list[dict] = []   # {'qty','entry','order'}
        self.avg_order = None
        self.last_entry: float | None = None
        self.dip_armed = True
        self._init_done = False

    def _add_position(self, i, entry_px, qty) -> None:
        od = self.b.place_limit_sell(entry_px + self.STEP, qty, "sat_tp", i + 1)
        self.positions.append({"qty": qty, "entry": entry_px, "order": od})

    def _cancel_avg(self) -> None:
        if self.avg_order is not None and self.avg_order.alive:
            self.b.cancel(self.avg_order)
        self.avg_order = None

    def on_fill(self, i, od, px, qty):
        if od.side == "sell" and od.tag == "sat_tp":
            self.positions = [p for p in self.positions if p["order"] is not od]
        elif od.side == "buy" and od.tag == "sat_avg":
            self.avg_order = None
            self._add_position(i, px, qty)

    def on_close(self, i, bar):
        c = bar.c
        if not self._init_done:
            qty = self.b.market_buy(self.core_pct * self.b.equity(c), c, i)
            self.core_qty += qty
            self._init_done = True
            self.ath = c
            self.dip_armed = True
            return

        prev_ath = self.ath
        self.ath = max(self.ath, c)
        is_new_ath = c > prev_ath

        # --- защёлка на новом ATH: половину BTC Спутника -> Ядро ---
        if is_new_ath and self.positions:
            for p in self.positions:
                move = p["qty"] * 0.5
                p["qty"] -= move
                self.core_qty += move
                if p["order"] is not None and p["order"].alive:
                    p["order"].qty = p["qty"]
                    if p["qty"] * p["order"].price < MIN_NOTIONAL:
                        self.b.cancel(p["order"])
            self.positions = [p for p in self.positions
                              if p["order"] is not None and p["order"].alive]
            self.dip_armed = True

        E = self.b.equity(c)
        w = self.b.weight(c)
        free = self.b.free_usdc()

        # --- докупка Ядра в просадке ---
        if c < (1 - self.dip_pct) * self.ath and self.dip_armed:
            usd = self.dip_usd_pct * free
            if usd >= MIN_NOTIONAL:
                qty = self.b.market_buy(usd, c, i)
                self.core_qty += qty
                self.dip_armed = False

        # --- первый вход Спутника ---
        # Если открытых позиций нет, устаревшая заявка на усреднение не должна
        # блокировать вход. Иначе стратегия навсегда залипает: позиций нет,
        # а живая avg-заявка не даёт ни войти, ни усредняться (баг порта,
        # найден на 3-летнем окне: 6 сделок и 2.8 года простоя).
        if not self.positions:
            self._cancel_avg()
            usd = self.q * free
            if usd >= MIN_NOTIONAL:
                qty = self.b.market_buy(usd, c, i)
                if qty > 0:
                    self.last_entry = c * (1 + self.b.slip)
                    self._add_position(i, self.last_entry, qty)
            return

        # --- усреднение Спутника ---
        if self.positions and self.last_entry is not None:
            level = self.last_entry - self.STEP
            if self.avg_order is not None and not self.avg_order.alive:
                self.avg_order = None
            if self.avg_order is None:
                usd = self.q * free
                od = self.b.place_limit_buy(level, usd, "sat_avg", i + 1)
                if od is not None:
                    self.avg_order = od
            elif self.avg_order is not None and self.avg_order.price > level + self.STEP:
                self.b.cancel(self.avg_order)
                self.avg_order = None

        # --- вдогонку ---
        if self.positions and w < self.target:
            max_tp = max(p["entry"] + self.STEP for p in self.positions)
            if c > max_tp - 0.5 * self.STEP:
                size_pct = self.q * max(0.0, 1 - w / self.target)
                usd = size_pct * free
                if usd >= MIN_NOTIONAL:
                    qty = self.b.market_buy(usd, c, i)
                    if qty > 0:
                        self.last_entry = c * (1 + self.b.slip)
                        self._add_position(i, self.last_entry, qty)