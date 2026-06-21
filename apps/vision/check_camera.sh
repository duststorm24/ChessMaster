#!/usr/bin/env bash
set -euo pipefail

echo "== Host =="
hostname
uptime
echo

echo "== rpicam cameras =="
rpicam-hello --list-cameras || true
echo

echo "== Camera config lines =="
grep -nEi 'camera|dtoverlay|imx|ov|start_x' /boot/firmware/config.txt /boot/config.txt 2>/dev/null || true
echo

if grep -q '^camera_auto_detect=0' /boot/firmware/config.txt 2>/dev/null && grep -qE '^dtoverlay=(imx|ov)' /boot/firmware/config.txt 2>/dev/null; then
  echo "== Config recommendation =="
  echo "Camera auto-detection is disabled and a sensor overlay is forced."
  echo "If the camera is not detected after plugging it in, run:"
  echo "  cd ~/ChessMaster/apps/vision && ./set_camera_autodetect.sh && sudo reboot"
  echo
fi

echo "== Camera device nodes =="
ls -l /dev/video* /dev/media* 2>/dev/null || true
echo

echo "== Recent camera kernel logs =="
dmesg | grep -Ei 'imx|ov|unicam|csi|camera|libcamera' | tail -n 80 || true
echo

echo "== Web app health =="
curl -sS --max-time 5 http://127.0.0.1:8000/health || true
echo
