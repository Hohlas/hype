"""
Hyperliquid Bot Modules Package
"""

from .hyperliquid_browser import HyperliquidBrowser
from .hyperliquid_client import HyperliquidClient
from .database import DataBase
from .retry import DataBaseError, CustomError, retry_async, retry_sync
from .utils import (
    round_cut, 
    async_sleep, 
    TgReport, 
    send_warning_notification
)
from .utils.modes import choose_mode, get_mode_info

# Импорт для совместимости
from .utils import TgReport as TgReportClass

# Глобальные переменные для блокировок
import asyncio
address_locks = {}

# Инициализация блокировок для адресов кошельков
def initialize_locks(wallet_addresses: list):
    """
    Инициализация блокировок для адресов кошельков.
    """
    global address_locks
    for address in wallet_addresses:
        if address not in address_locks:
            address_locks[address] = asyncio.Lock()

__all__ = [
    'HyperliquidBrowser',
    'HyperliquidClient', 
    'DataBase',
    'DataBaseError',
    'CustomError',
    'retry_async',
    'retry_sync',
    'round_cut',
    'async_sleep',
    'TgReport',
    'send_warning_notification',
    'choose_mode',
    'get_mode_info',
    'initialize_locks',
    'address_locks'
]
