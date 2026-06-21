#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
app_dir="$(pwd)"
app_user="$(id -un)"

if [ -f app.pid ]; then
  old_pid="$(cat app.pid)"
  kill "$old_pid" 2>/dev/null || true
  rm -f app.pid
fi

tmp_service="$(mktemp)"
cat > "$tmp_service" <<SERVICE
[Unit]
Description=Chess vision camera web UI
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${app_user}
WorkingDirectory=${app_dir}
Environment=HOST=0.0.0.0
Environment=PORT=8000
ExecStart=/usr/bin/python3 ${app_dir}/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

sudo install -m 0644 "$tmp_service" /etc/systemd/system/chessv2-camera.service
rm -f "$tmp_service"
sudo systemctl daemon-reload
sudo systemctl enable --now chessv2-camera.service

ip="$(hostname -I | awk '{print $1}')"
echo "Installed chessv2-camera.service"
echo "Camera UI running at http://${ip:-chesspi.local}:8000"
sudo systemctl --no-pager --lines=8 status chessv2-camera.service
