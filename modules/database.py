"""
Database module for Hyperliquid Bot
"""

import asyncio
import asyncpg
from loguru import logger
import settings


class DataBase:
    """
    Класс для работы с базой данных PostgreSQL.
    """
    
    def __init__(self):
        self.pool = None
        
    async def connect(self):
        """
        Подключение к базе данных.
        """
        try:
            self.pool = await asyncpg.create_pool(
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                database=settings.DB_NAME,
                user=settings.DB_USER,
                password=settings.DB_PASSWORD,
                min_size=1,
                max_size=10
            )
            logger.info("Database connected successfully")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise
            
    async def close(self):
        """
        Закрытие соединения с базой данных.
        """
        if self.pool:
            await self.pool.close()
            logger.info("Database connection closed")
            
    async def execute(self, query: str, *args):
        """
        Выполнение SQL запроса.
        """
        if not self.pool:
            logger.warning("Database pool not initialized, skipping query execution")
            return None
            
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)
            
    async def fetch(self, query: str, *args):
        """
        Получение данных из базы.
        """
        if not self.pool:
            logger.warning("Database pool not initialized, skipping query fetch")
            return []
            
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)
            
    async def fetchrow(self, query: str, *args):
        """
        Получение одной строки из базы.
        """
        if not self.pool:
            logger.warning("Database pool not initialized, skipping query fetchrow")
            return None
            
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)
            
    async def append_report(self, key: str, text: str, success: bool = True):
        """
        Добавление отчета в базу данных.
        """
        try:
            query = """
                INSERT INTO reports (wallet_address, message, success, created_at)
                VALUES ($1, $2, $3, NOW())
            """
            await self.execute(query, key, text, success)
        except Exception as e:
            logger.error(f"Failed to append report: {e}")
            
    async def get_account_reports(self, key: str, label: str, address: str, last_module: int, mode: int):
        """
        Получение отчетов аккаунта.
        """
        try:
            # Проверяем, что пул соединений инициализирован
            if not self.pool:
                logger.warning("Database pool not initialized, skipping report retrieval")
                return []
                
            query = """
                SELECT message FROM reports 
                WHERE wallet_address = $1 AND id > $2
                ORDER BY created_at DESC
                LIMIT 100
            """
            rows = await self.fetch(query, key, last_module)
            return [row['message'] for row in rows]
        except Exception as e:
            logger.error(f"Failed to get account reports: {e}")
            return []
            
    def get_all_modules(self, unique_wallets: bool = False):
        """
        Получение всех модулей для обработки.
        """
        from .wallet_loader import get_wallet_modules
        
        modules = get_wallet_modules()
        
        if not modules:
            return "No more accounts left"
        
        return modules
        
    def get_all_groups(self):
        """
        Получение всех групп для парной торговли.
        """
        # Заглушка - в реальной реализации здесь будет работа с базой данных
        return "No more accounts left"
        
    async def remove_module(self, module_data: dict):
        """
        Удаление модуля из очереди.
        """
        # Заглушка - в реальной реализации здесь будет работа с базой данных
        return 0
        
    async def remove_account(self, module_data: dict):
        """
        Удаление аккаунта из очереди.
        """
        # Заглушка - в реальной реализации здесь будет работа с базой данных
        return 0
        
    async def remove_group(self, group_data: dict):
        """
        Удаление группы из очереди.
        """
        # Заглушка - в реальной реализации здесь будет работа с базой данных
        return 0
        
    def create_modules(self, mode: int):
        """
        Создание модулей в базе данных.
        """
        # Заглушка - в реальной реализации здесь будет работа с базой данных
        pass
