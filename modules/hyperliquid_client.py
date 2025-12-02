import asyncio
import time
from decimal import Decimal
from typing import Dict, List, Optional
import settings
from modules.hyperliquid_browser import HyperliquidBrowser
from modules.utils import async_sleep


class HyperliquidClient:
    """
    Клиент для работы с Hyperliquid API.
    Адаптирован для spot торговли на децентрализованной платформе.
    """
    
    def __init__(self, browser: HyperliquidBrowser, wallet_address: str, private_key: str, 
                 label: str, proxy: str, db, group_data: dict = None):
        self.browser = browser
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.label = label.replace("Account ", "Acc ")
        self.proxy = proxy
        self.db = db
        self.group_data = group_data
        
        # Кэш для лимитов токенов (загружается один раз при старте)
        # Формат: {token_name: (min_qty, min_notional, sz_decimals)}
        self.token_limits_cache = {}
        
    async def calculate_usdc_amount_for_order(self, token_name: str) -> tuple[float, int]:
        """
        Расчет размера ордера в USDC для spot торговли.
        Возвращает (usdc_amount, leverage=1 для spot)
        """
        try:
            # Получаем доступный баланс USDC (с учетом зарезервированных средств)
            balance = float(await self.browser.get_available_balance())
            
            # Получаем лимиты токена
            min_qty, min_notional, _ = await self.get_token_limits(token_name)
            
            # Получаем текущую цену токена
            token_price = float(await self.browser.get_token_price(token_name))
            
            # Рассчитываем минимальный размер ордера в USDC для соответствия min_qty
            min_usdc_for_qty = min_qty * token_price
            
            # Рассчитываем размер ордера по проценту от баланса
            percent = settings.POSITION_SIZE_PERCENT
            usdc_amount_by_percent = balance * (percent / 100)
            
            # Используем максимальное значение из минимального требования и процентного расчета
            usdc_amount = max(min_usdc_for_qty, usdc_amount_by_percent, min_notional)
            
            # Для spot торговли кредитное плечо всегда 1
            leverage = 1
            
            # Логируем расчет для отладки (отключено для чистоты логов)
            # self.browser.log_message(
            #     f"💸 Order calculation: Available=${balance:.2f}, Percent={percent}%, "
            #     f"MinQty=${min_usdc_for_qty:.2f}, MinNotional=${min_notional:.2f}, "
            #     f"Final Amount=${usdc_amount:.2f}",
            #     level="DEBUG"
            # )
            
            return usdc_amount, leverage
            
        except Exception as e:
            self.browser.log_message(f"Failed to calculate order amount: {e}", level="ERROR")
            return 0.0, 1
            
    async def load_token_limits(self, token_name: str) -> tuple[float, float, int]:
        """
        Загрузка лимитов токена из API (вызывается один раз при старте).
        Сохраняет результат в кэш.
        Возвращает (min_qty, min_notional, sz_decimals)
        """
        try:
            # Получаем реальные лимиты от API Hyperliquid
            url = f"{self.browser.BASE_URL}/info"
            data = {
                "type": "spotMeta"
            }
            
            response = await self.browser.send_request("POST", url, data=data)
            result = await response.json()
            
            if response.status != 200:
                raise Exception(f'Failed to get spot meta: {result}')
            
            # Ищем токен в метаданных
            tokens = result.get("tokens", [])
            for token in tokens:
                token_name_from_api = token.get("name", "")
                if token_name_from_api == token_name or token_name_from_api == f"U{token_name}":
                    # Получаем минимальный размер из метаданных токена
                    meta = token.get("meta", {})
                    if isinstance(meta, dict):
                        # Пробуем разные возможные поля для минимального размера
                        min_qty = None
                        for field in ["minSz", "minSize", "min_qty", "minQuantity"]:
                            if field in meta:
                                try:
                                    min_qty = float(meta[field])
                                    break
                                except:
                                    continue
                        
                        # Если не нашли минимальный размер, используем очень маленькое значение
                        if min_qty is None:
                            min_qty = 0.0001  # Очень маленькое значение
                        
                        # Устанавливаем очень маленькое значение для тестирования
                        min_qty = 0.0001
                        
                        # Получаем minNotional из API, если доступно, иначе используем значение из ошибки биржи (10.0)
                        min_notional = float(meta.get("minNotional", "10.0"))
                        
                        # Получаем szDecimals (precision) для форматирования размера ордеров
                        sz_decimals = meta.get("szDecimals", 5)  # По умолчанию 5 знаков
                        if isinstance(sz_decimals, (int, float)):
                            sz_decimals = int(sz_decimals)
                        else:
                            sz_decimals = 5  # Fallback
                        
                        # Сохраняем в кэш
                        self.token_limits_cache[token_name] = (min_qty, min_notional, sz_decimals)
                        
                        self.browser.log_message(
                            f"✅ Loaded token limits: {token_name} | min_qty={min_qty}, min_notional={min_notional}, sz_decimals={sz_decimals}",
                            level="INFO"
                        )
                        
                        return min_qty, min_notional, sz_decimals
            
            # Если токен не найден, используем стандартные значения
            default_limits = (0.001, 10.0, 5)
            self.token_limits_cache[token_name] = default_limits
            self.browser.log_message(f"⚠️ Token {token_name} not found in API, using default limits", level="WARNING")
            return default_limits
            
        except Exception as e:
            default_limits = (0.001, 10.0, 5)
            self.token_limits_cache[token_name] = default_limits
            self.browser.log_message(f"Failed to load token limits: {e}, using defaults", level="ERROR")
            return default_limits
    
    async def get_token_limits(self, token_name: str) -> tuple[float, float, int]:
        """
        Получение лимитов токена из кэша (быстрый доступ без API запроса).
        Если кэш пуст, загружает лимиты из API.
        Возвращает (min_qty, min_notional, sz_decimals)
        """
        # Проверяем кэш
        if token_name in self.token_limits_cache:
            return self.token_limits_cache[token_name]
        
        # Если кэш пуст, загружаем (fallback для обратной совместимости)
        return await self.load_token_limits(token_name)
    
    def get_token_sz_decimals(self, token_name: str) -> int:
        """
        Получение precision (sz_decimals) для токена из кэша.
        Возвращает количество знаков после запятой для форматирования размера ордера.
        """
        if token_name in self.token_limits_cache:
            _, _, sz_decimals = self.token_limits_cache[token_name]
            return sz_decimals
        return 5  # Fallback значение
            
    async def place_order(self, token: str, side: str, order_type: str, 
                         quantity: float = None, price: float = None, 
                         usdc_amount: float = None, stop_price: float = None, **kwargs):
        """
        Размещение ордера на Hyperliquid
        
        Args:
            token: Название токена
            side: Сторона ордера (BUY/SELL)
            order_type: Тип ордера (LIMIT/MARKET/STOP_MARKET/STOP_LIMIT)
            quantity: Количество токенов (опционально, если указан usdc_amount)
            price: Цена ордера (обязательно для LIMIT и MARKET)
            usdc_amount: Сумма в USDC (опционально, если указан quantity)
            stop_price: Цена стопа (для STOP_MARKET/STOP_LIMIT)
        """
        try:
            # Цена обязательна для LIMIT и MARKET ордеров
            if price is None:
                if order_type in ["LIMIT", "MARKET"]:
                    raise ValueError(f"price is required for {order_type} orders")
            
            # Определяем количество токенов
            if quantity is None and usdc_amount is not None:
                # Используем переданную цену для расчета количества
                # Для STOP_MARKET используем stop_price если price не передан
                price_for_calculation = price
                if price_for_calculation is None and order_type == "STOP_MARKET" and stop_price is not None:
                    price_for_calculation = stop_price
                
                if price_for_calculation is None:
                    raise ValueError("price or stop_price is required for calculating quantity from usdc_amount")
                
                quantity = usdc_amount / float(price_for_calculation)
            elif quantity is None:
                raise ValueError("Either quantity or usdc_amount must be provided")
                
            # Получаем sz_decimals для форматирования размера ордера
            sz_decimals = self.get_token_sz_decimals(token)
            
            # Округляем количество до sz_decimals для проверки реальной стоимости
            rounded_quantity = round(quantity, sz_decimals)
            
            # Проверяем минимальную номинальную стоимость после округления
            # Для LIMIT ордеров используем price, для MARKET тоже price
            if order_type in ["LIMIT", "MARKET"] and price is not None:
                min_notional = 10.0  # Минимальная номинальная стоимость 10 USDC
                actual_notional = rounded_quantity * float(price)
                
                if actual_notional < min_notional:
                    raise ValueError(
                        f"Order size after rounding ({rounded_quantity:.{sz_decimals}f} * ${price:.2f} = ${actual_notional:.2f}) "
                        f"is less than minimum ${min_notional:.2f} USDC"
                    )
            
            # Подготовка данных ордера для Hyperliquid API
            order_data = {
                "coin": f"{token}-SPOT",  # Используем BTC-SPOT для spot торговли
                "is_buy": side == "BUY",  # true для покупки, false для продажи
                "sz": str(quantity),  # Size как строка
                "sz_decimals": sz_decimals,  # Precision для форматирования размера
                "reduce_only": False  # Reduce only
            }
            
            # Определяем тип ордера и соответствующие параметры
            if order_type == "LIMIT":
                order_data["limit_px"] = str(price)
                order_data["order_type"] = {"limit": {"tif": "Gtc"}}
            elif order_type == "MARKET":
                # Для маркет ордеров используем переданную цену
                order_data["limit_px"] = str(price)
                order_data["order_type"] = {"market": {}}
            elif order_type == "STOP_MARKET":
                # Временно используем лимитные ордера вместо стоп-маркет
                if stop_price is None:
                    raise ValueError("stop_price is required for STOP_MARKET orders")
                order_data["limit_px"] = str(stop_price)
                order_data["order_type"] = {"limit": {"tif": "Gtc"}}
            elif order_type == "STOP_LIMIT":
                # Стоп-лимит ордер точно как в примере Perplexity
                if stop_price is None:
                    raise ValueError("stop_price is required for STOP_LIMIT orders")
                order_data["order_type"] = {
                    "trigger": {
                        "isMarket": False,
                        "triggerPx": int(stop_price),  # Число, а не строка
                        "tpsl": "sl"
                    }
                }
                # Убираем limit_px для trigger ордеров
                if "limit_px" in order_data:
                    del order_data["limit_px"]
            else:
                raise ValueError(f"Unsupported order type: {order_type}")
            
            # Создание ордера
            result = await self.browser.create_order(order_data)
            
            # Детальное логирование ответа API (убрано для чистоты логов)
            
            # Добавляем количество токенов в результат для использования в уведомлениях
            result['quantity'] = quantity
            
            return result
            
        except Exception as e:
            self.browser.log_message(f"Failed to place order: {e}", level="ERROR")
            raise
            
    async def get_position_info(self, token_name: str) -> dict:
        """
        Получение информации о позиции для spot торговли.
        Для spot торговли позиции - это просто баланс токенов.
        """
        try:
            # Получаем баланс токена
            account_info = await self.browser.get_account_info()
            
            # Поиск позиции по токену
            for asset in account_info.get("assetPositions", []):
                if asset["position"]["coin"] == token_name:
                    position_size = float(asset["position"]["szi"])
                    entry_price = float(asset["position"]["entryPx"]) if asset["position"]["entryPx"] else 0.0
                    
                    return {
                        "size": position_size,
                        "entry_price": entry_price,
                        "unrealized_pnl": 0.0,  # Для spot торговли PnL рассчитывается отдельно
                        "side": "LONG" if position_size > 0 else "SHORT" if position_size < 0 else "NONE"
                    }
                    
            # Если позиции нет
            return {
                "size": 0.0,
                "entry_price": 0.0,
                "unrealized_pnl": 0.0,
                "side": "NONE"
            }
            
        except Exception as e:
            self.browser.log_message(f"Failed to get position info: {e}", level="ERROR")
            return {
                "size": 0.0,
                "entry_price": 0.0,
                "unrealized_pnl": 0.0,
                "side": "NONE"
            }
            
    async def get_open_orders(self, token_name: str) -> List[dict]:
        """
        Получение открытых ордеров для токена
        """
        try:
            orders = await self.browser.get_account_orders()
            
            # Фильтруем ордера по токену
            filtered_orders = []
            for order in orders:
                if order.get("coin") == token_name:
                    filtered_orders.append(order)
                    
            return filtered_orders
            
        except Exception as e:
            self.browser.log_message(f"Failed to get open orders: {e}", level="ERROR")
            return []
            
    async def cancel_all_orders(self, token_name: str):
        """
        Отмена всех ордеров для токена
        """
        try:
            orders = await self.get_open_orders(token_name)
            
            for order in orders:
                order_id = order.get("oid")  # Order ID
                if order_id:
                    await self.browser.cancel_order(token_name, order_id)
                    await async_sleep(0.1)  # Небольшая пауза между отменами
                    
        except Exception as e:
            self.browser.log_message(f"Failed to cancel all orders: {e}", level="ERROR")
            
    async def run_mode(self, mode: int):
        """
        Запуск торгового режима
        """
        try:
            if mode == 0:
                # Delayed Orders Strategy для spot торговли
                from modules.delayed_orders_strategy import trade_delayed_orders_strategy
                import settings
                # Берем токен из настроек
                token_name = settings.SPOT_TOKEN if hasattr(settings, 'SPOT_TOKEN') and settings.SPOT_TOKEN else "BTC"
                return await trade_delayed_orders_strategy(self, token_name)
            elif mode == 4:
                # Cancel Orders
                from modules.cancel_orders import cancel_all_orders
                return await cancel_all_orders(self)
            elif mode == 5:
                # Parse mode
                from modules.parse_mode import parse_mode
                return await parse_mode(self)
            else:
                self.browser.log_message(f"Unknown mode: {mode}", level="ERROR")
                return False
                
        except Exception as e:
            self.browser.log_message(f"Failed to run mode {mode}: {e}", level="ERROR")
            return False
