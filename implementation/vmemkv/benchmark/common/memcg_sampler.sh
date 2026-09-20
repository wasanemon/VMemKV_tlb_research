#!/usr/bin/env bash
# memcg_sampler.sh - Background workingset sampler for one isolated LTM group.
#
# Usage: memcg_sampler.sh OUT_PREFIX [MAX_SECONDS]
#
# Polls the memory.stat of the cgroup running benchmark/bench_kv once per second
# and appends timestamped snapshots to OUT_PREFIX.memcg_series. Exits when
# OUT_PREFIX.done appears (written by the outer wrapper after the measurement
# command returns) or after MAX_SECONDS (default 600). Purely observational:
# reads one cgroup file per second, never writes cgroup knobs.
#
# The whole-machine /proc/vmstat workingset counters cannot attribute refaults
# to the measured store; the memcg-scoped series closes that gap.
set -euo pipefail

OUT="$1"
MAX_SECS="${2:-600}"
SERIES="${OUT}.memcg_series"
DONE="${OUT}.done"

CG_STAT=""

resolve_bench_cgroup() {
  # The real benchmark binary's command line always carries a --benchmark flag;
  # wrapper shells quoting the binary path do not, so they never match.
  local pid
  pid="$(pgrep -f 'bench_kv --benchmark' | head -n 1 || true)"
  if [[ -z "$pid" || ! -r "/proc/$pid/cgroup" ]]; then
    return 1
  fi
  local rel
  rel="$(awk -F: '$1 == "0" && $3 != "" { print $3; exit }' "/proc/$pid/cgroup" 2>/dev/null || true)"
  if [[ -z "$rel" || ! -r "/sys/fs/cgroup${rel}/memory.stat" ]]; then
    return 1
  fi
  CG_STAT="/sys/fs/cgroup${rel}/memory.stat"
  echo "# scope: $rel" >>"$SERIES"
  return 0
}

for ((t = 0; t < MAX_SECS; t++)); do
  if [[ -f "$DONE" ]] && [[ -z "$(pgrep -f 'bench_kv --benchmark' || true)" ]]; then
    break
  fi
  if [[ -z "$CG_STAT" ]]; then
    resolve_bench_cgroup || true
  fi
  if [[ -n "$CG_STAT" ]]; then
    {
      printf 't=%s\n' "$(date +%s.%N)"
      grep -E '^(anon|file|pgfault|pgmajfault|workingset_)' "$CG_STAT" || true
      echo '---'
    } >>"$SERIES"
  fi
  sleep 1
done
