#!/usr/bin/env bash
set -euo pipefail

if [ "${CODEX_MONITOR_TESTING:-0}" = 1 ]; then
  root="${CODEX_MONITOR_TEST_ROOT:?}"
  proc_root="${CODEX_MONITOR_TEST_PROC_ROOT:?}"
else
  root=/opt/codex-quota-monitor
  proc_root=/proc
fi
snapshot_dir="$root/preflight"
service_snapshot="$snapshot_dir/services.tsv"
listener_snapshot="$snapshot_dir/listeners.tsv"
services=(
  ssh.service
  sing-box.service
  cdn-subscription.service
  friend-clash-sub.service
  share-100gb-sub.service
)
ports=(22 22222 2082 2086 2095 2052 8880)
project_ports=(1200 1201)

# shellcheck source=listener-snapshot.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/listener-snapshot.sh"

test "$(id -u)" = 0 || { echo "run as root" >&2; exit 1; }

for port in "${project_ports[@]}"; do
  if ss -H -ltn "sport = :$port" | grep -q .; then
    echo "refusing install: port $port is already in use" >&2
    exit 1
  fi
done

for service in "${services[@]}"; do
  test "$(systemctl is-active "$service")" = active || {
    echo "protected service is not active: $service" >&2
    exit 1
  }
done
if [ "${CODEX_MONITOR_TESTING:-0}" = 1 ]; then
  install -d -m 0700 "$snapshot_dir"
else
  install -d -o root -g root -m 0700 "$snapshot_dir"
fi
tmp_snapshot="$(mktemp "$snapshot_dir/services.tsv.XXXXXX")"
trap 'rm -f "$tmp_snapshot"' EXIT
for service in "${services[@]}"; do
  path="$(systemctl show -p FragmentPath --value "$service")"
  test -f "$path" || {
    echo "cannot locate unit file for $service" >&2
    exit 1
  }
  hash="$(sha256sum "$path" | cut -d' ' -f1)"
  printf '%s\t%s\t%s\n' "$service" "$path" "$hash" >> "$tmp_snapshot"
done
chmod 0600 "$tmp_snapshot"
mv -f "$tmp_snapshot" "$service_snapshot"
trap - EXIT
capture_listener_snapshot "$listener_snapshot" "${ports[@]}"

free -m
df -h /
echo "preflight passed"
