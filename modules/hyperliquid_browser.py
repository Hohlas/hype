import aiohttp
import asyncio
from decimal import Decimal
from typing import Dict, List, Optional
import json
import time
import hmac
import hashlib
import struct
import msgpack
from eth_account import Account
from eth_account.messages import encode_defunct, encode_structured_data
from eth_account.signers.local import LocalAccount
from eth_utils import keccak
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
        
        # Trading parameters
        self.btc_tick_size = 0.5  # Default tick size for BTC
        
        # Web3 setup
        self.w3 = web3.Web3(web3.HTTPProvider(settings.WEB3_RPC_URL))
        
            # Инициализация аккаунта
        if private_key:
            # Убедимся что ключ в правильном формате (без 0x или с 0x)
            if private_key.startswith('0x'):
                clean_key = private_key[2:]  # Убираем 0x
            else:
                clean_key = private_key
                
            # Создаем и сохраняем аккаунт
            try:
                self.account = Account.from_key(clean_key)
                
                # Проверка совпадения адресов
                if self.account.address.lower() != wallet_address.lower():
                    self.log_message(f"❌ MISMATCH: Account address {self.account.address} != Expected {wallet_address}", level="ERROR")
                    self.log_message(f"❌ This means the private key doesn't match the wallet address!", level="ERROR")
                else:
                    self.log_message(f"✅ Wallet initialized: {self.account.address}", level="INFO")
            except Exception as e:
                self.log_message(f"❌ Failed to create account: {str(e)}", level="ERROR")
                raise
        else:
            self.log_message(f"❌ No private key provided!", level="ERROR")
            self.account = None
        
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
            
    def float_to_wire(self, x: float, is_price: bool = False) -> str:
        """Конвертирует float в строку с правильным форматированием"""
        if is_price:
            # Для цен округляем до размера тика
            if self.btc_tick_size:
                x = round(x / self.btc_tick_size) * self.btc_tick_size
            
            # Для BTC-SPOT принудительно округляем до целых чисел
            # (API может требовать только целые числа для некоторых активов)
            x = round(x)
            return str(int(x))
        else:
            # Для остальных чисел (размер ордера и т.д.) используем прежнюю логику
            rounded = "{:.8f}".format(x)
            # Убираем trailing zeros, но оставляем хотя бы один знак после точки
            if '.' in rounded:
                rounded = rounded.rstrip('0')
                if rounded.endswith('.'):
                    rounded = rounded + '0'
        return rounded

    def sign_l1_action(self, action: dict, nonce: int, is_mainnet: bool = True) -> dict:
        """Подписывает action для Hyperliquid L1 с EIP-712"""
        try:
            # Проверим наличие аккаунта
            if not self.account:
                raise Exception("No account available for signing")
                
            # Проверка соответствия адреса
            if self.wallet_address.lower() != self.account.address.lower():
                raise Exception(f"Wallet address mismatch: Expected {self.wallet_address}, got {self.account.address}")
            
            def normalize_number(n):
                if isinstance(n, str):
                    if '.' in n:
                        return n.rstrip('0').rstrip('.')
                    return n
                elif isinstance(n, (int, float)):
                    return normalize_number(str(n))
                return n

            def normalize_action(obj):
                if isinstance(obj, dict):
                    if all(k in obj for k in ['a', 'b', 'p', 's', 'r', 't']):
                        return {
                            'a': int(obj['a']),
                            'b': bool(obj['b']),
                            'p': normalize_number(obj['p']),
                            's': normalize_number(obj['s']),
                            'r': bool(obj['r']),
                            't': normalize_action(obj['t'])
                        }
                    return {k: normalize_action(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [normalize_action(x) for x in obj]
                elif isinstance(obj, (int, float)):
                    return normalize_number(obj)
                return normalize_number(obj) if isinstance(obj, str) else obj

            # Нормализация и кодирование
            normalized_action = normalize_action(action)
            action_bytes = msgpack.packb(normalized_action, use_bin_type=True)
            
            # Создание буфера данных
            total_size = len(action_bytes) + 9
            data = bytearray(total_size)
            data[:len(action_bytes)] = action_bytes
            nonce_bytes = nonce.to_bytes(8, byteorder='big', signed=False)
            data[len(action_bytes):len(action_bytes) + 8] = nonce_bytes
            data[-1] = 0
            
            # Хеширование данных
            action_hash = keccak(bytes(data))
            
            # Логирование только при ошибках
            
            # Базовая структура EIP-712 (БЕЗ nonce в Agent согласно официальной документации)
            eip712_data = {
                "domain": {
                    "chainId": 1337,
                    "name": "Exchange",
                    "verifyingContract": "0x0000000000000000000000000000000000000000",
                    "version": "1"
                },
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"}
                    ],
                    "Agent": [
                        {"name": "source", "type": "string"},
                        {"name": "connectionId", "type": "bytes32"}
                    ]
                },
                "primaryType": "Agent"
            }
            
            success = False
            last_error = None
            signature = None
            
            # Правильная подпись БЕЗ nonce в Agent (согласно официальной документации)
            try:
                msg = eip712_data.copy()
                msg["message"] = {
                    "source": "a",
                    "connectionId": action_hash  # сырые байты для bytes32
                }
                encoded = encode_structured_data(msg)
                signature = self.account.sign_message(encoded)
                success = True
            except Exception as e:
                last_error = e
                self.log_message(f"❌ EIP-712 подпись failed: {e}", level="ERROR")

            if not success:
                raise Exception(f"EIP-712 подпись failed. Ошибка: {last_error}")

            # Возвращаем подпись в нужном формате
            return {
                "r": "0x" + signature.r.to_bytes(32, "big").hex(),
                "s": "0x" + signature.s.to_bytes(32, "big").hex(),
                "v": signature.v
            }
            
        except Exception as e:
            self.log_message(f"Failed to sign L1 action: {e}", level="ERROR")
            raise

    async def _sign_message(self, message: dict) -> str:
        """
        Подписание сообщения приватным ключом кошелька (legacy метод)
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
            
    async def get_available_balance(self):
        """
        Получение доступного баланса USDC с учетом зарезервированных средств в открытых ордерах
        """
        try:
            # Получаем общий баланс
            total_balance = await self.get_balance()
            
            # Получаем открытые ордера
            orders = await self.get_account_orders()
            
            # Рассчитываем зарезервированные средства для BUY ордеров
            reserved_usdc = 0.0
            
            for order in orders:
                coin = order.get("coin", "")
                is_buy = order.get("isBuy", False)
                size = float(order.get("sz", 0))
                price = float(order.get("limitPx", 0))
                
                # Для BUY ордеров резервируются USDC
                if is_buy and price > 0:
                    reserved_usdc += size * price
            
            # Доступный баланс = общий баланс - зарезервированные средства
            available_balance = total_balance - reserved_usdc
            
            # Логируем детали для отладки
            self.log_message(
                f"💰 Balance check: Total=${total_balance:.2f}, Reserved=${reserved_usdc:.2f}, Available=${available_balance:.2f}",
                level="DEBUG"
            )
            
            return max(0.0, available_balance)  # Не возвращаем отрицательные значения
            
        except Exception as e:
            self.log_message(f"Failed to get available balance: {e}", level="ERROR")
            return await self.get_balance()  # Fallback к обычному балансу
            
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
                    # Цена найдена
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
            
            # Получаем spotMeta
            response = await self.send_request("POST", url, data=spot_meta_data, build_signature=False)
            
            if response.status != 200:
                raise Exception(f"Failed to get spotMeta: {response.status}")
            
            spot_meta = await response.json()
            # Ищем нужный актив в universe
            universe = spot_meta.get("universe", [])
            
            # Ищем BTC или UBTC в spot активах через tokens
            target_symbols = [symbol_base, f"U{symbol_base}"]  # BTC и UBTC
            
            # Получаем список токенов для поиска
            tokens = spot_meta.get("tokens", [])
            
            # Ищем токен BTC в списке токенов и его параметры
            btc_token_index = None
            btc_tick_size = None
            for token in tokens:
                token_name = token.get("name", "")
                token_index = token.get("index", -1)
                
                if token_name in target_symbols:
                    btc_token_index = token_index
                    # Получаем размер тика для токена
                    meta = token.get("meta", {})
                    if isinstance(meta, dict):
                        self.btc_tick_size = float(meta.get("tick", "0.5"))
                    self.log_message(f"✅ Found {symbol_base} token: {token_name} (tick: {self.btc_tick_size})", level="INFO")
                    break
            
            if btc_token_index is None:
                # Показываем первые несколько токенов для справки
                self.log_message(f"📋 First 10 tokens: {[t.get('name', '') for t in tokens[:10]]}", level="INFO")
                
                # Попробуем найти BTC в полном списке токенов
                for i, token in enumerate(tokens):
                    token_name = token.get("name", "")
                    if "BTC" in token_name.upper() or token_name.upper() == "BTC":
                        btc_token_index = token.get("index", -1)
                        self.log_message(f"✅ Found BTC token: {token_name}", level="INFO")
                        break
                
                if btc_token_index is None:
                    raise Exception(f"Token {symbol_base} not found in tokens list")
            
            # Теперь ищем spot актив, который использует этот токен
            for asset in universe:
                asset_name = asset.get("name", "")
                asset_index = asset.get("index", -1)
                asset_tokens = asset.get("tokens", [])
                
                # Проверяем, содержит ли этот актив наш токен
                if btc_token_index in asset_tokens:
                    asset_id = 10000 + asset_index
                    self.log_message(f"✅ Found {symbol_base} spot asset: {asset_name} (asset_id: {asset_id})", level="INFO")
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
            # Диагностика кошелька (убрано  для чистоты логов)
            
            # Получаем числовой asset ID для spot актива
            coin = order_data.get("coin", "BTC-SPOT")
            asset_id = await self._get_spot_asset_id(coin)
            
            # Make sure wallet address matches
            if self.wallet_address.lower() != self.account.address.lower():
                raise Exception(f"Wallet address mismatch: Expected {self.wallet_address}, got {self.account.address}")
                
            # Using only the main exchange endpoint for spot trading
            endpoint_url = f"{self.BASE_URL}/exchange"
            
            # Форматируем размер до 5 знаков после запятой
            sz_value = float(order_data.get("sz", "0.001"))
            sz_str = f"{sz_value:.5f}"  # строка, напр. "0.00116"
            
            # Форматируем числовые поля с правильной точностью
            sz_value = float(order_data.get("sz", "0.001"))
            sz_str = f"{sz_value:.5f}"  # минимум 5 знаков после точки
            
            px_value = float(order_data.get("limit_px", 108000))
            px_str = self.float_to_wire(px_value, is_price=True)  # правильное округление до тика
            
            # Отладочная информация для проверки округления
            self.log_message(f"🔧 Price formatting: {px_value} -> {px_str} (tick_size: {self.btc_tick_size})", level="DEBUG")
            
            # Создаем правильную структуру ордера с EIP-712 подписью
            order = {
                "a": asset_id,  # asset ID из spotMeta
                "b": order_data.get("is_buy", True),  # true для покупки
                "p": px_str,  # цена уже отформатирована
                "s": self.float_to_wire(float(sz_str)),  # размер с правильным форматированием
                "r": order_data.get("reduce_only", False),  # reduceOnly
                "t": {"limit": {"tif": "Gtc"}}  # тип ордера
            }
            
            # Формируем action
            action = {
                "type": "order",
                "orders": [order],
                "grouping": "na"
            }
            
            # Создаем nonce
            nonce = int(time.time() * 1000)
            
            # Подписываем action с EIP-712
            signature = self.sign_l1_action(action, nonce, is_mainnet=True)
            
             # Формируем финальный payload
            payload = {
                "action": action,
                "nonce": nonce,
                "signature": signature,
                "vaultAddress": None  # Для основного кошелька используем None
            }
            
            # Логируем только при ошибках
            
            payload_variants = [payload]  # Теперь у нас только один правильный вариант
            
            success = False
            response = None
            
            # Получаем информацию о доступных активах только при ошибках
            
            # Отправляем правильно подписанный payload
            endpoint_url = f"{self.BASE_URL}/exchange"  # Используем только основной endpoint
            # Отправляем подписанный payload
            # Payload подготовлен для отправки
            
            try:
                # Специальные заголовки для API
                headers = {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'User-Agent': 'HyperliquidBot/1.0'
                }
                
                response = await self.send_request("POST", endpoint_url, data=payload, build_signature=False)
                
                # Читаем ответ только при ошибках
                response_text = await response.text()
                
                try:
                    result = json.loads(response_text)
                except json.JSONDecodeError:
                    # Если не JSON, используем текст как есть
                    result = {"status": "err", "response": response_text}
                
                if response.status == 200:
                    # Проверяем не только внешний status, но и содержимое
                    if result.get('status') == 'ok':
                        response_data = result.get('response', {})
                        statuses = response_data.get('data', {}).get('statuses', [])
                        
                        # Проверяем есть ли ошибки в statuses
                        has_errors = any('error' in status for status in statuses)
                        
                        if not has_errors:
                            self.log_message(f"✅ SUCCESS: Spot order created successfully!", level="INFO")
                            return result
                        else:
                            # Показываем ошибки из statuses
                            for status in statuses:
                                if 'error' in status:
                                    self.log_message(f"❌ Order error: {status['error']}", level="ERROR")
                            raise Exception(f"Order rejected: {[status.get('error') for status in statuses if 'error' in status]}")
                    else:
                        error_msg = result.get('response') if isinstance(result, dict) else str(result)
                        error_text = f"API Status: {result.get('status')}, Error: {error_msg}"
                        self.log_message(f"❌ Spot order failed: {error_text}", level="ERROR")
                        raise Exception(f'Failed to create spot order: {error_text}')
                else:
                    error_msg = result.get('response') if isinstance(result, dict) else str(result)
                    error_text = f"HTTP Status: {response.status}, Error: {error_msg}"
                    self.log_message(f"❌ Spot order failed: {error_text}", level="ERROR")
                    raise Exception(f'Failed to create spot order: {error_text}')
                        
            except Exception as e:
                self.log_message(f"❌ Spot order error: {str(e)}", level="ERROR")
                raise

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
            
            # Double check wallet address before preparing payload
            if self.wallet_address.lower() != self.account.address.lower():
                raise Exception(f"Wallet address mismatch: Expected {self.wallet_address}, got {self.account.address}")
            
            # ПРАВИЛЬНАЯ структура payload для futures торговли
            # Форматируем числовые поля с правильной точностью
            sz_value = float(order_data.get("sz", "0.001"))
            sz_str = f"{sz_value:.5f}"  # минимум 5 знаков после точки
            
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
            
            # Пытаемся парсить JSON и проверить статус
            try:
                result = await response.json()
                
                # Проверяем статус ответа для futures ордеров
                if response.status == 200:
                    if result.get('status') == 'ok':
                        response_data = result.get('response', {})
                        statuses = response_data.get('data', {}).get('statuses', [])
                        
                        # Проверяем есть ли ошибки в statuses
                        has_errors = any('error' in status for status in statuses)
                        
                        if not has_errors:
                            return result
                        else:
                            # Показываем ошибки из statuses
                            for status in statuses:
                                if 'error' in status:
                                    self.log_message(f"❌ Futures order error: {status['error']}", level="ERROR")
                            raise Exception(f"Futures order rejected: {[status.get('error') for status in statuses if 'error' in status]}")
                    else:
                        error_msg = result.get('response') if isinstance(result, dict) else str(result)
                        raise Exception(f'Futures order failed: API Status: {result.get("status")}, Error: {error_msg}')
                else:
                    error_msg = result.get('response') if isinstance(result, dict) else str(result)
                    raise Exception(f'Futures order failed: HTTP Status: {response.status}, Error: {error_msg}')
                    
            except Exception as json_error:
                # Если не JSON, возвращаем текст
                text_result = await response.text()
                self.log_message(f"❌ Non-JSON response: {text_result}", level="ERROR")
                raise Exception(f"Invalid response format: {text_result}")
            
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
