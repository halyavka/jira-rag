#!/usr/bin/env bash
# Safety-net incremental sync for cron / systemd timer.
# Catches anything the webhook missed (network blips, restarts, event drops).
set -euo pipefail

cd "$(dirname "$0")/.."

# Load .env if present (keep secrets out of crontab env).
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi

# Prefer the project's virtualenv if it exists; fall back to system python.
if [ -x .venv/bin/jira-rag ]; then
    exec .venv/bin/jira-rag sync "$@"
elif command -v jira-rag >/dev/null 2>&1; then
    exec jira-rag sync "$@"
else
    exec python -m jira_rag sync "$@"
fi
