"""
Маяк дежурства в канале связи (docs/GithubActions.md §1.5).

Одно сообщение, две строки: `heartbeat ДД.ММ ЧЧ:ММ` + `next leader: ДД.ММ ЧЧ:ММ`
(время московское). Новый ведущий подхватывает последний маяк поиском и правит
его каждый 5-й тик стука сердца; нечего подхватить или правка упала — один
перепост. Номер сообщения — только в памяти модуля, никуда не сохраняется.
Ожидающий ничего не пишет. Любая ошибка сети глушится: маяк никогда не роняет
дежурство.
"""

import aiohttp
from datetime import datetime, timedelta, timezone

from loguru import logger

from modules.ci_loader import merge_tg_tokens
from modules.tg_tokens_loader import load_tg_tokens_from_file

BEACON_PREFIX = "heartbeat "
# Стук сердца раз в 120 сек (§1.2); правим маяк каждый 5-й тик ≈ раз в 10 минут.
BEACON_EDIT_EVERY_TICKS = 5
MOSCOW_OFFSET = timedelta(hours=3)

_message_id: int | None = None
_tick = 0


def _creds():
    tokens = merge_tg_tokens(load_tg_tokens_from_file())
    return tokens.get("TG_BOT_TOKEN"), tokens.get("TG_USER_ID")


def _moscow_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Moscow"))
    except Exception:
        return datetime.now(timezone.utc) + MOSCOW_OFFSET


def _beacon_text(deadline_utc: datetime) -> str:
    now_msk = _moscow_now()
    try:
        from zoneinfo import ZoneInfo
        end_msk = deadline_utc.astimezone(ZoneInfo("Europe/Moscow"))
    except Exception:
        end_msk = deadline_utc + MOSCOW_OFFSET
    return (
        f"{BEACON_PREFIX}{now_msk.strftime('%d.%m %H:%M')}\n"
        f"next leader: {end_msk.strftime('%d.%m %H:%M')}"
    )


async def _api(token: str, method: str, payload: dict):
    url = f"https://api.telegram.org/bot{token}/{method}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"TG {method}: HTTP {resp.status}: {body[:200]}")
            import json as _json
            return _json.loads(body).get("result")


async def _find_last_beacon(token: str, chat_id: str):
    """Максимальный номер своего сообщения с префиксом среди свежих обновлений."""
    try:
        result = await _api(token, "getUpdates", {"limit": 100, "timeout": 0})
    except Exception as e:
        logger.warning(f"Beacon find failed: {e}")
        return None
    if not isinstance(result, list):
        return None
    best = None
    for update in result:
        for key in ("channel_post", "message"):
            msg = update.get(key) or {}
            text = msg.get("text") or ""
            if text.startswith(BEACON_PREFIX):
                mid = msg.get("message_id")
                if isinstance(mid, int) and (best is None or mid > best):
                    best = mid
    return best


async def leader_beacon_tick(deadline_utc: datetime) -> None:
    """Один тик маяка от ведущего: правка каждый N-й тик, иначе молчание."""
    global _message_id, _tick
    token, chat_id = _creds()
    if not token or not chat_id:
        return
    _tick += 1
    try:
        if _message_id is None:
            _message_id = await _find_last_beacon(token, chat_id)
        text = _beacon_text(deadline_utc)
        if _message_id is not None and _tick % BEACON_EDIT_EVERY_TICKS != 0:
            return
        if _message_id is not None:
            try:
                await _api(token, "editMessageText", {
                    "chat_id": chat_id,
                    "message_id": _message_id,
                    "text": text,
                })
                return
            except Exception as e:
                logger.warning(f"Beacon edit failed, reposting: {e}")
        sent = await _api(token, "sendMessage", {"chat_id": chat_id, "text": text})
        if isinstance(sent, dict) and isinstance(sent.get("message_id"), int):
            _message_id = sent["message_id"]
        else:
            _message_id = None
    except Exception as e:
        logger.warning(f"Beacon tick failed: {e}")


async def warn_owner(text: str) -> None:
    """Предупреждение владельцу (зазоры, ошибки) — только отклонения, не сводки."""
    token, chat_id = _creds()
    if not token or not chat_id:
        logger.warning(f"Owner warning dropped (no TG creds): {text}")
        return
    try:
        await _api(token, "sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.warning(f"Owner warning failed: {e}")
