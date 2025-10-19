from os import name as os_name
from random import randint
from loguru import logger
from time import sleep
import asyncio

from modules.retry import DataBaseError
from modules.utils import async_sleep, send_warning_notification
from modules.utils.modes import choose_mode
from modules import *
import settings


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
                logger.info(f'[•] DEBUG: Starting run_modules for {module_data["label"]}')
                hyperliquid_client = initialize_account(module_data)
                logger.opt(colors=True).info(f'[•] <white>{hyperliquid_client.label}</white> | Starting trading...')
                module_data["module_info"]["status"] = await hyperliquid_client.run_mode(mode=mode)

            except Exception as err:
                if hyperliquid_client:
                    logger.error(f'[-] {hyperliquid_client.label} | Account Error: {err}')
                    await db.append_report(key=hyperliquid_client.wallet_address, text=str(err), success=False)
                    
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
                if hyperliquid_client:
                    await hyperliquid_client.browser.close_sessions()

                if type(module_data) == dict:
                    if mode in [0, 1, 2]:
                        last_module = await db.remove_module(module_data=module_data)
                    else:
                        last_module = await db.remove_account(module_data=module_data)

                    reports = await db.get_account_reports(
                        key=hyperliquid_client.wallet_address,
                        label=hyperliquid_client.label,
                        address=hyperliquid_client.wallet_address,
                        last_module=last_module,
                        mode=mode,
                    )
                    await TgReport().send_log(logs=reports)

                    if module_data["module_info"]["status"] is True:
                        await async_sleep(randint(*settings.SLEEP_AFTER_ACC))
                    else:
                        await async_sleep(10)


async def run_pair(mode: int, group_data: dict, sem: asyncio.Semaphore):
    # Инициализируем блокировки для всех адресов кошельков
    wallet_addresses = [wallet_data["wallet_address"] for wallet_data in group_data["wallets_data"]]
    for address in wallet_addresses:
        if address not in address_locks:
            address_locks[address] = asyncio.Lock()
    
    async with MultiLock(wallet_addresses):
        async with sem:
            hyperliquid_clients = []
            try:
                hyperliquid_clients = [
                    initialize_account(wallet_data, group_data=group_data)
                    for wallet_data in group_data["wallets_data"]
                ]
                group_data["module_info"]["status"] = await PairAccounts(
                    accounts=hyperliquid_clients,
                    mode=mode,
                    db=db,
                )

            except Exception as err:
                logger.error(f'[-] Pair Error: {err}')
                await send_warning_notification(
                    error_type="Pair Error",
                    error_message=str(err),
                    account_label="Pair Trading"
                )

            finally:
                for client in hyperliquid_clients:
                    await client.browser.close_sessions()

                if type(group_data) == dict:
                    if mode in [3]:
                        last_module = await db.remove_group(group_data=group_data)
                    else:
                        last_module = await db.remove_account(module_data=group_data)

                    reports = await db.get_account_reports(
                        key=group_data["wallets_data"][0]["wallet_address"],
                        label=group_data["wallets_data"][0]["label"],
                        address=group_data["wallets_data"][0]["wallet_address"],
                        last_module=last_module,
                        mode=mode,
                    )
                    await TgReport().send_log(logs=reports)

                    if group_data["module_info"]["status"] is True:
                        await async_sleep(randint(*settings.SLEEP_AFTER_ACC))
                    else:
                        await async_sleep(10)


async def runner(mode: int):
    sem = asyncio.Semaphore(settings.THREADS)
    
    # Отправляем уведомление о старте
    mode_names = {
        0: "Averaging Market",
        4: "Cancel Orders", 
        5: "Parse"
    }
    mode_name = mode_names.get(mode, f"Mode {mode}")
    await TgReport().send_log(logs=f"🚀 <b>Hyperliquid Bot Started</b>\nMode: {mode_name}")

    if mode in [3]:
        all_groups = db.get_all_groups()
        if all_groups != 'No more accounts left':
            await asyncio.gather(*[
                run_pair(group_data=group_data, mode=mode, sem=sem)
                for group_data in all_groups
            ])

    else:
        all_modules = db.get_all_modules(unique_wallets=mode in [4, 5])
        if all_modules != 'No more accounts left':
            # Подсчитываем уникальные аккаунты
            unique_accounts = list(set([m['label'] for m in all_modules]))
            logger.opt(colors=True).info(
                f'[•] Loaded <white>{len(unique_accounts)}</white> account(s): '
                f'<white>{", ".join(unique_accounts)}</white> | '
                f'Total modules: <white>{len(all_modules)}</white>'
            )
            
            await asyncio.gather(*[
                run_modules(module_data=module_data, mode=mode, sem=sem)
                for module_data in all_modules
            ])

    logger.success(f'All accounts done.')
    return 'Ended'


if __name__ == '__main__':
    if os_name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    manual_stop = False
    
    try:
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

    except Exception as e:
        logger.error(f'[-] Global | {e}')
        input('\n > Exit\n')

    finally:
        if not manual_stop:
            logger.info(f'[•] Soft | Closed')
