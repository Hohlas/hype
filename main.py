"""Точка входа hype: GridRB 80/20 на споте Hyperliquid.

Один и тот же файл работает в двух средах:
- VPS / локально — бесконечный цикл (остановка Ctrl+C);
- GitHub Actions — ограниченный вотч (`--watch-seconds`, `--poll-interval`).

Старый алгоритм (сетка с отложенными ордерами), меню режимов и протокол
дежурства из рантайма убраны. Логика стратегии — modules/gridrb_strategy.py.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from os import name as os_name

from loguru import logger

import settings
from modules.ci_loader import RUNTIME_GITHUB_ACTIONS, get_runtime_mode
from modules.database import DataBase
from modules.gridrb_strategy import run_gridrb
from modules.hyperliquid_browser import HyperliquidBrowser
from modules.hyperliquid_client import HyperliquidClient
from modules.utils import TgReport, send_warning_notification

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logger.remove()
logger.add(
    sys.stdout,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}",
    level=settings.LOG_LEVEL,
)
logger.add(
    settings.LOG_FILE,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}",
    level=settings.LOG_LEVEL,
    rotation=settings.LOG_ROTATION,
    retention=settings.LOG_RETENTION,
    encoding="utf-8",
)

address_locks: dict[str, asyncio.Lock] = {}


def initialize_account(module_data: dict, db: DataBase) -> HyperliquidClient:
    browser = HyperliquidBrowser(
        proxy=module_data.get("proxy"),
        wallet_address=module_data["wallet_address"],
        private_key=module_data["private_key"],
        label=module_data["label"],
        db=db,
    )
    return HyperliquidClient(
        browser=browser,
        wallet_address=module_data["wallet_address"],
        private_key=module_data["private_key"],
        label=module_data["label"],
        proxy=module_data.get("proxy"),
        db=db,
    )


async def run_account(module_data: dict, db: DataBase,
                      watch_seconds: float | None, poll_interval: float,
                      max_iterations: int = 0,
                      sem: asyncio.Semaphore | None = None) -> None:
    wallet_address = module_data["wallet_address"]
    lock = address_locks.setdefault(wallet_address, asyncio.Lock())

    async with lock:
        client = None
        acquired = False
        try:
            if sem is not None:
                await sem.acquire()
                acquired = True
            client = initialize_account(module_data, db)
            logger.opt(colors=True).info('[•] starting GridRB')
            token = getattr(settings, "SPOT_TOKEN", "BTC") or "BTC"
            await run_gridrb(
                client, token,
                max_iterations=max_iterations,
                poll_interval=poll_interval,
                watch_seconds=watch_seconds,
            )
        except Exception as err:
            logger.error(f'[-] error: {err}')
            try:
                await send_warning_notification(
                    error_type="GridRB Error",
                    error_message=str(err),
                    account_label=module_data.get("label", "Unknown"),
                )
            except Exception:
                pass
        finally:
            if client is not None:
                try:
                    await client.browser.close_sessions()
                except Exception:
                    pass
            if acquired and sem is not None:
                sem.release()


async def runner(watch_seconds: float | None, poll_interval: float,
                 max_iterations: int = 0) -> str:
    db = DataBase()
    all_modules = db.get_all_modules(unique_wallets=True)

    if all_modules == "No more accounts left":
        logger.warning("Нет аккаунтов: проверьте WALLETS_DATA (CI) или "
                       "input_data/wallets.txt / databases/modules.json")
        return "Ended"
    if not all_modules:
        logger.warning("Список аккаунтов пуст")
        return "Ended"

    labels = list(dict.fromkeys(m["label"] for m in all_modules))
    logger.opt(colors=True).info(
        f'[•] аккаунтов: <white>{len(labels)}</white> | GridRB '
        f'{getattr(settings, "GRIDRB_F0", 0.80):.2f}/'
        f'{1 - getattr(settings, "GRIDRB_F0", 0.80):.2f}')

    try:
        mode = "watch" if watch_seconds else ("once" if max_iterations else "infinite")
        await TgReport().send_log(
            f"🚀 <b>hype GridRB</b> v.{settings.version}\n"
            f"accounts: {len(labels)} | mode: {mode} | "
            f"poll: {poll_interval:.0f}s")
    except Exception as e:
        logger.debug(f"startup TG skipped: {e}")

    sem = asyncio.Semaphore(max(1, int(getattr(settings, "THREADS", 1))))
    await asyncio.gather(*[
        run_account(m, db, watch_seconds, poll_interval, max_iterations, sem)
        for m in all_modules
    ])
    logger.success("All accounts done.")
    return "Ended"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw and raw.strip().isdigit() else default
    except Exception:
        return default


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="hype — GridRB 80/20")
    parser.add_argument("--watch-seconds", type=float, default=None,
                        help="сколько секунд работать (CI); без флага — бесконечно")
    parser.add_argument("--poll-interval", type=float, default=None,
                        help="пауза между итерациями, сек")
    parser.add_argument("--once", action="store_true",
                        help="один проход и выход")
    parser.add_argument("--init-db", action="store_true",
                        help="создать/пересоздать databases/modules.json "
                             "из input_data/wallets.txt (интерактивный пароль)")
    return parser.parse_args(argv)


def _acquire_instance_lock():
    """Не даёт двум GridRB на одном хосте одновременно управлять счётом."""
    try:
        import fcntl
    except ImportError:  # Windows: локальный запуск без блокировки
        return None
    os.makedirs("databases", exist_ok=True)
    path = os.path.join("databases", "gridrb.lock")
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise RuntimeError(
            "другой экземпляр GridRB уже работает (databases/gridrb.lock)")
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main(argv=None) -> int:
    args = parse_args(argv)
    runtime = get_runtime_mode()

    if args.init_db:
        try:
            DataBase().create_modules(mode=0)
            return 0
        except Exception as e:
            logger.error(f"[-] init-db | {e}")
            return 1

    try:
        _lock = _acquire_instance_lock()  # noqa: F841 — держим до выхода
    except RuntimeError as e:
        logger.error(f"[-] {e}")
        return 1

    poll = args.poll_interval
    if poll is None:
        poll = float(_env_int("BOT_POLL_INTERVAL",
                              int(getattr(settings, "GRIDRB_POLL_INTERVAL", 60))))
    poll = max(1.0, poll)

    watch = args.watch_seconds
    if watch is None and runtime == RUNTIME_GITHUB_ACTIONS:
        watch = float(_env_int("WATCH_SECONDS", 19800))

    max_iterations = 1 if args.once else 0
    if max_iterations:
        watch = None

    logger.success(
        f'[•] hype GridRB | runtime={runtime} | '
        f'watch={watch if watch else "∞"}s | poll={poll:.0f}s')

    try:
        result = asyncio.run(runner(watch, poll, max_iterations))
    except KeyboardInterrupt:
        logger.info("[•] Interrupted")
        return 130
    except Exception as e:
        logger.error(f"[-] Global | {e}")
        return 1

    return 0 if result == "Ended" else 1


if __name__ == "__main__":
    if os_name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(main())