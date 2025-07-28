#!/bin/bash

# --- ENV-based ping targets or fallback defaults ---
DEFAULT_TARGETS="8.8.8.8,1.1.1.1,9.9.9.9"
IFS=',' read -ra TARGETS <<< "${INTERNET_CHECK_TARGETS:-$DEFAULT_TARGETS}"
echo "setting check internet target ping IPs: ${TARGETS[*]}"

# File references
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
DB_FILE="$SCRIPT_DIR/logs/internet_status.db"
FAILURE_COUNT_FILE="$SCRIPT_DIR/logs/failure_count.txt"

now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
SUCCESS_COUNT=0
LATENCIES=()
PING_COUNT_PER_TARGET=5
TOTAL_COUNT=$(( ${#TARGETS[@]} * PING_COUNT_PER_TARGET ))
PING_TIMEOUT=2  # Reduced timeout for faster failure detection

# Cooldown period (10 minutes)
#COOLDOWN_PERIOD=600  # in seconds (600 seconds = 10 minutes)

# Initialize failure count file if it doesn't exist
if [ ! -f $FAILURE_COUNT_FILE ]; then
    echo "0" > $FAILURE_COUNT_FILE
fi

echo "starting..."

# Initialize SQLite database and table if not exists
sqlite3 $DB_FILE "CREATE TABLE IF NOT EXISTS internet_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME,
    status TEXT,
    success_percentage INTEGER,
    avg_latency_ms REAL,
    max_latency_ms REAL,
    min_latency_ms REAL,
    packet_loss REAL
);"

# New table for power cycle events
sqlite3 $DB_FILE "CREATE TABLE IF NOT EXISTS power_cycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME,
    reason TEXT
);"

# Function to ping targets and collect latency
check_internet() {
    for target in ${TARGETS[@]}; do
        for i in $(seq 1 $PING_COUNT_PER_TARGET); do
            PING_RESULT=$(ping -c 1 -W $PING_TIMEOUT $target | grep 'time=')
            if [[ $PING_RESULT ]]; then
                LATENCY=$(echo $PING_RESULT | sed -e 's/.*time=\([0-9.]*\).*/\1/')
                LATENCIES+=($LATENCY)
                SUCCESS_COUNT=$((SUCCESS_COUNT+1))
            fi
        done
    done
}

check_internet

SUCCESS_PERCENTAGE=$((SUCCESS_COUNT * 100 / TOTAL_COUNT))

if [[ $SUCCESS_COUNT -gt 0 ]]; then
    AVG_LATENCY=$(echo "${LATENCIES[@]}" | awk '{for(i=1;i<=NF;i++) sum+=$i; print sum/NF}')
    MAX_LATENCY=$(echo "${LATENCIES[@]}" | awk '{for(i=1;i<=NF;i++) if($i>max) max=$i; print max}')
    MIN_LATENCY=$(echo "${LATENCIES[@]}" | awk '{for(i=1;i<=NF;i++) if(min=="" || $i<min) min=$i; print min}')
else
    AVG_LATENCY="NULL"
    MAX_LATENCY="NULL"
    MIN_LATENCY="NULL"
fi

LOSS_COUNT=$((TOTAL_COUNT - SUCCESS_COUNT))
PACKET_LOSS_PERCENTAGE=$((LOSS_COUNT * 100 / TOTAL_COUNT))

if [[ $SUCCESS_PERCENTAGE -eq 100 ]]; then
    STATUS="Internet is fully up (100% success)"
elif [[ $SUCCESS_PERCENTAGE -gt 0 ]]; then
    STATUS="Internet is partially up ($SUCCESS_PERCENTAGE% success)"
else
    STATUS="Internet is down (0% success)"
fi

echo "DEBUG: Timestamp for internet_status DB insert: $now"
sqlite3 $DB_FILE "INSERT INTO internet_status (timestamp, status, success_percentage, avg_latency_ms, max_latency_ms, min_latency_ms, packet_loss)
VALUES ('$now', '$STATUS', $SUCCESS_PERCENTAGE, $AVG_LATENCY, $MAX_LATENCY, $MIN_LATENCY, $PACKET_LOSS_PERCENTAGE);"

if [[ $? -eq 0 ]]; then
    echo "Log successfully inserted into db"
else
    echo "Failed to insert log into db"
fi

sqlite3 $DB_FILE "DELETE FROM internet_status WHERE timestamp < datetime('now', '-14 days');"

if [[ $? -eq 0 ]]; then
    echo "Old data successfully cleaned up"
else
    echo "Failed to clean up old data"
fi

FAILURE_COUNT=$(cat $FAILURE_COUNT_FILE)

if [[ $SUCCESS_PERCENTAGE -eq 0 ]]; then
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
    echo $FAILURE_COUNT > $FAILURE_COUNT_FILE
    echo "Internet test failed!"

    FAILURE_COUNT=$(cat $FAILURE_COUNT_FILE)
    echo "Failure count is: " $FAILURE_COUNT

    FAILURE_THRESHOLD=${FAILURE_THRESHOLD:-5}
    if [[ $FAILURE_COUNT -ge $FAILURE_THRESHOLD ]]; then
        echo "Internet down for 5+ minutes. Power cycling modem..."

        python3 "$SCRIPT_DIR/power_cycle_nbn.py"

        if [[ $? -eq 0 ]]; then
            echo "Power cycle action logged successfully"
        else
            echo "Failed to log power cycle action"
        fi

        echo "0" > $FAILURE_COUNT_FILE
    fi
else
    echo "0" > $FAILURE_COUNT_FILE
fi
