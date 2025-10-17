# Инструкция по настройке Hyperliquid Bot

## 🔧 Обязательные настройки

### 1. Telegram Bot Token
1. Откройте Telegram и найдите @BotFather
2. Отправьте команду `/newbot`
3. Следуйте инструкциям для создания бота
4. Скопируйте полученный токен в `TG_BOT_TOKEN`

### 2. Telegram Chat  ID
1. Найдите @userinfobot в Telegram
2. Отправьте любое сообщение
3. Скопируйте ваш ID в `TG_CHAT_ID`

### 3. Infura Key (ОБЯЗАТЕЛЬНО!)
1. Перейдите на https://infura.io/
2. Зарегистрируйтесь (бесплатно)
3. Создайте новый проект
4. Выберите сеть "Ethereum Mainnet"
5. Скопируйте Project ID в `WEB3_RPC_URL`

Пример:
```
WEB3_RPC_URL = "https://mainnet.infura.io/v3/abc123def456ghi789"
```

## 📊 Опциональные настройки

### Database Settings (можно пропустить)
Если не планируете использовать базу данных PostgreSQL, оставьте настройки как есть.
Бот будет работать без базы данных, но не будет сохранять историю сделок.

### Proxy Settings
Если используете прокси, установите:
```python
USE_PROXY = True
PROXY_LIST = ["http://user:pass@ip:port"]
```

## 🚀 Быстрый старт

1. **Настройте settings.py:**
   - Замените `YOUR_TELEGRAM_BOT_TOKEN` на токен от @BotFather
   - Замените `YOUR_TELEGRAM_CHAT_ID` на ваш ID
   - Замените `YOUR_INFURA_KEY` на ключ от Infura

2. **Настройте кошельки в input_data/wallets.txt:**
   ```
   Acc1|0x1234...|0xabcd...|http://proxy:port
   ```

3. **Запустите бота:**
   ```bash
   python main.py
   ```

## ⚠️ Важные замечания

- **Никогда не делитесь приватными ключами!**
- **Используйте отдельный кошелек для торговли**
- **Начните с небольших сумм для тестирования**
- **Проверьте баланс кошелька перед запуском**

## 🔗 Полезные ссылки

- [Infura](https://infura.io/) - RPC провайдер
- [Alchemy](https://www.alchemy.com/) - альтернативный RPC провайдер
- [Hyperliquid](https://app.hyperliquid.xyz/) - торговая платформа
- [Arbitrum](https://arbitrum.io/) - сеть Hyperliquid
