"""
Загрузка секретов из переменных окружения для дежурства в GitHub Actions.

См. docs/GithubActions.md §8–§9.п3: в CI-выгрузке нет каталога input_data/
(он в .gitignore), поэтому кошельки и токены приезжают через Secrets → env.
Только стандартная библиотека: модуль обязан импортироваться без тяжёлых
зависимостей (его тянет и дежурный слой, и точка входа).
"""

import json
import os

RUNTIME_VPS = "vps"
RUNTIME_GITHUB_ACTIONS = "github_actions"

TG_TOKEN_KEYS = ("TG_BOT_TOKEN", "PROFIT_BOT_TOKEN", "TG_USER_ID")


def get_runtime_mode() -> str:
    """Среда запуска: явная BOT_RUNTIME, иначе авто (есть WALLETS_DATA — CI)."""
    explicit = os.getenv("BOT_RUNTIME", "").strip().lower()
    if explicit in (RUNTIME_VPS, RUNTIME_GITHUB_ACTIONS):
        return explicit
    if os.getenv("WALLETS_DATA", "").strip():
        return RUNTIME_GITHUB_ACTIONS
    return RUNTIME_VPS


def is_ci_runtime() -> bool:
    return get_runtime_mode() == RUNTIME_GITHUB_ACTIONS


def load_wallets_from_env():
    """Разбор WALLETS_DATA: JSON-список [{label, wallet_address, private_key, proxy?}].

    Возвращает None, если переменная не задана/пустая (тогда вызыватель
    идёт файловым путём). Битая структура — ValueError (быстрый отказ,
    а не молчаливый обход).
    """
    raw = os.getenv("WALLETS_DATA", "")
    if not raw or not raw.strip():
        return None
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"WALLETS_DATA is not valid JSON: {e}")
    if not isinstance(entries, list) or not entries:
        raise ValueError("WALLETS_DATA must be a non-empty JSON array")
    cleaned = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"WALLETS_DATA[{i}] must be an object")
        label = str(entry.get("label", "")).strip()
        wallet_address = str(entry.get("wallet_address", "")).strip()
        private_key = str(entry.get("private_key", "")).strip()
        if not label or not wallet_address or not private_key:
            raise ValueError(
                f"WALLETS_DATA[{i}] needs label, wallet_address, private_key"
            )
        proxy = entry.get("proxy")
        if isinstance(proxy, str):
            proxy = proxy.strip()
            if not proxy or proxy.upper() == "NONE":
                proxy = None
        elif proxy is not None:
            proxy = str(proxy)
        cleaned.append({
            "label": label,
            "wallet_address": wallet_address,
            "private_key": private_key,
            "proxy": proxy,
        })
    return cleaned


def build_wallet_modules(entries):
    """Та же форма записей, что строит файловый загрузчик (module_info.status)."""
    return [
        {
            "label": entry["label"],
            "wallet_address": entry["wallet_address"],
            "private_key": entry["private_key"],
            "proxy": entry.get("proxy"),
            "module_info": {"status": True},
        }
        for entry in entries
    ]


def load_tg_tokens_from_env():
    """Токены из окружения; отсутствующие ключи — None (не ошибка)."""
    tokens = {}
    for key in TG_TOKEN_KEYS:
        value = os.getenv(key, "")
        tokens[key] = value.strip() if value and value.strip() else None
    return tokens


def merge_tg_tokens(file_tokens: dict) -> dict:
    """Файловые значения как база, непустые env — поверх (CI перекрывает файл)."""
    merged = dict(file_tokens or {})
    for key, value in load_tg_tokens_from_env().items():
        if value:
            merged[key] = value
    return merged
