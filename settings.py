# Hyperliquid Bot Settings
version = "1.2.5"

# Risk Management

SPOT_TOKEN = "BTC"  # Spot Trading token 
POSITION_SIZE_PERCENT = 5  # Размер открываемой позиции в % от баланса
MAX_POSITION_SIZE_PERCENT = 90  # Максимальный размер всех открытых позиций в % от баланса
STEP = 100  # Шаг сетки (TP) в долларах для усреднения
MAX_GRID_ORDERS = 90  # Максимальное количество ордеров в сетке
AGGR=0.5  # Коэффициент агрессивности торговли (0.1 - 1.0)

# Hyperliquid API Configuration
HYPERLIQUID_BASE_URL = "https://api.hyperliquid.xyz"        # Основной API
HYPERLIQUID_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"  # Тестовая сеть

# Trading Configuration
TRADE_AMOUNTS = {
    "percent": [25, 25],  # Процент от баланса для первого и последующих ордеров
    "fixed": [100, 50],   # Фиксированные суммы в USDC
}

# Futures Trading Limits (для spot торговли используются как настройки TP)
FUTURES_LIMITS = {
    "price_diff_amount": [400.0, 400.0],  # Расстояние TP в долларах
    "max_averaging_orders": 35,         # Максимальное количество ордеров усреднения
    "max_pyramiding_orders": 10,        # Максимальное количество ордеров пирамидинга
}

# Sleep Settings
SLEEP_AFTER_ACC = [3, 6]  # Пауза между аккаунтами (секунды)
SLEEP_AFTER_FUTURE = [1, 2]  # Пауза после торговых операций

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

# Web3 Settings (ОБЯЗАТЕЛЬНО для Hyperliquid!)
# Infura - это сервис для подключения к блокчейну Ethereum
# Получите бесплатный ключ на https://infura.io/
WEB3_RPC_URL = "https://mainnet.infura.io/v3/798bf9053eac4979995170257179c022"  # Для Ethereum mainnet
# https://gas.api.infura.io/v3/798bf9053eac4979995170257179c022
WEB3_TESTNET_RPC_URL = "https://sepolia.infura.io/v3/YOUR_INFURA_KEY"  # Для тестнета

# Альтернативные RPC провайдеры (если не хотите использовать Infura):
# Alchemy: https://www.alchemy.com/
# QuickNode: https://www.quicknode.com/
# Public RPC (менее надежный): https://ethereum.publicnode.com

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
