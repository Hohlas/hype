"""
Mode selection module for Hyperliquid Bot
"""

import os
import sys
sys.__stdout__ = sys.stdout  # error with `import inquirer` without this string in some system

from dataclasses import dataclass
from typing import List, Dict, Any

# inquirer импортируется лениво внутри choose_mode: CI-путь не должен тянуть
# интерактивное меню даже на уровне импорта (см. §9.п7).


@dataclass
class Mode:
    """
    Класс для представления торгового режима.
    """
    soft_id: int
    text: str
    type: str
    is_new: bool = False
    is_numeric: bool = True

    def __str__(self) -> str:
        return ("⭐️ NEW | " if self.is_new else "") + self.text


# Карта BOT_MODE → режим (§9.п1). Неизвестное значение — ValueError,
# а не молчаливое падение в торговлю (для торгового бота risky default).
CI_MODE_MAP = {
    "0": (0, "Averaging Market"),
    "4": (4, "Cancel Orders"),
    "5": (5, "Parse"),
}


def get_mode_from_env() -> Mode:
    """Режим для CI: из BOT_MODE, без терминала."""
    mode_id = os.getenv("BOT_MODE", "0").strip()
    if mode_id not in CI_MODE_MAP:
        raise ValueError(
            f"Invalid BOT_MODE: {mode_id!r}. Valid: {', '.join(sorted(CI_MODE_MAP))}"
        )
    soft_id, text = CI_MODE_MAP[mode_id]
    return Mode(soft_id=soft_id, type="module", text=text)


# Режимы дежурства (docs/project_description.md §6.5). parse — только чтение,
# averaging — торговля. Значение задаётся в settings.py (переключатель владельца).
DUTY_MODES = ("parse", "averaging")
DUTY_MODE_DEFAULT = "parse"


def get_duty_mode() -> str:
    """Режим дежурства из settings.DUTY_MODE.

    Fail-safe: отсутствие/опечатка/неизвестное значение → "parse" (без торговли)
    с громким предупреждением. Дефолт для торгового бота обязан быть безопасным —
    ошибка конфигурации не должна включать постановку ордеров.
    """
    import settings  # ленивый импорт: modes.py не тянет конфиг при загрузке

    raw = str(getattr(settings, "DUTY_MODE", DUTY_MODE_DEFAULT) or "").strip().lower()
    if raw not in DUTY_MODES:
        if raw:
            print(f"⚠️ Unknown DUTY_MODE {raw!r} — falling back to 'parse' (no trading).")
        return DUTY_MODE_DEFAULT
    return raw


def choose_mode() -> Mode:
    """
    Интерактивный выбор торгового режима с использованием курсора (как в pacifica).
    Только для VPS/локального запуска — в CI используется get_mode_from_env().
    """
    from inquirer import prompt, List as InquirerList
    from inquirer.themes import load_theme_from_dict

    theme = load_theme_from_dict({"List": {
        "selection_cursor": "👉🏻",
        # "selection_color": "violetred1",
    }})

    def ask_question(question: str, modes: list):
        total_numerics = 0
        choices = []
        for mode in modes:
            mode_numeric = ""
            if mode.is_numeric:
                mode_numeric = f"{total_numerics}. "
                total_numerics += 1

            choices.append((f"{mode_numeric}{mode}", mode.soft_id))

        questions = [
            InquirerList(
                name='custom_question',
                message=question,
                choices=choices,
                carousel=True,
            )
        ]

        raw_answer = prompt(
            questions=questions,
            raise_keyboard_interrupt=True,
            theme=theme,
        )
        return next((mode for mode in modes if mode.soft_id == raw_answer['custom_question']))

    answer = ask_question(
        question="🚀 Choose mode",
        modes=[
            Mode(soft_id=0, type="module", text="Averaging Market", is_numeric=False),
            Mode(soft_id=4, type="module", text="Cancel Orders", is_numeric=False),
            Mode(soft_id=5, type="module", text="Parse", is_numeric=False),
            Mode(soft_id=-1, type="", text="(Re)Create Database", is_numeric=False),
        ]
    )

    if answer.soft_id == -1:
        answer = ask_question(
            question="💾 You want to delete current and create new database?",
            modes=[
                Mode(soft_id=-1, type="", text="← Exit", is_numeric=False),
                Mode(soft_id=101, type="database", text="Create new database", is_numeric=False),
            ]
        )

    return answer


def get_mode_info(mode_id: int) -> Dict[str, Any]:
    """
    Получение информации о режиме по ID.
    """
    mode_info = {
        0: {
            "name": "Averaging Market",
            "description": "Spot торговля с усреднением позиций",
            "supported": True,
            "features": ["Усреднение позиций", "Пирамидинг", "Take Profit", "Telegram уведомления"]
        },
        4: {
            "name": "Cancel Orders",
            "description": "Отмена всех ордеров",
            "supported": True,
            "features": ["Отмена всех активных ордеров"]
        },
        5: {
            "name": "Parse",
            "description": "Парсинг данных",
            "supported": True,
            "features": ["Получение данных аккаунта", "Анализ позиций"]
        }
    }
    
    return mode_info.get(mode_id, {
        "name": "Unknown",
        "description": "Неизвестный режим",
        "supported": False,
        "features": []
    })
