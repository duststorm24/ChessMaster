#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "== Service =="
systemctl is-enabled chessv2-camera.service || true
systemctl is-active chessv2-camera.service || true
echo

./check_camera.sh

echo "== Web UI =="
curl -sS --max-time 8 http://127.0.0.1:8000/health || true
echo
echo
ip="$(hostname -I | awk '{print $1}')"
echo "Open http://${ip:-chesspi.local}:8000 from your laptop."
