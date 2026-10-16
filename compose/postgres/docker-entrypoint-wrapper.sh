#!/usr/bin/env bash
# Leitet shared_buffers / effective_cache_size / maintenance_work_mem aus MEM_LIMIT ab.
set -euo pipefail

MEM_LIMIT="${MEM_LIMIT:-10g}"
CPUSET_DB="${CPUSET_DB:-0-2}"

parse_bytes() {
  local v="${1,,}"
  if [[ "$v" =~ ^([0-9]+)([kmgt]?)$ ]]; then
    local n="${BASH_REMATCH[1]}"
    local u="${BASH_REMATCH[2]}"
    case "$u" in
      k) echo $((n * 1024)) ;;
      m) echo $((n * 1024 * 1024)) ;;
      g) echo $((n * 1024 * 1024 * 1024)) ;;
      t) echo $((n * 1024 * 1024 * 1024 * 1024)) ;;
      *) echo "$n" ;;
    esac
  else
    echo "Ungültiges MEM_LIMIT: $1" >&2
    exit 1
  fi
}

to_mb() {
  echo $(($1 / 1024 / 1024))
}

BYTES="$(parse_bytes "$MEM_LIMIT")"
SHARED_MB=$(( $(to_mb "$BYTES") * 25 / 100 ))
EFFECTIVE_MB=$(( $(to_mb "$BYTES") * 75 / 100 ))
# HNSW-Build: groß, aber ≤ 50 % MEM_LIMIT
MAINT_MB=$(( $(to_mb "$BYTES") * 50 / 100 ))

# DB-Kerne = Anzahl IDs in CPUSET_DB (Format "0-2" oder "0,1,2")
if [[ "$CPUSET_DB" == *-* ]]; then
  IFS='-' read -r LO HI <<< "$CPUSET_DB"
  CORES=$((HI - LO + 1))
else
  CORES=$(echo "$CPUSET_DB" | tr ',' '\n' | grep -c .)
fi
WORKERS=$((CORES - 1))
if (( WORKERS < 1 )); then WORKERS=1; fi

TPL="/etc/postgresql/postgresql.conf.tpl"
OUT="/etc/postgresql/postgresql.conf"
sed \
  -e "s/{{SHARED_BUFFERS}}/${SHARED_MB}MB/" \
  -e "s/{{EFFECTIVE_CACHE_SIZE}}/${EFFECTIVE_MB}MB/" \
  -e "s/{{MAINTENANCE_WORK_MEM}}/${MAINT_MB}MB/" \
  -e "s/{{MAX_PARALLEL_MAINTENANCE_WORKERS}}/${WORKERS}/" \
  "$TPL" > "$OUT"

echo "postgresql.conf: shared_buffers=${SHARED_MB}MB effective_cache_size=${EFFECTIVE_MB}MB maintenance_work_mem=${MAINT_MB}MB max_parallel_maintenance_workers=${WORKERS}" >&2

exec docker-entrypoint.sh "$@"
