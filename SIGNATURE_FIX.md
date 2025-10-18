# Исправление EIP-712 подписи для Hyperliquid API

## Проблема
API возвращал ошибку "User or API Wallet 0x... does not exist" с **разными адресами каждый раз**, что указывало на неправильную структуру EIP-712 подписи.

## Основные исправления

### 1. Убрали `nonce` из структуры `Agent`
**Было (неправильно):**
```python
"Agent": [
    {"name": "source", "type": "string"},
    {"name": "connectionId", "type": "bytes32"},
    {"name": "nonce", "type": "uint256"}  # ❌ ЭТО НЕПРАВИЛЬНО!
]
```

**Стало (правильно):**
```python
"Agent": [
    {"name": "source", "type": "string"},
    {"name": "connectionId", "type": "bytes32"}
    # ✅ nonce НЕ включается в Agent согласно официальной документации
]
```

### 2. Исправили формат `connectionId`
**Было (первая попытка):**
```python
msg["message"] = {
    "source": "a",
    "connectionId": "0x" + action_hash.hex()  # ❌ hex string не работает с eth_account
    "nonce": nonce                             # ❌ nonce в message
}
```

**Стало (правильно):**
```python
msg["message"] = {
    "source": "a",
    "connectionId": action_hash  # ✅ сырые байты для bytes32
    # ✅ nonce НЕ включается в message
}
```

## Почему это важно

Согласно официальному контракту Hyperliquid `Signature.sol`:
```solidity
struct Agent {
    string source;
    bytes32 connectionId;
}
```

**НЕТ поля `nonce`!** Nonce используется только для создания `action_hash`, но не включается в структуру `Agent` для EIP-712 подписи.

## Процесс подписи (исправленный)

1. **Нормализация action** - убираем trailing zeros из чисел
2. **Сериализация в msgpack** - `msgpack.packb(normalized_action)`
3. **Создание буфера** - `msgpack_bytes + nonce_bytes + vault_flag`
4. **Хеширование** - `keccak256(buffer)` → `action_hash`
5. **EIP-712 подпись** - подписываем только `source` и `connectionId` (БЕЗ nonce)

## Тестирование

Запустите `test_signature.py` для проверки исправленной подписи:

```bash
python test_signature.py
```

## Ожидаемый результат

После исправлений API должен:
- ✅ Принимать подпись без ошибок
- ✅ Восстанавливать правильный адрес кошелька
- ✅ Успешно создавать ордера

## Дополнительные проверки

Если проблема остается:

1. **Проверьте приватный ключ** - убедитесь что он соответствует адресу кошелька
2. **Проверьте chainId** - должен быть `1337` для Hyperliquid mainnet
3. **Проверьте порядок полей** - в msgpack важен порядок: `a, b, p, s, r, t`
4. **Проверьте нормализацию чисел** - убирайте trailing zeros правильно

## Ссылки

- [Официальная документация Hyperliquid по подписи](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/signing)
- [Контракт Signature.sol](https://github.com/hyperliquid-dex/contracts/blob/master/Signature.sol)
- [Unofficial TypeScript SDK](https://github.com/nktkas/hyperliquid)
