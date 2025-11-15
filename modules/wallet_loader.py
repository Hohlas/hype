"""
Wallet loader module for Hyperliquid Bot
"""

import os
import json
from os import path
from typing import List, Dict
from loguru import logger


def load_wallets_from_file(file_path: str = "input_data/wallets.txt") -> List[Dict]:
    """
    Загружает кошельки из файла wallets.txt (используется только при создании базы данных)
    
    Формат файла:
    label|wallet_address|private_key|proxy
    
    Returns:
        List[Dict]: Список словарей с данными кошельков
    """
    wallets = []
    
    if not os.path.exists(file_path):
        logger.warning(f"File {file_path} not found.")
        return wallets
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                
                # Пропускаем пустые строки и комментарии
                if not line or line.startswith('#'):
                    continue
                
                # Разбираем строку
                parts = line.split('|')
                
                if len(parts) < 3:
                    logger.warning(f"Line {line_num}: invalid format (need at least 3 fields)")
                    continue
                
                wallet_data = {
                    "label": parts[0].strip(),
                    "wallet_address": parts[1].strip(),
                    "private_key": parts[2].strip(),
                    "proxy": parts[3].strip() if len(parts) > 3 else "",
                }
                
                wallets.append(wallet_data)
    
    except Exception as e:
        logger.error(f"Error reading file {file_path}: {e}")
    
    return wallets


def get_wallet_modules(db=None) -> List[Dict]:
    """
    Получает модули кошельков для торговли из зашифрованной базы данных.
    
    Args:
        db: Экземпляр DataBase для расшифровки ключей
    
    Returns:
        List[Dict]: Список модулей для обработки
    """
    modules_db_name = 'databases/modules.json'
    
    # Если база данных не существует, пытаемся загрузить из файла (для обратной совместимости)
    if not path.isfile(modules_db_name):
        logger.warning(f"Database {modules_db_name} not found. Trying to load from wallets.txt...")
        wallets = load_wallets_from_file()
        
        modules = []
        for wallet in wallets:
            module_data = {
                "label": wallet["label"],
                "wallet_address": wallet["wallet_address"],
                "private_key": wallet["private_key"],
                "proxy": wallet["proxy"],
                "module_info": {
                    "status": True
                }
            }
            modules.append(module_data)
        
        return modules
    
    # Загружаем из зашифрованной базы данных
    if db is None:
        logger.error("Database instance is required to decrypt private keys!")
        return []
    
    # Получаем пароль для расшифровки
    logger.debug("Calling get_password() to decrypt database...")
    db.get_password()
    
    if db.personal_key is None:
        logger.error("Failed to get password for database decryption!")
        return []
    
    logger.debug("Password obtained successfully, decrypting wallets...")
    
    try:
        with open(modules_db_name, encoding="utf-8") as f:
            modules_db = json.load(f)
        
        if not modules_db:
            logger.warning("Database is empty!")
            return []
        
        modules = []
        for encoded_pk, wallet_data in modules_db.items():
            try:
                # Расшифровываем приватный ключ
                private_key = db.decode_pk(encoded_pk)
                
                module_data = {
                    "label": wallet_data.get("label", "Unknown"),
                    "wallet_address": wallet_data.get("wallet_address", ""),
                    "private_key": private_key,
                    "proxy": wallet_data.get("proxy"),
                    "module_info": {
                        "status": True
                    }
                }
                modules.append(module_data)
            except Exception as e:
                logger.error(f"Failed to decrypt wallet {wallet_data.get('label', 'Unknown')}: {e}")
                continue
        
        return modules
        
    except Exception as e:
        logger.error(f"Error loading modules from database: {e}")
        return []
