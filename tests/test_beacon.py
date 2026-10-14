"""Тесты маяка дежурства (§1.5): одно сообщение — правка, а не поток новых.

Регрессия: раньше id маяка жил только в памяти процесса, а ``getUpdates`` не
отдаёт боту его собственные сообщения — каждый новый ведущий слал НОВОЕ
сообщение. Теперь id ходит через ветку ``state`` (``beacon_message_id``).
"""

import asyncio
from datetime import datetime, timedelta, timezone

from modules import beacon


def _deadline():
    return datetime.now(timezone.utc) + timedelta(hours=1)


def _setup(monkeypatch, calls, send_id=4242):
    async def fake_api(token, method, payload):
        calls.append((method, dict(payload)))
        return {"message_id": payload.get("message_id", send_id)}

    monkeypatch.setattr(beacon, "_creds", lambda: ("tok", "chat"))
    monkeypatch.setattr(beacon, "_api", fake_api)
    monkeypatch.setattr(beacon, "_message_id", None)
    monkeypatch.setattr(beacon, "_tick", 0)


def test_beacon_edits_known_message_no_repost(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    dl = _deadline()

    # Тики 1–4: id известен из ветки — никаких отправок (правка на 5-м тике).
    for _ in range(4):
        assert asyncio.run(beacon.leader_beacon_tick(dl, known_id=555)) == 555
    assert calls == []

    # 5-й тик — правка того же сообщения, нового сообщения нет.
    assert asyncio.run(beacon.leader_beacon_tick(dl, known_id=555)) == 555
    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["message_id"] == 555
    assert all(method != "sendMessage" for method, _ in calls)


def test_beacon_sends_once_when_no_known_id(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, send_id=4242)

    async def fake_find(token, chat):
        return None  # getUpdates не отдаёт собственные сообщения бота

    monkeypatch.setattr(beacon, "_find_last_beacon", fake_find)
    assert asyncio.run(beacon.leader_beacon_tick(_deadline(), known_id=None)) == 4242
    assert calls[0][0] == "sendMessage"


def test_beacon_reposts_when_edit_fails(monkeypatch):
    calls = []

    async def fake_api(token, method, payload):
        calls.append((method, dict(payload)))
        if method == "editMessageText":
            raise RuntimeError("message to edit not found")
        return {"message_id": 9999}

    monkeypatch.setattr(beacon, "_creds", lambda: ("tok", "chat"))
    monkeypatch.setattr(beacon, "_api", fake_api)
    monkeypatch.setattr(beacon, "_message_id", 555)
    monkeypatch.setattr(beacon, "_tick", 4)  # следующий тик = 5 → правка (упадёт)

    assert asyncio.run(beacon.leader_beacon_tick(_deadline(), known_id=555)) == 9999
    assert calls[0][0] == "editMessageText"
    assert calls[-1][0] == "sendMessage"
