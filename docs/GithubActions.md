# План: запуск бота в GitHub Actions

## Анализ проблем

1. **Интерактивный ввод режима** — `modules/utils/modes.py:59` использует `inquirer.prompt()`, требует TTY
2. **Интерактивный ввод пароля** — `modules/database.py:145` использует `input()` в `get_password()`, требует TTY
3. **Секреты в файлах** — `input_data/wallets.txt` содержит приватные ключи, `input_data/tg_bot_tokens.txt` — токены Telegram-ботов. Файлы в `.gitignore` и не попадут в CI-чекаут — секреты нужно передавать через env/secrets
4. **Бесконечный цикл стратегии** — `modules/delayed_orders_strategy.py:1150` `while True` — в CI нужно ограничение по времени. `main.py:233` — цикл меню, не проблема CI (при env-режиме завершится после одного прогона)
5. **Локальное состояние** — `databases/modules.json`, `databases/stats.json`, `databases/last_buy_timestamps.json`, `hype_stat.xlsx` теряются между запусками
6. **Блокирующий `input()` в конце** — `main.py:265,269,286` — падение с EOFError (или hang до timeout — зависит от stdin раннера)

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

      - name: Restore state
        id: restore-cache
        uses: actions/cache/restore@v4
        with:
          path: |
            databases/
            hype_stat.xlsx
          key: bot-state-${{ github.run_id }}
          restore-keys: bot-state-

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run bot
        env:
          BOT_MODE: "0"
          DB_ENCRYPTION_PASSWORD: ${{ secrets.DB_ENCRYPTION_PASSWORD }}
          WALLETS_DATA: ${{ secrets.WALLETS_DATA }}
          TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}
          TG_USER_ID: ${{ secrets.TG_USER_ID }}
        run: python main.py < /dev/null

      - name: Save state
        if: always() && steps.restore-cache.outputs.cache-hit != 'true'
        uses: actions/cache/save@v4
        with:
          path: |
            databases/
            hype_stat.xlsx
          key: bot-state-${{ github.run_id }}

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

      - name: Restore state
        id: restore-cache
        uses: actions/cache/restore@v4
        with:
          path: |
            databases/
            hype_stat.xlsx
          key: bot-state-${{ github.run_id }}
          restore-keys: bot-state-

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run bot
        env:
          BOT_MODE: "0"
          DB_ENCRYPTION_PASSWORD: ${{ secrets.DB_ENCRYPTION_PASSWORD }}
          WALLETS_DATA: ${{ secrets.WALLETS_DATA }}
          TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}
          TG_USER_ID: ${{ secrets.TG_USER_ID }}
          BOT_MAX_RUNTIME: "330"  # минут
        run: python main.py < /dev/null

      - name: Save state
        if: always() && steps.restore-cache.outputs.cache-hit != 'true'
        uses: actions/cache/save@v4
        with:
          path: |
            databases/
            hype_stat.xlsx
          key: bot-state-${{ github.run_id }}
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

### 1. Замена интерактивного выбора режима

**Файл: `main.py`** — заменить вызов `choose_mode()` на чтение из переменной окружения:

```python
import os
from types import SimpleNamespace

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

    # Fallback на интерактивный ввод (только для локального запуска)
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

Добавить fallback на переменные окружения при отсутствии файлов.

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

### 7. Сохранение состояния между запусками

Использовать `actions/cache/restore@v4` + `actions/cache/save@v4` (save с `if: always()`) для файлов:
- `databases/modules.json` (зашифрованная БД)
- `databases/stats.json`
- `databases/last_buy_timestamps.json`
- `hype_stat.xlsx`

## Риски

### Критичный: кэш с зашифрованными ключами + дефолтный пароль в коде

`databases/modules.json` содержит Fernet-шифртексты приватных ключей. Дефолтный пароль шифрования захардкожен в коде (`database.py:95,129,150`). GitHub docs не рекомендуют хранить секреты в кэше — любой с read-доступом может открыть PR и прочитать кэш base-ветки.

**Митигация:**
- Использовать только нестандартный `DB_ENCRYPTION_PASSWORD` (не дефолт)
- Рассмотреть `upload-artifact` с retention и ограничением доступа вместо кэша
- Либо отказаться от persist `modules.json` — пересоздавать БД из `WALLETS_DATA` каждый ран (ключи только из secrets, не в кэше)

### Важный: параллельные запуски

`schedule` может запаздывать, `workflow_dispatch` позволяет ручной запуск поверх работающего scheduled-джоба. Блокировки `address_locks` действуют только в рамках одного процесса — между раннерами дедупликации нет.

**Митигация:** `concurrency` с `cancel-in-progress: false` (уже добавлено в YAML выше).

## Шаги реализации

1. Создать `.github/workflows/bot.yml`
2. Модифицировать `main.py` — убрать интерактивный ввод, добавить `get_mode_from_env()`
3. Модифицировать `modules/database.py` — поддержка `DB_ENCRYPTION_PASSWORD` env в `get_password()`
4. Создать `modules/ci_loader.py` — загрузка секретов из env
5. Модифицировать `wallet_loader.py` и `tg_tokens_loader.py` — fallback на env
6. Добавить таймаут в `delayed_orders_strategy.py`
7. Протестировать локально: `BOT_MODE=0 DB_ENCRYPTION_PASSWORD=test WALLETS_DATA='[...]' python main.py < /dev/null`
8. Добавить секреты в GitHub Repository Settings
9. Проверить, что `.github/workflows/*.yml` и `settings.py` закоммичены
