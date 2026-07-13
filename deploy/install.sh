#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "${CODEX_MONITOR_TESTING:-0}" = 1 ]; then
  root="${CODEX_MONITOR_TEST_ROOT:?}"
  systemd_unit_dir="${CODEX_MONITOR_TEST_UNIT_DIR:?}"
  logrotate_dir="${CODEX_MONITOR_TEST_LOGROTATE_DIR:?}"
  runtime_log_dir="$root/log"
else
  root=/opt/codex-quota-monitor
  systemd_unit_dir=/etc/systemd/system
  logrotate_dir=/etc/logrotate.d
  runtime_log_dir=/var/log/codex-quota-monitor
fi
node_version=22.20.0
archive="node-v${node_version}-linux-x64.tar.xz"
sha=00bbd05e306ea68b6e13e17360d0e2f680b493ef95f2fea1c4296ff7437530bc

test "$(id -u)" = 0 || { echo "run as root" >&2; exit 1; }
"$source_root/deploy/preflight.sh"

id codex-monitor >/dev/null 2>&1 || \
  useradd --system --home-dir "$root" --shell /usr/sbin/nologin codex-monitor
if [ "${CODEX_MONITOR_TESTING:-0}" = 1 ]; then
  install -d -m 0755 \
    "$root" "$root/config" "$root/deploy" "$root/rsshub" "$root/runtime" "$root/src"
  install -d -m 0700 "$root/data" "$runtime_log_dir"
else
  install -d -o root -g root -m 0755 \
    "$root" "$root/config" "$root/deploy" "$root/rsshub" "$root/runtime" "$root/src"
  install -d -o codex-monitor -g codex-monitor -m 0700 \
    "$root/data" "$runtime_log_dir"
fi

if [ "$source_root" != "$root" ]; then
  cp -R "$source_root/src/." "$root/src/"
  install -m 0644 "$source_root/config/sources.json" "$root/config/sources.json"
  install -m 0644 "$source_root/rsshub/package.json" "$root/rsshub/package.json"
  install -m 0644 "$source_root/rsshub/package-lock.json" \
    "$root/rsshub/package-lock.json"
  install -m 0644 "$source_root/rsshub/server.mjs" "$root/rsshub/server.mjs"
  for script in "$source_root"/deploy/*.sh; do
    install -m 0755 "$script" "$root/deploy/$(basename "$script")"
  done
  install -m 0644 "$source_root/deploy/codex-quota-monitor.service" "$root/deploy/"
  install -m 0644 "$source_root/deploy/codex-quota-monitor.timer" "$root/deploy/"
  install -m 0644 "$source_root/deploy/codex-quota-monitor.logrotate" "$root/deploy/"

  if [ -f "$source_root/.env" ]; then
    install -o root -g codex-monitor -m 0640 "$source_root/.env" "$root/.env"
  fi
fi
test -f "$root/.env" || {
  echo "missing $root/.env; create it from .env.example before installing" >&2
  exit 1
}
chmod 640 "$root/.env"
chown root:codex-monitor "$root/.env"

if [ ! -x "$root/runtime/node/bin/node" ]; then
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  curl --fail --location --silent --show-error \
    "https://nodejs.org/dist/v${node_version}/${archive}" -o "$tmp"
  echo "$sha  $tmp" | sha256sum --check --status
  tar -xJf "$tmp" -C "$root/runtime"
  ln -sfn "node-v${node_version}-linux-x64" "$root/runtime/node"
  rm -f "$tmp"
  trap - EXIT
fi

systemd-run --quiet --wait --pipe --collect \
  --unit=codex-quota-monitor-install \
  --property=MemoryMax=512M \
  --property=CPUQuota=40% \
  --working-directory="$root/rsshub" \
  --setenv="PATH=$root/runtime/node/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  "$root/runtime/node/bin/npm" ci \
    --omit=dev --ignore-scripts --no-audit --no-fund

install -m 0644 "$root/deploy/codex-quota-monitor.service" "$systemd_unit_dir/"
install -m 0644 "$root/deploy/codex-quota-monitor.timer" "$systemd_unit_dir/"
install -m 0644 "$root/deploy/codex-quota-monitor.logrotate" \
  "$logrotate_dir/codex-quota-monitor"
systemctl daemon-reload
echo "installed but timer remains disabled until postflight passes"
