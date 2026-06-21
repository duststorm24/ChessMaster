#!/usr/bin/env bash
set -euo pipefail

sudo systemctl disable --now chessv2-camera.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/chessv2-camera.service
sudo systemctl daemon-reload

echo "Removed chessv2-camera.service"

