#!/usr/bin/env bash
set -euo pipefail

if [ "${CODEX_MONITOR_TESTING:-0}" = 1 ]; then
  root="${CODEX_MONITOR_TEST_ROOT:?}"
  runtime_log_dir="$root/log"
else
  root=/opt/codex-quota-monitor
  runtime_log_dir=/var/log/codex-quota-monitor
fi

test "$(id -u)" = 0 || { echo "run as root" >&2; exit 1; }

# Refresh the two public X query IDs in a separate root-only, tightly bounded
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

# A fixed name makes concurrent dry-runs fail safely in systemd.  Deliberately
# omit --replace so this can never displace an already running dry-run.
exec systemd-run --quiet --wait --pipe --collect \
  --unit=codex-quota-monitor-dry-run \
  --uid=codex-monitor \
  --gid=codex-monitor \
  --working-directory="$root" \
  --property=Type=oneshot \
  --property="EnvironmentFile=$root/.env" \
  --setenv=PORT=1201 \
  --setenv=RSSHUB_BASE_URL=http://127.0.0.1:1201 \
  --setenv=DATABASE_PATH=/tmp/codex-quota-monitor-dry-run.db \
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
  PORT=1201 \
  RSSHUB_BASE_URL=http://127.0.0.1:1201 \
  DATABASE_PATH=/tmp/codex-quota-monitor-dry-run.db \
  "$root/deploy/run-monitor.sh" dry-run
