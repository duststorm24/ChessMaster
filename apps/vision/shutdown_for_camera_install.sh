#!/usr/bin/env bash
set -euo pipefail

cat <<'TEXT'
Camera install shutdown checklist

1. Run this script with --confirm to shut the Pi down.
2. Wait until the Pi is fully powered off.
3. Unplug the USB-C power cable.
4. Connect the CSI ribbon to CAM/DISP0.
5. Reconnect power.
6. Wait for the Pi to boot, then run:

   ssh <pi-user>@chesspi.local
   cd ~/ChessMaster/apps/vision
   ./post_boot_camera_check.sh

TEXT

if [ "${1:-}" != "--confirm" ]; then
  echo "No shutdown performed."
  echo "Run './shutdown_for_camera_install.sh --confirm' when you are ready."
  exit 0
fi

echo "Stopping chess vision camera service before shutdown..."
sudo systemctl stop chessv2-camera.service 2>/dev/null || true

echo "Shutting down now. Wait for power-off before unplugging power."
sudo shutdown now
