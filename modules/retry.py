"""
Retry and error handling module for Hyperliquid Bot
"""

import asyncio
from loguru import logger


class DataBaseError(Exception):
    """
    Исключение для ошибок базы данных.
    """
    pass


class CustomError(Exception):
    """
    Пользовательское исключение.
    """
    pass


async def retry_async(func, max_retries: int = 3, delay: float = 1.0, *args, **kwargs):
    """
    Асинхронная функция с повторными попытками и специальной обработкой ошибки 429.
    
    Args:
        func: Функция для выполнения
        max_retries: Максимальное количество попыток
        delay: Задержка между попытками в секундах
        *args: Аргументы функции
        **kwargs: Именованные аргументы функции
    
    Returns:
        Результат выполнения функции
    
    Raises:
        Exception: Если все попытки исчерпаны
    """
    last_exception = None
    current_delay = delay
    
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            error_str = str(e)
            
            # Специальная обработка ошибки 429 (Too Many Requests)
            is_429_error = '429' in error_str or 'Too Many Requests' in error_str
            
            if attempt < max_retries:
                if is_429_error:
                    # Для 429 ошибки используем более длинную задержку
                    # Начинаем с 10 секунд (как указано в документации API)
                    retry_delay = 10.0 * (2 ** attempt)  # 10s, 20s, 40s
                    logger.warning(
                        f"Attempt {attempt + 1} failed with 429 (Rate Limit): {e}. "
                        f"Retrying in {retry_delay:.1f} seconds..."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    # Для других ошибок используем стандартную экспоненциальную задержку
                    logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {current_delay:.1f} seconds...")
                    await asyncio.sleep(current_delay)
                    current_delay *= 2  # Экспоненциальная задержка
            else:
                logger.error(f"All {max_retries + 1} attempts failed. Last error: {e}")
    
    raise last_exception


def retry_sync(func, max_retries: int = 3, delay: float = 1.0, *args, **kwargs):
    """
    Синхронная функция с повторными попытками.
    
    Args:
        func: Функция для выполнения
        max_retries: Максимальное количество попыток
        delay: Задержка между попытками в секундах
        *args: Аргументы функции
        **kwargs: Именованные аргументы функции
    
    Returns:
        Результат выполнения функции
    
    Raises:
        Exception: Если все попытки исчерпаны
    """
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay} seconds...")
                import time
                time.sleep(delay)
                delay *= 2  # Экспоненциальная задержка
            else:
                logger.error(f"All {max_retries + 1} attempts failed. Last error: {e}")
    
    raise last_exception
