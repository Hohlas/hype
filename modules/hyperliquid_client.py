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
            # Получаем баланс USDC
            balance = float(await self.browser.get_balance())
            
            # Используем процент от баланса для spot торговли
            percent = settings.TRADE_AMOUNTS["percent"][0]
            usdc_amount = balance * (percent / 100)
            
            # Для spot торговли кредитное плечо всегда 1
            leverage = 1
            
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
            # Для Hyperliquid используем стандартные лимиты
            # Минимальное количество токена
            min_qty = 0.001  # Минимум 0.001 токена
            
            # Минимальная номинальная стоимость в USDC
            min_notional = 5.0  # Минимум $5 USDC
            
            return min_qty, min_notional
            
        except Exception as e:
            self.browser.log_message(f"Failed to get token limits: {e}", level="ERROR")
            return 0.001, 5.0
            
    async def place_order(self, token: str, side: str, order_type: str, 
                         quantity: float = None, price: float = None, 
                         usdc_amount: float = None, **kwargs):
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
            # Правильная структура согласно документации
            order_data = {
                "a": token,  # Asset (токен)
                "b": side == "BUY",  # Is buy order
                "p": str(price) if price else str(token_price),  # Price
                "s": str(quantity),  # Size (количество)
                "r": False,  # Reduce only
                "t": "Limit" if order_type == "LIMIT" else "Market",  # Order type
                "i": int(time.time() * 1000)  # Client order ID
            }
            
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
                order_id = order.get("oid")
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
                # Averaging Market для spot торговли
                from modules.averaging_market import trade_averaging_market
                return await trade_averaging_market(self, "BTC", "BUY")
            elif mode == 1:
                # Futures Market (не поддерживается для spot)
                self.browser.log_message("Futures trading not supported in spot mode", level="WARNING")
                return False
            elif mode == 2:
                # Futures Limit (не поддерживается для spot)
                self.browser.log_message("Futures trading not supported in spot mode", level="WARNING")
                return False
            elif mode == 3:
                # Pair trading (не поддерживается для spot)
                self.browser.log_message("Pair trading not supported in spot mode", level="WARNING")
                return False
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
