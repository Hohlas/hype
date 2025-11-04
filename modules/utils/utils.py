"""
Utility functions for Hyperliquid Bot
"""

from decimal import Decimal, ROUND_DOWN
import asyncio
from loguru import logger
from modules.tg_tokens_loader import load_tg_tokens_from_file


def round_cut(value: float, decimals: int = 2) -> float:
    """
    Округляет число вниз до указанного количества знаков после запятой.
    """
    return float(Decimal(str(value)).quantize(Decimal('0.' + '0' * decimals), rounding=ROUND_DOWN))


async def async_sleep(seconds: float):
    """
    Асинхронная пауза.
    """
    await asyncio.sleep(seconds)


def _get_tg_tokens():
    """Кэшированная загрузка токенов из файла"""
    if not hasattr(_get_tg_tokens, '_cached'):
        _get_tg_tokens._cached = load_tg_tokens_from_file()
    return _get_tg_tokens._cached


class TgReport:
    """
    Класс для отправки отчетов в Telegram.
    """
    
    def __init__(self):
        tokens = _get_tg_tokens()
        self.bot_token = tokens.get("TG_BOT_TOKEN") or "12345:AABBCC"
        self.chat_id = tokens.get("TG_USER_ID") or "12345"
        
    async def send_log(self, logs: str):
        """
        Отправляет логи в Telegram.
        """
        try:
            import aiohttp
            
            # Проверяем, что logs не пустой и содержит корректный текст
            if not logs or not isinstance(logs, str) or logs.strip() == "":
                logger.warning("Empty or invalid logs text, skipping Telegram notification")
                return
            
            # Очищаем текст от потенциально проблемных символов
            clean_logs = logs.strip()
            
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": clean_logs,
                "parse_mode": "HTML"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as response:
                    if response.status != 200:
                        response_text = await response.text()
                        logger.error(f"Failed to send Telegram message: {response.status} - {response_text}")
                        
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")


async def send_warning_notification(error_type: str, error_message: str, account_label: str = "Unknown"):
    """
    Отправляет уведомление о критической ошибке в Telegram согласно требованиям.
    Формат: 🚨 [проект] | [аккаунт] ❌ Ошибка: [тип ошибки] 📝 Описание: [описание ошибки]
    Использует TG_BOT_TOKEN для отправки ошибок.
    """
    try:
        import aiohttp
        
        tokens = _get_tg_tokens()
        bot_token = tokens.get("TG_BOT_TOKEN") or "12345:AABBCC"
        chat_id = tokens.get("TG_USER_ID") or "12345"
        
        if not bot_token or not chat_id:
            logger.warning("TG_BOT_TOKEN or TG_CHAT_ID not configured")
            return
            
        message = f"🚨 Hyperliquid Bot | {account_label}\n"
        message += f"❌ Ошибка: {error_type}\n"
        message += f"📝 Описание: {error_message}"
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data) as response:
                if response.status != 200:
                    logger.error(f"Failed to send warning notification: {response.status}")
                    
    except Exception as e:
        logger.error(f"Failed to send warning notification: {e}")


async def send_profit_notification(profit_message: str):
    """
    Отправляет уведомление о профите в PROFIT_BOT_TOKEN.
    Формат сообщения: Profit $-0.28 | $69.27USDC + 0.008550BTC = $964.55
    """
    try:
        import aiohttp
        
        tokens = _get_tg_tokens()
        bot_token = tokens.get("PROFIT_BOT_TOKEN")
        chat_id = tokens.get("TG_USER_ID") or "12345"
        
        if not bot_token or not chat_id:
            # Если PROFIT_BOT_TOKEN не настроен, не отправляем сообщение
            return
            
        # Извлекаем только текст о профите (без эмодзи 🎯)
        # Формат: Profit $... | $...USDC + ...BTC = $...
        import re
        # Убираем эмодзи в начале
        profit_text = re.sub(r'^[🎯✅❌📉📈🚀]+', '', profit_message).strip()
        # Если осталось что-то вроде "Profit $...", используем это
        if not profit_text.startswith("Profit"):
            # Пытаемся найти паттерн Profit в сообщении
            match = re.search(r'Profit\s+\$[-\d.]+.*', profit_message)
            if match:
                profit_text = match.group(0)
            else:
                profit_text = profit_message
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": profit_text,
            "parse_mode": "HTML"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data) as response:
                if response.status != 200:
                    logger.error(f"Failed to send profit notification: {response.status}")
                    
    except Exception as e:
        logger.error(f"Failed to send profit notification: {e}")
