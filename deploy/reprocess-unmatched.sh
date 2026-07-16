#!/usr/bin/env bash
set -euo pipefail

if [ "${CODEX_MONITOR_TESTING:-0}" = 1 ]; then
  root="${CODEX_MONITOR_TEST_ROOT:?}"
  runtime_log_dir="$root/log"
else
  root=/opt/codex-quota-monitor
  runtime_log_dir=/var/log/codex-quota-monitor
fi

days=7
limit=100
while (($# > 0)); do
  case "$1" in
    --days)
      [ "$#" -ge 2 ] || { echo "--days requires a value" >&2; exit 2; }
      days="$2"
      shift 2
      ;;
    --limit)
      [ "$#" -ge 2 ] || { echo "--limit requires a value" >&2; exit 2; }
      limit="$2"
      shift 2
      ;;
    -h|--help)
      printf 'usage: %s [--days 1..31] [--limit 1..100]\n' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$days" in
  ""|*[!0-9]*) echo "days must be an integer" >&2; exit 2 ;;
esac
case "$limit" in
  ""|*[!0-9]*) echo "limit must be an integer" >&2; exit 2 ;;
esac
if (( days < 1 || days > 31 )); then
  echo "days must be between 1 and 31" >&2
  exit 2
fi
if (( limit < 1 || limit > 100 )); then
  echo "limit must be between 1 and 100" >&2
  exit 2
fi

test "$(id -u)" = 0 || { echo "run as root" >&2; exit 1; }

# Refresh the public X query IDs in a separate root-only, tightly bounded
# transient unit.  The monitor itself remains the unprivileged codex-monitor
# user and cannot modify the root-owned RSSHub package.
systemd-run --quiet --wait --pipe --collect \
  --unit=codex-quota-monitor-refresh \
  --working-directory="$root" \
  --property=Type=oneshot \
  --property="EnvironmentFile=$root/.env" \
  --property=TimeoutStartSec=1min \
  --property=MemoryMax=128M \
  --property=CPUQuota=20% \
  --property=Nice=10 \
  --property=NoNewPrivileges=yes \
  --property=PrivateTmp=yes \
  --property=ProtectSystem=strict \
  --property=ProtectHome=yes \
  --property="ReadWritePaths=$root/rsshub/node_modules/rsshub/dist-lib" \
  --property="RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" \
  --property=UMask=0077 \
  "$root/runtime/node/bin/node" \
  "$root/rsshub/server.mjs" --refresh-query-ids

# A fixed name makes concurrent backfills fail safely.  Deliberately omit
# --replace so this can never displace the scheduled production service.
exec systemd-run --quiet --wait --pipe --collect \
  --unit=codex-quota-monitor-reprocess \
  --uid=codex-monitor \
  --gid=codex-monitor \
  --working-directory="$root" \
  --property=Type=oneshot \
  --property="EnvironmentFile=$root/.env" \
  --setenv=PORT=1200 \
  --setenv=RSSHUB_BASE_URL=http://127.0.0.1:1200 \
  --setenv=LISTEN_INADDR_ANY=0 \
  --setenv=CACHE_TYPE=memory \
  --setenv=NODE_ENV=production \
  --setenv=NODE_OPTIONS=--max-old-space-size=256 \
  --property=TimeoutStartSec=5min \
  --property=MemoryMax=384M \
  --property=CPUQuota=30% \
  --property=Nice=10 \
  --property=IOSchedulingClass=idle \
  --property=NoNewPrivileges=yes \
  --property=PrivateTmp=yes \
  --property=ProtectSystem=strict \
  --property=ProtectHome=yes \
  --property="ReadWritePaths=$root/data $runtime_log_dir" \
  --property="RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" \
  --property=UMask=0077 \
  /usr/bin/env \
  PORT=1200 \
  RSSHUB_BASE_URL=http://127.0.0.1:1200 \
  "$root/deploy/run-monitor.sh" reprocess-unmatched \
  --days "$days" \
  --limit "$limit"
