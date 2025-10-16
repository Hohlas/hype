# Hyperliquid Bot Settings

# Telegram Bot Configuration
TG_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TG_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"
WARNING_BOT_TOKEN = "YOUR_WARNING_BOT_TOKEN"
WARNING_CHAT_ID = "YOUR_WARNING_CHAT_ID"

# Hyperliquid API Configuration
HYPERLIQUID_BASE_URL = "https://api.hyperliquid.xyz"
HYPERLIQUID_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

# Trading Configuration
TRADE_AMOUNTS = {
    "percent": [15, 15],  # Процент от баланса для первого и последующих ордеров
    "fixed": [100, 50],   # Фиксированные суммы в USDT
}

# Futures Trading Limits
FUTURES_LIMITS = {
    "price_diff_amount": [10.0, 20.0],  # Расстояние TP в долларах
    "max_averaging_orders": 35,         # Максимальное количество ордеров усреднения
    "max_pyramiding_orders": 10,        # Максимальное количество ордеров пирамидинга
}

# Sleep Settings
SLEEP_AFTER_ACC = [30, 60]  # Пауза между аккаунтами (секунды)
SLEEP_AFTER_FUTURE = [10, 20]  # Пауза после фьючерсных операций

# Threading
THREADS = 3  # Количество одновременных потоков

# Database Settings
DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "hyperliquid_bot"
DB_USER = "postgres"
DB_PASSWORD = "password"

# Proxy Settings (optional)
USE_PROXY = False
PROXY_LIST = []

# Web3 Settings
WEB3_RPC_URL = "https://mainnet.infura.io/v3/YOUR_INFURA_KEY"  # Для Ethereum mainnet
WEB3_TESTNET_RPC_URL = "https://sepolia.infura.io/v3/YOUR_INFURA_KEY"  # Для тестнета

# Hyperliquid Specific Settings
HYPERLIQUID_CHAIN_ID = 42161  # Arbitrum One (mainnet)
HYPERLIQUID_TESTNET_CHAIN_ID = 421614  # Arbitrum Sepolia (testnet)

# Supported Tokens for Spot Trading
SUPPORTED_SPOT_TOKENS = [
    "ETH",
    "BTC", 
    "USDC",
    "USDT",
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
