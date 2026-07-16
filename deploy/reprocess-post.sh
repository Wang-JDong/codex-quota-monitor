#!/usr/bin/env bash
set -euo pipefail

if [ "${CODEX_MONITOR_TESTING:-0}" = 1 ]; then
  root="${CODEX_MONITOR_TEST_ROOT:?}"
  runtime_log_dir="$root/log"
else
  root=/opt/codex-quota-monitor
  runtime_log_dir=/var/log/codex-quota-monitor
fi

post_id="${1:-}"
case "$post_id" in
  ""|*[!0-9]*)
    echo "post ID must contain digits only" >&2
    exit 2
    ;;
esac

test "$(id -u)" = 0 || { echo "run as root" >&2; exit 1; }

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

# A fixed transient unit fails safely if another backfill is already running.
# The runner's listener ownership and SQLite lock also reject overlap with the
# scheduled production service.
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
  "$root/deploy/run-monitor.sh" reprocess-post "$post_id"
