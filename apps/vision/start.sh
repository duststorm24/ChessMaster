#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if systemctl list-unit-files chessv2-camera.service >/dev/null 2>&1; then
  sudo systemctl restart chessv2-camera.service
  ip="$(hostname -I | awk '{print $1}')"
  echo "Camera UI running at http://${ip:-chesspi.local}:8000"
  exit 0
fi

if [ -f app.pid ]; then
  old_pid="$(cat app.pid)"
  if kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid" 2>/dev/null || true
    sleep 1
  fi
fi

nohup python3 app.py > app.log 2>&1 &
echo "$!" > app.pid
sleep 1

pid="$(cat app.pid)"
if ! kill -0 "$pid" 2>/dev/null; then
  tail -n 40 app.log
  exit 1
fi

ip="$(hostname -I | awk '{print $1}')"
echo "Camera UI running at http://${ip:-chesspi.local}:8000"
