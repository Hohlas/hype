# План: запуск бота в GitHub Actions

## Анализ проблем

1. **Интерактивный ввод режима** — `modules/utils/modes.py:59` использует `inquirer.prompt()`, требует TTY
2. **Интерактивный ввода пароля** — `modules/database.py:145` использует `input()` в `get_password()`, требует TTY
3. **Секреты в файлах** — `input_data/wallets.txt` содержит приватные ключи, `input_data/tg_bot_tokens.txt` — токены Telegram-ботов. Файлы в `.gitignore` и не попадут в CI-чекаут — секреты нужно передавать через env/secrets
4. **Бесконечный цикл стратегии** — `modules/delayed_orders_strategy.py:1150` `while True` — в CI нужно ограничение по времени. `main.py:233` — цикл меню, не проблема CI (при env-режиме завершится после одного прогона)
5. **Локальное состояние** — `databases/modules.json`, `databases/stats.json`, `databases/last_buy_timestamps.json`, `hype_stat.xlsx` теряются между запусками
6. **Блокирующий `input()` в конце** — `main.py:265,269,286` — падение с EOFError (или hang до timeout — зависит от stdin раннера)

## Переключатель режимов: VPS vs GitHub Actions

Бот должен работать в двух режимах:

| Режим | Как определяется | Источник секретов | Состояние | Ввод режима |
|---|---|---|---|---|
| **VPS** (по умолчанию) | `BOT_RUNTIME` не задан или `=vps` | Файлы `input_data/` | Зашифрованная БД на диске | Интерактивный (`inquirer`) |
| **GitHub Actions** | `BOT_RUNTIME=github_actions` или авто (есть `WALLETS_DATA`) | GitHub Secrets → env | Пересоздаётся каждый ран | Env `BOT_MODE` |

### Логика автоопределения

```python
def get_runtime_mode():
    explicit = os.getenv('BOT_RUNTIME', '').lower()
    if explicit in ('vps', 'github_actions'):
        return explicit
    # Автоопределение: есть WALLETS_DATA — значит CI
    return 'github_actions' if os.getenv('WALLETS_DATA') else 'vps'
```

### Поведение в каждом режиме

**VPS-режим (как раньше):**
- Интерактивный выбор режима через `inquirer`
- Кошельки из `input_data/wallets.txt`
- Токены TG из `input_data/tg_bot_tokens.txt`
- Зашифрованная БД `databases/modules.json` на диске
- Пароль запрашивается интерактивно (или дефолт)

**GitHub Actions-режим:**
- Режим из `BOT_MODE` env (без интерактива)
- Кошельки из `WALLETS_DATA` env (JSON)
- Токены TG из env
- БД пересоздаётся из `WALLETS_DATA` каждый запуск (ключи не кэшируются)
- Пароль шифрования из `DB_ENCRYPTION_PASSWORD` env

## Варианты харнесов

### Вариант 1: Периодический запуск (рекомендуется)

```yaml
# .github/workflows/bot.yml
name: Hyperliquid Bot
on:
  schedule:
    - cron: '0 */6 * * *'  # каждые 6 часов (UTC)
  workflow_dispatch:        # ручной запуск

concurrency:
  group: bot-${{ github.ref }}
  cancel-in-progress: false

jobs:
  bot:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run bot
        env:
          BOT_RUNTIME: "github_actions"
          BOT_MODE: "0"
          DB_ENCRYPTION_PASSWORD: ${{ secrets.DB_ENCRYPTION_PASSWORD }}
          WALLETS_DATA: ${{ secrets.WALLETS_DATA }}
          TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}
          TG_USER_ID: ${{ secrets.TG_USER_ID }}
        run: python main.py < /dev/null

      - name: Upload logs
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: bot-logs
          path: hyperliquid_bot.log
```

### Вариант 2: Долгоживущий процесс (макс. 6 часов)

```yaml
# .github/workflows/bot-long.yml
name: Hyperliquid Bot Long Run
on:
  workflow_dispatch:

concurrency:
  group: bot-${{ github.ref }}
  cancel-in-progress: false

jobs:
  bot:
    runs-on: ubuntu-latest
    timeout-minutes: 345  # запас на корректное завершение
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run bot
        env:
          BOT_RUNTIME: "github_actions"
          BOT_MODE: "0"
          DB_ENCRYPTION_PASSWORD: ${{ secrets.DB_ENCRYPTION_PASSWORD }}
          WALLETS_DATA: ${{ secrets.WALLETS_DATA }}
          TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}
          TG_USER_ID: ${{ secrets.TG_USER_ID }}
          BOT_MAX_RUNTIME: "330"  # минут
        run: python main.py < /dev/null
```

### Вариант 3: Разделение по режимам

Два отдельных файла:

```yaml
# .github/workflows/averaging.yml
name: Bot - Averaging
on:
  schedule:
    - cron: '0 */4 * * *'  # UTC
  workflow_dispatch:

concurrency:
  group: bot-averaging-${{ github.ref }}
  cancel-in-progress: false

jobs:
  bot:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      # ... аналогично варианту 1 с BOT_MODE: "0"
```

```yaml
# .github/workflows/cancel.yml
name: Bot - Cancel Orders
on:
  schedule:
    - cron: '0 2 * * *'  # раз в день в 2:00 UTC
  workflow_dispatch:

concurrency:
  group: bot-cancel-${{ github.ref }}
  cancel-in-progress: false

jobs:
  bot:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      # ... аналогично варианту 1 с BOT_MODE: "4"
```

## Необходимые изменения кода

### 1. Переключатель режимов + замена интерактивного выбора режима

**Файл: `main.py`**:

```python
import os
from types import SimpleNamespace

def get_runtime_mode():
    explicit = os.getenv('BOT_RUNTIME', '').lower()
    if explicit in ('vps', 'github_actions'):
        return explicit
    return 'github_actions' if os.getenv('WALLETS_DATA') else 'vps'

def get_mode():
    if get_runtime_mode() == 'github_actions':
        return get_mode_from_env()
    return choose_mode()  # интерактивный для VPS

def get_mode_from_env():
    mode_id = os.getenv('BOT_MODE', '0')
    mode_map = {
        '0': {'type': 'module', 'soft_id': 0, 'text': 'Averaging Market'},
        '4': {'type': 'module', 'soft_id': 4, 'text': 'Cancel Orders'},
        '5': {'type': 'module', 'soft_id': 5, 'text': 'Parse'},
    }
    if mode_id not in mode_map:
        raise ValueError(f"Invalid BOT_MODE: {mode_id}. Valid: 0, 4, 5")
    m = mode_map[mode_id]
    return SimpleNamespace(soft_id=m['soft_id'], type=m['type'], text=m['text'])
```

### 2. Замена интерактивного ввода пароля

**Файл: `modules/database.py`** — добавить поддержку переменной окружения в `get_password()`:

```python
def get_password(self):
    if self.personal_key is not None:
        logger.debug('Password already set, skipping password request')
        return

    if not path.isfile(self.modules_db_name):
        logger.debug(f'Database file {self.modules_db_name} not found')
        return

    with open(self.modules_db_name, encoding="utf-8") as f:
        modules_db = json.load(f)

    if not modules_db:
        logger.debug('Database is empty')
        return

    test_key = list(modules_db.keys())[0]

    # Пробуем пароль из переменной окружения
    env_password = os.getenv('DB_ENCRYPTION_PASSWORD')
    if env_password:
        try:
            temp_key = Fernet(urlsafe_b64encode(md5(env_password.encode()).hexdigest().encode()))
            self.decode_pk(pk=test_key, key=temp_key)
            self.personal_key = temp_key
            logger.info('[+] Soft | Password from env worked\n')
            return
        except InvalidToken:
            logger.error('[-] Soft | DB_ENCRYPTION_PASSWORD is set but invalid!\n')
            raise

    # Пробуем дефолтный пароль
    try:
        temp_key = Fernet(urlsafe_b64encode(md5("@karamelniy dumb shit encrypting".encode()).hexdigest().encode()))
        self.decode_pk(pk=test_key, key=temp_key)
        self.personal_key = temp_key
        logger.info('[+] Soft | Default password worked\n')
        return
    except InvalidToken:
        pass

    # Fallback на интерактивный ввод (только для VPS)
    print('\n' + '='*60)
    print('🔐 Database is encrypted. Please enter password to decrypt.')
    print('='*60)
    while True:
        try:
            print('\nEnter password to decrypt your privatekeys (empty for default):')
            raw_password = input("> ")
            # ... остальная часть интерактивного ввода
```

### 3. Загрузка кошельков из переменных окружения

**Новый файл: `modules/ci_loader.py`**:

```python
import os
import json

def load_wallets_from_env():
    """
    Загружает кошельки из переменной окружения WALLETS_DATA.
    Формат: JSON-массив объектов [{"label": "...", "wallet_address": "...", "private_key": "...", "proxy": "..."}]
    """
    wallets_json = os.getenv('WALLETS_DATA')
    if not wallets_json:
        return None
    return json.loads(wallets_json)

def load_tg_tokens_from_env():
    return {
        'TG_BOT_TOKEN': os.getenv('TG_BOT_TOKEN'),
        'PROFIT_BOT_TOKEN': os.getenv('PROFIT_BOT_TOKEN'),
        'TG_USER_ID': os.getenv('TG_USER_ID'),
    }
```

### 4. Модификация `wallet_loader.py` и `tg_tokens_loader.py`

Добавить fallback на переменные окружения при отсутствии файлов:

```python
# wallet_loader.py
def get_wallet_modules(db=None):
    # Сначала пробуем env (CI-режим)
    env_wallets = load_wallets_from_env()
    if env_wallets:
        return [
            {
                "label": w["label"],
                "wallet_address": w["wallet_address"],
                "private_key": w["private_key"],
                "proxy": w.get("proxy"),
                "module_info": {"status": True}
            }
            for w in env_wallets
        ]
    # Fallback на файлы (VPS-режим)
    ...
```

### 5. Удаление блокирующих `input()` в `main.py`

Заменить все `input('\n > Exit\n')` на `sys.exit(0)` или просто убрать.

### 6. Ограничение времени работы для режима Averaging

**Файл: `modules/delayed_orders_strategy.py`** — добавить проверку таймаута:

```python
import os
import time

MAX_RUNTIME_SECONDS = int(os.getenv('BOT_MAX_RUNTIME', '0')) * 60  # минуты -> секунды
_start_time = time.time()

# В цикле стратегии (после проверки stop-флага):
if MAX_RUNTIME_SECONDS and (time.time() - _start_time) > MAX_RUNTIME_SECONDS:
    logger.info("Max runtime reached, stopping gracefully")
    break
```

### 7. Пересоздание БД в CI-режиме (вариант A)

В GitHub Actions `databases/modules.json` **не кэшируется**. БД пересоздаётся из `WALLETS_DATA` каждый запуск:

```python
# main.py или database.py
def ensure_database_for_ci(db):
    """В CI-режиме пересоздаёт БД из WALLETS_DATA, если modules.json отсутствует."""
    if get_runtime_mode() != 'github_actions':
        return
    if not os.getenv('WALLETS_DATA'):
        raise RuntimeError("WALLETS_DATA is required in github_actions mode")
    # Пересоздаём БД из env (ключи не попадают в кэш)
    db.create_modules_from_env()
```

## Риски

### Важный: параллельные запуски

`schedule` может запаздывать, `workflow_dispatch` позволяет ручной запуск поверх работающего scheduled-джоба. Блокировки `address_locks` действуют только в рамках одного процесса — между раннерами дедупликации нет.

**Митигация:** `concurrency` с `cancel-in-progress: false` (уже добавлено в YAML выше).

### Средний: потеря состояния при пересоздании БД

При варианте A `databases/modules.json` пересоздаётся каждый ран. Файлы `stats.json` и `last_buy_timestamps.json` также не кэшируются — бот теряет историю покупок между запусками.

**Митигация:** критично только для `last_buy_timestamps.json` (используется для детекции филлов после рестарта). Можно кэшировать только этот файл, либо принять потерю как ограничение CI-режима.

## Шаги реализации

1. Создать `.github/workflows/bot.yml`
2. Модифицировать `main.py` — добавить `get_runtime_mode()`, `get_mode()`, `get_mode_from_env()`, убрать блокирующие `input()`
3. Модифицировать `modules/database.py` — поддержка `DB_ENCRYPTION_PASSWORD` env в `get_password()`
4. Создать `modules/ci_loader.py` — загрузка секретов из env
5. Модифицировать `wallet_loader.py` и `tg_tokens_loader.py` — fallback на env
6. Добавить таймаут в `delayed_orders_strategy.py`
7. Протестировать VPS-режим: `python main.py` (как раньше)
8. Протестировать CI-режим: `BOT_RUNTIME=github_actions BOT_MODE=0 DB_ENCRYPTION_PASSWORD=test WALLETS_DATA='[...]' python main.py < /dev/null`
9. Добавить секреты в GitHub Repository Settings
10. Проверить, что `.github/workflows/*.yml` и `settings.py` закоммичены
