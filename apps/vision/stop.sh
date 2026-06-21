#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if systemctl is-active --quiet chessv2-camera.service 2>/dev/null; then
  sudo systemctl stop chessv2-camera.service
  rm -f app.pid
  echo "Stopped chessv2-camera.service."
  exit 0
fi

if [ ! -f app.pid ]; then
  echo "Camera UI is not running."
  exit 0
fi

pid="$(cat app.pid)"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "Stopped Camera UI process $pid."
else
  echo "Camera UI process $pid is not running."
fi

rm -f app.pid
