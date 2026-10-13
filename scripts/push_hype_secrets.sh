#!/usr/bin/env bash
# Pushes GitHub Secrets for the hype duty workflow from local files.
# Repo is detected from git remote (fork-friendly), overridable via --repo.
#
#   ./scripts/push_hype_secrets.sh [--repo OWNER/REPO] [--dry-run]
#
# Sources (all gitignored, never committed):
#   input_data/wallets.txt        label|wallet_address|private_key|proxy (or NONE)
#   input_data/tg_bot_tokens.txt  KEY = 'value' lines (TG_BOT_TOKEN, ...)
#   DB password: env DB_ENCRYPTION_PASSWORD, else hidden prompt.
# Secret VALUES ARE NEVER PRINTED (only names and lengths).
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root (this script lives in scripts/)

REPO=""
DRY_RUN=0

usage() {
  echo "Usage: $0 [--repo OWNER/REPO] [--dry-run]"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:?need OWNER/REPO}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "❌ Unknown argument: $1"; usage; exit 2 ;;
  esac
done

if ! command -v gh >/dev/null; then
  echo "❌ gh not found. Install: https://cli.github.com/ , then: gh auth login"
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "❌ gh not authorized. Run: gh auth login"
  exit 1
fi

if [ -z "$REPO" ]; then
  REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  if [ -z "$REPO" ]; then
    origin_url="$(git remote get-url origin 2>/dev/null || true)"
    REPO="$(printf '%s' "$origin_url" | sed -E -e 's#^https?://[^/]*@#https://#' -e 's#^(https?://github.com/|git@github.com:)##' -e 's#\.git$##')"
    case "$REPO" in
      *:*|*@*) REPO="" ;;
      */*) ;;
      *) REPO="" ;;
    esac
  fi
  if [ -z "$REPO" ]; then
    echo "❌ Could not detect repository. Pass: $0 --repo OWNER/REPO"
    exit 1
  fi
fi
echo "Repository: $REPO"

# Placeholder detector (example values instead of real ones)?
is_placeholder() {
  case "$1" in
    ""|*REPLACE*|*ЗАМЕНИТЕ*|*CHANGEME*|*your_*|*Example*|*"'"*|*"<"*|*">"* ) return 0 ;;
  esac
  return 1
}

gh_set_secret() {  # name value required(1/0)
  local name="$1" value="$2" required="$3"
  if is_placeholder "$value"; then
    if [ "$required" = 1 ]; then
      echo "❌ $name missing (empty or placeholder). Fill it and retry."
      exit 1
    fi
    echo "⏭️  $name skipped (unset, optional)"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    echo "DRY secret   $name (length ${#value})"
  else
    gh secret set "$name" --repo "$REPO" --body "$value" >/dev/null
    echo "✅ secret   $name (length ${#value})"
  fi
}

# --- WALLETS_DATA: build JSON array from input_data/wallets.txt via python3
# (avoids shell quoting pitfalls; values never touch stdout) ---
if [ ! -f input_data/wallets.txt ]; then
  echo "❌ No input_data/wallets.txt. Copy the example and fill it:"
  echo "   cp input_data/wallets.txt.example input_data/wallets.txt"
  exit 1
fi
WALLETS_DATA="$(python3 - input_data/wallets.txt <<'EOF'
import json, sys
out = []
with open(sys.argv[1], encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        label, addr, key = parts[0], parts[1], parts[2]
        proxy = parts[3] if len(parts) > 3 else "NONE"
        if proxy.upper() == "NONE":
            proxy = None
        out.append({"label": label, "wallet_address": addr,
                    "private_key": key, "proxy": proxy})
print(json.dumps(out, ensure_ascii=False))
EOF
)"
if [ "$WALLETS_DATA" = "[]" ] || [ -z "$WALLETS_DATA" ]; then
  echo "❌ No wallets parsed from input_data/wallets.txt (only comments?)."
  exit 1
fi
WALLET_COUNT="$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$WALLETS_DATA")"
echo "Wallets parsed: $WALLET_COUNT (values hidden)"

# --- TG tokens from input_data/tg_bot_tokens.txt (KEY = 'value' / [..]) ---
get_tg() {  # KEY → value without quotes
  grep -E "^$1[[:space:]]*=" input_data/tg_bot_tokens.txt 2>/dev/null | tail -1 \
    | cut -d= -f2- | tr -d '\r' | sed "s/^[[:space:]]*//;s/[[:space:]]*\$//;s/^'//;s/'\$//;s/^\[//;s/\]\$//" || true
}
TG_BOT_TOKEN="$(get_tg TG_BOT_TOKEN)"
PROFIT_BOT_TOKEN="$(get_tg PROFIT_BOT_TOKEN)"
TG_USER_ID="$(get_tg TG_USER_ID | cut -d, -f1 | tr -d ' ')"

# --- DB password: env first, else hidden prompt ---
DB_PASSWORD="${DB_ENCRYPTION_PASSWORD:-}"
if is_placeholder "$DB_PASSWORD"; then
  if [ "$DRY_RUN" = 1 ]; then
    DB_PASSWORD="dry-run-fake-password"
    echo "DB password: placeholder (dry-run)"
  else
    echo "Enter DB_ENCRYPTION_PASSWORD (hidden input):"
    read -rs DB_PASSWORD < /dev/tty
    echo ""
  fi
fi

echo "--- Secrets ---"
# WALLETS_DATA via temp file to dodge arg-length limits; size guard 48 KB.
if [ "$DRY_RUN" = 1 ]; then
  echo "DRY secret   WALLETS_DATA ($WALLET_COUNT wallets, $(printf '%s' "$WALLETS_DATA" | wc -c) bytes, values hidden)"
else
  if [ "$(printf '%s' "$WALLETS_DATA" | wc -c)" -gt 49152 ]; then
    echo "❌ WALLETS_DATA exceeds the 48 KB secret limit — split into WALLETS_DATA_1..N (see docs/GithubActions.md §8)."
    exit 1
  fi
  tmp="$(mktemp)"; printf '%s' "$WALLETS_DATA" > "$tmp"
  gh secret set WALLETS_DATA --repo "$REPO" < "$tmp" >/dev/null; rm -f "$tmp"
  echo "✅ secret   WALLETS_DATA ($WALLET_COUNT wallets)"
fi
gh_set_secret DB_ENCRYPTION_PASSWORD "$DB_PASSWORD" 1
gh_set_secret TG_BOT_TOKEN "$TG_BOT_TOKEN" 0
gh_set_secret PROFIT_BOT_TOKEN "$PROFIT_BOT_TOKEN" 0
gh_set_secret TG_USER_ID "$TG_USER_ID" 0

echo ""
if [ "$DRY_RUN" = 1 ]; then
  echo "DRY-RUN: nothing written. Drop --dry-run to write for real."
else
  echo "Done. Next: docs/GithubActions.md §10 ladder (manual Parse run first)."
fi
