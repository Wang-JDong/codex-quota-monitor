#!/usr/bin/env bash
set -euo pipefail

unit=codex-quota-monitor.service
properties="$(LC_ALL=C systemctl show "$unit" \
  --property=MemoryMax \
  --property=CPUQuotaPerSecUSec)"
printf '%s\n' "$properties"

memory_max=""
cpu_quota=""
while IFS='=' read -r key value; do
  case "$key" in
    MemoryMax) memory_max="$value" ;;
    CPUQuotaPerSecUSec) cpu_quota="$value" ;;
  esac
done <<< "$properties"

test "$memory_max" = 402653184 || {
  echo "unexpected MemoryMax: ${memory_max:-missing}; expected 402653184" >&2
  exit 1
}
test "$cpu_quota" = 300ms || {
  echo "unexpected CPUQuotaPerSecUSec: ${cpu_quota:-missing}; expected 300ms" >&2
  exit 1
}

echo "resource limits verified: 384 MiB memory, 30% CPU"
