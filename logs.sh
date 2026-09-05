#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAIL_LINES=50
FOLLOW=0
ERR_LOG=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --err|-e)
      ERR_LOG=1
      shift
      ;;
    --follow|-f)
      FOLLOW=1
      shift
      ;;
    --tail|-n)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for $1"
        exit 1
      fi
      TAIL_LINES="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: ./logs.sh [--err|-e] [--follow|-f] [--tail|-n N]"
      exit 1
      ;;
  esac
done

cd "$ROOT_DIR"

if [[ "$ERR_LOG" -eq 1 ]]; then
  LOG_FILE="$ROOT_DIR/bot.err.log"
else
  LOG_FILE="$ROOT_DIR/bot.log"
fi

if [[ ! -f "$LOG_FILE" ]]; then
  echo "Log file not found: $LOG_FILE"
  exit 1
fi

if [[ "$FOLLOW" -eq 1 ]]; then
  exec tail -n "$TAIL_LINES" -f "$LOG_FILE"
fi

tail -n "$TAIL_LINES" "$LOG_FILE"
