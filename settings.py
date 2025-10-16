# Hyperliquid Bot Settings

# Telegram Bot Configuration
# Создайте бота через @BotFather в Telegram и получите токен
TG_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"  # Токен от @BotFather
TG_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"      # ID вашего чата (можно получить через @userinfobot)
WARNING_BOT_TOKEN = "YOUR_WARNING_BOT_TOKEN"  # Отдельный бот для критических уведомлений
WARNING_CHAT_ID = "YOUR_WARNING_CHAT_ID"      # ID чата для критических уведомлений

# Hyperliquid API Configuration
HYPERLIQUID_BASE_URL = "https://api.hyperliquid.xyz"        # Основной API
HYPERLIQUID_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"  # Тестовая сеть

# Trading Configuration
TRADE_AMOUNTS = {
    "percent": [20, 20],  # Увеличено с 5% до 20% для Bitcoin
    "fixed": [100, 50],   # Фиксированные суммы в USDC
}

# Futures Trading Limits (для spot торговли используются как настройки TP)
FUTURES_LIMITS = {
    "price_diff_amount": [10.0, 20.0],  # Расстояние TP в долларах
    "max_averaging_orders": 35,         # Максимальное количество ордеров усреднения
    "max_pyramiding_orders": 10,        # Максимальное количество ордеров пирамидинга
}

# Sleep Settings
SLEEP_AFTER_ACC = [30, 60]  # Пауза между аккаунтами (секунды)
SLEEP_AFTER_FUTURE = [10, 20]  # Пауза после торговых операций

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
WEB3_RPC_URL = "https://mainnet.infura.io/v3/YOUR_INFURA_KEY"  # Для Ethereum mainnet
WEB3_TESTNET_RPC_URL = "https://sepolia.infura.io/v3/YOUR_INFURA_KEY"  # Для тестнета

# Альтернативные RPC провайдеры (если не хотите использовать Infura):
# Alchemy: https://www.alchemy.com/
# QuickNode: https://www.quicknode.com/
# Public RPC (менее надежный): https://ethereum.publicnode.com

# Hyperliquid Specific Settings
HYPERLIQUID_CHAIN_ID = 42161  # Arbitrum One (mainnet) - сеть Hyperliquid
HYPERLIQUID_TESTNET_CHAIN_ID = 421614  # Arbitrum Sepolia (testnet)

# Supported Tokens for Spot Trading
SUPPORTED_SPOT_TOKENS = [
    "ETH",   # Ethereum
    "BTC",   # Bitcoin
    "USDC", # USD Coin
    "USDT", # Tether
    # Добавьте другие токены, поддерживаемые Hyperliquid
]

# Risk Management
MAX_POSITION_SIZE_PERCENT = 50  # Максимальный размер позиции в % от баланса
STOP_LOSS_PERCENT = 20  # Стоп-лосс в % от входа
TAKE_PROFIT_PERCENT = 10  # Тейк-профит в % от входа

# Logging
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "hyperliquid_bot.log"
LOG_ROTATION = "1 day"
LOG_RETENTION = "7 days"