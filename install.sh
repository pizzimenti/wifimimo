#!/usr/bin/env bash
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$ROOT_DIR/services"

TARGET_SCRIPT="/usr/local/bin/wifi-antenna-check"
TARGET_TRAY="/usr/local/bin/wifi-antenna-tray"
TARGET_MON="/usr/local/bin/wifi-antenna-mon"
TARGET_SERVICE="/etc/systemd/system/wifi-antenna-check.service"
TARGET_TIMER="/etc/systemd/system/wifi-antenna-check.timer"
TRAY_SERVICE_SRC="$SERVICE_DIR/wifi-antenna-tray.service"

if [[ $EUID -ne 0 ]]; then
    exec pkexec bash "$SELF" "$@"
fi

install -Dm755 "$ROOT_DIR/wifi-antenna-check.sh" "$TARGET_SCRIPT"
install -Dm644 "$SERVICE_DIR/wifi-antenna-check.service" "$TARGET_SERVICE"
install -Dm644 "$SERVICE_DIR/wifi-antenna-check.timer"   "$TARGET_TIMER"

# Launcher shim for the TUI monitor
install -Dm755 /dev/stdin "$TARGET_MON" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$ROOT_DIR/wifi-antenna-mon.py" "\$@"
EOF

# Launcher shim for the systray app
install -Dm755 /dev/stdin "$TARGET_TRAY" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$ROOT_DIR/wifi-antenna-tray.py" "\$@"
EOF

# Tray user service (optional — skip if file missing)
if [[ -f "$TRAY_SERVICE_SRC" ]]; then
    TRAY_USER_SVC_DIR="/etc/xdg/systemd/user"
    install -Dm644 "$TRAY_SERVICE_SRC" "$TRAY_USER_SVC_DIR/wifi-antenna-tray.service"
fi

# Desktop entry — lets KDE log notifications under a known app name
install -Dm644 "$ROOT_DIR/wifi-monitor.desktop" "/usr/share/applications/wifi-monitor.desktop"

# Fix ownership if invoked via pkexec (PKEXEC_UID is set by polkit)
if [[ -n "${PKEXEC_UID:-}" ]]; then
    owner_home="$(getent passwd "$PKEXEC_UID" | cut -d: -f6)"
    if [[ -n "$owner_home" && -d "$owner_home" ]]; then
        find "$ROOT_DIR" -xdev -user root -exec chown "$(id -un "$PKEXEC_UID"):$(id -gn "$PKEXEC_UID")" {} +
    fi
fi

systemctl daemon-reload
systemctl enable --now wifi-antenna-check.timer

echo "Installed:"
echo "  $TARGET_SCRIPT"
echo "  $TARGET_MON      (→ $ROOT_DIR/wifi-antenna-mon.py)"
echo "  $TARGET_TRAY     (→ $ROOT_DIR/wifi-antenna-tray.py)"
echo "  $TARGET_SERVICE"
echo "  $TARGET_TIMER"
echo
echo "Timer status:"
systemctl status wifi-antenna-check.timer --no-pager
echo
echo "Run monitor:  wifi-antenna-mon"
echo "Run tray:     systemctl --user enable --now wifi-antenna-tray"
echo "View logs:    journalctl -t wifi-antenna-check -f"
