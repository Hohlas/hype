# GridRB — точная формализация боевого алгоритма

Соответствует коду: `modules/gridrb_strategy.py`, класс `GridRBEngine`; конфигурация —
`GridRBConfig.from_settings()` (`settings.py`, префикс `GRIDRB_*`). Бэктест-стенд,
который гоняет **тот же** движок: `tests/backtest/minute_replay.py`.

> Историческая справка: до 2026-11 документ описывал исследовательский вариант
> 70/30 из `research/backtest_lib.py` (файла больше нет). Боевой алгоритм с тех
> пор изменился: 80/20, добавлены ранняя докупка и гистерезис перестановки.
> Отличия от старой версии — в §10.

---

## 1. Идея

GridRB — контроллер веса (ребалансер 80/20), «исполненный сеткой»: вместо
рыночных ребалансов при выходе веса BTC за полосу выставляются лестницы
лимитных ордеров (maker), а внутри полосы работают ранняя докупка и микро-сетка.
Никаких индикаторов, трендовых фильтров, плеча, шортов, стопов. Только спот,
long-only. За всю жизнь стратегии ровно один маркет-ордер — бутстрап на старте
(если счёт пуст).

## 2. Решающий шаг и живой контур

Алгоритм — чистая функция `plan(price, usdc, btc, open_oids) -> actions`
(см. §5). Живой контур (`run_gridrb`):

1. старт: сессия, лимиты токена (`sz_decimals`), **снятие всех ордеров** по счёту;
2. цикл: снимок балансов/цены/ордеров → `plan()` → исполнение действий →
   журналы/Telegram → `sleep(poll)`.

Важные следствия:

- Внутреннее состояние (`first`, якоря, списки ордеров) живёт в памяти процесса.
  На рестарте оно **не** восстанавливается — старые ордера снимаются, лестницы
  строятся заново от текущей цены. Балансы и позиция восстанавливаются с биржи.
- `poll` ≥ 60 с (`main.py`): одно решение не чаще раза в минуту. За один цикл
  может быть выставлено несколько ордеров (лестница из `n` + микро/ранняя).
- Исполнения на бирже могут происходить в любой момент; бот узнаёт о них на
  следующем опросе через сверку (`_reconcile`, §5.5).
- `GRIDRB_DRY_RUN=True` — действия только логируются, ордера не отправляются.
- Журналы: `gridrb_events.csv` (каждое событие) и `gridrb_state.csv` (снимок
  раз в `GRIDRB_STATE_CSV_EVERY` итераций); Telegram шлёт события, отмены — по
  флагу `GRIDRB_TG_CANCELS`.
- Если бутстрап-маркет не удался, флаг `first` сбрасывается и попытка
  повторяется на следующем тике. Если лимитка не получила `oid`, действие
  снимается с учёта — «сироту» снимет сверка на следующем цикле.

## 3. Параметры (боевые значения)

| Символ | `settings.py` | Значение | Смысл |
|---|---|---|---|
| f0 | `GRIDRB_F0` | **0.80** | целевая доля BTC |
| band | `GRIDRB_BAND` | **0.04** | полуширина полосы (±4 п.п.) |
| δ | `GRIDRB_DELTA` | **0.004** | шаг лестниц и микро-сетки (0.4 %) |
| n | `GRIDRB_LEVELS` | **5** | число уровней лестницы |
| micro_lot | `GRIDRB_MICRO_LOT` | **0.02** | лот микро-сетки (2 % от V) |
| micro_max | `GRIDRB_MICRO_MAX` | **3** | максимум микро-ордеров (обе ноги) |
| MIN_NOTIONAL | `GRIDRB_MIN_NOTIONAL` | **$10** | минимальный ордер |
| early_lot | `GRIDRB_EARLY_LOT` | **0.15** | ранняя докупка внутри полосы (15 % V) |
| rearm_eps | `GRIDRB_REARM_EPS` | **0.002** | переставлять лестницу при уходе цены на 0.2 % |
| rearm_cooldown | `GRIDRB_REARM_COOLDOWN` | **300 с** | и не чаще раза в 5 минут |
| poll | `GRIDRB_POLL_INTERVAL` | **60 с** | пауза между итерациями |

Производные зоны (при f0=0.80, band=0.04):

- продажи избытка (trim): **w > 0.84**;
- микро-сетка: **w < 0.84** (включая зоны ниже);
- ранняя докупка: **0.76 ≤ w < 0.80**;
- докупка к цели (defc): **w < 0.76**.

## 4. Внутреннее состояние

- `first` — флаг бутстрапа (`None` — ещё не знаем, `True` — счёт пуст, `False` — позиция есть);
- `trim_anchor`, `defc_anchor` — цена, от которой построена текущая лестница (∅ — нет);
- `trim_orders`, `defc_orders`, `early_orders`, `micro_buys`, `micro_sells` — отслеживаемые ордера;
- `last_trim_rearm`, `last_defc_rearm` — момент последней перестановки (для кулдауна).

## 5. Алгоритм (порядок внутри `plan`: сверка → бутстрап → trim → defc → early → micro)

### 5.0 Сверка с биржей (`_reconcile`)

- Любой открытый ордер, которого нет в отслеживаемых, **снимается** (защита от дублей).
- Отслеживаемый ордер, которого больше нет на бирже, считается **исполненным**:
  генерируется событие `fill`; для `micro` сразу выставляется тейк (`micro_take`).
  Ордер убирается из списка.

### 5.1 Бутстрап

Если `first is None` и на счёте нет ни USDC, ни BTC — ждём следующего тика.
Иначе `first = (btc ≤ 1e-12 и usdc ≥ MIN_NOTIONAL)`.
Если `first`: `usd = min(f0·V, 0.999·usdc)`; если `usd ≥ MIN_NOTIONAL` —
маркет-покупка (в live — MARKET-ордер с ценовым буфером
`GRIDRB_MARKET_SLIP` = 1 %, чтобы IOC точно исполнился), `first = False`;
выход из шага. На счёте с BTC бутстрапа нет.

### 5.2 Лестница продаж «trim» — зона w > f0+band

1. `q_tot = min((w − f0)·V/c, max(0, B − reserved_sell))`, где
   `reserved_sell = Σ q` по `trim_orders + micro_sells`.
2. Перестроить лестницу, если `_need_rearm(trim_anchor, alive, c, last_trim_rearm, now)`:
   - `alive` — есть хотя бы один живой trim-ордер;
   - перестановка при: нет якоря **или** нет живых ордеров (T1/T3), **или**
     `c > anchor·(1+rearm_eps)` **и** с прошлой перестановки прошло ≥ `rearm_cooldown` (T2).
   Перестроение: снять все trim-ордера; если `q_tot·c < n·MIN_NOTIONAL` —
   лестницы нет, якорь = ∅; иначе `n` sell-лимиток `S_k = _px(c·(1+k·δ))`,
   `qty = _qty(q_tot/n)`, только если `_ok(qty, S_k)`; якорь = c, `last_trim_rearm = now`.
3. Если перестановки нет — **ничего не делать**: и уровни, и объёмы заморожены.
4. Если w ≤ f0+band: снять все trim-ордера, якорь = ∅.

> Отличие от старой версии: объёмы стоящей лестницы **не** пересчитываются.

### 5.3 Лестница покупок «defc» — зона w < f0−band

1. `budget = min((f0 − w)·V, 0.999·usdc)`.
2. Перестроить при том же `_need_rearm` (якорь `defc_anchor`, кулдаун `last_defc_rearm`):
   снять все defc-ордера; если `budget < n·MIN_NOTIONAL` — лестницы нет, якорь = ∅;
   иначе `n` buy-лимиток `L_k = _px(c·(1−k·δ))`, `qty = _qty((budget/n)/L_k)`,
   ставится только если `_ok(qty, L_k)` **и** `free_usdc − spent_usd ≥ qty·L_k·1.001`
   (защита кэша); `spent_usd` накапливается; якорь = c.
3. Если перестановки нет — ничего не делать (уровни и объёмы заморожены).
4. Если w ≥ f0−band: снять все defc-ордера, якорь = ∅.

### 5.4 Ранняя докупка «early» — зона (f0−band) ≤ w < f0

- Если `early_lot > 0` и вес в окне:
  - при отсутствии живого early-ордера: `budget = min(early_lot·V, free_usdc − spent_usd)`,
    `px = _px(c·(1−δ))`, `qty = _qty(budget/px)`; если `_ok(qty, px)` —
    buy-лимит `early`; `spent_usd += qty·px`.
- Вне окна: снять все early-ордера.

У ранней докупки **нет** собственного тейка: купленное продаётся только через
trim при w > f0+band.

### 5.5 Микро-сетка — разрешена при w < f0+band

- `n_micro = #(живые micro) + #(живые micro_take)`; если `n_micro < micro_max`:
  - `available = free_usdc − spent_usd`;
  - `usd = min(micro_lot·V, available)`;
  - `px = _px(c·(1−δ))`;
  - `qty = _qty(usd/px)`; если `qty·px < MIN_NOTIONAL` — `qty = _qty_up(MIN_NOTIONAL/px)`
    (подъём до минимума; перебор не больше одного шага размера);
  - если `_ok(qty, px)` и `qty·px ≤ available` — buy-лимит `micro`.
- За один вызов `plan()` — не более одной новой micro-заявки.
- При исполнении micro (`_reconcile`): сразу sell-лимит
  `tp = _px(fill_px·(1+δ))`, `qty = _qty(fill_qty)`, тег `micro_take`,
  `entry = fill_px`, если `_ok(qty, tp)`.

## 6. Точная семантика

**6.1 Храповик лестниц.** Перестановка — только по трём причинам: нет якоря /
нет живых ордеров / цена ушла выше якоря на `rearm_eps` (и прошёл кулдаун).
При падении цены уровни покупок замораживаются — цена «падает» в лестницу и
наполняет её; после исчерпания лестница переставляется от новой, более низкой
цены. Продажи зеркально: при росте выше якоря лестница подскакивает вверх, при
исчерпании — от новой цены.

**6.2 Выход только весовой.** У докупок defc/early нет индивидуальных тейков.
Всё купленное продаётся через trim при w > f0+band; суммарный объём trim =
избыток над f0. Если весь trim исполнится, вес вернётся точно к f0.

**6.3 Микро-цикл.** Вход на `c·(1−δ)`, тейк на `fill·(1+δ)` от фактической цены
входа. Валовая прибыль `+2δ = +0.8 %` при δ=0.4 %; нетто ≈ **+0.72 %** (две
maker-комиссии по 0.04 %). Гэп вниз улучшает вход (`fill = min(o, L)`).

**6.4 Микро-сетка катится за ценой.** Каждый вызов — максимум одна новая
micro-заявка на 0.4 % ниже последнего close. Ордера `micro`/`micro_take` внутри
сессии не переставляются; на рестарте снимаются вместе со всеми (см. §2).
Одновременно ≤ `micro_max` микро-ордеров (обе ноги).

**6.5 Кэш.** `free_usdc = max(0, usdc − Σ(price·qty) по defc + micro + early)`.
Бюджет defc считается от `usdc` (не от `free_usdc`), но каждая заявка проходит
проверку `free_usdc − spent_usd`. `early` и `micro` берут только
`free_usdc − spent_usd`. Переподписки сверх свободного кэша нет.

**6.6 Асимметрия объёмов.** defc делит бюджет поровну в USD → нижние уровни
покупают больше BTC. trim делит поровну в BTC. early — один ордер на 15 % V.

**6.7 Режимы по весу (f0=0.80, band=0.04):**

| Зона | trim | defc | early | micro-buy | micro-take |
|---|---|---|---|---|---|
| w > 0.84 | да (если избыток ≥ n·$10) | нет | нет | нет | работают |
| 0.80 ≤ w ≤ 0.84 | нет | нет | нет | да | работают |
| 0.76 ≤ w < 0.80 | нет | нет | да | да | работают |
| w < 0.76 | нет | да | нет | да | работают |

**6.8 Округления.** `_qty` — вниз до `sz_decimals`; `_qty_up` — вверх (только для
подъёма micro до минимального нотионала); `_px` — до 5 значащих цифр (требование
Hyperliquid); `_ok` — `qty>0`, `price>0`, `qty·price ≥ MIN_NOTIONAL`.

**6.9 Инварианты.** Все ордера maker, кроме одного бутстрапа; продажи только из
имеющегося B (no shorts); Σ(q висящих sell) ≤ B; ордер, на который не хватило
кэша в момент касания, не исполняется и остаётся висеть.

## 7. Псевдокод (1-в-1 с `plan()`)

```
plan(price, usdc, btc, open_oids):
    actions = reconcile(open_oids)

    if first is None:
        if usdc <= 0 and btc <= 0: return actions
        first = (btc <= 1e-12 and usdc >= MIN_NOTIONAL)
    V = usdc + btc*price
    if V <= 0: return actions
    if first:
        usd = min(f0*V, 0.999*usdc)
        if usd >= MIN_NOTIONAL: actions += market_buy(usd); first = False
        return actions

    w = btc*price / V
    free_usdc = max(0, usdc - reserved_buy())   # reserved_buy = defc + micro + early
    spent = 0

    # TRIM
    if w > f0+band:
        q_tot = min((w-f0)*V/price, max(0, btc - reserved_sell()))
        if need_rearm(trim_anchor, alive(trim), price, last_trim_rearm, now):
            cancel_all(trim); trim = []
            if q_tot*price >= n*MIN_NOTIONAL:
                for k in 1..n:
                    px = _px(price*(1+k*delta)); qty = _qty(q_tot/n)
                    if _ok(qty, px): place_sell(px, qty, "trim")
                trim_anchor = price; last_trim_rearm = now
            else: trim_anchor = None
        # иначе: ничего (уровни и объёмы заморожены)
    else:
        cancel_all(trim); trim = []; trim_anchor = None

    # DEFC
    if w < f0-band:
        budget = min((f0-w)*V, 0.999*usdc)
        if need_rearm(defc_anchor, alive(defc), price, last_defc_rearm, now):
            cancel_all(defc); defc = []
            if budget >= n*MIN_NOTIONAL:
                per = budget/n
                for k in 1..n:
                    px = _px(price*(1-k*delta)); qty = _qty(per/px); cost = qty*px
                    if _ok(qty, px) and free_usdc - spent >= cost*1.001:
                        place_buy(px, qty, "defc"); spent += cost
                defc_anchor = price; last_defc_rearm = now
            else: defc_anchor = None
    else:
        cancel_all(defc); defc = []; defc_anchor = None

    # EARLY
    if early_lot > 0 and (f0-band) <= w < f0:
        if not alive(early):
            budget = min(early_lot*V, free_usdc - spent)
            px = _px(price*(1-delta)); qty = _qty(budget/px)
            if _ok(qty, px): place_buy(px, qty, "early"); spent += qty*px
    else:
        cancel_all(early); early = []

    # MICRO
    if count(alive micro) + count(alive micro_take) < micro_max and w < f0+band:
        available = free_usdc - spent
        usd = min(micro_lot*V, available)
        px = _px(price*(1-delta))
        if px > 0 and usd >= MIN_NOTIONAL:
            qty = _qty(usd/px)
            if qty*px < MIN_NOTIONAL: qty = _qty_up(MIN_NOTIONAL/px)
            if _ok(qty, px) and qty*px <= available: place_buy(px, qty, "micro")

    return actions

need_rearm(anchor, alive, price, last_ts, now):
    if anchor is None or not alive: return True
    return price > anchor*(1+rearm_eps) and (now - last_ts) >= rearm_cooldown
```

## 8. Модель исполнения в бэктесте

`tests/backtest/engine.py` (используется `minute_replay.py`):

- maker 0.040 %, taker 0.070 %, проскальзывание маркета 0.020 %, MIN_NOTIONAL $10;
- лимитный buy исполняется, если `l ≤ L`, по `min(o, L)`; sell — если `h ≥ S`, по `max(o, S)`;
- внутри бара сначала покупки, потом продажи;
- buy не исполняется при нехватке USDC; sell — на `min(q, B)`;
- опционально `penetration` — цена должна уйти за уровень на заданную долю
  (грубая поправка на очередь мейкера; по умолчанию 0).

Оговорка: это не биржа. Нет очереди мейкера, частичных исполнений, латентности;
цены — Binance USDT, а не спот Hyperliquid (UBTC/USDC).

## 9. Справочные результаты (боевой движок, старт $10 000)

| Окно | Данные | GridRB | Hold | Excess | maxDD GridRB / hold |
|---|---|---|---|---|---|
| 3 года: 2023-11-01 → 2026-11-01 | BTCUSDT H1 | $20 731 (+107.3 %) | $22 494 (+124.9 %) | −17.6 п.п. | −46.7 % / −53.7 % |
| 2 года: 2024-11-02 → 2026-11-02 | BTCUSDT H1 | $11 477 (+14.8 %) | $11 180 (+11.8 %) | +3.0 п.п. | −45.8 % / −53.7 % |
| 3 года: 2023-11-01 → 2026-11-01 | SOLUSDT H1 (`sz_decimals=3`) | $25 592 (+155.9 %) | $24 764 (+147.6 %) | +8.3 п.п. | −70.4 % / −78.7 % |

Число сделок: BTC 3 года — 270; BTC 2 года — 549; SOL 3 года — 1148.
Подробности: `tests/backtest/REPORT_gridrb_3y.md`, `tests/backtest/REPORT_sol_3y.md`.

## 10. Отличия от старой исследовательской версии (70/30)

| Что | Было (старый спек) | Стало (боевой код) |
|---|---|---|
| Параметры | f0=0.70, band=0.05, δ=0.005 | f0=0.80, band=0.04, δ=0.004 |
| Ранняя докупка | нет | `early_lot=0.15` в зоне 0.76–0.80 |
| Перестановка (T2) | любой `c > anchor` | `c > anchor·(1+0.2 %)` и кулдаун 300 с |
| Объёмы стоящей лестницы | пересчитывались | **не** пересчитываются |
| Бутстрап | `f0·V` | `min(f0·V, 0.999·U)` + проверка минимума |
| Микро-размер | `usd/L` | округление вниз + подъём до `MIN_NOTIONAL` |
| Кэш | не резервировал micro | `free_usdc` учитывает defc + micro + early |
| Живой контур | H1-бар | опрос ≥ 60 с, снятие ордеров на старте, сверка с биржей |

## 11. Чего в GridRB нет

- нет «вдогонки» маркетом при росте;
- нет индивидуальных тейков у лестничных покупок — выход только весовой;
- нет привязки к ATH: якорь лестниц — последний close;
- нет трендового фильтра и переключения режимов;
- нет индикаторов: входные переменные — только `w`, `c` и собственные ордера;
- нет шортов, плеча, стопов.