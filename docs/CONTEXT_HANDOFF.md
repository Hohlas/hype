# Живое состояние проекта (передача контекста)

Обновлено: 2026-10-14. Это текущее рабочее состояние, не устройство алгоритма
(`docs/project_description.md`, раздел «Дежурство» §6). Не дублировать сюда эти
документы.

## Состояние кода

**Закоммичено `7de6bf3`** — перенос невосстанавливаемой памяти дежурства через
ветку `state` и починка маяка (запушено, дерево чистое). Предыдущая веха —
`307c4ee` — торговая сшивка дежурства за выключателем `DUTY_MODE`.

**Что вошло в `7de6bf3`:**

- Перенос невосстанавливаемой памяти дежурства через ветку `state`:
  - `modules/duty.py` — `merge_anchors`, `merge_buy_ts`, оба встроены в
    `merge_states`; `beacon_message_id` (скаляр, локальное значение свежее).
    Побочно починен латентный баг: `merge_states` сводила только
    `marks/error_counters/last_run_utc`, а любой ключ, лежащий ТОЛЬКО в ветке,
    терялся при слиянии.
  - `modules/duty_trade.py` — `_restore_anchors`/`_capture_anchors` и
    `_restore_buy_ts`/`_capture_buy_ts` вокруг вызова стратегии.
  - `modules/database.py` — `get_all_last_buy_timestamps()`,
    `merge_last_buy_timestamps()` (union, top-10, по убыванию).
  - `modules/delayed_orders_strategy.py` — `save_hold_anchor(..., timestamp=None)`.
  - `scripts/seed_state_memory.py` — одноразовый посев (уже выполнен, см. ниже).
- Починка маяка: `modules/beacon.py` (`leader_beacon_tick(deadline, known_id)`
  возвращает id для записи в `state`) + `modules/duty.py::_heartbeat_loop`
  (теперь получает `state` и сохраняет `beacon_message_id`).

Тесты: **39 зелёных** (`./venv/bin/python -m pytest tests/`), `compileall` чистый.
Локальный `./venv` восстановлен (Python 3.14, `pytest` доустановлен).

## Сшивка (закоммичено `307c4ee`)

- `settings.py`/`settings.py.example`: база — `DUTY_MODE = "parse"` (прод — только
  чтение). `"averaging"` — реальные ордера. Смена = коммит в `main`.
  **Локально подготовлено (не запушено):** `settings.py` → `DUTY_MODE = "averaging"`
  (ступень 3).
- `modules/utils/modes.py::get_duty_mode()` — fail-safe: неизвестное значение → `parse`.
- `modules/duty.py::run_trading_iteration` — диспетчер parse/averaging.
- `modules/duty_trade.py` — одна итерация на кошелёк через БОЕВУЮ стратегию в
  режиме одного прохода: `trade_delayed_orders_strategy(client, "BTC",
  max_iterations=1, poll_interval_override=1.0)`. Клиенты кэшируются на процесс.
- `modules/delayed_orders_strategy.py`: `max_iterations` (0 = прежний бесконечный
  VPS-цикл; 1 = один проход) и `poll_interval_override`.

## Невосстанавливаемая память (эта сессия)

Источник истины по ордерам/балансам — биржа (§4). Но два местных параметра
с биржи не берутся и в CI теряются (каталог `databases/` эфемерен):

- `hold_benchmark_anchor` (в ветке как `hold_anchor`) — снимок депо и цены BTC в
  момент первой позиции, write-once. Только метрика hold-бенчмарка, НЕ торговое
  решение.
- `last_buy_timestamps` (в ветке как `last_buy_ts`) — окно для стартового скана
  сирот поверх лимита биржи ~500 сделок. В основном восстанавливается с биржи;
  ветка — расширение окна.

**Посев выполнен** в ветку `state` (скрипт `scripts/seed_state_memory.py`,
`--dry-run` есть, значения не печатает). В `state.json` теперь: `hold_anchor`
(label `acc`), `last_buy_ts` (ключ `0xBdC5…4e71_BTC`). Ветка ПУБЛИЧНА — по
согласию владельца `depo_start` (≈размер счёта) там лежит открыто.

## Боевое окружение

- **VPS нет.** Основной рантайм — дежурство в GitHub Actions, репо public, не форк,
  ветка по умолчанию `main`. Текущая ветка `state` = `state.json` (+ leader/standby/work
  пишутся отдельными файлами через Contents API).
- Secrets: `WALLETS_DATA` (1 кошелёк), `DB_ENCRYPTION_PASSWORD`, `TG_BOT_TOKEN`,
  `PROFIT_BOT_TOKEN`, `TG_USER_ID` (`scripts/push_hype_secrets.sh`).
- Воркфлоу `Hyperliquid Bot Duty` — active, крон `7,19,33,46,58` UTC, `concurrency` нет.
- `workflow_dispatch` входы: `poll_interval` (деф. 60), `watch_seconds` (деф. 19800).
  Короткая вахта для теста — БЕЗ коммита: `gh workflow run "Hyperliquid Bot Duty"
  -f watch_seconds=600 -f poll_interval=10`.

## Проверено фактами (ротация лидер↔дежурный)

Прогнаны короткие ручные вахты (A→B→C). Протокол работает:

- A-лог: `becoming standby` → `Leader dead — taking over` (отменённый лидер протух) →
  `shift start` → `retiring` → `shift complete: iterations 23`.
- B-лог: `becoming standby` → `Leader retiring — taking over` в `11:58:52`
  (A ушёл `11:58:47`, зазор ~5 с) → 25 итераций → `retiring`.
- Координационное состояние читается напрямую из ветки `state`
  (`leader.json`/`standby.json`/`work.json`).
- Наблюдения: `⚠️ PUT standby.json: HTTP 409` — транзиентная гонка SHA, ретраится.
  Плановых (schedule) вахт пока **0 завершённых** — 1 в работе (см. ниже); рождения редки.

## Маяк: был баг, поправлен

Симптом владельца: каждые ~5 мин в TG приходило НОВОЕ сообщение вместо правки
одного. Причина: `beacon._find_last_beacon` ищет маяк через `getUpdates`, а тот
**не отдаёт боту его собственные отправленные сообщения** (проверено: 0 обновлений).
Лечение: id маяка хранится в ветке `state` (`beacon_message_id`), ведущий правит
то же сообщение. При обнулении id (как сейчас) следующий ведущий создаст ОДИН новый
маяк; 4 старых сообщения останутся — удалить вручную (их id недоступны).

## Что дальше

1. ~~Закоммитить/запушить изменения этой сессии в `main`.~~ Сделано (`7de6bf3`).
   Локально готово к пушу (по решению владельца): `DUTY_MODE="averaging"` (ступень 3),
   чистка ссылок на удалённый `docs/GithubActions.md`, перенос инвариантов в
   `project_description.md` §6, удаление `docs/audit.md`.
2. Ступень 2 — плановые вахты разбора: сейчас 1 рождённая (`schedule`), наблюдаем.
   Копить 2–3 чистых `success` + `shift complete`, мерить интервалы рождений.
3. Ступень 3 — одна вахта усреднения под наблюдением: `DUTY_MODE="averaging"`,
   ручной запуск; проверить первых постановки, сверку «ордер уже висит», что новая
   вахта видит ордера предыдущей. Затем вернуть `parse` или к ступени 4.
4. Ступень 4 — cron усреднения (штат). Первый `cancelled` ≈ лимит — стоп и разбор бюджета.
5. Отдельный суточный запуск Cancel (`BOT_MODE=4`).

## Открытые вопросы / риски

- Публичность: адреса кошелька/signer и балансы в логах (не маскируем — решение
  владельца 14.10); `depo_start` засеян в публичную ветку `state` (согласовано).
- Маяк: старые дубли останутся до ручной чистки; правка раз в ~10 мин (короче
  нашей короткой вахты — за тест маяк может не обновиться, это норма).
- Рождения плановых запусков редки (§15.1) — не путать тик расписания и факт запуска.
- `parse_mode` шумит ошибками по USDC/USDT — существующее, к дежурству не относится.
- Сеть до `api.github.com` из рабочего окружения периодически рвётся (мешает `gh`).
