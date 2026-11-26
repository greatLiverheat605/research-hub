#!/bin/bash
# Transactional repair inside the dedicated macOS guest. No remote probes.
set -Eeuo pipefail
case "$(/usr/sbin/sysctl -n hw.model)" in
  VirtualMac*) ;;
  *) echo 'Run only inside the dedicated macOS VM.' >&2; exit 1 ;;
esac
/sbin/ifconfig en0 | /usr/bin/grep -q '52:54:00:77:00:03' || {
  echo 'Expected dedicated VM interface not found.' >&2; exit 1;
}
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo inside the VM.' >&2; exit 1; }
umask 077
cs_script_dir="$(cd "$(dirname "$0")" && pwd)"
[ -f "$cs_script_dir/guest-network-common.sh" ] && [ ! -L "$cs_script_dir/guest-network-common.sh" ] || {
  echo 'The approved guest transaction helper is missing.' >&2; exit 1;
}
source "$cs_script_dir/guest-network-common.sh"
[ -f "$cs_config" ] && [ ! -L "$cs_config" ] &&
  [ -f "$cs_daemon" ] && [ ! -L "$cs_daemon" ] &&
  [ -x "$cs_core/mihomo" ] && [ ! -L "$cs_core/mihomo" ] || {
  echo 'Existing guest proxy installation not found.' >&2; exit 1;
}
cs_begin
# Preserve the existing sole-node credentials; strict semantic checking follows.
/usr/bin/perl -0777 - "$cs_backup/config.yaml" "$cs_backup/candidate.yaml" <<'PERL'
use strict;
use warnings;
my ($source, $destination) = @ARGV;
open my $in, '<', $source or die "Cannot read existing configuration\n";
my $data = <$in>;
close $in;
my $tun = "tun:\n  enable: true\n  stack: gvisor\n  mtu: 1380\n  auto-route: true\n  auto-detect-interface: false\n  strict-route: true\n  route-exclude-address:\n  - 198.51.100.24/32\n  dns-hijack:\n  - any:53\n";
($data =~ s/^tun:\n(?:[ \t].*\n)+/$tun/m) == 1
  or die "Unrecognized TUN configuration; nothing changed\n";
($data =~ s/^  listen: 127\.0\.0\.1:(?:1053|53)$/  listen: 127.0.0.1:53/m) == 1
  or die "Unrecognized DNS listener; nothing changed\n";
if ($data !~ s/^interface-name:.*$/interface-name: en0/m) {
  $data =~ s/^log-level:/interface-name: en0\nlog-level:/m
    or $data =~ s/^tun:/interface-name: en0\ntun:/m;
}
$data =~ s/^keep-alive-idle:.*\n//mg;
$data =~ s/^keep-alive-interval:.*\n//mg;
$data =~ s/^log-level:[ \t]*([^\n]+)\n/log-level: $1\nkeep-alive-idle: 25\nkeep-alive-interval: 10\n/m
  or die "Unrecognized log-level field; nothing changed\n";
# Remove listener-level proxy override so port 7894 shares the unified ruleset with TUN
$data =~ s/^(listeners:\n(?:[ \t-].*\n)*?)^[ \t]+proxy:.*\n/$1/m;
# Ensure fake-ip-filter is present under fake-ip-range
$data =~ s/^  fake-ip-filter:\n(?:  - .*\n)+//m;
my $filter = "  fake-ip-filter:\n  - '*.lan'\n  - '*.local'\n  - +.home.arpa\n  - +.localdomain\n";
($data =~ s/^(  fake-ip-range: [^\n]+\n)/$1$filter/m) == 1
  or die "Unrecognized fake-ip-range field; nothing changed\n";
# Ensure the 3 Apple telemetry REJECT rules are present before MATCH
$data =~ s/^- DOMAIN-SUFFIX,(?:push|ess|xp)\.apple\.com,REJECT\n//mg;
my $apple_reject = "- DOMAIN-SUFFIX,push.apple.com,REJECT\n- DOMAIN-SUFFIX,ess.apple.com,REJECT\n- DOMAIN-SUFFIX,xp.apple.com,REJECT\n";
($data =~ s/^(- NETWORK,udp,REJECT\n)/$1$apple_reject/m) == 1
  or die "Unrecognized rules block; nothing changed\n";
open my $out, '>', $destination or die "Cannot write candidate configuration\n";
print {$out} $data;
close $out or die "Cannot finish candidate configuration\n";
PERL
/bin/chmod 600 "$cs_backup/candidate.yaml"
cs_validate_candidate "$cs_core/mihomo"
cs_commit false
