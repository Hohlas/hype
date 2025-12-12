"""
Database module for Hyperliquid Bot
"""

import asyncio
import asyncpg
from loguru import logger
import settings
import json
import os
from os import path, mkdir
from time import sleep
from hashlib import md5
from base64 import urlsafe_b64encode
from cryptography.fernet import Fernet, InvalidToken


class DataBase:
    """
    Класс для работы с базой данных PostgreSQL и зашифрованной базой приватных ключей.
    """
    
    def __init__(self):
        self.pool = None
        self.modules_db_name = 'databases/modules.json'
        self.report_db_name = 'databases/report.json'
        self.stats_db_name = 'databases/stats.json'
        self.last_buy_timestamps_db_name = 'databases/last_buy_timestamps.json'
        self.personal_key = None
        
        # Создаем каталог databases, если его нет
        if not path.isdir(self.modules_db_name.split('/')[0]):
            mkdir(self.modules_db_name.split('/')[0])
        
        # Создаем файлы баз данных, если их нет
        for db in [
            {"name": self.modules_db_name, "default": "{}"},
            {"name": self.report_db_name, "default": "{}"},
            {"name": self.stats_db_name, "default": "{}"},
            {"name": self.last_buy_timestamps_db_name, "default": "{}"},
        ]:
            if not path.isfile(db["name"]):
                with open(db["name"], 'w', encoding='utf-8') as f:
                    f.write(db["default"])
        
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
        Получение всех модулей для обработки из зашифрованной базы данных.
        """
        from .wallet_loader import get_wallet_modules
        
        modules = get_wallet_modules(db=self)
        
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
        
    def set_password(self):
        """
        Установка пароля для шифрования приватных ключей.
        """
        if self.personal_key is not None:
            return
        
        logger.debug('Enter password to encrypt privatekeys (empty for default):')
        raw_password = input("")
        
        if not raw_password:
            raw_password = "@karamelniy dumb shit encrypting"
            logger.success('[+] Soft | You set empty password for Database\n')
        else:
            print('')
        sleep(0.2)
        
        password = md5(raw_password.encode()).hexdigest().encode()
        self.personal_key = Fernet(urlsafe_b64encode(password))
    
    def get_password(self):
        """
        Получение пароля для расшифровки приватных ключей.
        """
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
        
        # Берем первый ключ словаря (это зашифрованный приватный ключ)
        # Структура: {encoded_pk: {wallet_address, label, proxy, ...}}
        test_key = list(modules_db.keys())[0]
        
        # Пробуем дефолтный пароль
        try:
            temp_key = Fernet(urlsafe_b64encode(md5("@karamelniy dumb shit encrypting".encode()).hexdigest().encode()))
            self.decode_pk(pk=test_key, key=temp_key)
            self.personal_key = temp_key
            logger.info('[+] Soft | Default password worked\n')
            return
        except InvalidToken:
            # Дефолтный пароль не подошел, запрашиваем у пользователя
            pass
        
        # Запрашиваем пароль у пользователя
        print('\n' + '='*60)
        print('🔐 Database is encrypted. Please enter password to decrypt.')
        print('='*60)
        while True:
            try:
                print('\nEnter password to decrypt your privatekeys (empty for default):')
                raw_password = input("> ")
                
                # Если пустой пароль, пробуем дефолтный
                if not raw_password:
                    try:
                        temp_key = Fernet(urlsafe_b64encode(md5("@karamelniy dumb shit encrypting".encode()).hexdigest().encode()))
                        self.decode_pk(pk=test_key, key=temp_key)
                        self.personal_key = temp_key
                        logger.success('[+] Soft | Default password worked\n')
                        return
                    except InvalidToken:
                        logger.error('[-] Soft | Default password also failed!\n')
                        continue
                
                password = md5(raw_password.encode()).hexdigest().encode()
                
                temp_key = Fernet(urlsafe_b64encode(password))
                self.decode_pk(pk=test_key, key=temp_key)
                self.personal_key = temp_key
                logger.success('[+] Soft | Access granted!\n')
                return
                
            except InvalidToken:
                logger.error('[-] Soft | Invalid password. Please try again.\n')
            except KeyboardInterrupt:
                logger.error('\n[-] Soft | Password entry cancelled.\n')
                raise
    
    def encode_pk(self, pk: str, key: None | Fernet = None):
        """
        Шифрование приватного ключа.
        """
        if key is None:
            return self.personal_key.encrypt(pk.encode()).decode()
        return key.encrypt(pk.encode()).decode()
    
    def decode_pk(self, pk: str, key: None | Fernet = None):
        """
        Расшифровка приватного ключа.
        """
        if key is None:
            return self.personal_key.decrypt(pk).decode()
        return key.decrypt(pk).decode()
    
    def create_modules(self, mode: int):
        """
        Создание зашифрованной базы данных из файла wallets.txt.
        """
        self.set_password()
        
        wallets_data = []
        labels = []
        proxies = []
        
        # Читаем кошельки из файла
        wallets_file = 'input_data/wallets.txt'
        if not path.isfile(wallets_file):
            raise Exception(f"File {wallets_file} not found!")
        
        with open(wallets_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                
                # Пропускаем пустые строки и комментарии
                if not line or line.startswith('#'):
                    continue
                
                # Разбираем строку: label|wallet_address|private_key|proxy
                parts = line.split('|')
                
                if len(parts) < 3:
                    logger.warning(f"Line {line_num}: invalid format (need at least 3 fields)")
                    continue
                
                label = parts[0].strip()
                wallet_address = parts[1].strip()
                private_key = parts[2].strip()
                proxy = parts[3].strip() if len(parts) > 3 else ""
                
                # Обрабатываем NONE как None
                if proxy.strip().upper() == 'NONE':
                    proxy = None
                
                wallets_data.append({
                    "wallet_address": wallet_address,
                    "private_key": private_key,
                })
                labels.append(label)
                proxies.append(proxy)
        
        if not wallets_data:
            raise Exception("No wallets found in wallets.txt!")
        
        # Создаем зашифрованную базу данных
        # Структура: {encoded_pk: {wallet_address, label, proxy}}
        new_modules = {}
        for wallet_data, label, proxy in zip(wallets_data, labels, proxies):
            encoded_pk = self.encode_pk(wallet_data["private_key"])
            new_modules[encoded_pk] = {
                "wallet_address": wallet_data["wallet_address"],
                "label": label,
                "proxy": proxy,
            }
        
        # Сохраняем базу данных
        with open(self.modules_db_name, 'w', encoding="utf-8") as f:
            json.dump(new_modules, f, indent=2, ensure_ascii=False)
        
        # Очищаем report базу
        with open(self.report_db_name, 'w', encoding='utf-8') as f:
            f.write('{}')
        
        logger.critical('Dont Forget To Remove Private Keys from wallets.txt!')
        logger.info(f'Created Database for {len(new_modules)} accounts!\n')
    
    def get_last_buy_timestamps(self, wallet_address: str, token_name: str) -> list[int]:
        """
        Получает список last_buy_timestamps для кошелька и токена из БД.
        
        Args:
            wallet_address: Адрес кошелька
            token_name: Название токена (например, "BTC")
            
        Returns:
            Список timestamp в миллисекундах (до 10 элементов), отсортированный по убыванию.
            Пустой список, если не найдено.
        """
        try:
            if not path.isfile(self.last_buy_timestamps_db_name):
                return []
            
            with open(self.last_buy_timestamps_db_name, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            key = f"{wallet_address}_{token_name}"
            timestamps = data.get(key, [])
            
            # Поддерживаем обратную совместимость: если значение - число, преобразуем в список
            if isinstance(timestamps, int):
                return [timestamps]
            elif isinstance(timestamps, list):
                # Убеждаемся, что список отсортирован по убыванию и содержит максимум 10 элементов
                timestamps = sorted(timestamps, reverse=True)[:10]
                return timestamps
            else:
                return []
        except Exception as e:
            logger.error(f"Failed to get last_buy_timestamps: {e}")
            return []
    
    def add_last_buy_timestamp(self, wallet_address: str, token_name: str, timestamp: int):
        """
        Добавляет last_buy_timestamp в список для кошелька и токена в БД.
        Поддерживает максимум 10 элементов, удаляя самые старые при превышении.
        
        Args:
            wallet_address: Адрес кошелька
            token_name: Название токена (например, "BTC")
            timestamp: timestamp в миллисекундах
        """
        try:
            # Загружаем существующие данные
            if path.isfile(self.last_buy_timestamps_db_name):
                with open(self.last_buy_timestamps_db_name, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = {}
            
            key = f"{wallet_address}_{token_name}"
            timestamps = data.get(key, [])
            
            # Поддерживаем обратную совместимость: если значение - число, преобразуем в список
            if isinstance(timestamps, int):
                timestamps = [timestamps]
            elif not isinstance(timestamps, list):
                timestamps = []
            
            # Добавляем новый timestamp, если его еще нет в списке
            if timestamp not in timestamps:
                timestamps.append(timestamp)
                # Сортируем по убыванию (новые первыми) и оставляем максимум 10
                timestamps = sorted(timestamps, reverse=True)[:10]
                data[key] = timestamps
            
            # Сохраняем обратно
            with open(self.last_buy_timestamps_db_name, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to add last_buy_timestamp: {e}")
    
    def save_last_buy_timestamps(self, wallet_address: str, token_name: str, timestamps: list[int]):
        """
        Сохраняет список last_buy_timestamps для кошелька и токена в БД.
        Используется для массовой инициализации.
        
        Args:
            wallet_address: Адрес кошелька
            token_name: Название токена (например, "BTC")
            timestamps: Список timestamp в миллисекундах (будет обрезан до 10 элементов)
        """
        try:
            # Загружаем существующие данные
            if path.isfile(self.last_buy_timestamps_db_name):
                with open(self.last_buy_timestamps_db_name, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = {}
            
            key = f"{wallet_address}_{token_name}"
            # Сортируем по убыванию (новые первыми) и оставляем максимум 10
            sorted_timestamps = sorted(timestamps, reverse=True)[:10]
            data[key] = sorted_timestamps
            
            # Сохраняем обратно
            with open(self.last_buy_timestamps_db_name, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save last_buy_timestamps: {e}")
