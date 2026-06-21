#!/usr/bin/env bash
set -euo pipefail

config="/boot/firmware/config.txt"
backup="${config}.bak.$(date +%Y%m%d-%H%M%S)"

if [ ! -f "$config" ]; then
  echo "Missing $config"
  exit 1
fi

sudo cp "$config" "$backup"

tmp="$(mktemp)"
awk '
  /^camera_auto_detect=0/ {
    print "camera_auto_detect=1"
    next
  }
  /^dtoverlay=(imx|ov)[[:alnum:]_,-]*$/ {
    print "# " $0 " # disabled by ChessV2 autodetect helper"
    next
  }
  { print }
' "$config" > "$tmp"

sudo install -m 0755 "$tmp" "$config"
rm -f "$tmp"

echo "Updated $config"
echo "Backup saved to $backup"
echo "Reboot the Pi for camera config changes to take effect:"
echo "  sudo reboot"

