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
        
    async def calculate_usdc_amount_for_order(self, token_name: str) -> tuple[float, int]:
        """
        Расчет размера ордера в USDC для spot торговли.
        Возвращает (usdc_amount, leverage=1 для spot)
        """
        try:
            # Получаем доступный баланс USDC (с учетом зарезервированных средств)
            balance = float(await self.browser.get_available_balance())
            
            # Получаем лимиты токена
            min_qty, min_notional = await self.get_token_limits(token_name)
            
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
            
            # Логируем расчет для отладки
            self.browser.log_message(
                f"💸 Order calculation: Available=${balance:.2f}, Percent={percent}%, "
                f"MinQty=${min_usdc_for_qty:.2f}, MinNotional=${min_notional:.2f}, "
                f"Final Amount=${usdc_amount:.2f}",
                level="DEBUG"
            )
            
            return usdc_amount, leverage
            
        except Exception as e:
            self.browser.log_message(f"Failed to calculate order amount: {e}", level="ERROR")
            return 0.0, 1
            
    async def get_token_limits(self, token_name: str) -> tuple[float, float]:
        """
        Получение лимитов токена для spot торговли.
        Возвращает (min_qty, min_notional)
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
                    # Логируем полную структуру токена для отладки
                    self.browser.log_message(
                        f"🔍 DEBUG: Full token data for {token_name_from_api}: {token}",
                        level="DEBUG"
                    )
                    
                    # Получаем минимальный размер из метаданных токена
                    meta = token.get("meta", {})
                    if isinstance(meta, dict):
                        # Логируем все доступные поля в meta
                        self.browser.log_message(
                            f"🔍 DEBUG: Meta fields: {list(meta.keys())}",
                            level="DEBUG"
                        )
                        
                        # Пробуем разные возможные поля для минимального размера
                        min_qty = None
                        for field in ["minSz", "minSize", "min_qty", "minQuantity", "szDecimals"]:
                            if field in meta:
                                try:
                                    min_qty = float(meta[field])
                                    self.browser.log_message(f"🔍 DEBUG: Found {field}={min_qty}", level="DEBUG")
                                    break
                                except:
                                    continue
                        
                        # Если не нашли минимальный размер, используем очень маленькое значение
                        if min_qty is None:
                            min_qty = 0.0001  # Очень маленькое значение
                            self.browser.log_message(f"🔍 DEBUG: No min size found, using {min_qty}", level="DEBUG")
                        
                        # ВРЕМЕННО: Устанавливаем очень маленькое значение для тестирования
                        min_qty = 0.0001
                        self.browser.log_message(f"🔍 DEBUG: TEMPORARY: Overriding min_qty to {min_qty} for testing", level="DEBUG")
                        
                        min_notional = float(meta.get("minNotional", "5.0"))  # Минимальная номинальная стоимость
                        
                        self.browser.log_message(
                            f"📏 Token limits from API: {token_name_from_api} - MinQty={min_qty}, MinNotional=${min_notional}",
                            level="INFO"
                        )
                        
                        return min_qty, min_notional
            
            # Если токен не найден, используем стандартные значения
            self.browser.log_message(f"⚠️ Token {token_name} not found in API, using default limits", level="WARNING")
            return 0.001, 10.0
            
        except Exception as e:
            self.browser.log_message(f"Failed to get token limits: {e}", level="ERROR")
            return 0.001, 10.0
            
    async def place_order(self, token: str, side: str, order_type: str, 
                         quantity: float = None, price: float = None, 
                         usdc_amount: float = None, stop_price: float = None, **kwargs):
        """
        Размещение ордера на Hyperliquid
        """
        try:
            # Получаем текущую цену токена
            token_price = float(await self.browser.get_token_price(token))
            
            # Определяем количество токенов
            if quantity is None and usdc_amount is not None:
                quantity = usdc_amount / token_price
            elif quantity is None:
                raise ValueError("Either quantity or usdc_amount must be provided")
                
            # Подготовка данных ордера для Hyperliquid API
            order_data = {
                "coin": f"{token}-SPOT",  # Используем BTC-SPOT для spot торговли
                "is_buy": side == "BUY",  # true для покупки, false для продажи
                "sz": str(quantity),  # Size как строка
                "reduce_only": False  # Reduce only
            }
            
            # Определяем тип ордера и соответствующие параметры
            if order_type == "LIMIT":
                order_data["limit_px"] = str(price) if price else str(token_price)
                order_data["order_type"] = {"limit": {"tif": "Gtc"}}
            elif order_type == "MARKET":
                # Для маркет ордеров используем текущую цену
                order_data["limit_px"] = str(token_price)
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
                return await trade_delayed_orders_strategy(self, "BTC")
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
