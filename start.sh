#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT_DIR/bot.pid"
STDOUT_LOG="$ROOT_DIR/bot.log"
STDERR_LOG="$ROOT_DIR/bot.err.log"
VENV_DIR="$ROOT_DIR/.venv"

FOREGROUND=0
INSTALL_DEPS=0

for arg in "$@"; do
  case "$arg" in
    --foreground|-f)
      FOREGROUND=1
      ;;
    --install)
      INSTALL_DEPS=1
      ;;
    *)
      echo "Unknown argument: $arg"
      echo "Usage: ./start.sh [--foreground|-f] [--install]"
      exit 1
      ;;
  esac
done

cd "$ROOT_DIR"

is_running() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 1
  fi

  local pid
  pid="$(tr -d '[:space:]' < "$PID_FILE")"
  if [[ -z "$pid" ]]; then
    rm -f "$PID_FILE"
    return 1
  fi

  if kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  rm -f "$PID_FILE"
  return 1
}

ensure_python() {
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
    return
  fi
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
    return
  fi

  echo "Python 3 was not found. Please install Python 3 first."
  exit 1
}

setup_venv() {
  if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    INSTALL_DEPS=1
  fi

  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"

  if [[ "$INSTALL_DEPS" -eq 1 ]]; then
    python -m pip install --upgrade pip
    python -m pip install \
      discord.py \
      python-dotenv \
      aiohttp \
      certifi \
      yt-dlp \
      f2 \
      imageio \
      imageio-ffmpeg \
      pillow \
      pypdf \
      pypdfium2 \
      pynacl \
      davey \
      tzdata
  fi
}

warn_runtime_deps() {
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[WARN] ffmpeg was not found in PATH. Music playback may fail."
  fi
}

ensure_python
setup_venv
warn_runtime_deps

if is_running; then
  echo "Bot is already running. PID: $(cat "$PID_FILE")"
  echo "Log file: $STDOUT_LOG"
  exit 0
fi

if [[ "$FOREGROUND" -eq 1 ]]; then
  echo "Starting bot in foreground..."
  exec python -u bot.py
fi

rm -f "$STDOUT_LOG" "$STDERR_LOG"
nohup python -u bot.py >"$STDOUT_LOG" 2>"$STDERR_LOG" &
BOT_PID=$!
echo "$BOT_PID" > "$PID_FILE"

sleep 2
if ! kill -0 "$BOT_PID" 2>/dev/null; then
  echo "Bot failed to start. Check $STDERR_LOG"
  rm -f "$PID_FILE"
  exit 1
fi

echo "Bot started. PID: $BOT_PID"
echo "Log file: $STDOUT_LOG"
