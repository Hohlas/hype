"""Hype duty layer — leader/standby relay over the ``state`` branch.

SKELETON (see ``docs/GithubActions.md``): pure duty logic ported from
ChatSum ``run_once.py`` (symbol names mirror the original so the doc maps
   1:1). Trading calls are NOT here — the leader loop calls
   :func:`run_trading_iteration`, wired to the read-only parse pass
   (``modules.duty_parse``) for the §10 ladder; full trading reconcile
   lands later. Import-safe: stdlib only,
   no package-root imports at module load (``python -m modules.duty`` must not
   pull ``inquirer``/browser side effects).

Protocol summary (frozen, see doc §1):
- One leader trades, the rest are standby (no trading, flag reads only).
- Claim: check-then-PUT, last-writer-wins + one retry on sha race.
  New runs claim ``ready: False``, flip to ``ready: True`` only after a
  successful exchange connection + first parse.
- Yield: only to a live + ready + strictly newer leader (equal start time
  breaks by bigger ``run_id``).
- Promote: flag missing, heartbeat older than ``LEADER_STALE_SEC``, or
  leader ``retiring`` — confirmed over ``PROMOTE_CONFIRM_READS`` reads.
- Wedge: heartbeat fresh but ``work.json`` of the SAME ``run_id`` older
  than ``WORK_STALE_SEC`` — take over after the same confirming reads.
- Deadline inheritance: a promoted standby leads the REMAINDER of its own
  watch, never a fresh full watch.
- State merge is a UNION (fresher date per mark, ``max()`` counters and
  ``last_run_utc``); writes are pull-merge-then-PUT + verify tripwire.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------
# Load-bearing numbers (doc §1.2/§2.2 — change only with the owner)
# --------------------------------------------------------------------------
LEADER_FILE = "leader.json"
STANDBY_FILE = "standby.json"
WORK_FILE = "work.json"
STATE_DEFAULT = "state.json"

LEADER_STALE_SEC = 300       # leader without heartbeat longer than this is dead
LEADER_HEARTBEAT_SEC = 120   # heartbeat + state push period
SETTINGS_REFRESH_SEC = 600   # settings.py staleness inside a 5.5h run
PROMOTE_CONFIRM_READS = 2    # consecutive stale reads before promotion
WORK_STALE_SEC = 900         # fresh heartbeat + no progress this long → wedged
GAP_WARN_SEC = 240           # duty silence longer than this → owner warning
STATE_RETENTION_DAYS = 3     # counter pruning window

WATCH_DEFAULT_SEC = 19800    # 5.5h leader watch (budget: 330 + 15 + 1 <= 355)
POLL_DEFAULT_SEC = 60        # trading iteration step


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def _utc_now():
    return datetime.now(timezone.utc)


def _utc_str(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(raw):
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def my_run_id() -> str:
    run_id = os.getenv("GITHUB_RUN_ID", "").strip()
    if run_id:
        return f"{run_id}/{os.getenv('GITHUB_RUN_ATTEMPT', '').strip() or '1'}"
    return f"local-{uuid.uuid4().hex[:8]}"


def handoff_enabled() -> bool:
    return bool(os.getenv("GITHUB_TOKEN", "").strip()
                and os.getenv("GITHUB_REPOSITORY", "").strip())


# --------------------------------------------------------------------------
# Pure duty logic (unit-test this first — doc §9.п9)
# --------------------------------------------------------------------------
def new_leader_doc(run_id: str) -> dict:
    now = _utc_str(_utc_now())
    return {"run_id": run_id, "ready": False,
            "heartbeat_utc": now, "watch_started_utc": now}


def leader_is_live(doc, now=None) -> bool:
    if not isinstance(doc, dict):
        return False
    hb = _parse_utc(doc.get("heartbeat_utc"))
    if hb is None:
        return False
    now = now or _utc_now()
    return (now - hb).total_seconds() < LEADER_STALE_SEC


def leader_should_yield(mine, remote, now=None) -> bool:
    """Yield only to a live + ready + strictly newer leader."""
    if not isinstance(remote, dict):
        return False
    if remote.get("run_id") == (mine or {}).get("run_id"):
        return False
    if not leader_is_live(remote, now):
        return False
    if not remote.get("ready"):
        return False
    rs = str(remote.get("watch_started_utc") or "")
    ms = str((mine or {}).get("watch_started_utc") or "")
    if rs != ms:
        return rs >= ms
    return str(remote.get("run_id") or "") > str((mine or {}).get("run_id") or "")


def standby_should_promote(remote, consec_stale_reads, now=None,
                           required=PROMOTE_CONFIRM_READS) -> bool:
    if not isinstance(remote, dict):
        return True
    if remote.get("retiring"):
        return True
    if not leader_is_live(remote, now):
        return consec_stale_reads >= required
    return False


def now_or_age(raw, now=None):
    ts = _parse_utc(raw)
    if ts is None:
        return None
    age = ((now or _utc_now()) - ts).total_seconds()
    return age if age > 0 else None


def work_is_wedged(work_doc, leader_doc, now=None,
                   threshold_sec=WORK_STALE_SEC) -> bool:
    if not isinstance(work_doc, dict) or not isinstance(leader_doc, dict):
        return False
    if work_doc.get("run_id") != leader_doc.get("run_id"):
        return False
    age = now_or_age(work_doc.get("last_work_utc"), now)
    if age is None:
        return False
    return age > threshold_sec


def leader_watch_seconds(watch_remaining, watch_seconds) -> float:
    """Fresh leaders take the full watch; promoted ones inherit the remainder."""
    if watch_remaining is None:
        return watch_seconds
    return max(0.0, watch_remaining)


def duty_gap_info(last_hb_utc, now=None, threshold_sec=GAP_WARN_SEC):
    hb = _parse_utc(last_hb_utc)
    if hb is None:
        return None, False
    gap = ((now or _utc_now()) - hb).total_seconds()
    if gap <= 0:
        return None, False
    return gap, gap > threshold_sec


# --------------------------------------------------------------------------
# State merge — UNION, never intersection (doc §1.3)
# --------------------------------------------------------------------------
def _as_dict(value):
    return value if isinstance(value, dict) else {}


def merge_marks(local_marks, remote_marks):
    merged = dict(remote_marks or {})
    for k, v in (local_marks or {}).items():
        if k not in merged or str(v) >= str(merged[k]):
            merged[k] = v
    return merged


def merge_counters(local_counters, remote_counters):
    merged = {k: dict(v) if isinstance(v, dict) else {"n": v}
              for k, v in (remote_counters or {}).items()}
    for k, v in (local_counters or {}).items():
        v = dict(v) if isinstance(v, dict) else {"n": v}
        cur = merged.get(k)
        cur_n = cur.get("n", 0) if isinstance(cur, dict) else 0
        try:
            new_n = int(v.get("n", 0))
        except (TypeError, ValueError):
            new_n = 0
        if k not in merged or new_n >= cur_n:
            merged[k] = v
    return merged


def merge_states(local, remote):
    merged = dict(local or {})
    remote = remote or {}
    merged["marks"] = merge_marks(_as_dict((local or {}).get("marks")),
                                  _as_dict(remote.get("marks")))
    merged["error_counters"] = merge_counters(
        _as_dict((local or {}).get("error_counters")),
        _as_dict(remote.get("error_counters")))
    if str(remote.get("last_run_utc") or "") > str(merged.get("last_run_utc") or ""):
        merged["last_run_utc"] = remote["last_run_utc"]
    return merged


def prune_state(state, now=None):
    """Drop counters older than STATE_RETENTION_DAYS (marks are history)."""
    now = now or _utc_now()
    counters = _as_dict(state.get("error_counters"))
    keep = {}
    for k, v in counters.items():
        last = _parse_utc(v.get("last_utc")) if isinstance(v, dict) else None
        if last is None or (now - last).days <= STATE_RETENTION_DAYS:
            keep[k] = v
    state["error_counters"] = keep
    return state


# --------------------------------------------------------------------------
# Contents API transport (shared persistent spot — doc §1.1)
# --------------------------------------------------------------------------
def _gh_contents_url(path: str) -> str:
    api = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    return f"{api}/repos/{repo}/contents/{path}"


def _gh_headers():
    import base64  # local import: keep module import-safe

    _ = base64  # placeholder to keep structure parallel with ChatSum
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN', '').strip()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_state_file_get(path: str):
    """Read a file from the ``state`` branch: (doc|None, sha|None)."""
    import base64

    if not handoff_enabled():
        return None, None
    url = _gh_contents_url(path) + "?ref=state"
    req = urllib.request.Request(url, headers=_gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        print(f"⚠️ GET {path}: HTTP {e.code}")
        return None, None
    except Exception as e:
        print(f"⚠️ GET {path}: {e}")
        return None, None
    try:
        text = base64.b64decode(payload.get("content", "")).decode("utf-8")
        return json.loads(text), payload.get("sha")
    except Exception:
        return None, payload.get("sha")


def gh_state_file_put(path: str, text: str, sha=None, message=None) -> bool:
    """Write a file to the ``state`` branch (never prints the content)."""
    import base64

    if not handoff_enabled():
        return False
    body = {
        "message": message or f"duty: {my_run_id()}",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": "state",
    }
    if sha:
        body["sha"] = sha
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_gh_contents_url(path), data=data,
                                 headers={**_gh_headers(),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"⚠️ PUT {path}: HTTP {e.code}")
        return False
    except Exception as e:
        print(f"⚠️ PUT {path}: {e}")
        return False


def _flag_claim(flag_file: str, doc: dict, label: str) -> bool:
    _, sha = gh_state_file_get(flag_file)
    text = json.dumps(doc if isinstance(doc, dict) else {}, ensure_ascii=False)
    if gh_state_file_put(flag_file, text, sha,
                         message=f"{label}: {doc.get('run_id') if isinstance(doc, dict) else '?'}"):
        return True
    _, sha2 = gh_state_file_get(flag_file)
    return gh_state_file_put(
        flag_file, text, sha2,
        message=f"{label}: {doc.get('run_id') if isinstance(doc, dict) else '?'} (retry)")


def _flag_read(flag_file: str):
    doc, sha = gh_state_file_get(flag_file)
    return (doc if isinstance(doc, dict) else None), sha


def leader_read():
    return _flag_read(LEADER_FILE)


def leader_claim(doc):
    return _flag_claim(LEADER_FILE, doc, "leader")


def standby_read():
    return _flag_read(STANDBY_FILE)


def standby_claim(doc):
    return _flag_claim(STANDBY_FILE, doc, "standby")


def work_read():
    return _flag_read(WORK_FILE)


# --------------------------------------------------------------------------
# Local state file (lives in $RUNNER_TEMP — doc §3)
# --------------------------------------------------------------------------
def state_path() -> str:
    return (os.getenv("STATE_PATH", "").strip()
            or os.path.join(os.getcwd(), STATE_DEFAULT))


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def pull_state_best_effort(path: str) -> None:
    local = load_state(path)
    remote, _ = gh_state_file_get(STATE_DEFAULT)
    if not isinstance(remote, dict):
        return
    try:
        save_state(path, merge_states(local, remote))
    except Exception as e:
        print(f"⚠️ Could not save state after pull: {e}")


def push_state_best_effort(path: str) -> bool:
    local = load_state(path)
    if not local:
        return False
    remote, sha = gh_state_file_get(STATE_DEFAULT)
    text = json.dumps(merge_states(local, remote) if isinstance(remote, dict) else local,
                      ensure_ascii=False, indent=2)
    if gh_state_file_put(STATE_DEFAULT, text, sha, message=f"state: {my_run_id()}"):
        return _verify_push(path, local)
    remote2, sha2 = gh_state_file_get(STATE_DEFAULT)
    text2 = json.dumps(merge_states(local, remote2) if isinstance(remote2, dict) else local,
                       ensure_ascii=False, indent=2)
    if gh_state_file_put(STATE_DEFAULT, text2, sha2,
                         message=f"state: {my_run_id()} (retry)"):
        return _verify_push(path, local)
    return False


def _verify_push(path: str, local: dict) -> bool:
    want = set(_as_dict(local.get("marks")).keys())
    if not want:
        return True
    remote, _ = gh_state_file_get(STATE_DEFAULT)
    if isinstance(remote, dict):
        have = set(_as_dict(remote.get("marks")).keys())
        if want <= have:
            return True
        missing = sorted(want - have)
        print(f"🚨 State push did not stick (missing {missing}) — retrying PUT")
        merged = json.dumps(merge_states(local, remote), ensure_ascii=False, indent=2)
        _, sha = gh_state_file_get(STATE_DEFAULT)
        if gh_state_file_put(STATE_DEFAULT, merged, sha,
                             message=f"state: {my_run_id()} (verify-retry)"):
            return True
        print("🚨 State push: retry unconfirmed — marks stay local, "
              "the trailing Persist step carries them")
    else:
        print("🚨 State push: could not re-read the branch for verification")
    return False


# --------------------------------------------------------------------------
# Trading seam — parse wiring for the §10 ladder (read-only steps 1–2).
# Full trading reconcile lands later; placements stay forbidden here.
# --------------------------------------------------------------------------
async def run_trading_iteration(state: dict) -> None:
    """One read-only parse pass over all wallets (doc §5 minus placements)."""
    from modules.duty_parse import run_parse_iteration  # lazy: keeps import-safe

    await run_parse_iteration(state)


def note_work_progress(path: str, run_id: str, state: dict) -> None:
    """Маркер «работа движется» в общий state (единый источник истины в цикле).

    Пишет в переданный словарь, а не перечитывает диск: иначе сохранённые
    метки разбора затирались бы на каждой итерации (§1.3 — только надмножество).
    """
    try:
        state.setdefault("_work", {}).update(
            {"run_id": run_id, "last_work_utc": _utc_str(_utc_now())})
        save_state(path, state)
    except Exception as e:
        print(f"⚠️ Work marker: {e}")


async def _heartbeat_loop(me: dict, path: str, deadline_utc) -> None:
    """Background heartbeat every LEADER_HEARTBEAT_SEC (best-effort).

    Separate task ON PURPOSE: an iteration-bound heartbeat froze during long
    tasks and standby declared a live leader dead (ChatSum 2026-10-04).
    Also drives the duty beacon (§1.5): edit every 5th tick.
    """
    try:
        while True:
            await asyncio.sleep(LEADER_HEARTBEAT_SEC)
            me["heartbeat_utc"] = _utc_str(_utc_now())
            leader_claim(me)
            push_state_best_effort(path)
            try:
                from modules import beacon  # lazy: keeps duty import-safe

                await beacon.leader_beacon_tick(deadline_utc)
            except Exception as e:
                print(f"⚠️ Beacon: {e}")
    except asyncio.CancelledError:
        pass


async def _run_leader_loop(state: dict, path: str, me: dict, poll: int,
                           watch_remaining=None, watch_seconds=None) -> int:
    iteration = 0
    if watch_seconds is None:
        watch_seconds = float(os.getenv("WATCH_SECONDS", WATCH_DEFAULT_SEC))
    watch_seconds = leader_watch_seconds(watch_remaining, watch_seconds)
    deadline = time.monotonic() + watch_seconds
    deadline_utc = _utc_now() + timedelta(seconds=watch_seconds)
    hb_task = None
    if handoff_enabled():
        hb_task = asyncio.create_task(_heartbeat_loop(me, path, deadline_utc))
    try:
        print(f"shift start: run {me['run_id']}")
        while True:
            iteration += 1
            note_work_progress(path, me["run_id"], state)
            if handoff_enabled():
                # Work marker to the branch: fresh heartbeat + stale marker
                # of the SAME run_id is the wedge signal (§1.2).
                _flag_claim(WORK_FILE,
                            {"run_id": me["run_id"],
                             "last_work_utc": _utc_str(_utc_now())},
                            "work")
                remote, _ = leader_read()
                if leader_should_yield(me, remote):
                    print(f"👑 Newer leader {remote.get('run_id')} — yielding.")
                    break
            if time.monotonic() >= deadline:
                me["retiring"] = True
                if handoff_enabled():
                    leader_claim(me)
                print("👑 retiring: watch over, yielding to next run.")
                break
            try:
                await run_trading_iteration(state)
            except Exception as e:
                print(f"⚠️ Iteration {iteration} skipped: {e}")
            push_state_best_effort(path)
            await asyncio.sleep(poll)
        print(f"shift complete: iterations {iteration}")
        return 0
    finally:
        if hb_task is not None:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass


async def _run_standby_loop(state: dict, path: str, me: dict, poll: int,
                            watch_seconds: float) -> int:
    me["ready"] = True
    me["heartbeat_utc"] = _utc_str(_utc_now())
    standby_claim(me)
    print(f"🛡️ Standby {me['run_id']}: watching the leader, no trading.")
    stale_reads = 0
    wedge_reads = 0
    try:
        deadline = time.monotonic() + watch_seconds
        while True:
            if time.monotonic() >= deadline:
                print("🛡️ Standby: deadline — exiting, leader lives without me.")
                break
            await asyncio.sleep(min(poll, max(1.0, deadline - time.monotonic())))
            # Own heartbeat to the shared standby file: the freshest standby
            # is the one that takes over (§1.2); gap diagnosis reads it (§1.4).
            me["heartbeat_utc"] = _utc_str(_utc_now())
            standby_claim(me)
            sremote, _ = standby_read()
            if leader_should_yield(me, sremote):
                print(f"🛡️ Newer standby {sremote.get('run_id')} — exiting.")
                break
            remote, _ = leader_read()
            takeover_why = None
            if isinstance(remote, dict) and not remote.get("retiring") \
                    and leader_is_live(remote):
                stale_reads = 0
                try:
                    wdoc, _ = work_read()
                except Exception:
                    wdoc = None
                if work_is_wedged(wdoc, remote):
                    wedge_reads += 1
                    if wedge_reads >= PROMOTE_CONFIRM_READS:
                        takeover_why = "wedged (heartbeat fresh, no progress)"
                else:
                    wedge_reads = 0
            else:
                wedge_reads = 0
                if not isinstance(remote, dict) or remote.get("retiring"):
                    stale_reads = PROMOTE_CONFIRM_READS
                else:
                    stale_reads += 1
                if standby_should_promote(remote, stale_reads):
                    takeover_why = "retiring" if isinstance(remote, dict) \
                        and remote.get("retiring") else "dead"
            if takeover_why:
                print(f"🛡️ Leader {takeover_why} — taking over.")
                try:
                    # Probe BEFORE leading: a run that cannot reach the
                    # exchange must never look like a leader (§1.2).
                    await run_trading_iteration(state)
                except Exception as e:
                    print(f"🛡️ Probe failed ({e}) — staying standby.")
                    stale_reads = 0
                    wedge_reads = 0
                    continue
                me["ready"] = True
                leader_claim(me)
                remaining = max(0.0, deadline - time.monotonic())
                return await _run_leader_loop(state, path, me, poll, remaining,
                                              watch_seconds=watch_seconds)
    finally:
        pull_state_best_effort(path)
        try:
            save_state(path, state)
        except Exception as e:
            print(f"⚠️ Standby cleanup save: {e}")
    return 0


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Hype duty skeleton (doc §1).")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--poll-interval", type=int, default=POLL_DEFAULT_SEC)
    p.add_argument("--watch-seconds", type=float, default=WATCH_DEFAULT_SEC)
    p.add_argument("--persist", action="store_true",
                   help="Final save helper (the workflow Persist step owns this).")
    return p.parse_args(argv)


async def _report_duty_gap(my_run_id_: str) -> None:
    """Самодиагностика зазоров (§1.4): тишина дольше GAP_WARN_SEC — владельцу."""
    try:
        last_hb = None
        for reader in (leader_read, standby_read):
            try:
                doc, _ = reader()
            except Exception:
                continue
            if isinstance(doc, dict) and doc.get("run_id") != my_run_id_:
                hb = _parse_utc(doc.get("heartbeat_utc"))
                if hb is not None and (last_hb is None or hb > last_hb):
                    last_hb = hb
        if last_hb is None:
            return
        gap = (_utc_now() - last_hb).total_seconds()
        if gap <= 0:
            return
        if gap > GAP_WARN_SEC:
            msg = (f"⚠️ Duty gap: no heartbeat for {gap:.0f}s "
                   f"(last {last_hb.strftime('%d.%m %H:%M')} UTC)")
            print(msg)
            try:
                from modules import beacon  # lazy: keeps duty import-safe

                await beacon.warn_owner(msg)
            except Exception as e:
                print(f"⚠️ Gap warning not sent: {e}")
        else:
            print(f"Duty silence {gap:.0f}s (below warn threshold).")
    except Exception as e:
        print(f"⚠️ Gap diagnosis: {e}")


async def _amain(args) -> int:
    path = state_path()
    state = load_state(path)
    me = new_leader_doc(my_run_id())
    poll = max(1, args.poll_interval)
    if args.persist:
        pull_state_best_effort(path)
        return 0
    if not handoff_enabled():
        print("👑 Handoff off (no GITHUB_TOKEN/GITHUB_REPOSITORY) — solo mode.")
        me["ready"] = True
        return await _run_leader_loop(state, path, me, poll, None,
                                      watch_seconds=args.watch_seconds)
    await _report_duty_gap(me["run_id"])
    remote, _ = leader_read()
    if (isinstance(remote, dict) and remote.get("run_id") != me["run_id"]
            and not remote.get("retiring") and leader_is_live(remote)):
        print(f"👑 Leader {remote.get('run_id')} is live — becoming standby.")
        return await _run_standby_loop(state, path, me, poll, args.watch_seconds)
    # Claim unready first; ready flips only after exchange + first parse (§1.2).
    leader_claim(me)
    try:
        await run_trading_iteration(state)
    except Exception as e:
        print(f"❌ First parse failed ({e}) — not becoming leader.")
        return 1
    me["ready"] = True
    leader_claim(me)
    push_state_best_effort(path)
    return await _run_leader_loop(state, path, me, poll, None,
                                  watch_seconds=args.watch_seconds)


def main(argv=None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
