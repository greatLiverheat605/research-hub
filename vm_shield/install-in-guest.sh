#!/bin/bash
# Proxy-network installation only, inside the dedicated macOS VM.
# Client installation and first contact require a separate user-approved step.
set -Eeuo pipefail
case "$(/usr/sbin/sysctl -n hw.model)" in
  VirtualMac*) ;;
  *) echo 'This installer only runs inside the dedicated macOS VM.' >&2; exit 1 ;;
esac
/sbin/ifconfig en0 | /usr/bin/grep -q '52:54:00:77:00:03' || {
  echo 'Expected private VM interface not found; refusing installation.' >&2; exit 1;
}
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo inside the VM.' >&2; exit 1; }
umask 077
cs_script_dir="$(cd "$(dirname "$0")" && pwd)"
[ -f "$cs_script_dir/guest-network-common.sh" ] && [ ! -L "$cs_script_dir/guest-network-common.sh" ] || {
  echo 'The approved guest transaction helper is missing.' >&2; exit 1;
}
source "$cs_script_dir/guest-network-common.sh"
[ ! -e "$cs_daemon" ] && [ ! -L "$cs_daemon" ] &&
  [ ! -e "$cs_config" ] && [ ! -L "$cs_config" ] &&
  [ ! -e "$cs_core/mihomo" ] && [ ! -L "$cs_core/mihomo" ] || {
  echo 'A guest installation already exists; use the reviewed repair transaction.' >&2; exit 1;
}
cs_begin
[ "$(cs_check field "$cs_backup/network-state.json" service_loaded)" = false ] || {
  echo 'A launch service is already loaded; no installation was performed.' >&2; exit 1;
}
/bin/cp "$cs_backup/payload/mihomo-residential.yaml" "$cs_backup/candidate.yaml"
/bin/chmod 600 "$cs_backup/candidate.yaml"
/bin/chmod 700 "$cs_backup/payload/verge-mihomo"
cs_validate_candidate "$cs_backup/payload/verge-mihomo"
cs_commit true
