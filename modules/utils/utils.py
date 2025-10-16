"""
Utility functions for Hyperliquid Bot
"""

from decimal import Decimal, ROUND_DOWN
import asyncio
from loguru import logger
import settings


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


class TgReport:
    """
    Класс для отправки отчетов в Telegram.
    """
    
    def __init__(self):
        self.bot_token = settings.TG_BOT_TOKEN
        self.chat_id = settings.TG_CHAT_ID
        
    async def send_log(self, logs: str):
        """
        Отправляет логи в Telegram.
        """
        try:
            import aiohttp
            
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": logs,
                "parse_mode": "HTML"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as response:
                    if response.status != 200:
                        logger.error(f"Failed to send Telegram message: {response.status}")
                        
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")


async def send_warning_notification(error_type: str, error_message: str, account_label: str = "Unknown"):
    """
    Отправляет уведомление о критической ошибке в Telegram.
    """
    try:
        import aiohttp
        
        bot_token = settings.WARNING_BOT_TOKEN
        chat_id = settings.WARNING_CHAT_ID
        
        if not bot_token or not chat_id:
            logger.warning("Warning bot token or chat ID not configured")
            return
            
        message = f"🚨 <b>Hyperliquid Bot Warning</b>\n"
        message += f"<b>Type:</b> {error_type}\n"
        message += f"<b>Account:</b> {account_label}\n"
        message += f"<b>Error:</b> {error_message}"
        
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
