"""Тесты CI-загрузки (§9.пп.1–4): режимы, секреты из окружения."""

import json

import pytest

from modules import ci_loader
from modules.ci_loader import (
    build_wallet_modules,
    get_runtime_mode,
    load_tg_tokens_from_env,
    load_wallets_from_env,
    merge_tg_tokens,
)
from modules.utils.modes import get_mode_from_env


def test_runtime_explicit_and_auto(monkeypatch):
    monkeypatch.setenv("BOT_RUNTIME", "github_actions")
    assert get_runtime_mode() == "github_actions"
    monkeypatch.setenv("BOT_RUNTIME", "vps")
    assert get_runtime_mode() == "vps"
    monkeypatch.delenv("BOT_RUNTIME")
    monkeypatch.delenv("WALLETS_DATA", raising=False)
    assert get_runtime_mode() == "vps"
    monkeypatch.setenv("WALLETS_DATA", "[{}]")
    assert get_runtime_mode() == "github_actions"


def test_mode_from_env(monkeypatch):
    monkeypatch.setenv("BOT_MODE", "5")
    mode = get_mode_from_env()
    assert (mode.soft_id, mode.type, mode.text) == (5, "module", "Parse")
    monkeypatch.setenv("BOT_MODE", "0")
    assert get_mode_from_env().soft_id == 0
    monkeypatch.setenv("BOT_MODE", "7")
    with pytest.raises(ValueError):
        get_mode_from_env()


def test_wallets_from_env_none_when_unset(monkeypatch):
    monkeypatch.delenv("WALLETS_DATA", raising=False)
    assert load_wallets_from_env() is None


def test_wallets_from_env_valid(monkeypatch):
    entries = [
        {"label": "a", "wallet_address": "0x1", "private_key": "pk",
         "proxy": "NONE"},
    ]
    monkeypatch.setenv("WALLETS_DATA", json.dumps(entries))
    loaded = load_wallets_from_env()
    assert loaded == [{"label": "a", "wallet_address": "0x1",
                       "private_key": "pk", "proxy": None}]
    modules = build_wallet_modules(loaded)
    assert modules[0]["module_info"] == {"status": True}


def test_wallets_from_env_broken_fails_fast(monkeypatch):
    monkeypatch.setenv("WALLETS_DATA", "not-json")
    with pytest.raises(ValueError):
        load_wallets_from_env()
    monkeypatch.setenv("WALLETS_DATA", json.dumps([{"label": "a"}]))
    with pytest.raises(ValueError):
        load_wallets_from_env()


def test_tg_tokens_env_overrides_file(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "env-token")
    monkeypatch.delenv("PROFIT_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_USER_ID", raising=False)
    assert load_tg_tokens_from_env()["TG_BOT_TOKEN"] == "env-token"
    merged = merge_tg_tokens({"TG_BOT_TOKEN": "file-token",
                              "TG_USER_ID": "123"})
    assert merged["TG_BOT_TOKEN"] == "env-token"
    assert merged["TG_USER_ID"] == "123"


def test_beacon_text_format(monkeypatch):
    import datetime as _dt

    from modules import beacon
    monkeypatch.setenv("TG_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TG_USER_ID", "123")
    token, chat = beacon._creds()
    assert (token, chat) == ("dummy", "123")
    deadline = _dt.datetime(2026, 10, 14, 12, 0,
                            tzinfo=_dt.timezone.utc)
    text = beacon._beacon_text(deadline)
    lines = text.splitlines()
    assert lines[0].startswith("heartbeat ")
    assert lines[1].startswith("next leader: ")
    # Москва = UTC+3.
    assert lines[1] == "next leader: 14.10 15:00"

