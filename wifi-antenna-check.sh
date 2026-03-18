#!/usr/bin/env bash
# wifi-antenna-check — WiFi antenna health check for 2x2 MIMO
# Checks per-antenna signal levels, spatial streams, and TX retry rate.
# Logs to syslog and fires a single desktop notification summarising all issues.

IFACE="${WIFI_IFACE:-wlp1s0}"
LOG_TAG="wifi-antenna-check"
ALERT_DIFF_DBM=15    # warn if antennas diverge more than this
ALERT_SIGNAL_DBM=-75 # warn if any antenna is weaker than this
ALERT_RETRY_PCT=30   # warn if TX retry rate exceeds this
NOTIFY_USER="bradley"
NOTIFY_UID=$(id -u "$NOTIFY_USER" 2>/dev/null || true)
STATE_FILE="/run/user/${NOTIFY_UID}/wifi-antenna-state"
PREV_STATE_FILE="/run/user/${NOTIFY_UID}/wifi-antenna-prev-state"

# ── load previous state (for restoration detection) ──────────────────────────
PREV_ANTENNA_COUNT=0
PREV_TX_NSS=""
PREV_RX_NSS=""
if [[ -f "$PREV_STATE_FILE" ]]; then
    PREV_ANTENNA_COUNT=$(grep '^antenna_count=' "$PREV_STATE_FILE" | cut -d= -f2 || echo 0)
    PREV_TX_NSS=$(grep '^tx_nss=' "$PREV_STATE_FILE" | cut -d= -f2 || true)
    PREV_RX_NSS=$(grep '^rx_nss=' "$PREV_STATE_FILE" | cut -d= -f2 || true)
fi

# ── issue collection ──────────────────────────────────────────────────────────
# Each issue is stored as "urgency|title|detail".
# A single notification is sent at the end rather than one per problem.

ISSUES=()
MAX_URGENCY="normal"

log()  { logger -t "$LOG_TAG" "$*"; echo "$*"; }
info() { log "INFO: $*"; }

_add_issue() {
    local urgency="$1" title="$2" detail="$3"
    ISSUES+=("${urgency}|${title}|${detail}")
    [[ "$urgency" == "critical" ]] && MAX_URGENCY="critical"
    log "WARNING: $title — $detail"
}

_send() {
    command -v notify-send &>/dev/null || return 0
    local title="$1" body="$2" urgency="${3:-normal}"
    sudo -u "$NOTIFY_USER" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$NOTIFY_USER")/bus" \
        notify-send \
            --app-name="WiFi Monitor" \
            --urgency="$urgency" \
            --icon="network-wireless" \
            --expire-time=12000 \
            --hint=string:desktop-entry:wifi-monitor \
            "$title" "$body" 2>/dev/null || true
}

_fire_notification() {
    if [[ ${#ISSUES[@]} -eq 0 ]]; then
        info "All checks passed — WiFi antennas healthy"
        return
    fi

    local title body

    if [[ ${#ISSUES[@]} -eq 1 ]]; then
        # Single issue — title is the issue title, body is the detail
        local urg="${ISSUES[0]%%|*}"
        local rest="${ISSUES[0]#*|}"
        title="${rest%%|*}"
        body="${rest##*|}"
        _send "$title" "$body" "$urg"
    else
        # Multiple issues — summary title, bullet list body
        title="${#ISSUES[@]} Warnings  ·  $IFACE"
        body=""
        for issue in "${ISSUES[@]}"; do
            local rest="${issue#*|}"
            local t="${rest%%|*}"
            local d="${rest##*|}"
            body+="• $t  —  $d"$'\n'
        done
        body="${body%$'\n'}"
        _send "$title" "$body" "$MAX_URGENCY"
    fi
}

# ── collect data ──────────────────────────────────────────────────────────────

DUMP=$(iw dev "$IFACE" station dump 2>/dev/null)
if [[ -z "$DUMP" ]]; then
    _send "Not Connected" "No station data for $IFACE" "normal"
    exit 1
fi

SIGNAL_LINE=$(echo "$DUMP" | grep -E '^\s+signal:\s+' | head -1)
info "--- WiFi Antenna Report for $IFACE ---"
info "Raw: $SIGNAL_LINE"

# Per-antenna values from brackets, e.g. [-56, -51]
ANTENNA_VALUES=$(echo "$SIGNAL_LINE" | grep -oP '\[[-\d, ]+\]' | tr -d '[]' | tr ',' '\n' | tr -d ' ')
ANTENNA_COUNT=$(echo "$ANTENNA_VALUES" | grep -c '^-')

if [[ "$ANTENNA_COUNT" -lt 2 ]]; then
    _add_issue "critical" "MIMO Offline" \
        "Only ${ANTENNA_COUNT}/2 antennas reporting — check antenna connection"
else
    info "$ANTENNA_COUNT antennas active (2×2 MIMO confirmed)"
fi

ant_idx=0
min_sig=0
max_sig=-999

while IFS= read -r sig; do
    [[ -z "$sig" ]] && continue
    ant_idx=$(( ant_idx + 1 ))
    info "Antenna $ant_idx: ${sig} dBm"
    if (( sig < ALERT_SIGNAL_DBM )); then
        _add_issue "normal" "Weak Signal — Antenna $ant_idx" \
            "${sig} dBm  (threshold ${ALERT_SIGNAL_DBM} dBm)"
    fi
    (( sig > max_sig )) && max_sig="$sig"
    (( sig < min_sig || min_sig == 0 )) && min_sig="$sig"
done <<< "$ANTENNA_VALUES"

if [[ "$ANTENNA_COUNT" -ge 2 ]]; then
    DIFF=$(( max_sig - min_sig ))
    info "Antenna spread: ${DIFF} dBm"
    if (( DIFF > ALERT_DIFF_DBM )); then
        _add_issue "critical" "Antenna Imbalance" \
            "${DIFF} dBm spread  (${min_sig} to ${max_sig} dBm)  —  possible loose connector"
    fi
fi

TX_LINE=$(echo "$DUMP" | grep -E 'tx bitrate:')
RX_LINE=$(echo "$DUMP" | grep -E 'rx bitrate:')

_parse_nss() { echo "$1" | grep -oP '(?:HE|VHT)-NSS \K\d+' || true; }

TX_NSS=$(_parse_nss "$TX_LINE")
RX_NSS=$(_parse_nss "$RX_LINE")
TX_RATE=$(echo "$TX_LINE" | grep -oP '[\d.]+ MBit/s' | head -1)
RX_RATE=$(echo "$RX_LINE" | grep -oP '[\d.]+ MBit/s' | head -1)
info "TX: ${TX_RATE} NSS=${TX_NSS:-N/A} | RX: ${RX_RATE} NSS=${RX_NSS:-N/A}"

if [[ -n "$TX_NSS" && "$TX_NSS" != "2" ]]; then
    _add_issue "critical" "MIMO Degraded — TX" \
        "Dropped to ${TX_NSS}×1 SISO  (expected 2×2)  —  check antenna"
fi
if [[ -n "$RX_NSS" && "$RX_NSS" != "2" ]]; then
    _add_issue "critical" "MIMO Degraded — RX" \
        "Dropped to ${RX_NSS}×1 SISO  (expected 2×2)  —  check antenna"
fi

TX_PKTS=$(echo "$DUMP" | grep -oP 'tx packets:\s+\K\d+')
TX_RETRIES=$(echo "$DUMP" | grep -oP 'tx retries:\s+\K\d+')
if [[ -n "$TX_PKTS" && "$TX_PKTS" -gt 0 ]]; then
    RETRY_PCT=$(( TX_RETRIES * 100 / TX_PKTS ))
    info "TX retry rate: ${RETRY_PCT}% (${TX_RETRIES}/${TX_PKTS})"
    if (( RETRY_PCT > ALERT_RETRY_PCT )); then
        _add_issue "normal" "High Interference" \
            "TX retry rate: ${RETRY_PCT}%  (threshold ${ALERT_RETRY_PCT}%)"
    fi
fi

# ── write state file for tray icon ───────────────────────────────────────────
if [[ -n "$NOTIFY_UID" ]]; then
    {
        echo "timestamp=$(date +%s)"
        echo "connected=true"
        echo "tx_nss=${TX_NSS:-}"
        echo "rx_nss=${RX_NSS:-}"
        echo "signal_dbm=${SIGNAL_DBM:-0}"
        echo "retry_pct=${RETRY_PCT:-0}"
        echo "issue_count=${#ISSUES[@]}"
        echo "antenna_count=${ANTENNA_COUNT:-0}"
    } > "$STATE_FILE"
    chmod 644 "$STATE_FILE"
fi

_fire_notification

# ── restoration notifications ─────────────────────────────────────────────────
# Fire a "cleared" notice if a previous critical state is now healthy.

if [[ "$PREV_ANTENNA_COUNT" -lt 2 && "$PREV_ANTENNA_COUNT" -gt 0 && "$ANTENNA_COUNT" -ge 2 ]]; then
    _send "MIMO Online" "Both antennas active — 2×2 MIMO restored" "normal"
    info "RECOVERY: both antennas back online"
fi

if [[ "$PREV_TX_NSS" == "1" && "$TX_NSS" == "2" ]]; then
    _send "TX MIMO Restored" "TX back to 2×2 — ${TX_RATE:-?}" "normal"
    info "RECOVERY: TX NSS restored to 2"
fi

if [[ "$PREV_RX_NSS" == "1" && "$RX_NSS" == "2" ]]; then
    _send "RX MIMO Restored" "RX back to 2×2 — ${RX_RATE:-?}" "normal"
    info "RECOVERY: RX NSS restored to 2"
fi

# ── save state for next run ───────────────────────────────────────────────────
if [[ -n "$NOTIFY_UID" ]]; then
    {
        echo "tx_nss=${TX_NSS:-}"
        echo "rx_nss=${RX_NSS:-}"
        echo "antenna_count=${ANTENNA_COUNT:-0}"
    } > "$PREV_STATE_FILE"
    chmod 644 "$PREV_STATE_FILE"
fi
