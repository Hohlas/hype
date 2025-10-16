"""
Wallet loader module for Hyperliquid Bot
"""

import os
from typing import List, Dict


def load_wallets_from_file(file_path: str = "input_data/wallets.txt") -> List[Dict]:
    """
    Загружает кошельки из файла wallets.txt
    
    Формат файла:
    label|wallet_address|private_key|proxy
    
    Returns:
        List[Dict]: Список словарей с данными кошельков
    """
    wallets = []
    
    if not os.path.exists(file_path):
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
                    print(f"⚠️ Строка {line_num}: неверный формат (нужно минимум 3 поля)")
                    continue
                
                wallet_data = {
                    "label": parts[0].strip(),
                    "wallet_address": parts[1].strip(),
                    "private_key": parts[2].strip(),
                    "proxy": parts[3].strip() if len(parts) > 3 else "",
                }
                
                wallets.append(wallet_data)
                print(f"✅ Загружен кошелек: {wallet_data['label']} ({wallet_data['wallet_address'][:10]}...)")
    
    except Exception as e:
        print(f"❌ Ошибка чтения файла {file_path}: {e}")
    
    return wallets


def get_wallet_modules() -> List[Dict]:
    """
    Получает модули кошельков для торговли
    
    Returns:
        List[Dict]: Список модулей для обработки
    """
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
