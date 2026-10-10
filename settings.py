# Hyperliquid Bot Settings
version = "1.3.7"

# Risk Management

SPOT_TOKEN = "BTC"  # Spot Trading token 
POSITION_SIZE_PERCENT = 5  # Размер открываемой позиции в % от баланса
STEP = 500  # Шаг сетки (TP) в долларах для усреднения
AGGR=0.5  # Коэффициент агрессивности торговли (0.1 - 1.0)

# Hyperliquid API Configuration
HYPERLIQUID_BASE_URL = "https://api.hyperliquid.xyz"        # Основной API
HYPERLIQUID_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"  # Тестовая сеть

# Threading
THREADS = 3  # Количество одновременных потоков

# Database Settings (ОПЦИОНАЛЬНО - можно не использовать)
# Если не используете базу данных, оставьте как есть
DB_HOST = "localhost"      # Адрес PostgreSQL сервера
DB_PORT = 5432             # Порт PostgreSQL (по умолчанию 5432)
DB_NAME = "hyperliquid_bot"  # Название базы данных
DB_USER = "postgres"       # Пользователь базы данных
DB_PASSWORD = "password"    # Пароль базы данных

# Proxy Settings (ОПЦИОНАЛЬНО)
USE_PROXY = False          # Использовать прокси (True/False)
PROXY_LIST = []            # Список прокси в формате ["http://user:pass@ip:port", ...]

# Hyperliquid Specific Settings
HYPERLIQUID_CHAIN_ID = 42161  # Arbitrum One (mainnet) - сеть Hyperliquid
HYPERLIQUID_TESTNET_CHAIN_ID = 421614  # Arbitrum Sepolia (testnet)


# Logging
LOG_LEVEL = "DEBUG"  # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "hyperliquid_bot.log"
LOG_ROTATION = "1 day"
LOG_RETENTION = "7 days"
# Упрощенный формат логов (без DEBUG сообщений)
ENABLE_DEBUG_LOGS = True  # Временно включено для отладки истории сделок 
