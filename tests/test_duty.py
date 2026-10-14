"""Тесты дежурной логики (docs/GithubActions.md §9.п9).

Чистые функции modules.duty + бюджет времени по workflow. Набор обязан ловить
структурные баги протокола до прода.
"""

import os
import re

import pytest

from modules import duty
from modules.duty import (
    LEADER_STALE_SEC,
    PROMOTE_CONFIRM_READS,
    WORK_STALE_SEC,
    duty_gap_info,
    leader_is_live,
    leader_should_yield,
    leader_watch_seconds,
    merge_counters,
    merge_marks,
    merge_states,
    new_leader_doc,
    standby_should_promote,
    work_is_wedged,
)

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.join(HERE, "..", ".github", "workflows", "bot.yml")


def _doc(run_id="1/1", **kw):
    doc = new_leader_doc(run_id)
    doc.update(kw)
    return doc


# --- Слияние: объединение, никогда пересечение (§1.3) ---

def test_merge_marks_union_fresh_wins():
    local = {"a": "2026-10-10", "b": "2026-10-12"}
    remote = {"b": "2026-10-11", "c": "2026-10-09"}
    assert merge_marks(local, remote) == {
        "a": "2026-10-10", "b": "2026-10-12", "c": "2026-10-09"}


def test_merge_counters_max_wins():
    local = {"err": {"n": 5}}
    remote = {"err": {"n": 9}, "warn": {"n": 1}}
    merged = merge_counters(local, remote)
    assert merged["err"]["n"] == 9
    assert merged["warn"]["n"] == 1
    # Местное большее — не откатывается чужим меньшим.
    assert merge_counters({"err": {"n": 12}}, remote)["err"]["n"] == 12


def test_merge_states_last_run_max():
    local = {"last_run_utc": "2026-10-13T10:00:00Z"}
    remote = {"last_run_utc": "2026-10-13T12:00:00Z"}
    assert merge_states(local, remote)["last_run_utc"] == remote["last_run_utc"]
    assert merge_states(remote, local)["last_run_utc"] == remote["last_run_utc"]


# --- Уступка: только живому + готовому + строго более новому ---

def test_yield_only_to_live_ready_newer():
    mine = _doc("10/1", ready=True,
                watch_started_utc="2026-10-13T10:00:00Z",
                heartbeat_utc="2026-10-13T12:00:00Z")
    newer = _doc("11/1", ready=True,
                 watch_started_utc="2026-10-13T11:00:00Z",
                 heartbeat_utc="2026-10-13T12:00:00Z")
    now = duty._parse_utc("2026-10-13T12:01:00Z")
    assert leader_should_yield(mine, newer, now) is True
    # Тот же run_id — не уступать.
    assert leader_should_yield(mine, dict(newer, run_id="10/1"), now) is False
    # Не готов — не уступать.
    assert leader_should_yield(mine, dict(newer, ready=False), now) is False
    # Протухший — не уступать.
    stale = dict(newer, heartbeat_utc="2026-10-13T11:00:00Z")
    assert leader_should_yield(mine, stale, now) is False
    # Старший — не уступать.
    older = dict(newer, watch_started_utc="2026-10-13T09:00:00Z")
    assert leader_should_yield(mine, older, now) is False


def test_yield_equal_start_breaks_by_run_id():
    mine = _doc("10/1", ready=True,
                watch_started_utc="2026-10-13T10:00:00Z",
                heartbeat_utc="2026-10-13T12:00:00Z")
    now = duty._parse_utc("2026-10-13T12:01:00Z")
    same_start_bigger = dict(mine, run_id="11/1")
    same_start_smaller = dict(mine, run_id="09/1")
    assert leader_should_yield(mine, same_start_bigger, now) is True
    # Иначе уступят оба и дежурство останется без ведущего.
    assert leader_should_yield(mine, same_start_smaller, now) is False


# --- Подхват: 2 подтверждающих чтения, retiring — сразу ---

def test_promote_missing_or_retiring_immediate():
    assert standby_should_promote(None, 0) is True
    assert standby_should_promote({"retiring": True}, 0) is True


def test_promote_stale_needs_confirm_reads():
    now = duty._parse_utc("2026-10-13T12:10:00Z")
    stale = {"heartbeat_utc": "2026-10-13T12:00:00Z"}  # старше LEADER_STALE_SEC
    assert leader_is_live(stale, now) is False
    assert standby_should_promote(stale, PROMOTE_CONFIRM_READS - 1, now) is False
    assert standby_should_promote(stale, PROMOTE_CONFIRM_READS, now) is True
    live = {"heartbeat_utc": "2026-10-13T12:09:00Z"}
    assert standby_should_promote(live, 99, now) is False


# --- Клин: сердце свежее, работа стоит дольше WORK_STALE_SEC ---

def test_wedge_same_run_stale_work():
    now = duty._parse_utc("2026-10-13T12:30:00Z")
    leader = {"run_id": "7/1", "heartbeat_utc": "2026-10-13T12:29:00Z"}
    work = {"run_id": "7/1", "last_work_utc": "2026-10-13T12:00:00Z"}
    assert work_is_wedged(work, leader, now) is True
    fresh_work = {"run_id": "7/1", "last_work_utc": "2026-10-13T12:25:00Z"}
    assert work_is_wedged(fresh_work, leader, now) is False


def test_wedge_forbidden_grounds():
    now = duty._parse_utc("2026-10-13T12:30:00Z")
    leader = {"run_id": "7/1", "heartbeat_utc": "2026-10-13T12:29:00Z"}
    # Чужой run_id, нет маркера, метка из будущего — takeover запрещён.
    assert work_is_wedged({"run_id": "8/1",
                           "last_work_utc": "2026-10-13T12:00:00Z"},
                          leader, now) is False
    assert work_is_wedged({}, leader, now) is False
    assert work_is_wedged({"run_id": "7/1",
                           "last_work_utc": "2026-10-13T13:00:00Z"},
                          leader, now) is False


# --- Наследование дедлайна: остаток, не полный вотч ---

def test_deadline_inheritance_remainder_not_full():
    assert leader_watch_seconds(None, 19800) == 19800
    assert leader_watch_seconds(300.0, 19800) == 300.0
    assert leader_watch_seconds(-5.0, 19800) == 0.0


# --- Диагностика дыр ---

def test_gap_warn_threshold():
    now = duty._parse_utc("2026-10-13T12:10:00Z")
    gap, warn = duty_gap_info("2026-10-13T12:00:00Z", now)
    assert gap == pytest.approx(600.0)
    assert warn is True
    gap, warn = duty_gap_info("2026-10-13T12:09:00Z", now)
    assert warn is False


# --- Бюджет времени: вотч + хвост + установка ≤ timeout, запас ≥ 5 мин ---

def _workflow_text():
    assert os.path.isfile(WORKFLOW), \
        f"{WORKFLOW} missing — create it at ladder step 2 (§10), not earlier"
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


def test_timeout_budget():
    text = _workflow_text()
    timeout = int(re.search(r"timeout-minutes:\s*(\d+)", text).group(1))
    # Вотч по умолчанию виден в workflow (19800 сек = 330 мин).
    assert re.search(r"watch_seconds:.*?default:\s*'19800'", text, re.S)
    watch_min, tail_min, setup_min = 330, 15, 1
    assert watch_min + tail_min + setup_min <= timeout
    assert timeout - (watch_min + tail_min + setup_min) >= 5


def test_no_concurrency_cancel():
    text = _workflow_text()
    # Ключа concurrency быть не должно (комментарий-напоминание — можно).
    assert re.search(r"(?m)^\s*concurrency\s*:", text) is None  # §0.п5
    assert "cancel-in-progress" not in text


def test_workflow_observability_minimum():
    text = _workflow_text()
    assert "7,19,33,46,58" in text  # явный список минут (§2.1)
    assert "PYTHONUNBUFFERED" in text  # иначе журнал отстаёт (§3)
    assert "add-mask" in text  # секреты не светят в журнал (§0.п6)
    assert text.count("if: always()") >= 2  # Persist + вложение журналов (§3, §6)
