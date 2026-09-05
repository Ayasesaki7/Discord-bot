#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT_DIR/bot.pid"

cd "$ROOT_DIR"

if [[ ! -f "$PID_FILE" ]]; then
  echo "bot.pid was not found. The bot does not look like it is running in background."
  exit 0
fi

PID_VALUE="$(tr -d '[:space:]' < "$PID_FILE")"
if [[ -z "$PID_VALUE" ]]; then
  rm -f "$PID_FILE"
  echo "bot.pid was empty and has been removed."
  exit 0
fi

if kill -0 "$PID_VALUE" 2>/dev/null; then
  kill "$PID_VALUE" 2>/dev/null || true
  sleep 2
  if kill -0 "$PID_VALUE" 2>/dev/null; then
    kill -9 "$PID_VALUE" 2>/dev/null || true
  fi
  echo "Bot stopped. PID: $PID_VALUE"
else
  echo "Process $PID_VALUE is not running anymore. bot.pid has been cleaned up."
fi

rm -f "$PID_FILE"
