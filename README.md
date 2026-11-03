# Hyperliquid Bot — GridRB 80/20

Спотовый бот для Hyperliquid: держит целевой вес BTC 80 % и торгует сеткой
(`GridRB`) вокруг него. Только спот, long-only, без плеча, шортов и стоп-лоссов.
Точные правила — `docs/GridRB_spec.md`, устройство — `docs/project_description.md`.

## 🚀 Особенности

- **GridRB 80/20**: лестница продаж при перевесе BTC, лестница покупок при
  недовесе, микро-сетка внутри полосы.
- **Один маркет за жизнь стратегии** — бутстрап до целевого веса при пустой
  позиции; дальше только maker-лимитки.
- **Состояние — с биржи**: после рестарта снимаются старые ордера и лестницы
  строятся от текущей цены; отдельной персистентности нет.
- **Два режима запуска**: бесконечный цикл на VPS/локально и ограниченный вотч
  в GitHub Actions.
- **Telegram-уведомления** о старте и ошибках.

## 📋 Требования

- Python 3.8+
- Кошелёк Hyperliquid (адрес + приватный ключ или API Wallet)
- Telegram Bot Token (опционально)
- `input_data/wallets.txt` или `WALLETS_DATA` в CI

## 🛠 Установка

1. **Клонируйте репозиторий:**
```bash
git clone <repository-url>
cd hype
```

2. **Создайте виртуальное окружение:**
```bash
python3 -m venv venv
```
Окружение обязательно: на Debian/Ubuntu системный `pip` заблокирован (PEP 668,
`externally-managed-environment`). Каталог `venv/` в `.gitignore`.

Если `python3 -m venv` падает с `ensurepip is not available`, поставьте
`python3-venv` (например, `sudo apt install python3.14-venv`).

3. **Установите зависимости:**
```bash
./venv/bin/python -m pip install -r requirements.txt
```
Дальше используйте интерпретатор окружения (`./venv/bin/python ...`).

4. **Настройте конфигурацию:**
```bash
cp settings.py.example settings.py
cp input_data/wallets.txt.example input_data/wallets.txt
cp input_data/tg_bot_tokens.txt.example input_data/tg_bot_tokens.txt
# Отредактируйте созданные файлы
```

## ⚙️ Настройка

### 1. Кошелёк

Кошельки задаются в `input_data/wallets.txt`
(`label|wallet_address|private_key|proxy`, прокси опционально). Затем создайте
зашифрованную базу:

```bash
./venv/bin/python main.py --init-db
```

Команда интерактивно спросит пароль и соберёт `databases/modules.json`.
В GitHub Actions база не нужна: кошельки приходят из `WALLETS_DATA`.

Подпись ордеров — локальная (`eth-account`, EIP-712), внешний RPC не нужен.

### 2. Telegram

Токены читаются из `input_data/tg_bot_tokens.txt`
(`KEY = 'value'`: `TG_BOT_TOKEN`, `PROFIT_BOT_TOKEN`, `TG_USER_ID = [...]`).
В CI — из одноимённых Secrets.

### 3. Торговля

```python
# settings.py (активные ключи GridRB)
SPOT_TOKEN = "BTC"
GRIDRB_F0 = 0.80            # целевая доля BTC
GRIDRB_BAND = 0.04          # полоса бездействия ±4 п.п.
GRIDRB_DELTA = 0.004        # шаг лестниц и микро-сетки, 0.4%
GRIDRB_LEVELS = 5           # уровней в лестнице
GRIDRB_MICRO_LOT = 0.02     # лот микро-сетки, 2% капитала
GRIDRB_MICRO_MAX = 3        # максимум микро-ордеров
GRIDRB_MIN_NOTIONAL = 10.0  # минимальный ордер, USDC
GRIDRB_POLL_INTERVAL = 60   # пауза между итерациями, сек
GRIDRB_MARKET_SLIP = 0.01   # буфер маркет-бутстрапа, 1%
GRIDRB_DRY_RUN = False      # True — только логировать, не торговать
GRIDRB_REARM_EPS = 0.002    # переставлять лестницу при уходе цены на 0.2%
GRIDRB_REARM_COOLDOWN = 300 # и не чаще раза в 5 минут
THREADS = 3                 # параллельных аккаунтов
```

## 🚀 Запуск

**VPS / локально — бесконечный цикл:**
```bash
./venv/bin/python main.py
# остановка Ctrl+C
```

**Один проход (диагностика):**
```bash
./venv/bin/python main.py --once
```

**GitHub Actions — ограниченный вотч:**
```bash
python main.py --watch-seconds 19800 --poll-interval 60
```
Workflow `.github/workflows/bot.yml` запускается вручную (`workflow_dispatch`),
автозапуск по расписанию отключён.

Перед боевым запуском рекомендуется `GRIDRB_DRY_RUN = True`: бот будет только
писать в лог, какие ордера поставил бы.

## 📊 Мониторинг

Telegram-уведомления различают типы ордеров:

- **Ребаланс 80/20**: «📥 Ребаланс: BuyLimit (докупка до 80 %)»,
  «📤 Ребаланс: SellLimit (продажа избытка до 80 %)»;
- **Сетка**: «📥 Сетка: BuyLimit», «📤 Сетка: TP»;
- **Бутстрап**: «🚀 Бутстрап: покупка до 80 %».

Журналы (старый `hype_stat.csv` не трогается):

- `gridrb_events.csv` — каждое событие: постановка, отмена, исполнение,
  бутстрап, с ценой, объёмом, балансами, весом и прибылью по сеточным тейкам;
- `gridrb_state.csv` — снимок состояния раз в 10 итераций (цена, балансы,
  equity, вес, якоря лестниц, число ордеров).

Подробные действия также пишутся в `hyperliquid_bot.log`.

## ⚠️ Безопасность

- **Никогда не делитесь приватным ключом.**
- Используйте отдельный кошелёк и начните с малой суммы.
- `GRIDRB_DRY_RUN = True` для первой проверки.
- Не запускайте два процесса на одном кошельке: локально защищает
  `databases/gridrb.lock`, в CI — `concurrency`.
- `input_data/*`, `databases/` и `settings.py` не коммитить.

## 🔧 Структура проекта

```
hype/
├── main.py                       # Точка входа: GridRB, VPS + GitHub Actions
├── settings.py                   # Конфигурация
├── requirements.txt
├── modules/
│   ├── gridrb_strategy.py        # Активная стратегия GridRB 80/20
│   ├── hyperliquid_browser.py    # HTTP-клиент Hyperliquid API
│   ├── hyperliquid_client.py     # Оркестратор аккаунта
│   ├── database.py               # Кошельки (JSON/шифр)
│   ├── retry.py                  # Повторы и типы ошибок
│   └── utils/                    # Telegram, задержки, утилиты
├── tests/backtest/               # Бэктест и исследование стратегий
├── input_data/                   # Секреты (*.txt не коммитить)
├── databases/                    # Состояние (не коммитить)
└── docs/                         # Описание алгоритма и спецификации
```

Старые модули (`delayed_orders_strategy.py`, `duty*.py`, `beacon.py`,
`cancel_orders.py`, `parse_mode.py`) оставлены как архив и рантаймом не
вызываются.

## 📝 Логирование

`loguru`, файл `hyperliquid_bot.log` с ротацией раз в день.

## 📄 Лицензия

Проект предназначен для образовательных целей. Используйте на свой страх и риск.

## ⚠️ Отказ от ответственности

Торговля криптовалютами связана с высокими рисками. Автор не несёт
ответственности за возможные убытки. Тестируйте на небольших суммах.