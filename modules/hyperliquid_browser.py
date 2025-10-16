import aiohttp
import asyncio
from decimal import Decimal
from typing import Dict, List, Optional
import json
import time
import hmac
import hashlib
from eth_account import Account
from eth_account.messages import encode_defunct
import web3
from loguru import logger
import settings


class HyperliquidBrowser:
    """
    Клиент для работы с Hyperliquid API.
    Адаптирован для Web3-кошельков и децентрализованной торговли.
    """
    
    def __init__(self, proxy: str = None, wallet_address: str = None, private_key: str = None, label: str = None, db=None):
        self.proxy = proxy
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.label = label
        self.db = db
        
        # Hyperliquid API endpoints
        self.BASE_URL = settings.HYPERLIQUID_BASE_URL
        self.TESTNET_URL = settings.HYPERLIQUID_TESTNET_URL
        
        # Web3 setup
        self.w3 = web3.Web3(web3.HTTPProvider(settings.WEB3_RPC_URL))
        self.account = Account.from_key(private_key) if private_key else None
        
        # Session management
        self.session = None
        
        # Rate limiting
        self.last_request_time = 0
        self.min_request_interval = 0.1  # 100ms between requests
        
    async def __aenter__(self):
        await self.start_session()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_sessions()
        
    async def start_session(self):
        """Инициализация HTTP сессии"""
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
        timeout = aiohttp.ClientTimeout(total=30)
        
        if self.proxy:
            connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
            timeout = aiohttp.ClientTimeout(total=30)
        
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                'User-Agent': 'HyperliquidBot/1.0',
                'Content-Type': 'application/json',
            }
        )
        
    async def close_sessions(self):
        """Закрытие HTTP сессий"""
        if self.session:
            await self.session.close()
            self.session = None
            
    async def send_request(self, method: str, url: str, params: dict = None, data: dict = None, build_signature: bool = False):
        """
        Отправка HTTP запроса с подписью (если требуется)
        """
        if not self.session:
            await self.start_session()
            
        # Rate limiting
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.min_request_interval:
            await asyncio.sleep(self.min_request_interval - time_since_last)
        self.last_request_time = time.time()
        
        # Подготовка данных для подписи
        if build_signature and data:
            signature = await self._sign_message(data)
            headers = {
                'Authorization': f'Bearer {signature}',
                'X-Wallet-Address': self.wallet_address,
            }
        else:
            headers = {}
            
        try:
            if method == "GET":
                response = await self.session.get(url, params=params, headers=headers)
            elif method == "POST":
                # Для Hyperliquid API используем json вместо data
                if build_signature:
                    headers["Content-Type"] = "application/json"
                    response = await self.session.post(url, json=data, params=params, headers=headers)
                else:
                    response = await self.session.post(url, json=data, params=params, headers=headers)
            elif method == "PUT":
                response = await self.session.put(url, json=data, params=params, headers=headers)
            elif method == "DELETE":
                response = await self.session.delete(url, params=params, headers=headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
                
            return response
            
        except Exception as e:
            logger.error(f"Request failed: {e}")
            raise
            
    async def _sign_message(self, message: dict) -> str:
        """
        Подписание сообщения приватным ключом кошелька
        """
        try:
            # Сериализация сообщения
            message_str = json.dumps(message, separators=(',', ':'), sort_keys=True)
            
            # Создание подписи
            message_hash = encode_defunct(text=message_str)
            signed_message = self.account.sign_message(message_hash)
            
            return signed_message.signature.hex()
            
        except Exception as e:
            logger.error(f"Failed to sign message: {e}")
            raise
            
    async def get_account_info(self):
        """
        Получение информации об аккаунте (perp торговля)
        """
        try:
            url = f"{self.BASE_URL}/info"
            data = {
                "type": "clearinghouseState",
                "user": self.wallet_address
            }
            
            response = await self.send_request("POST", url, data=data)
            result = await response.json()
            
            if response.status != 200:
                raise Exception(f'Failed to get account info: {result}')
                
            return result
            
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            raise
            
    async def get_spot_account_info(self):
        """
        Получение информации о spot аккаунте
        """
        try:
            url = f"{self.BASE_URL}/info"
            data = {
                "type": "spotClearinghouseState",
                "user": self.wallet_address
            }
            
            response = await self.send_request("POST", url, data=data)
            result = await response.json()
            
            if response.status != 200:
                raise Exception(f'Failed to get spot account info: {result}')
                
            return result
            
        except Exception as e:
            logger.error(f"Failed to get spot account info: {e}")
            raise
            
    async def get_balance(self):
        """
        Получение баланса spot аккаунта
        """
        try:
            # Используем spot API вместо perp API
            spot_info = await self.get_spot_account_info()
            
            # Логируем только важные поля
            # self.log_message(f"🔍 DEBUG: Spot account fields:", level="DEBUG")
            
            # Ищем USDC баланс в spot аккаунте
            balances = spot_info.get("balances", [])
            
            for balance in balances:
                coin = balance.get("coin", "")
                total = float(balance.get("total", 0))
                
                if coin == "USDC":
                    return total
            
            return 0.0
            
        except Exception as e:
            self.log_message(f"Failed to get spot balance: {e}", level="ERROR")
            return 0.0
            
    async def get_token_price(self, token_name: str):
        """
        Получение текущей цены токена
        """
        try:
            url = f"{self.BASE_URL}/info"
            data = {
                "type": "allMids"
            }
            
            response = await self.send_request("POST", url, data=data)
            result = await response.json()
            
            if response.status != 200:
                raise Exception(f'Failed to get token price: {result}')
            
            # Логируем структуру ответа для отладки (отключено)
            # self.log_message(f"🔍 DEBUG: Price data keys: {list(result.keys())}", level="DEBUG")
            
            # Пробуем разные варианты названий токенов
            possible_keys = [
                f"{token_name}USD",
                f"{token_name}USDT", 
                f"{token_name}",
                token_name.upper(),
                token_name.lower()
            ]
            
            for key in possible_keys:
                if key in result:
                    price = Decimal(str(result[key]))
                    # self.log_message(f"🔍 DEBUG: Found price for {token_name} as {key}: ${price}", level="DEBUG")
                    return price
            
            # Если не нашли, показываем доступные токены
            available_tokens = [key for key in result.keys() if 'USD' in key or 'USDT' in key]
            self.log_message(f"🔍 DEBUG: Available tokens: {available_tokens}", level="DEBUG")
            
            raise Exception(f'Token {token_name} not found in price data. Available: {available_tokens}')
                
        except Exception as e:
            self.log_message(f"Failed to get token price: {e}", level="ERROR")
            raise
            
    async def get_account_positions(self):
        """
        Получение позиций аккаунта
        """
        try:
            account_info = await self.get_account_info()
            positions = account_info.get("assetPositions", [])
            
            # Фильтруем только позиции с ненулевым размером
            active_positions = []
            for pos in positions:
                if float(pos["position"]["szi"]) != 0:
                    active_positions.append(pos)
                    
            return active_positions
            
        except Exception as e:
            logger.error(f"Failed to get account positions: {e}")
            return []
            
    async def get_account_orders(self):
        """
        Получение активных ордеров аккаунта для spot торговли
        """
        try:
            url = f"{self.BASE_URL}/info"
            data = {
                "type": "openOrders",
                "user": self.wallet_address
            }
            
            response = await self.send_request("POST", url, data=data)
            result = await response.json()
            
            if response.status != 200:
                raise Exception(f'Failed to get account orders: {result}')
                
            return result
            
        except Exception as e:
            logger.error(f"Failed to get account orders: {e}")
            return []
            
    async def create_order(self, order_data: dict):
        """
        Создание ордера
        """
        try:
            # Для spot торговли используем специальный endpoint
            url = f"{self.BASE_URL}/spot-exchange"
            
            # Подготовка данных ордера для Hyperliquid API
            # Используем структуру с action для spot торговли
            order_payload = {
                "action": {
                    "type": "order",
                    "orders": [order_data]
                },
                "nonce": int(time.time() * 1000)
            }
            
            # Логируем данные ордера для отладки
            self.log_message(f"🔍 DEBUG: Order payload: {order_payload}", level="DEBUG")
            
            # Логируем подпись для отладки
            signature = await self._sign_message(order_payload)
            self.log_message(f"🔍 DEBUG: Signature: {signature}", level="DEBUG")
            
            response = await self.send_request("POST", url, data=order_payload, build_signature=True)
            
            # Проверяем статус ответа
            if response.status != 200:
                # Пытаемся получить текст ошибки
                try:
                    error_text = await response.text()
                    self.log_message(f"🔍 DEBUG: Error response text: {error_text}", level="DEBUG")
                    raise Exception(f'Failed to create order: {response.status}, message=\'{error_text}\', url=\'{url}\'')
                except Exception as text_error:
                    self.log_message(f"🔍 DEBUG: Failed to get error text: {text_error}", level="DEBUG")
                    raise Exception(f'Failed to create order: {response.status}, url=\'{url}\'')
            
            # Пытаемся парсить JSON
            try:
                result = await response.json()
                return result
            except Exception as json_error:
                # Если не JSON, возвращаем текст
                text_result = await response.text()
                self.log_message(f"🔍 DEBUG: Non-JSON response: {text_result}", level="DEBUG")
                return {"status": "success", "message": text_result}
            
        except Exception as e:
            logger.error(f"Failed to create order: {e}")
            raise
            
    async def cancel_order(self, token_name: str, order_id: int):
        """
        Отмена ордера
        """
        try:
            # Для spot торговли используем специальный endpoint
            url = f"{self.BASE_URL}/spot-exchange"
            
            cancel_payload = {
                "action": {
                    "type": "cancel",
                    "cancels": [{
                        "coin": token_name,
                        "oid": order_id
                    }]
                },
                "nonce": int(time.time() * 1000),
            }
            
            response = await self.send_request("POST", url, data=cancel_payload, build_signature=True)
            result = await response.json()
            
            if response.status != 200:
                raise Exception(f'Failed to cancel order: {result}')
                
            return result
            
        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
            raise
            
    async def get_all_orders_history(self, token_name: str, limit: int = 500):
        """
        Получение истории всех ордеров для токена
        """
        try:
            url = f"{self.BASE_URL}/info"
            data = {
                "type": "userTrades",
                "user": self.wallet_address,
                "startTime": 0,  # Начало времени
                "endTime": int(time.time() * 1000),  # Текущее время
            }
            
            response = await self.send_request("POST", url, data=data)
            result = await response.json()
            
            if response.status != 200:
                raise Exception(f'Failed to get orders history: {result}')
                
            # Фильтруем ордера по токену
            filtered_orders = []
            for trade in result:
                if trade.get("coin") == token_name:
                    filtered_orders.append(trade)
                    
            return filtered_orders[:limit]
            
        except Exception as e:
            logger.error(f"Failed to get orders history: {e}")
            return []
            
    def log_message(self, message: str, level: str = "INFO"):
        """
        Логирование сообщений
        """
        logger.log(level, f'[{self.label}] {message}')
