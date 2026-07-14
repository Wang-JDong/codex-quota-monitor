#!/usr/bin/env bash
set -euo pipefail

if [ "${CODEX_MONITOR_TESTING:-0}" = 1 ]; then
  root="${CODEX_MONITOR_TEST_ROOT:?}"
  python_bin="${CODEX_MONITOR_TEST_PYTHON:?}"
  ss_bin="${CODEX_MONITOR_TEST_SS:?}"
else
  root=/opt/codex-quota-monitor
  python_bin=/usr/bin/python3
  ss_bin=/usr/bin/ss
fi
mode="${1:-run}"
cli_args=("$mode")
rsshub_pid=""
monitor_pid=""

# Defense in depth: keep RSSHub private and bounded even if this runner is
# invoked outside the installed unit.  The service and dry-run cgroups enforce
# the independent process-level limits.
case "$mode" in
  run|reprocess-post)
    PORT="${PORT:-1200}"
    RSSHUB_BASE_URL="${RSSHUB_BASE_URL:-http://127.0.0.1:$PORT}"
    if [ "$mode" = reprocess-post ]; then
      post_id="${2:-}"
      case "$post_id" in
        ""|*[!0-9]*)
          echo "post ID must contain digits only" >&2
          exit 2
          ;;
      esac
      cli_args+=("$post_id")
    fi
    ;;
  dry-run)
    # Reapply these values after EnvironmentFile processing so a dry-run can
    # never share the production listener or SQLite database.
    PORT=1201
    RSSHUB_BASE_URL=http://127.0.0.1:1201
    DATABASE_PATH=/tmp/codex-quota-monitor-dry-run.db
    ;;
  *)
    echo "unsupported monitor mode: $mode" >&2
    exit 2
    ;;
esac
case "$PORT" in
  ""|*[!0-9]*)
    echo "invalid RSSHub port: $PORT" >&2
    exit 2
    ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "invalid RSSHub port: $PORT" >&2
  exit 2
fi
expected_base_url="http://127.0.0.1:$PORT"
test "${RSSHUB_BASE_URL%/}" = "$expected_base_url" || {
  echo "RSSHUB_BASE_URL must be loopback http://127.0.0.1:$PORT" >&2
  exit 2
}
RSSHUB_BASE_URL="$expected_base_url"
health_url="$RSSHUB_BASE_URL/healthz"

export PORT
export RSSHUB_BASE_URL
export DATABASE_PATH
export LISTEN_INADDR_ANY=0
export CACHE_TYPE=memory
export NODE_ENV=production
export NODE_OPTIONS=--max-old-space-size=256

cleanup() {
  if [ -n "$monitor_pid" ]; then
    if kill -0 "$monitor_pid" 2>/dev/null; then
      kill "$monitor_pid" 2>/dev/null || true
    fi
    wait "$monitor_pid" 2>/dev/null || true
    monitor_pid=""
  fi
  if [ -n "$rsshub_pid" ]; then
    if kill -0 "$rsshub_pid" 2>/dev/null; then
      kill "$rsshub_pid" 2>/dev/null || true
    fi
    wait "$rsshub_pid" 2>/dev/null || true
    rsshub_pid=""
  fi
}
trap cleanup EXIT INT TERM
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

listener_owned_by_rsshub() {
  local output line address_pattern pid_pattern
  local matched=false

  if ! output="$("$ss_bin" -H -ltnp "sport = :$PORT" 2>/dev/null)"; then
    return 2
  fi
  [ -n "$output" ] || return 1

  address_pattern="(^|[[:space:]])127[.]0[.]0[.]1:${PORT}([[:space:]]|$)"
  pid_pattern="(^|[[:space:],(])pid=${rsshub_pid}([[:space:],)]|$)"
  while IFS= read -r line; do
    [[ "$line" =~ $address_pattern ]] || return 2
    [[ "$line" =~ $pid_pattern ]] || return 2
    matched=true
  done <<< "$output"
  [ "$matched" = true ] || return 2
}

"$root/runtime/node/bin/node" \
  "$root/rsshub/server.mjs" &
rsshub_pid=$!

healthy=false
for _ in $(seq 1 30); do
  kill -0 "$rsshub_pid" 2>/dev/null || break
  if listener_owned_by_rsshub; then
    :
  else
    ownership_status=$?
    if [ "$ownership_status" -eq 1 ]; then
      sleep 1
      continue
    fi
    break
  fi
  if curl --fail --silent --connect-timeout 2 --max-time 2 \
    "$health_url" >/dev/null; then
    # Do not mistake another listener on the same port for this process.
    kill -0 "$rsshub_pid" 2>/dev/null || break
    listener_owned_by_rsshub || break
    healthy=true
    break
  fi
  sleep 1
done
test "$healthy" = true || {
  echo "private RSSHub failed to become healthy" >&2
  exit 1
}

PYTHONPATH="$root/src" "$python_bin" -m codex_quota_monitor \
  --config "$root/config/sources.json" "${cli_args[@]}" &
monitor_pid=$!
wait "$monitor_pid"
monitor_status=$?
monitor_pid=""
exit "$monitor_status"
