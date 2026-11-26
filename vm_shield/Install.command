#!/bin/bash
# Double-click inside the dedicated macOS guest: proxy network only.
set -Eeuo pipefail
case "$(/usr/sbin/sysctl -n hw.model)" in
  VirtualMac*) ;;
  *) echo 'Open this installer inside the dedicated macOS VM.' >&2; exit 1 ;;
esac
/sbin/ifconfig en0 | /usr/bin/grep -q '52:54:00:77:00:03' || {
  echo 'Expected private VM interface not found.' >&2; exit 1;
}
media_dir="$(cd "$(dirname "$0")" && pwd)"
echo 'This installs only the dedicated proxy network. It does not install or start clients.'
echo 'Enter the password for your macOS VM account when prompted.'
/usr/bin/sudo /bin/bash "$media_dir/install-in-guest.sh"
