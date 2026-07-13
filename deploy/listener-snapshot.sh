#!/usr/bin/env bash

capture_listener_snapshot() {
  local output=$1
  shift
  local temporary="${output}.tmp.$$"
  local port raw line local_address peer_address process_info pids pid units owner

  : > "$temporary"
  for port in "$@"; do
    raw="$(ss -H -ltnp "sport = :$port")" || {
      echo "cannot inspect protected listener on port $port" >&2
      rm -f "$temporary"
      return 1
    }
    test -n "$raw" || {
      echo "protected port $port is not listening" >&2
      rm -f "$temporary"
      return 1
    }

    while IFS= read -r line; do
      local_address="$(printf '%s\n' "$line" | awk '{print $4}')"
      peer_address="$(printf '%s\n' "$line" | awk '{print $5}')"
      process_info="$(printf '%s\n' "$line" | awk '{for (i=6; i<=NF; i++) printf "%s%s", (i == 6 ? "" : " "), $i}')"
      pids="$(printf '%s\n' "$process_info" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | LC_ALL=C sort -nu | paste -sd, -)"
      test -n "$local_address" && test -n "$process_info" && test -n "$pids" || {
        echo "cannot establish ownership for protected port $port" >&2
        rm -f "$temporary"
        return 1
      }

      owner=""
      IFS=, read -r -a listener_pids <<< "$pids"
      for pid in "${listener_pids[@]}"; do
        test -r "$proc_root/$pid/cgroup" || {
          echo "cannot inspect cgroup for protected listener pid $pid" >&2
          rm -f "$temporary"
          return 1
        }
        units="$(awk -F/ '{for (i=1; i<=NF; i++) if ($i ~ /\.service$/) print $i}' "$proc_root/$pid/cgroup" | LC_ALL=C sort -u | paste -sd, -)"
        test -n "$units" || {
          echo "cannot identify service for protected listener pid $pid" >&2
          rm -f "$temporary"
          return 1
        }
        owner="${owner}${owner:+;}${pid}:${units}"
      done
      printf '%s\t%s\t%s\t%s\t%s\n' \
        "$port" "$local_address" "$peer_address" "$process_info" "$owner" \
        >> "$temporary"
    done <<< "$raw"
  done

  LC_ALL=C sort "$temporary" -o "$temporary"
  chmod 0600 "$temporary"
  mv -f "$temporary" "$output"
}
