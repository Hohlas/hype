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

# Класс для парной торговли (заглушка)
class PairAccounts:
    """
    Класс для парной торговли (не поддерживается в spot режиме).
    """
    
    def __init__(self, accounts: list, mode: int, db):
        self.accounts = accounts
        self.mode = mode
        self.db = db
        
    async def __call__(self):
        """
        Запуск парной торговли.
        """
        # Заглушка - парная торговля не поддерживается для spot
        for account in self.accounts:
            account.browser.log_message("Pair trading not supported in spot mode", level="WARNING")
        return False

# Класс для мультиблокировки
class MultiLock:
    """
    Класс для множественной блокировки адресов.
    """
    
    def __init__(self, addresses: list):
        self.addresses = addresses
        self.locks = []
        
    async def __aenter__(self):
        # Получаем блокировки для всех адресов
        for address in self.addresses:
            if address in address_locks:
                self.locks.append(address_locks[address])
        
        # Блокируем все адреса
        for lock in self.locks:
            await lock.acquire()
            
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Освобождаем все блокировки
        for lock in reversed(self.locks):
            lock.release()

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
    'PairAccounts',
    'MultiLock',
    'initialize_locks',
    'address_locks'
]
