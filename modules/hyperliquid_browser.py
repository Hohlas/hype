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
                'Content-Type': 'application/json',
            }
        else:
            headers = {
                'Content-Type': 'application/json',
            }
            
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
        Создание ордера - используем spot торговлю с числовым asset ID
        """
        try:
            # Проверяем, это spot или futures торговля
            coin = order_data.get("coin", "BTC-SPOT")
            
            if "-SPOT" in coin:
                # Spot торговля - используем числовой asset ID
                return await self._create_spot_order(order_data)
            else:
                # Futures торговля - используем обычный placeOrder
                return await self._create_futures_order(order_data)
                
        except Exception as e:
            self.log_message(f"Failed to create order: {e}", level="ERROR")
            raise
    

    async def _get_spot_asset_id(self, coin: str) -> int:
        """
        Получить числовой asset ID для spot актива.
        BTC-SPOT -> используем стандартный asset ID для BTC.
        """
        try:
            # Для основных криптовалют используем стандартные asset ID
            symbol_base = coin.split("-")[0]  # "BTC-SPOT" -> "BTC"
            
            # Стандартные asset ID для основных криптовалют в Hyperliquid
            asset_mapping = {
                "BTC": 10000,  # BTC spot asset ID
                "ETH": 10001,  # ETH spot asset ID  
                "SOL": 10002,  # SOL spot asset ID
                "AVAX": 10003, # AVAX spot asset ID
                "PURR": 10004, # PURR spot asset ID
            }
            
            if symbol_base in asset_mapping:
                asset_id = asset_mapping[symbol_base]
                self.log_message(f"✅ Using standard asset ID for {symbol_base}: {asset_id}", level="INFO")
                return asset_id
            
            # Если не нашли в стандартных, попробуем получить через API
            self.log_message(f"⚠️ {symbol_base} not in standard mapping, trying API...", level="INFO")
            
            # Попробуем разные типы запросов к API
            api_requests = [
                {"type": "meta"},
                {"type": "allMids"},
                {"type": "clearinghouseState"},
                {"type": "openOrders"},
            ]
            
            for request_type in api_requests:
                try:
                    url = f"{self.BASE_URL}/info"
                    response = await self.send_request("POST", url, data=request_type, build_signature=False)
                    
                    if response.status == 200:
                        data = await response.json()
                        self.log_message(f"🔍 DEBUG: API response for {request_type}: {data}", level="DEBUG")
                        
                        # Ищем актив в ответе
                        if isinstance(data, dict):
                            for key, value in data.items():
                                if isinstance(value, list):
                                    for item in value:
                                        if isinstance(item, dict) and item.get("name") == symbol_base:
                                            asset_id = item.get("index", 0) + 10000
                                            self.log_message(f"✅ Found {symbol_base} in {request_type}: asset_id={asset_id}", level="INFO")
                                            return asset_id
                except Exception as e:
                    self.log_message(f"⚠️ API request {request_type} failed: {e}", level="DEBUG")
                    continue
            
            # Если ничего не нашли, используем дефолтный asset ID для BTC
            if symbol_base == "BTC":
                default_asset_id = 10000
                self.log_message(f"⚠️ Using default asset ID for BTC: {default_asset_id}", level="WARNING")
                return default_asset_id
            
            raise Exception(f"Spot asset not found for {coin}. Tried standard mapping and API requests.")
            
        except Exception as e:
            self.log_message(f"Failed to get spot asset ID: {e}", level="ERROR")
            raise

    async def get_spot_assets_list(self):
        """
        Получить полный список доступных spot активов для диагностики
        """
        try:
            url = f"{self.BASE_URL}/info"
            payload = {"type": "spotMeta"}
            
            response = await self.send_request("POST", url, data=payload, build_signature=False)
            
            if response.status != 200:
                raise Exception(f"Failed to get spot meta: {response.status}")
            
            data = await response.json()
            
            # Записываем подробную диагностику в файл
            import json
            import os
            from datetime import datetime
            
            # Создаем папку для логов если её нет
            log_dir = "logs"
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            
            # Имя файла с временной меткой
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = os.path.join(log_dir, f"spot_assets_{timestamp}.json")
            
            # Записываем полный ответ API в файл
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            self.log_message(f"📋 Spot assets data saved to: {log_file}", level="INFO")
            
            universe = data.get("universe", [])
            self.log_message(f"📋 Found {len(universe)} spot assets", level="INFO")
            
            # Показываем только первые 5 активов в консоли
            for i, pair in enumerate(universe[:5]):
                pair_name = pair.get("name", "")
                pair_index = pair.get("index", i)
                asset_id = 10000 + pair_index
                self.log_message(f"  {i+1}. {pair_name} (index: {pair_index}, asset_id: {asset_id})", level="INFO")
            
            if len(universe) > 5:
                self.log_message(f"  ... and {len(universe) - 5} more (see {log_file})", level="INFO")
            
            return universe
            
        except Exception as e:
            self.log_message(f"Failed to get spot assets list: {e}", level="ERROR")
            raise

    async def _create_spot_order(self, order_data: dict):
        """
        Создание spot ордера через общий endpoint /exchange с числовым asset ID.
        """
        try:
            # Общий endpoint для всех ордеров (spot и futures)
            url = f"{self.BASE_URL}/exchange"
            
            # Получаем числовой asset ID для spot актива
            coin = order_data.get("coin", "BTC-SPOT")
            asset_id = await self._get_spot_asset_id(coin)
            
            # Форматируем размер до 5 знаков после запятой
            sz_value = float(order_data.get("sz", "0.001"))
            sz_str = f"{sz_value:.5f}"  # строка, напр. "0.00116"
            
            # Форматируем цену до 2 знаков после запятой
            px_value = float(order_data.get("limit_px", 108000))
            px_str = f"{px_value:.2f}"  # строка, напр. "107833.50"
            
            # ПРАВИЛЬНАЯ структура для spot ордера с числовым asset ID
            spot_payload = {
                "action": {
                "type": "order",
                    "orders": [{
                        "a": asset_id,  # ЧИСЛОВОЙ asset ID (например, 10002)
                        "b": bool(order_data.get("is_buy", True)),  # boolean
                        "s": sz_str,  # строка размера
                        "p": px_str,  # строка цены
                        "r": False,  # reduceOnly
                        "t": {"limit": {"tif": "Gtc"}}
                    }],
                    "grouping": "na"
                },
                "nonce": int(time.time() * 1000)
            }
            
            self.log_message(f"🔍 DEBUG: Spot payload with asset_id {asset_id}: {spot_payload}", level="DEBUG")
            
            # Отправка запроса с подписью
            response = await self.send_request(
                method="POST",
                url=url,
                data=spot_payload,
                build_signature=True
            )
            
            # Проверяем статус
            if response.status != 200:
                text = await response.text()
                self.log_message(f"❌ Spot order failed: {response.status}, {text}", level="ERROR")
                raise Exception(f"Failed to create spot order: {response.status}, message={text!r}")
            
            result = await response.json()
            self.log_message(f"✅ Spot order created successfully! Response: {result}", level="INFO")
            return result

        except Exception as e:
            self.log_message(f"Failed to create spot order: {e}", level="ERROR")
            raise
    
    async def _create_futures_order(self, order_data: dict):
        """
        Создание futures ордера через placeOrder
        """
        try:
            # Используем только рабочий endpoint для futures торговли
            endpoints_to_try = [
                f"{self.BASE_URL}/exchange"
            ]
            
            # Структуры payload для futures торговли (используем action wrapper!)
            payload_variants = [
                # Вариант 1: С action wrapper (как для spot)
                {
                    "action": {
                        "type": "order",
                        "orders": [{
                            "a": order_data.get("coin", "BTC-PERP"),  # строка "BTC-PERP"
                            "b": bool(order_data.get("is_buy", True)),  # boolean
                            "s": str(float(order_data.get("sz", "0.001"))),  # строка размера
                            "p": str(float(order_data.get("limit_px", 108000))),  # строка цены
                            "r": False,  # reduceOnly
                            "t": {"market": {}}
                        }],
                        "grouping": "na"
                    },
                    "nonce": int(time.time() * 1000)
                },
                # Вариант 2: Limit ордер с action wrapper
                {
                    "action": {
                        "type": "order",
                        "orders": [{
                            "a": order_data.get("coin", "BTC-PERP"),
                            "b": bool(order_data.get("is_buy", True)),
                            "s": str(float(order_data.get("sz", "0.001"))),
                            "p": str(float(order_data.get("limit_px", 108000))),
                            "r": False,
                            "t": {"limit": {"tif": "Gtc"}}
                        }],
                        "grouping": "na"
                    },
                    "nonce": int(time.time() * 1000)
                },
                # Вариант 3: Простая структура (старый формат)
                {
                    "coin": order_data.get("coin", "BTC-PERP"),
                    "is_buy": order_data.get("is_buy", True),
                    "sz": str(float(order_data.get("sz", "0.001"))),
                    "limit_px": str(float(order_data.get("limit_px", 108000))),
                    "reduce_only": order_data.get("reduce_only", False),
                    "order_type": {"market": {}},
                    "nonce": int(time.time() * 1000)
                }
            ]
            
            # Диагностика: проверим баланс и права доступа
            usdc_balance = None  # Инициализируем переменную
            try:
                self.log_message("🔍 DEBUG: Checking account balance and permissions...", level="DEBUG")
                account_info = await self.get_account_info()
                self.log_message(f"🔍 DEBUG: Account info: {account_info}", level="DEBUG")
                
                # Проверим, есть ли spot баланс
                spot_balance = account_info.get("spotBalances", [])
                self.log_message(f"🔍 DEBUG: Spot balances: {spot_balance}", level="DEBUG")
                
                # Попробуем получить spot данные через другой endpoint
                try:
                    spot_url = f"{self.BASE_URL}/info"
                    spot_data = {"type": "spotClearinghouseState", "user": self.wallet_address}
                    spot_response = await self.send_request("POST", spot_url, data=spot_data, build_signature=False)
                    if spot_response.status == 200:
                        spot_info = await spot_response.json()
                        self.log_message(f"🔍 DEBUG: Spot clearinghouse info: {spot_info}", level="DEBUG")
                        
                        # Проверим балансы в spot данных
                        if "balances" in spot_info:
                            spot_balances = spot_info["balances"]
                            self.log_message(f"🔍 DEBUG: Spot balances from clearinghouse: {spot_balances}", level="DEBUG")
                            
                            # Проверим, есть ли USDC баланс
                            for balance in spot_balances:
                                if balance.get("coin") == "USDC":
                                    usdc_balance = float(balance.get("total", "0"))
                                    self.log_message(f"🔍 DEBUG: USDC balance from clearinghouse: {usdc_balance}", level="DEBUG")
                                    break
                    else:
                        self.log_message(f"⚠️ WARNING: Failed to get spot clearinghouse info: {spot_response.status}", level="WARNING")
                except Exception as spot_error:
                    self.log_message(f"⚠️ WARNING: Failed to get spot clearinghouse info: {spot_error}", level="WARNING")
                
                # Проверим, есть ли USDC баланс в основном аккаунте
                if usdc_balance is None:
                    for balance in spot_balance:
                        if balance.get("coin") == "USDC":
                            usdc_balance = float(balance.get("total", "0"))
                            self.log_message(f"🔍 DEBUG: USDC balance from main account: {usdc_balance}", level="DEBUG")
                            break
                
                # Финальная проверка
                if usdc_balance is None:
                    self.log_message("⚠️ WARNING: No USDC balance found in any account", level="WARNING")
                else:
                    self.log_message(f"✅ USDC balance confirmed: {usdc_balance}", level="INFO")
                    
            except Exception as e:
                self.log_message(f"⚠️ WARNING: Failed to check account info: {e}", level="WARNING")
            
            success = False
            response = None
            
            # Попробуем каждый endpoint с каждым payload
            for endpoint_url in endpoints_to_try:
                self.log_message(f"🔍 DEBUG: Trying endpoint: {endpoint_url}", level="DEBUG")
                
                for i, order_payload in enumerate(payload_variants, 1):
                    try:
                        self.log_message(f"🔍 DEBUG: Trying payload variant {i}: {order_payload}", level="DEBUG")
                        response = await self.send_request("POST", endpoint_url, data=order_payload, build_signature=True)
                        if response.status == 200:
                            success = True
                            self.log_message(f"✅ SUCCESS: Endpoint {endpoint_url} with payload variant {i} worked!", level="INFO")
                            break
                        else:
                            self.log_message(f"❌ Endpoint {endpoint_url} payload variant {i} failed: {response.status}", level="DEBUG")
                    except Exception as e:
                        self.log_message(f"❌ Endpoint {endpoint_url} payload variant {i} error: {e}", level="DEBUG")
                
                if success:
                    break
            
            if not success:
                self.log_message("❌ All payload variants failed", level="WARNING")
                
                # Проверяем, есть ли USDC баланс (используем данные из clearinghouse)
                if usdc_balance is None or usdc_balance == 0:
                    self.log_message("❌ CRITICAL: No USDC balance in spot account! You need to deposit USDC first.", level="ERROR")
                    self.log_message("💡 SOLUTION: Transfer USDC to your spot account before trading.", level="INFO")
                    raise Exception("No USDC balance in spot account")
                else:
                    self.log_message(f"✅ USDC balance found: {usdc_balance}, but trading still failed", level="WARNING")
                    self.log_message("💡 This suggests the issue is with the order structure, not balance", level="INFO")
            
            # Проверяем статус ответа
            if response and response.status != 200:
                # Пытаемся получить текст ошибки
                try:
                    error_text = await response.text()
                    self.log_message(f"🔍 DEBUG: Error response text: {error_text}", level="DEBUG")
                    raise Exception(f'Failed to create order: {response.status}, message=\'{error_text}\'')
                except Exception as text_error:
                    self.log_message(f"🔍 DEBUG: Failed to get error text: {text_error}", level="DEBUG")
                    raise Exception(f'Failed to create order: {response.status}')
            
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
            # Используем стандартный endpoint для всех типов торговли
            url = f"{self.BASE_URL}/exchange"
            
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
