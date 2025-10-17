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
            
            # Пробуем разные варианты названий токенов
            possible_keys = [
                f"{token_name}USD",
                f"{token_name}USDT", 
                f"{token_name}",
                token_name.upper(),
                token_name.lower(),
                f"{token_name}-PERP",  # Для futures
                f"{token_name}-SPOT"   # Для spot
            ]
            
            for key in possible_keys:
                if key in result:
                    price = Decimal(str(result[key]))
                    self.log_message(f"🔍 DEBUG: Found price for {token_name} as {key}: ${price}", level="DEBUG")
                    return price
            
            # Если не нашли, показываем доступные токены
            available_tokens = [key for key in result.keys() if 'USD' in key or 'USDT' in key or 'PERP' in key or 'SPOT' in key]
            self.log_message(f"🔍 DEBUG: Available tokens: {available_tokens[:10]}...", level="DEBUG")
            
            raise Exception(f'Token {token_name} not found in price data. Available: {available_tokens[:10]}...')
                
        except Exception as e:
            self.log_message(f"Failed to get token price: {e}", level="ERROR")
            raise
            
    async def get_available_assets(self):
        """
        Получение списка доступных активов для торговли
        """
        try:
            url = f"{self.BASE_URL}/info"
            data = {
                "type": "meta"
            }
            
            response = await self.send_request("POST", url, data=data)
            result = await response.json()
            
            if response.status != 200:
                raise Exception(f'Failed to get assets: {result}')
            
            # Извлекаем информацию об активах
            assets = result.get("universe", [])
            asset_names = [asset.get("name", "") for asset in assets]
            
            self.log_message(f"📋 Available assets: {asset_names[:10]}...", level="INFO")
            return asset_names
            
        except Exception as e:
            self.log_message(f"Failed to get available assets: {e}", level="ERROR")
            return []
            
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
        Создание ордера - используем spot торговлю с правильной структурой payload
        """
        try:
            # Проверяем, это spot или futures торговля
            coin = order_data.get("coin", "BTC-SPOT")
            
            if "-SPOT" in coin:
                # Spot торговля - используем правильную структуру payload
                return await self._create_spot_order(order_data)
            else:
                # Futures торговля - используем обычный placeOrder
                return await self._create_futures_order(order_data)
                
        except Exception as e:
            self.log_message(f"Failed to create order: {e}", level="ERROR")
            raise
    

    async def _get_spot_asset_id(self, coin: str) -> int:
        """
        Получить числовой asset ID для spot актива через spotMeta.
        BTC-SPOT -> ищем UBTC/USDC в spotMeta и используем 10000 + index.
        """
        try:
            symbol_base = coin.split("-")[0]  # "BTC-SPOT" -> "BTC"
            
            # Получаем spotMeta для определения правильного asset ID
            url = f"{self.BASE_URL}/info"
            spot_meta_data = {"type": "spotMeta"}
            
            self.log_message(f"🔍 Getting spotMeta for {symbol_base}...", level="DEBUG")
            response = await self.send_request("POST", url, data=spot_meta_data, build_signature=False)
            
            if response.status != 200:
                raise Exception(f"Failed to get spotMeta: {response.status}")
            
            spot_meta = await response.json()
            self.log_message(f"🔍 DEBUG: spotMeta response: {spot_meta}", level="DEBUG")
            
            # Ищем нужный актив в universe
            universe = spot_meta.get("universe", [])
            self.log_message(f"📋 Found {len(universe)} spot assets in universe", level="INFO")
            
            # Ищем BTC или UBTC в spot активах
            target_symbols = [symbol_base, f"U{symbol_base}"]  # BTC и UBTC
            
            for asset in universe:
                asset_name = asset.get("name", "")
                asset_index = asset.get("index", -1)
                
                # Показываем все доступные активы для справки
                self.log_message(f"  📋 Asset: {asset_name} (index: {asset_index})", level="DEBUG")
                
                # Проверяем, подходит ли этот актив
                if asset_name in target_symbols:
                    asset_id = 10000 + asset_index
                    self.log_message(f"✅ Found {symbol_base} as {asset_name} with index {asset_index}, asset_id: {asset_id}", level="INFO")
                    return asset_id
            
            # Если не нашли точное совпадение, попробуем найти по базовому токену
            self.log_message(f"⚠️ Exact match not found for {target_symbols}, searching by base token...", level="WARNING")
            
            for asset in universe:
                asset_name = asset.get("name", "")
                asset_index = asset.get("index", -1)
                
                # Проверяем, содержит ли название наш символ
                if symbol_base in asset_name or asset_name in symbol_base:
                    asset_id = 10000 + asset_index
                    self.log_message(f"✅ Found similar asset {asset_name} with index {asset_index}, asset_id: {asset_id}", level="INFO")
                    return asset_id
            
            # Если ничего не нашли, показываем все доступные активы
            available_assets = [asset.get("name", "") for asset in universe]
            self.log_message(f"❌ Asset {symbol_base} not found in spot universe", level="ERROR")
            self.log_message(f"📋 Available spot assets: {available_assets}", level="INFO")
            
            raise Exception(f"Spot asset {symbol_base} not found. Available: {available_assets}")
            
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

    async def transfer_between_spot_and_perp(self, amount: float, to_perp: bool = True):
        """
        Перевод средств между spot и futures аккаунтами
        """
        try:
            url = f"{self.BASE_URL}/exchange"
            
            # Правильная структура для перевода средств согласно Hyperliquid API
            transfer_payload = {
                "action": {
                    "type": "transferBetweenSpotAndPerp",
                    "amount": str(amount),
                    "toPerp": to_perp
                },
                "nonce": int(time.time() * 1000)
            }
            
            self.log_message(f"🔄 Transferring ${amount} {'to futures' if to_perp else 'to spot'}...", level="INFO")
            self.log_message(f"🔍 DEBUG: Transfer payload: {transfer_payload}", level="DEBUG")
            
            # Проверяем, что у нас есть приватный ключ для подписи
            if not self.private_key:
                raise Exception("Private key is required for transfers")
            
            response = await self.send_request("POST", url, data=transfer_payload, build_signature=True)
            
            if response.status != 200:
                error_text = await response.text()
                self.log_message(f"❌ Transfer failed: {error_text}", level="ERROR")
                raise Exception(f"Transfer failed: {response.status}, {error_text}")
            
            # Проверяем, что ответ содержит валидный JSON
            try:
                result = await response.json()
                self.log_message(f"✅ Transfer successful: {result}", level="INFO")
                return result
            except Exception as json_error:
                # Если не JSON, возвращаем текст ответа
                text_result = await response.text()
                self.log_message(f"✅ Transfer successful (non-JSON): {text_result}", level="INFO")
                return {"status": "success", "message": text_result}
            
        except Exception as e:
            self.log_message(f"Failed to transfer between spot and perp: {e}", level="ERROR")
            raise

    async def _create_spot_order(self, order_data: dict):
        """
        Создание spot ордера через общий endpoint /exchange с числовым asset ID.
        """
        try:
            # Попробуем разные endpoint'ы для spot торговли
            endpoints_to_try = [
                f"{self.BASE_URL}/exchange",  # Основной endpoint
                f"{self.BASE_URL}/spot",     # Возможный spot endpoint
                f"{self.BASE_URL}/orders",  # Возможный orders endpoint
            ]
            
            # Получаем числовой asset ID для spot актива
            coin = order_data.get("coin", "BTC-SPOT")
            asset_id = await self._get_spot_asset_id(coin)
            
            # Форматируем размер до 5 знаков после запятой
            sz_value = float(order_data.get("sz", "0.001"))
            sz_str = f"{sz_value:.5f}"  # строка, напр. "0.00116"
            
            # Форматируем цену до 2 знаков после запятой
            px_value = float(order_data.get("limit_px", 108000))
            px_str = f"{px_value:.2f}"  # строка, напр. "107833.50"
            
            # Используем ту же правильную структуру payload, что и для futures
            # Форматируем числовые поля с правильной точностью
            sz_value = float(order_data.get("sz", "0.001"))
            sz_str = f"{sz_value:.5f}"  # минимум 5 знаков после точки
            
            px_value = float(order_data.get("limit_px", 108000))
            px_str = f"{px_value:.2f}"  # минимум 1-2 знака после точки для цены
            
            payload_variants = [
                # Вариант 1: Правильная структура согласно документации Hyperliquid
                {
                    "action": {
                        "type": "order",
                        "orders": [{
                            "a": asset_id,  # 10000 + spot_index из spotMeta
                            "b": order_data.get("is_buy", True),  # true для покупки
                            "p": px_str,  # цена как строка
                            "s": sz_str,  # размер как строка
                            "r": order_data.get("reduce_only", False),  # reduceOnly
                            "t": {"limit": {"tif": "Gtc"}},  # тип ордера
                            "c": None  # cloid опционально
                        }],
                        "grouping": "na"
                    },
                    "nonce": int(time.time() * 1000)
                },
                # Вариант 2: Market ордер с IOC
                {
                    "action": {
                        "type": "order",
                        "orders": [{
                            "a": asset_id,
                            "b": order_data.get("is_buy", True),
                            "p": px_str,
                            "s": sz_str,
                            "r": order_data.get("reduce_only", False),
                            "t": {"limit": {"tif": "Ioc"}},  # Market ордер через IOC
                            "c": None
                        }],
                        "grouping": "na"
                    },
                    "nonce": int(time.time() * 1000)
                },
                # Вариант 3: Без поля "c" (cloid)
                {
                    "action": {
                        "type": "order",
                        "orders": [{
                            "a": asset_id,
                            "b": order_data.get("is_buy", True),
                            "p": px_str,
                            "s": sz_str,
                            "r": order_data.get("reduce_only", False),
                            "t": {"limit": {"tif": "Gtc"}}
                        }],
                        "grouping": "na"
                    },
                    "nonce": int(time.time() * 1000)
                },
                # Вариант 4: Без поля "grouping"
                {
                    "action": {
                        "type": "order",
                        "orders": [{
                            "a": asset_id,
                            "b": order_data.get("is_buy", True),
                            "p": px_str,
                            "s": sz_str,
                            "r": order_data.get("reduce_only", False),
                            "t": {"limit": {"tif": "Gtc"}},
                            "c": None
                        }]
                    },
                    "nonce": int(time.time() * 1000)
                }
            ]
            
            success = False
            response = None
            
            # Сначала получим информацию о доступных активах для диагностики
            try:
                available_assets = await self.get_available_assets()
                self.log_message(f"🔍 DEBUG: Available assets for reference: {available_assets[:5]}...", level="DEBUG")
            except Exception as e:
                self.log_message(f"⚠️ Failed to get available assets: {e}", level="WARNING")
            
            # Пробуем каждый endpoint с каждым вариантом payload
            for endpoint_url in endpoints_to_try:
                self.log_message(f"🔍 DEBUG: Trying endpoint: {endpoint_url}", level="DEBUG")
                
                for i, order_payload in enumerate(payload_variants, 1):
                    try:
                        self.log_message(f"🔍 DEBUG: Trying spot payload variant {i}: {order_payload}", level="DEBUG")
                        response = await self.send_request("POST", endpoint_url, data=order_payload, build_signature=True)
                        if response.status == 200:
                            success = True
                            self.log_message(f"✅ SUCCESS: Endpoint {endpoint_url} with spot payload variant {i} worked!", level="INFO")
                            break
                        else:
                            self.log_message(f"❌ Endpoint {endpoint_url} spot payload variant {i} failed: {response.status}", level="DEBUG")
                            
                            # Получаем детальную информацию об ошибке
                            try:
                                error_text = await response.text()
                                self.log_message(f"🔍 DEBUG: Error details for {endpoint_url} variant {i}: {error_text}", level="DEBUG")
                            except Exception as e:
                                self.log_message(f"🔍 DEBUG: Failed to get error details for {endpoint_url} variant {i}: {e}", level="DEBUG")
                                
                    except Exception as e:
                        self.log_message(f"❌ Endpoint {endpoint_url} spot payload variant {i} error: {e}", level="DEBUG")
                
                if success:
                    break
            
            if not success:
                error_text = await response.text() if response else "No response"
                raise Exception(f'Failed to create spot order: {response.status if response else "No response"}, message=\'{error_text}\'')
            
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
            
            # ПРАВИЛЬНАЯ структура payload для futures торговли согласно Perplexity
            # Форматируем числовые поля с правильной точностью
            sz_value = float(order_data.get("sz", "0.001"))
            sz_str = f"{sz_value:.5f}"  # минимум 5 знаков после точки для BTC-PERP
            
            px_value = float(order_data.get("limit_px", 108000))
            px_str = f"{px_value:.2f}"  # минимум 1-2 знака после точки для цены
            
            payload_variants = [
                # Вариант 1: Правильная структура для Hyperliquid API v2
                {
                    "action": {
                        "type": "order",
                        "orders": [{
                            "a": order_data.get("coin", "BTC-PERP"),  # Asset name
                            "b": order_data.get("is_buy", True),  # Boolean для покупки/продажи
                            "p": px_str,  # Price как строка
                            "s": sz_str,  # Size как строка
                            "r": order_data.get("reduce_only", False),  # Reduce only
                            "t": {"market": {}}  # Market order
                        }],
                        "grouping": "na"
                    },
                    "nonce": int(time.time() * 1000)
                },
                # Вариант 2: Limit ордер с GTC
                {
                    "action": {
                        "type": "order", 
                        "orders": [{
                            "a": order_data.get("coin", "BTC-PERP"),
                            "b": order_data.get("is_buy", True),
                            "p": px_str,
                            "s": sz_str,
                            "r": order_data.get("reduce_only", False),
                            "t": {"limit": {"tif": "Gtc"}}
                        }],
                        "grouping": "na"
                    },
                    "nonce": int(time.time() * 1000)
                },
                # Вариант 3: Упрощенная структура без grouping
                {
                    "action": {
                        "type": "order",
                        "orders": [{
                            "a": order_data.get("coin", "BTC-PERP"),
                            "b": order_data.get("is_buy", True),
                            "p": px_str,
                            "s": sz_str,
                            "r": order_data.get("reduce_only", False),
                            "t": {"market": {}}
                        }]
                    },
                    "nonce": int(time.time() * 1000)
                },
                # Вариант 4: Альтернативная структура с числовыми значениями
                {
                    "action": {
                        "type": "order",
                        "orders": [{
                            "a": order_data.get("coin", "BTC-PERP"),
                            "b": order_data.get("is_buy", True),
                            "p": float(px_str),  # Числовое значение цены
                            "s": float(sz_str),  # Числовое значение размера
                            "r": order_data.get("reduce_only", False),
                            "t": {"market": {}}
                        }],
                        "grouping": "na"
                    },
                    "nonce": int(time.time() * 1000)
                }
            ]
            
            # Диагностика: проверим баланс и права доступа
            usdc_balance = None  # Инициализируем переменную
            try:
                self.log_message("🔍 DEBUG: Checking account balance and permissions...", level="DEBUG")
                
                # Проверим, есть ли средства на futures аккаунте
                account_info = await self.get_account_info()
                account_value = float(account_info.get('marginSummary', {}).get('accountValue', '0.0'))
                
                if account_value == 0.0:
                    self.log_message("⚠️ WARNING: No funds on futures account. Trying to transfer USDC from spot...", level="WARNING")
                    
                    # Сначала проверим spot баланс
                    spot_balance = await self.get_balance()
                    self.log_message(f"🔍 DEBUG: Spot USDC balance: ${spot_balance}", level="DEBUG")
                    
                    if spot_balance > 0:
                        # Попробуем перевести USDC с spot на futures
                        try:
                            transfer_amount = min(spot_balance * 0.5, 50.0)  # Переводим 50% от баланса или максимум $50
                            transfer_result = await self.transfer_between_spot_and_perp(transfer_amount, True)
                            self.log_message(f"✅ Transfer result: {transfer_result}", level="INFO")
                        except Exception as e:
                            self.log_message(f"❌ Transfer failed: {e}", level="ERROR")
                            self.log_message("💡 SOLUTION: You need to deposit USDC to your futures account or transfer from spot", level="INFO")
                            raise Exception("No funds on futures account and transfer failed")
                    else:
                        self.log_message("❌ CRITICAL: No USDC balance in spot account! You need to deposit USDC first.", level="ERROR")
                        self.log_message("💡 SOLUTION: Transfer USDC to your spot account before trading.", level="INFO")
                        raise Exception("No USDC balance in spot account")
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
                
                # Получаем детальную информацию об ошибке
                if response:
                    try:
                        error_text = await response.text()
                        self.log_message(f"🔍 DEBUG: Last error response: {error_text}", level="DEBUG")
                        
                        # Попробуем получить JSON ошибки
                        try:
                            error_json = await response.json()
                            self.log_message(f"🔍 DEBUG: Error JSON: {error_json}", level="DEBUG")
                        except:
                            pass
                    except Exception as e:
                        self.log_message(f"🔍 DEBUG: Failed to get error details: {e}", level="DEBUG")
                
                # Проверяем, есть ли USDC баланс (используем данные из clearinghouse)
                if usdc_balance is None or usdc_balance == 0:
                    self.log_message("❌ CRITICAL: No USDC balance in spot account! You need to deposit USDC first.", level="ERROR")
                    self.log_message("💡 SOLUTION: Transfer USDC to your spot account before trading.", level="INFO")
                    raise Exception("No USDC balance in spot account")
                else:
                    self.log_message(f"✅ USDC balance found: {usdc_balance}, but trading still failed", level="WARNING")
                    self.log_message("💡 This suggests the issue is with the order structure, not balance", level="INFO")
                    self.log_message("💡 SOLUTION: Check Hyperliquid API documentation for correct payload format", level="INFO")
            
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
            
    async def diagnose_api_structure(self):
        """
        Диагностика структуры API Hyperliquid для понимания правильного формата
        """
        try:
            self.log_message("🔍 Starting API structure diagnosis...", level="INFO")
            
            # Получаем мета-информацию для futures
            try:
                url = f"{self.BASE_URL}/info"
                meta_data = {"type": "meta"}
                response = await self.send_request("POST", url, data=meta_data, build_signature=False)
                
                if response.status == 200:
                    meta_result = await response.json()
                    self.log_message(f"✅ Futures Meta API response: {meta_result}", level="INFO")
                    
                    # Извлекаем информацию об активах
                    universe = meta_result.get("universe", [])
                    self.log_message(f"📋 Futures Universe contains {len(universe)} assets", level="INFO")
                    
                    # Показываем первые несколько активов
                    for i, asset in enumerate(universe[:5]):
                        self.log_message(f"  {i+1}. {asset}", level="INFO")
                        
                else:
                    self.log_message(f"❌ Futures Meta API failed: {response.status}", level="ERROR")
                    
            except Exception as e:
                self.log_message(f"❌ Futures Meta API error: {e}", level="ERROR")
            
            # Получаем spotMeta для spot торговли
            try:
                url = f"{self.BASE_URL}/info"
                spot_meta_data = {"type": "spotMeta"}
                response = await self.send_request("POST", url, data=spot_meta_data, build_signature=False)
                
                if response.status == 200:
                    spot_meta_result = await response.json()
                    self.log_message(f"✅ Spot Meta API response: {spot_meta_result}", level="INFO")
                    
                    # Извлекаем информацию о spot активах
                    spot_universe = spot_meta_result.get("universe", [])
                    self.log_message(f"📋 Spot Universe contains {len(spot_universe)} assets", level="INFO")
                    
                    # Показываем все spot активы с их индексами
                    for i, asset in enumerate(spot_universe):
                        asset_name = asset.get("name", "")
                        asset_index = asset.get("index", -1)
                        asset_id = 10000 + asset_index
                        self.log_message(f"  {i+1}. {asset_name} (index: {asset_index}, asset_id: {asset_id})", level="INFO")
                        
                else:
                    self.log_message(f"❌ Spot Meta API failed: {response.status}", level="ERROR")
                    
            except Exception as e:
                self.log_message(f"❌ Spot Meta API error: {e}", level="ERROR")
            
            # Получаем информацию о ценах
            try:
                url = f"{self.BASE_URL}/info"
                price_data = {"type": "allMids"}
                response = await self.send_request("POST", url, data=price_data, build_signature=False)
                
                if response.status == 200:
                    price_result = await response.json()
                    price_keys = list(price_result.keys())
                    self.log_message(f"📊 Price API contains {len(price_keys)} assets", level="INFO")
                    self.log_message(f"📊 Sample price keys: {price_keys[:10]}", level="INFO")
                    
                else:
                    self.log_message(f"❌ Price API failed: {response.status}", level="ERROR")
                    
            except Exception as e:
                self.log_message(f"❌ Price API error: {e}", level="ERROR")
            
            # Тестируем различные endpoint'ы
            test_endpoints = [
                f"{self.BASE_URL}/exchange",
                f"{self.BASE_URL}/spot", 
                f"{self.BASE_URL}/orders",
                f"{self.BASE_URL}/trade",
                f"{self.BASE_URL}/api/v1/orders"
            ]
            
            for endpoint in test_endpoints:
                try:
                    # Простой GET запрос для проверки доступности
                    response = await self.send_request("GET", endpoint)
                    self.log_message(f"🔍 Endpoint {endpoint}: {response.status}", level="INFO")
                except Exception as e:
                    self.log_message(f"🔍 Endpoint {endpoint}: Error - {e}", level="DEBUG")
            
            self.log_message("✅ API diagnosis completed", level="INFO")
            
        except Exception as e:
            self.log_message(f"❌ API diagnosis failed: {e}", level="ERROR")

    def log_message(self, message: str, level: str = "INFO"):
        """
        Логирование сообщений
        """
        logger.log(level, f'[{self.label}] {message}')
