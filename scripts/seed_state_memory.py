#!/usr/bin/env python3
"""Одноразовый посев невосстанавливаемой памяти дежурства в ветку `state`.

Переносит из локальных `databases/` в `state.json` ветки `state`:

  * ``hold_benchmark_anchor`` -> ``hold_anchor``  (снимок первой позиции;
    с биржи НЕ берётся — точка во времени);
  * ``last_buy_timestamps``   -> ``last_buy_ts``   (окно для скана сирот поверх
    лимита биржи ~500 сделок).

ВНИМАНИЕ: репозиторий public, ветка `state` публична — значения (в т.ч.
``depo_start`` ≈ размер счёта) станут видны всем. Запускать осознанно.

Значения НЕ печатаются — только имена ключей и количества.

  ./venv/bin/python scripts/seed_state_memory.py [--repo OWNER/REPO] [--dry-run]
"""

import argparse
import base64
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules.duty import merge_anchors, merge_buy_ts  # noqa: E402

STATE_FILE = "state.json"
STATS_DB = "databases/stats.json"
BUY_TS_DB = "databases/last_buy_timestamps.json"
HOLD_ANCHOR_KEY = "hold_benchmark_anchor"


def gh(*args, input_text=None):
    proc = subprocess.run(["gh", *args], input=input_text,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def detect_repo():
    try:
        out = gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        out = out.strip()
        if "/" in out:
            return out
    except Exception:
        pass
    return None


def read_json_file(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"⚠️  {path}: {e}")
        return {}


def load_local_anchors():
    """Только per-label формат; legacy плоский (без label) пропускаем."""
    raw = read_json_file(STATS_DB)
    anchors = raw.get(HOLD_ANCHOR_KEY)
    if not isinstance(anchors, dict):
        return {}
    if "depo_start" in anchors and "price_start" in anchors:
        print("⚠️  stats.json: legacy flat anchor (no label) — skipped")
        return {}
    out = {}
    for label, entry in anchors.items():
        if isinstance(entry, dict) and entry.get("depo_start") and entry.get("price_start"):
            out[label] = {k: entry.get(k)
                          for k in ("depo_start", "price_start", "timestamp")}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="OWNER/REPO; по умолчанию из git remote")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    repo = args.repo or detect_repo()
    if not repo:
        print("❌ Could not detect repository. Pass --repo OWNER/REPO")
        return 1
    print(f"Repository: {repo}")

    local_anchors = load_local_anchors()
    local_ts = read_json_file(BUY_TS_DB)
    print(f"Local anchors: {len(local_anchors)} label(s) {sorted(local_anchors)}")
    print(f"Local buy-ts:  {len(local_ts)} key(s) {sorted(local_ts)}")

    try:
        out = gh("api", f"repos/{repo}/contents/{STATE_FILE}?ref=state")
        payload = json.loads(out)
        decoded = base64.b64decode(payload.get("content", "") or "")
        current = json.loads(decoded or b"{}")
        sha = payload.get("sha")
    except Exception:
        current, sha = {}, None
    if not isinstance(current, dict):
        current = {}

    merged = dict(current)
    merged["hold_anchor"] = merge_anchors(local_anchors, current.get("hold_anchor"))
    merged["last_buy_ts"] = merge_buy_ts(local_ts, current.get("last_buy_ts"))

    print(f"After merge: hold_anchor {len(merged['hold_anchor'])} label(s), "
          f"last_buy_ts {len(merged['last_buy_ts'])} key(s)")

    if args.dry_run:
        print("DRY-RUN: nothing written.")
        return 0

    text = json.dumps(merged, ensure_ascii=False, indent=2)
    body = {
        "message": "state: seed non-recoverable memory (hold_anchor, last_buy_ts)",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": "state",
    }
    if sha:
        body["sha"] = sha
    gh("api", "--method", "PUT", f"repos/{repo}/contents/{STATE_FILE}",
       "--input", "-", input_text=json.dumps(body))
    print("✅ Seeded. The next averaging watch restores both from the branch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
