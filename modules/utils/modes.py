"""
Mode selection module for Hyperliquid Bot
"""

import sys
sys.__stdout__ = sys.stdout  # error with `import inquirer` without this string in some system

from dataclasses import dataclass
from typing import List, Dict, Any
from inquirer import prompt, List as InquirerList
from inquirer.themes import load_theme_from_dict


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


THEME = load_theme_from_dict({"List": {
    "selection_cursor": "👉🏻",
    # "selection_color": "violetred1",
}})


def choose_mode() -> Mode:
    """
    Интерактивный выбор торгового режима с использованием курсора (как в pacifica).
    """
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
            theme=THEME,
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
