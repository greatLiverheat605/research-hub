#!/bin/bash
# Shared transaction implementation. Entry scripts enforce the fixed VM guard.
# No environment-variable command overrides or identity bypasses are supported.

cs_core='/Library/Application Support/ClaudeShieldResidential'
cs_config="$cs_core/config.yaml"
cs_daemon='/Library/LaunchDaemons/local.claudeshield.residential.plist'
cs_label='system/local.claudeshield.residential'
cs_backup=''
cs_mutating=false
cs_lock_held=false

cs_fail() { echo "$1" >&2; return 1; }
cs_check() { /usr/bin/ruby "$cs_checker_path" "$@"; }

cs_release_lock() {
  if [ "$cs_lock_held" = true ]; then
    /bin/rm -f "$cs_core/.network-change-lock/pid"
    /bin/rmdir "$cs_core/.network-change-lock" || true
  fi
}

cs_observe_network() {
  local directory="$1"
  /bin/mkdir -p "$directory"
  /usr/sbin/networksetup -getinfo "$cs_service" > "$directory/netinfo.txt"
  /usr/sbin/networksetup -getdnsservers "$cs_service" > "$directory/dns.txt"
  /usr/sbin/networksetup -getMTU "$cs_service" > "$directory/mtu.txt"
  /usr/sbin/networksetup -getnetworkserviceenabled "$cs_service" > "$directory/enabled.txt"
}

cs_save_file() {
  local target="$1" key="$2"
  if [ -e "$target" ] || [ -L "$target" ]; then
    [ -f "$target" ] && [ ! -L "$target" ] || cs_fail 'A production path is not a regular file.'
    /bin/cp -p "$target" "$cs_backup/$key"
    /usr/bin/cmp -s "$target" "$cs_backup/$key"
    /usr/bin/touch "$cs_backup/$key.present"
  fi
}

cs_restore_file() {
  local target="$1" key="$2"
  if [ -f "$cs_backup/$key.present" ]; then
    /bin/cp -p "$cs_backup/$key" "$target.cs-restore.$$" &&
      /bin/mv -f "$target.cs-restore.$$" "$target"
  else
    /bin/rm -f "$target"
  fi
}

cs_service_loaded() {
  local output="$1" observation
  if observation=$(/bin/launchctl print "$cs_label" 2>&1); then
    /usr/bin/printf '%s\n' "$observation" | /usr/bin/awk '/^[[:space:]]*(state =|pid =|last exit code =)/' > "$output"
    return 0
  fi
  if /usr/bin/printf '%s\n' "$observation" | /usr/bin/grep -qi 'could not find service'; then
    echo 'service_loaded=false' > "$output"
    return 1
  fi
  echo 'service_state_observation_failed' > "$output"
  cs_fail 'Cannot establish the original launch service state.'
  return 2
}

cs_observe_disabled() {
  /bin/launchctl print-disabled system | /usr/bin/awk '/^[[:space:]]*"local[.]claudeshield[.]residential"[[:space:]]*=>/' > "$1"
}

cs_restore() {
  local failed=false loaded disabled current_result
  trap - ERR INT TERM HUP
  set +e
  echo "Guest transaction failed; restoring its saved settings. Backup: $cs_backup" >&2
  /bin/launchctl bootout "$cs_label" > "$cs_backup/rollback-bootout.txt" 2>&1
  current_result=$?
  if [ "$current_result" -ne 0 ]; then
    if cs_service_loaded "$cs_backup/rollback-service.txt"; then failed=true;
    else [ "$?" -eq 1 ] || failed=true; fi
  fi
  cs_restore_file "$cs_config" config.yaml || failed=true
  cs_restore_file "$cs_daemon" service.plist || failed=true
  cs_restore_file "$cs_core/mihomo" mihomo || failed=true
  cs_restore_file "$cs_core/guest-config-check.rb" guest-config-check.rb || failed=true
  cs_restore_file "$cs_core/guest-network-common.sh" guest-network-common.sh || failed=true
  cs_check restore-network "$cs_backup/network-state.json" || failed=true
  loaded=$(cs_check field "$cs_backup/network-state.json" service_loaded) || failed=true
  disabled=$(cs_check field "$cs_backup/network-state.json" service_disabled) || failed=true
  # Bootstrap may be denied while disabled. Restore that flag after restoring load state.
  /bin/launchctl enable "$cs_label" || failed=true
  if [ "$loaded" = true ]; then
    /bin/launchctl bootstrap system "$cs_daemon" > "$cs_backup/rollback-bootstrap.txt" 2>&1 || failed=true
  fi
  if [ "$disabled" = true ]; then /bin/launchctl disable "$cs_label" || failed=true; fi
  cs_observe_network "$cs_backup/restored-network" || failed=true
  cs_check verify-restored "$cs_backup/network-state.json" "$cs_backup/restored-network" || failed=true
  if cs_service_loaded "$cs_backup/restored-service.txt"; then current_result=true;
  else
    [ "$?" -eq 1 ] || failed=true
    current_result=false
  fi
  [ "$current_result" = "$loaded" ] || failed=true
  cs_observe_disabled "$cs_backup/restored-disabled.txt" || failed=true
  cs_check verify-disabled "$cs_backup/network-state.json" "$cs_backup/restored-disabled.txt" || failed=true
  if [ "$cs_log_created" = true ]; then /bin/rm -f "$cs_core/mihomo.log" || failed=true; fi
  /usr/bin/dscacheutil -flushcache || failed=true
  /usr/bin/killall -HUP mDNSResponder || failed=true
  if [ "$failed" = true ]; then
    echo 'Restore needs attention; retain the private backup and keep the gateway boundary closed.' >&2
  else
    echo 'Previous guest address mode, addresses, DNS, MTU and launch load state restored.' >&2
  fi
  exit 1
}

cs_begin() {
  local loaded=false result name
  export LC_ALL=C
  cs_checker_path="$cs_script_dir/guest-config-check.rb"
  [ -f "$cs_script_dir/guest-config-check.rb" ] && [ ! -L "$cs_script_dir/guest-config-check.rb" ] || cs_fail 'Guest checker is missing.'
  cs_check verify-payload "$cs_script_dir"
  [ ! -L "$cs_core" ] && [ ! -L "$cs_daemon" ] || cs_fail 'A protected production path is a symlink.'
  /bin/mkdir -p "$cs_core"
  /bin/chmod 700 "$cs_core"
  /bin/mkdir "$cs_core/.network-change-lock" || cs_fail 'Another guest transaction is active; no network changes made.'
  cs_lock_held=true
  trap cs_release_lock EXIT
  echo "$$" > "$cs_core/.network-change-lock/pid"
  cs_backup=$(/usr/bin/mktemp -d "$cs_core/network-backup.XXXXXX")
  /bin/chmod 700 "$cs_backup"
  cs_service=$(/usr/sbin/networksetup -listnetworkserviceorder | /usr/bin/awk '
    /^\([0-9]+\) / { name=$0; sub(/^\([0-9]+\) /,"",name) }
    /Device: en0\)/ { print name; exit }
  ')
  [ -n "$cs_service" ] || cs_fail 'Cannot identify the en0 network service.'
  cs_observe_network "$cs_backup"
  if cs_service_loaded "$cs_backup/service-before.txt"; then loaded=true;
  else
    result=$?
    [ "$result" -eq 1 ] || return "$result"
  fi
  cs_observe_disabled "$cs_backup/disabled.txt"
  cs_check snapshot "$cs_service" "$cs_backup" "$loaded"
  [ "$(cs_check field "$cs_backup/network-state.json" enabled)" = true ] || cs_fail 'The dedicated network service is disabled; unchanged.'
  cs_save_file "$cs_config" config.yaml
  cs_save_file "$cs_daemon" service.plist
  cs_save_file "$cs_core/mihomo" mihomo
  cs_save_file "$cs_core/guest-config-check.rb" guest-config-check.rb
  cs_save_file "$cs_core/guest-network-common.sh" guest-network-common.sh
  /bin/mkdir "$cs_backup/payload"
  for name in verge-mihomo mihomo-residential.yaml install-in-guest.sh repair-guest-network.sh Install.command guest-network-common.sh guest-config-check.rb guest-payload-manifest.json; do
    /bin/cp "$cs_script_dir/$name" "$cs_backup/payload/$name"
  done
  cs_check verify-payload "$cs_backup/payload"
  cs_checker_path="$cs_backup/payload/guest-config-check.rb"
  cs_log_created=false
  if [ -e "$cs_core/mihomo.log" ] || [ -L "$cs_core/mihomo.log" ]; then
    [ -f "$cs_core/mihomo.log" ] && [ ! -L "$cs_core/mihomo.log" ] || cs_fail 'Guest log path is not a regular file.'
  fi
}

cs_make_plist() {
  /bin/cat > "$cs_backup/candidate.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>local.claudeshield.residential</string>
<key>WorkingDirectory</key><string>/Library/Application Support/ClaudeShieldResidential</string>
<key>EnvironmentVariables</key><dict>
<key>PATH</key><string>/usr/sbin:/usr/bin:/bin:/sbin</string>
<key>HOME</key><string>/var/root</string>
</dict>
<key>ProgramArguments</key><array>
<string>/Library/Application Support/ClaudeShieldResidential/mihomo</string>
<string>-d</string><string>/Library/Application Support/ClaudeShieldResidential</string>
<string>-f</string><string>/Library/Application Support/ClaudeShieldResidential/config.yaml</string>
</array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>/Library/Application Support/ClaudeShieldResidential/mihomo.log</string>
<key>StandardErrorPath</key><string>/Library/Application Support/ClaudeShieldResidential/mihomo.log</string>
</dict></plist>
PLIST
  /usr/bin/plutil -lint "$cs_backup/candidate.plist" > /dev/null
  /usr/sbin/chown root:wheel "$cs_backup/candidate.plist"
  /bin/chmod 644 "$cs_backup/candidate.plist"
}

cs_validate_candidate() {
  local core_binary="$1"
  cs_check check "$cs_backup/candidate.yaml"
  /bin/mkdir "$cs_backup/offline-home"
  /usr/bin/sandbox-exec -p '(version 1) (allow default) (deny network*)' \
    "$core_binary" -t -d "$cs_backup/offline-home" -f "$cs_backup/candidate.yaml" > "$cs_backup/config-check.log" 2>&1 ||
    cs_fail 'Candidate failed offline core validation; production settings unchanged.'
  cs_make_plist
}

cs_install_file() {
  local source="$1" destination="$2" mode="$3"
  /bin/cp "$source" "$destination.cs-pending.$$"
  /usr/sbin/chown root:wheel "$destination.cs-pending.$$"
  /bin/chmod "$mode" "$destination.cs-pending.$$"
  /bin/mv -f "$destination.cs-pending.$$" "$destination"
}

cs_local_ready() {
  local attempt pid probe answer
  probe="guardian-$$-$RANDOM.invalid"
  for attempt in 1 2 3 4 5; do
    pid=$(/bin/launchctl print "$cs_label" 2>/dev/null | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+$/ {print $3; exit}')
    if [[ "$pid" =~ ^[0-9]+$ ]] &&
       /usr/sbin/lsof -nP -a -p "$pid" -iTCP:7894 -sTCP:LISTEN > /dev/null 2>&1 &&
       /usr/sbin/lsof -nP -a -p "$pid" -iTCP:53 -sTCP:LISTEN > /dev/null 2>&1 &&
       /usr/sbin/lsof -nP -a -p "$pid" -iUDP:53 > /dev/null 2>&1; then
      answer=$(/usr/bin/dig @127.0.0.1 "$probe" A +time=1 +tries=1 +short 2>/dev/null) || answer=''
      if [[ "$answer" =~ ^198\.18\.[0-9]+\.[0-9]+$ ]]; then return 0; fi
    fi
    /bin/sleep 1
  done
  cs_fail 'Local synthetic DNS or the expected proxy listeners did not become ready.'
}

cs_commit() {
  local install_core="$1" key target loaded_now expected_loaded
  # Detect conflicting edits before stopping the old service.
  for key in config.yaml service.plist mihomo guest-config-check.rb guest-network-common.sh; do
    case "$key" in service.plist) target="$cs_daemon" ;; *) target="$cs_core/$key" ;; esac
    if [ -f "$cs_backup/$key.present" ]; then
      [ -f "$target" ] && [ ! -L "$target" ] && /usr/bin/cmp -s "$target" "$cs_backup/$key" || cs_fail 'Production files changed after preflight; unchanged.'
    else
      [ ! -e "$target" ] && [ ! -L "$target" ] || cs_fail 'A production file appeared after preflight; unchanged.'
    fi
  done
  cs_observe_network "$cs_backup/precommit-network"
  cs_check verify-restored "$cs_backup/network-state.json" "$cs_backup/precommit-network"
  if cs_service_loaded "$cs_backup/precommit-service.txt"; then loaded_now=true;
  else
    [ "$?" -eq 1 ] || return 1
    loaded_now=false
  fi
  expected_loaded=$(cs_check field "$cs_backup/network-state.json" service_loaded)
  [ "$loaded_now" = "$expected_loaded" ] || cs_fail 'Launch service changed after preflight; unchanged.'
  cs_observe_disabled "$cs_backup/precommit-disabled.txt"
  cs_check verify-disabled "$cs_backup/network-state.json" "$cs_backup/precommit-disabled.txt"
  trap cs_restore ERR INT TERM HUP
  cs_mutating=true
  echo 'commit-started' > "$cs_backup/transaction-state.txt"
  if [ "$(cs_check field "$cs_backup/network-state.json" service_loaded)" = true ]; then
    /bin/launchctl bootout "$cs_label"
  fi
  cs_install_file "$cs_backup/candidate.yaml" "$cs_config" 600
  cs_install_file "$cs_backup/candidate.plist" "$cs_daemon" 644
  if [ "$install_core" = true ]; then cs_install_file "$cs_backup/payload/verge-mihomo" "$cs_core/mihomo" 700; fi
  cs_install_file "$cs_backup/payload/guest-config-check.rb" "$cs_core/guest-config-check.rb" 600
  cs_install_file "$cs_backup/payload/guest-network-common.sh" "$cs_core/guest-network-common.sh" 600
  if [ ! -e "$cs_core/mihomo.log" ]; then
    cs_log_created=true
    /usr/bin/touch "$cs_core/mihomo.log"
    /usr/sbin/chown root:wheel "$cs_core/mihomo.log"
    /bin/chmod 600 "$cs_core/mihomo.log"
  fi
  /usr/sbin/networksetup -setmanual "$cs_service" 10.77.0.3 255.255.255.0 10.77.0.1
  /usr/sbin/networksetup -setMTU "$cs_service" 1380
  /bin/launchctl enable "$cs_label"
  /bin/launchctl bootstrap system "$cs_daemon"
  cs_local_ready
  /usr/sbin/networksetup -setdnsservers "$cs_service" 127.0.0.1
  /usr/bin/dscacheutil -flushcache
  /usr/bin/killall -HUP mDNSResponder
  cs_observe_network "$cs_backup/applied-network"
  cs_check verify-applied "$cs_service" "$cs_backup/applied-network"
  cs_local_ready
  echo 'committed-local-only' > "$cs_backup/transaction-state.txt"
  trap - ERR INT TERM HUP
  cs_mutating=false
  echo 'Guest proxy network transaction applied; local synthetic checks passed.'
  echo 'No client was installed or started. Remote egress remains a separate acceptance step.'
  echo "Private recovery backup: $cs_backup"
}
