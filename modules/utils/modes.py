"""
Mode selection module for Hyperliquid Bot
"""

from dataclasses import dataclass
from typing import List, Dict, Any


@dataclass
class Mode:
    """
    Класс для представления торгового режима.
    """
    type: str
    soft_id: int
    name: str
    description: str


def choose_mode() -> Mode:
    """
    Интерактивный выбор торгового режима.
    """
    modes = [
        Mode("module", 0, "Averaging Market", "Spot торговля с усреднением позиций"),
        Mode("module", 1, "Futures Market", "Фьючерсная торговля (не поддерживается)"),
        Mode("module", 2, "Futures Limit", "Фьючерсные лимитные ордера (не поддерживается)"),
        Mode("module", 3, "Pair Futures Limits", "Парная торговля (не поддерживается)"),
        Mode("module", 4, "Cancel Orders", "Отмена всех ордеров"),
        Mode("module", 5, "Parse", "Парсинг данных"),
        Mode("database", 0, "Create Database", "Создание базы данных"),
        Mode("exit", 0, "Exit", "Выход из программы"),
    ]
    
    print("\n" + "="*60)
    print("🚀 HYPERLIQUID BOT - Выбор режима торговли")
    print("="*60)
    
    for i, mode in enumerate(modes, 1):
        status = "✅" if mode.type == "module" and mode.soft_id in [0, 4, 5] else "❌" if mode.type == "module" else "📊"
        print(f"{i:2d}. {status} {mode.name}")
        print(f"    {mode.description}")
        print()
    
    while True:
        try:
            choice = input("Выберите режим (номер): ").strip()
            
            if not choice:
                continue
                
            choice_num = int(choice)
            
            if 1 <= choice_num <= len(modes):
                selected_mode = modes[choice_num - 1]
                print(f"\n✅ Выбран режим: {selected_mode.name}")
                return selected_mode
            else:
                print("❌ Неверный номер режима. Попробуйте снова.")
                
        except ValueError:
            print("❌ Введите корректный номер режима.")
        except KeyboardInterrupt:
            print("\n👋 До свидания!")
            return Mode("exit", 0, "Exit", "Выход из программы")
        except Exception as e:
            print(f"❌ Ошибка: {e}")


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
        1: {
            "name": "Futures Market",
            "description": "Фьючерсная торговля",
            "supported": False,
            "features": []
        },
        2: {
            "name": "Futures Limit",
            "description": "Фьючерсные лимитные ордера",
            "supported": False,
            "features": []
        },
        3: {
            "name": "Pair Futures Limits",
            "description": "Парная торговля",
            "supported": False,
            "features": []
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
