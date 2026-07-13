#!/usr/bin/env bash
set -euo pipefail

if [ "${CODEX_MONITOR_TESTING:-0}" = 1 ]; then
  root="${CODEX_MONITOR_TEST_ROOT:?}"
  proc_root="${CODEX_MONITOR_TEST_PROC_ROOT:?}"
else
  root=/opt/codex-quota-monitor
  proc_root=/proc
fi
service_snapshot="$root/preflight/services.tsv"
listener_snapshot="$root/preflight/listeners.tsv"
ports=(22 22222 2082 2086 2095 2052 8880)
project_ports=(1200 1201)

# shellcheck source=listener-snapshot.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/listener-snapshot.sh"

test "$(id -u)" = 0 || { echo "run as root" >&2; exit 1; }
test -s "$service_snapshot"
test -s "$listener_snapshot"

while IFS=$'\t' read -r service path expected_hash; do
  test "$(systemctl is-active "$service")" = active || {
    echo "protected service is no longer active: $service" >&2
    exit 1
  }
  test "$(sha256sum "$path" | cut -d' ' -f1)" = "$expected_hash" || {
    echo "protected unit changed: $service" >&2
    exit 1
  }
done < "$service_snapshot"

current_listeners="$(mktemp)"
trap 'rm -f "$current_listeners"' EXIT
capture_listener_snapshot "$current_listeners" "${ports[@]}"
cmp -s "$listener_snapshot" "$current_listeners" || {
  echo "protected listener changed" >&2
  exit 1
}
for port in "${project_ports[@]}"; do
  if ss -H -ltn "sport = :$port" | grep -q .; then
    echo "project port $port remained in use" >&2
    exit 1
  fi
done
echo "postflight passed; existing node services are unchanged"
