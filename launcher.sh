#!/bin/zsh
set -euo pipefail

REPO_DIR="/Users/paolofuresi/Pollo-Studio/Claude/token-dashboard"
PYTHON_BIN="$(command -v python3)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
URL="http://${HOST}:${PORT}/"
LOG_FILE="$HOME/.claude/token-dashboard.log"
PID_FILE="$HOME/.claude/token-dashboard.pid"

mkdir -p "$HOME/.claude"

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    open -a Safari "$URL"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if pgrep -f "$REPO_DIR/cli.py dashboard" >/dev/null 2>&1; then
  open -a Safari "$URL"
  exit 0
fi

cd "$REPO_DIR"
open -a Safari "$URL"

child_pid=""

cleanup() {
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
}

trap cleanup EXIT INT TERM HUP

"$PYTHON_BIN" "$REPO_DIR/cli.py" dashboard --no-open >> "$LOG_FILE" 2>&1 &
child_pid="$!"
echo "$child_pid" > "$PID_FILE"
wait "$child_pid"
