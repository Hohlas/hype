"""
Telegram tokens loader module for Hyperliquid Bot
"""

import os
import re
from typing import Dict, Optional


def load_tg_tokens_from_file(file_path: str = "input_data/tg_bot_tokens.txt") -> Dict[str, Optional[str]]:
    """
    Загружает токены Telegram ботов из файла tg_bot_tokens.txt
    
    Формат файла:
    # --- TG BOT SETTINGS ---
    TG_BOT_TOKEN        = '8372...eB9kT7E' # комментарий
    PROFIT_BOT_TOKEN    = '7848...GmU' # комментарий
    TG_USER_ID          = [332...342] 
    
    Returns:
        Dict[str, Optional[str]]: Словарь с токенами и ID пользователя
    """
    tokens = {
        "TG_BOT_TOKEN": None,
        "PROFIT_BOT_TOKEN": None,
        "TG_USER_ID": None
    }
    
    if not os.path.exists(file_path):
        return tokens
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                
                # Пропускаем пустые строки и комментарии
                if not line or line.startswith('#'):
                    continue
                
                # Ищем паттерн: KEY = 'value' или KEY = "value" или KEY = [value]
                # Сначала убираем комментарии из строки
                line_without_comments = re.sub(r'#.*$', '', line).strip()
                
                # Ищем KEY = value
                match = re.match(r'(\w+)\s*=\s*(.+)', line_without_comments)
                if match:
                    key = match.group(1)
                    value = match.group(2).strip()
                    
                    # Убираем кавычки если есть (одинарные или двойные)
                    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
                        value = value[1:-1]
                    
                    # Обработка списка для TG_USER_ID
                    if key == "TG_USER_ID" and value.startswith('[') and value.endswith(']'):
                        # Извлекаем содержимое списка
                        inner_value = value[1:-1].strip()
                        # Разделяем по запятым и берем первый элемент
                        if ',' in inner_value:
                            inner_value = inner_value.split(',')[0].strip()
                        # Убираем кавычки если есть
                        if inner_value.startswith("'") and inner_value.endswith("'"):
                            inner_value = inner_value[1:-1]
                        elif inner_value.startswith('"') and inner_value.endswith('"'):
                            inner_value = inner_value[1:-1]
                        if inner_value:
                            tokens[key] = inner_value
                    elif key in tokens:
                        if value:  # Не сохраняем пустые значения
                            tokens[key] = value
    
    except Exception as e:
        print(f"⚠️ Ошибка при чтении файла {file_path}: {e}")
    
    return tokens

