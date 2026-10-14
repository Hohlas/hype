from os import name as os_name
from loguru import logger
from time import sleep
import asyncio
import sys

from modules.retry import DataBaseError
from modules.utils import async_sleep, send_warning_notification
from modules.utils.modes import choose_mode
from modules import *
import settings

# Настройка упрощенного формата логов
logger.remove()  # Удаляем стандартный обработчик
logger.add(
    sys.stdout,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}",
    level=settings.LOG_LEVEL
)
logger.add(
    settings.LOG_FILE,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}",
    level=settings.LOG_LEVEL,
    rotation=settings.LOG_ROTATION,
    retention=settings.LOG_RETENTION,
    encoding="utf-8",
)


def initialize_account(module_data: dict, group_data: dict = None):
    browser = HyperliquidBrowser(
        proxy=module_data["proxy"],
        wallet_address=module_data["wallet_address"],
        private_key=module_data["private_key"],
        label=module_data["label"],
        db=db,
    )
    hyperliquid_client = HyperliquidClient(
        browser=browser,
        wallet_address=module_data["wallet_address"],
        private_key=module_data["private_key"],
        label=module_data["label"],
        proxy=module_data["proxy"],
        db=db,
        group_data=group_data,
    )

    return hyperliquid_client


async def run_modules(mode: int, module_data: dict, sem: asyncio.Semaphore):
    # Инициализируем блокировку для адреса кошелька
    wallet_address = module_data["wallet_address"]
    if wallet_address not in address_locks:
        address_locks[wallet_address] = asyncio.Lock()
    
    async with address_locks[wallet_address]:
        async with sem:
            hyperliquid_client = None
            try:
                logger.info(f'[•] Starting run_modules for {module_data["label"]}')
                hyperliquid_client = initialize_account(module_data)
                logger.opt(colors=True).info(f'[•] <white>{hyperliquid_client.label}</white> | Starting trading...')
                module_data["module_info"]["status"] = await hyperliquid_client.run_mode(mode=mode)

            except Exception as err:
                if hyperliquid_client:
                    logger.error(f'[-] {hyperliquid_client.label} | Account Error: {err}')

                    # Отправляем уведомление об ошибке аккаунта
                    await send_warning_notification(
                        error_type="Account Error",
                        error_message=str(err),
                        account_label=hyperliquid_client.label
                    )
                else:
                    logger.error(f'[-] {module_data["label"]} | Global error: {err}')
                    
                    # Отправляем уведомление о глобальной ошибке
                    await send_warning_notification(
                        error_type="Global Error",
                        error_message=str(err),
                        account_label=module_data.get("label", "Unknown")
                    )

            finally:
                # hyperliquid_client is None, если initialize_account упал
                # (битый ключ/прокси): атрибутов клиента нет — только очередь.
                if hyperliquid_client:
                    await hyperliquid_client.browser.close_sessions()

                if mode in [0, 1, 2]:
                    await db.remove_module(module_data=module_data)
                else:
                    await db.remove_account(module_data=module_data)

                await async_sleep(5)


async def get_account_startup_info(module_data: dict):
    """
    Получает информацию об аккаунте для стартового сообщения
    """
    client = None
    try:
        client = initialize_account(module_data)
        await client.browser.start_session()
        
        # Получаем данные параллельно
        all_balances, orders, btc_price = await asyncio.gather(
            client.browser.get_all_balances(),
            client.browser.get_account_orders(),
            client.browser.get_token_price("BTC")
        )
        
        # Извлекаем балансы
        usdc_balance = all_balances.get("USDC", 0.0)
        btc_balance = all_balances.get("UBTC", 0.0) or all_balances.get("BTC", 0.0)
        
        # Рассчитываем Total Value
        total_value = usdc_balance + (btc_balance * float(btc_price))
        
        # Рассчитываем Limit Profit (сумма Order Value всех лимитных ордеров на продажу)
        limit_profit = 0.0
        for order in orders:
            side = order.get("side", "")
            is_buy = order.get("isBuy", True)
            limit_price = float(order.get("limitPx", 0))
            
            # Ордер на продажу: side in ["SELL", "S", "A"] или isBuy == False
            is_sell = side in ["SELL", "S", "A"] or (not is_buy)
            
            # Проверяем, что это лимитный ордер (есть limitPx > 0)
            is_limit = limit_price > 0
            
            if is_sell and is_limit:
                size = float(order.get("sz", 0))
                if size > 0:
                    limit_profit += size * limit_price
        
        return {
            "label": module_data["label"],
            "usdc_balance": usdc_balance,
            "btc_balance": btc_balance,
            "total_value": total_value,
            "limit_profit": limit_profit
        }
    except Exception as e:
        logger.error(f"Failed to get startup info for {module_data.get('label', 'Unknown')}: {e}")
        return None
    finally:
        # Сессию закрываем и при исключении (иначе утечка aiohttp-сессии)
        if client is not None:
            try:
                await client.browser.close_sessions()
            except Exception:
                pass


async def runner(mode: int):
    sem = asyncio.Semaphore(settings.THREADS)

    all_modules = db.get_all_modules(unique_wallets=mode in [4, 5])

    if all_modules == 'No more accounts left':
        logger.warning("No accounts found in wallets.txt or wallet_loader returned empty list")
        logger.info("Please check input_data/wallets.txt file - it should contain wallet data in format: label|wallet_address|private_key|proxy")
    elif not all_modules:
        logger.warning("get_all_modules returned empty list or None")
    else:
        # Подсчитываем уникальные аккаунты
        unique_accounts = list(set([m['label'] for m in all_modules]))
        logger.opt(colors=True).info(
            f'[•] Loaded <white>{len(unique_accounts)}</white> account(s): '
            f'<white>{", ".join(unique_accounts)}</white> | '
            f'Total modules: <white>{len(all_modules)}</white>'
        )

        # Получаем информацию о стартовом состоянии аккаунтов
        account_infos = []
        for module_data in all_modules:
            info = await get_account_startup_info(module_data)
            if info:
                account_infos.append(info)

        # Формируем стартовое сообщение
        message_lines = [f"🚀 <b>Hyperliquid Bot Started</b>, v.{settings.version}", f"Risk: {settings.POSITION_SIZE_PERCENT}%, STEP: ${settings.STEP}"]
        log_lines = [f"🚀 Hyperliquid Bot Started, v.{settings.version}", f"Risk: {settings.POSITION_SIZE_PERCENT}%, STEP: ${settings.STEP}"]

        for info in account_infos:
            account_line = f"{info['label']}: {info['usdc_balance']:.0f}USDC + {info['btc_balance']:.2f}BTC = ${info['total_value']:.0f}, SellValue = ${info['limit_profit']:.0f}"
            message_lines.append(account_line)
            log_lines.append(account_line)

        # Отправляем сообщение в Telegram
        await TgReport().send_log(logs="\n".join(message_lines))

        # Выводим в логах
        for line in log_lines:
            logger.info(line)

        # return_exceptions: один упавший аккаунт не роняет остальные
        results = await asyncio.gather(*[
            run_modules(module_data=module_data, mode=mode, sem=sem)
            for module_data in all_modules
        ], return_exceptions=True)
        for module_data, result in zip(all_modules, results):
            if isinstance(result, Exception):
                logger.error(f'[-] {module_data.get("label", "?")} | Unhandled error: {result!r}')

    logger.success(f'All accounts done.')
    return 'Ended'


def run_ci() -> int:
    """Неинтерактивный запуск для GitHub Actions (§9.п1).

    Режим — из BOT_MODE, ни одного input(). Возвращает код выхода:
    0 — отработал, 1 — быстрый отказ (секреты/база), 130 — прерывание.
    """
    from modules.ci_loader import RUNTIME_GITHUB_ACTIONS  # noqa: F401 (сигнал читателю)
    from modules.utils.modes import get_mode_from_env

    try:
        mode = get_mode_from_env()
    except ValueError as e:
        logger.error(f'[-] CI | {e}')
        return 2

    # Сбрасываем stop-флаг стратегии при запуске (прошлый запуск мог
    # завершиться по Ctrl+C с выставленным флагом)
    try:
        from modules.delayed_orders_strategy import set_bot_stop_flag
        set_bot_stop_flag(False)
        logger.info('[•] Bot stop flag reset on startup')
    except Exception as e:
        logger.warning(f'Failed to reset stop flag: {e}')

    mode_names = {
        0: "Averaging Market",
        4: "Cancel Orders",
        5: "Parse"
    }
    mode_name = mode_names.get(mode.soft_id, f"Mode {mode.soft_id}")
    logger.success(f'[•] Bot started in CI mode: {mode_name}')

    global db
    try:
        db = DataBase()
        result = asyncio.run(runner(mode=mode.soft_id))
    except (DataBaseError, RuntimeError) as e:
        logger.error(f'[-] CI | {e}')
        return 1
    except KeyboardInterrupt:
        logger.info('[•] CI | Interrupted, setting stop flag')
        try:
            from modules.delayed_orders_strategy import set_bot_stop_flag
            set_bot_stop_flag(True)
        except Exception as e:
            logger.error(f'Failed to set stop flag: {e}')
        return 130
    except Exception as e:
        logger.error(f'[-] CI Global | {e}')
        return 1

    logger.info('[•] Soft | Closed')
    return 0 if result == 'Ended' else 1


def run_vps_interactive() -> None:
    """Прежний интерактивный цикл (VPS/локально): меню + паузы input()."""
    manual_stop = False

    # Сбрасываем stop-флаг стратегии при запуске (прошлый запуск мог
    # завершиться по Ctrl+C с выставленным флагом)
    try:
        from modules.delayed_orders_strategy import set_bot_stop_flag
        set_bot_stop_flag(False)
        logger.info('[•] Bot stop flag reset on startup')
    except Exception as e:
        logger.warning(f'Failed to reset stop flag: {e}')

    try:
        global db
        db = DataBase()

        while True:
            mode = choose_mode()

            match mode.type:
                case "database":
                    db.create_modules(mode=mode.soft_id)

                case "module":
                    # Отправляем уведомление о старте
                    mode_names = {
                        0: "Averaging Market",
                        4: "Cancel Orders",
                        5: "Parse"
                    }
                    mode_name = mode_names.get(mode.soft_id, f"Mode {mode.soft_id}")

                    logger.success(f'[•] Bot started in mode: {mode_name}')

                    if asyncio.run(runner(mode=mode.soft_id)) == 'Ended': break
                    print('')

                case "exit":
                    # Выход из программы
                    logger.info(f'[•] Bot | Exiting...')
                    break

                case _:
                    # Неизвестный режим
                    logger.warning(f'[•] Unknown mode type: {mode.type}')
                    continue

        sleep(0.1)
        input('\n > Exit\n')

    except DataBaseError as e:
        logger.error(f'[-] Database | {e}')
        input('\n > Exit\n')

    except KeyboardInterrupt:
        logger.info(f'[•] Soft | Closed')
        manual_stop = True

        # Флаг остановки: циклы стратегии завершатся сами, фантомных
        # уведомлений/ордеров от умирающего процесса не будет
        try:
            from modules.delayed_orders_strategy import set_bot_stop_flag
            set_bot_stop_flag(True)
            logger.info('[•] Stop flag set to prevent phantom processes')
        except Exception as e:
            logger.error(f'Failed to set stop flag: {e}')

    except Exception as e:
        logger.error(f'[-] Global | {e}')
        input('\n > Exit\n')

    finally:
        if not manual_stop:
            logger.info(f'[•] Soft | Closed')


if __name__ == '__main__':
    if os_name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from modules.ci_loader import get_runtime_mode, RUNTIME_GITHUB_ACTIONS

    if get_runtime_mode() == RUNTIME_GITHUB_ACTIONS:
        raise SystemExit(run_ci())
    else:
        run_vps_interactive()
