#!/usr/bin/env bash
set -euo pipefail

# --- Config via env (with sane defaults) ---
DEFAULT_TARGETS="8.8.8.8,1.1.1.1,9.9.9.9"
PING_COUNT_PER_TARGET="${PING_COUNT_PER_TARGET:-5}"
PING_TIMEOUT="${PING_TIMEOUT:-2}"             # seconds
RETENTION_DAYS="${RETENTION_DAYS:-14}"        # how long to keep data
VACUUM_INTERVAL_RUNS="${VACUUM_INTERVAL_RUNS:-720}"  # ≈12h if loop is 1/min
FAILURE_THRESHOLD="${FAILURE_THRESHOLD:-5}"   # consecutive minutes down before power-cycle

# --- Paths ---
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
# Default to keeping the database in a dedicated data directory,
# with logs and counters separate.
DB_PATH="${DB_PATH:-$SCRIPT_DIR/../data/internet_status.db}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/../logs}"
FAILURE_COUNT_FILE="$LOG_DIR/failure_count.txt"
MAINT_COUNTER_FILE="$LOG_DIR/maintenance_counter.txt"

mkdir -p "$(dirname "$DB_PATH")" "$LOG_DIR"
[ -f "$FAILURE_COUNT_FILE" ] || echo "0" > "$FAILURE_COUNT_FILE"
[ -f "$MAINT_COUNTER_FILE" ] || echo "0" > "$MAINT_COUNTER_FILE"

# --- Targets ---
IFS=',' read -r -a TARGETS <<< "${INTERNET_CHECK_TARGETS:-$DEFAULT_TARGETS}"
echo "setting check internet target ping IPs: ${TARGETS[*]}"

now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
SUCCESS_COUNT=0
LATENCIES=()
TOTAL_COUNT=$(( ${#TARGETS[@]} * PING_COUNT_PER_TARGET ))

echo "starting..."

# --- Initialize DB schema (idempotent) ---
sqlite3 "$DB_PATH" <<'SQL'
CREATE TABLE IF NOT EXISTS internet_status (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp DATETIME,
  status TEXT,
  success_percentage INTEGER,
  avg_latency_ms REAL,
  max_latency_ms REAL,
  min_latency_ms REAL,
  packet_loss REAL
);
CREATE TABLE IF NOT EXISTS power_cycle_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp DATETIME,
  reason TEXT
);
SQL

# --- Ping each target and collect latencies ---
check_internet() {
  for target in "${TARGETS[@]}"; do
    for _ in $(seq 1 "$PING_COUNT_PER_TARGET"); do
      # -c1 = one packet, -W timeout (s); numeric output is default on busybox/iputils
      if PING_RESULT="$(ping -c 1 -W "$PING_TIMEOUT" "$target" 2>/dev/null | grep 'time=')" ; then
        # extract the number after time=
        latency="$(sed -e 's/.*time=\([0-9.]*\).*/\1/' <<<"$PING_RESULT")"
        LATENCIES+=("$latency")
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
      fi
    done
  done
}
check_internet

SUCCESS_PERCENTAGE=$(( SUCCESS_COUNT * 100 / TOTAL_COUNT ))
LOSS_COUNT=$(( TOTAL_COUNT - SUCCESS_COUNT ))
PACKET_LOSS_PERCENTAGE=$(( LOSS_COUNT * 100 / TOTAL_COUNT ))

if (( SUCCESS_COUNT > 0 )); then
  # avg/max/min using awk; ensure proper numeric formatting
  AVG_LATENCY="$(awk '{for(i=1;i<=NF;i++) s+=$i; printf "%.3f\n", s/NF}' <<<"${LATENCIES[*]}")"
  MAX_LATENCY="$(awk 'BEGIN{m=0}{for(i=1;i<=NF;i++) if($i>m) m=$i} END{printf "%.3f\n", m}' <<<"${LATENCIES[*]}")"
  MIN_LATENCY="$(awk 'BEGIN{m=""}{for(i=1;i<=NF;i++) if(m==""||$i<m) m=$i} END{printf "%.3f\n", m}' <<<"${LATENCIES[*]}")"
else
  AVG_LATENCY="NULL"
  MAX_LATENCY="NULL"
  MIN_LATENCY="NULL"
fi

if   (( SUCCESS_PERCENTAGE == 100 )); then
  STATUS="Internet is fully up (100% success)"
elif (( SUCCESS_PERCENTAGE > 0 )); then
  STATUS="Internet is partially up (${SUCCESS_PERCENTAGE}% success)"
else
  STATUS="Internet is down (0% success)"
fi

# Escape single quotes for SQL safety
STATUS_ESC="${STATUS//\'/\'\'}"

echo "DEBUG: Timestamp for internet_status DB insert: $now"
sqlite3 "$DB_PATH" "INSERT INTO internet_status
  (timestamp, status, success_percentage, avg_latency_ms, max_latency_ms, min_latency_ms, packet_loss)
  VALUES
  ('$now', '$STATUS_ESC', $SUCCESS_PERCENTAGE, $AVG_LATENCY, $MAX_LATENCY, $MIN_LATENCY, $PACKET_LOSS_PERCENTAGE);"
if (( $? == 0 )); then
  echo "Log successfully inserted into db"
else
  echo "Failed to insert log into db"
fi

# --- Retention + maintenance ---
# 1) Enable WAL (great for concurrent writer/reader) and add indexes (idempotent)
sqlite3 "$DB_PATH" <<'SQL'
PRAGMA journal_mode=WAL;
CREATE INDEX IF NOT EXISTS idx_internet_status_timestamp ON internet_status(timestamp);
CREATE INDEX IF NOT EXISTS idx_power_cycle_events_timestamp ON power_cycle_events(timestamp);
SQL

# 2) Prune older than RETENTION_DAYS using julianday() (robust against ISO strings)
sqlite3 "$DB_PATH" "DELETE FROM internet_status
  WHERE julianday(timestamp) < julianday('now','-${RETENTION_DAYS} days');"
sqlite3 "$DB_PATH" "DELETE FROM power_cycle_events
  WHERE julianday(timestamp) < julianday('now','-${RETENTION_DAYS} days');"

# 3) Periodic WAL checkpoint + VACUUM to actually reclaim space
maint_count="$(cat "$MAINT_COUNTER_FILE" 2>/dev/null || echo 0)"
maint_count=$(( maint_count + 1 ))
printf "%s" "$maint_count" > "$MAINT_COUNTER_FILE"

if (( maint_count >= VACUUM_INTERVAL_RUNS )); then
  printf "0" > "$MAINT_COUNTER_FILE"
  # Truncate WAL and compact the main DB file
  sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"
fi

# --- Failure handling / Power cycle logic ---
FAILURE_COUNT="$(cat "$FAILURE_COUNT_FILE")"

if (( SUCCESS_PERCENTAGE == 0 )); then
  FAILURE_COUNT=$(( FAILURE_COUNT + 1 ))
  echo "$FAILURE_COUNT" > "$FAILURE_COUNT_FILE"
  echo "Internet test failed!"
  echo "Failure count is: $FAILURE_COUNT"

  if (( FAILURE_COUNT >= FAILURE_THRESHOLD )); then
    echo "Internet down for ${FAILURE_THRESHOLD}+ minutes. Power cycling modem..."
    if python3 "$SCRIPT_DIR/power_cycle_nbn.py" ; then
      echo "Power cycle action logged successfully"
    else
      echo "Failed to log power cycle action"
    fi
    echo "0" > "$FAILURE_COUNT_FILE"
  fi
else
  echo "0" > "$FAILURE_COUNT_FILE"
fi
