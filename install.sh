#!/usr/bin/env bash
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$ROOT_DIR/services"
PLASMOID_DIR="$ROOT_DIR/plasmoid/org.kde.plasma.wifimimo"

TARGET_DAEMON="/usr/local/bin/wifimimo-daemon"
TARGET_MON="/usr/local/bin/wifimimo-mon"
TARGET_DESKTOP="/usr/share/applications/wifimimo.desktop"
USER_SERVICE_NAME="wifimimo-daemon.service"

OLD_ROOT_FILES=(
    "/usr/local/bin/wifimimo-check"
    "/usr/local/bin/wifimimo-notify"
    "/usr/local/bin/wifi-antenna-check"
    "/usr/local/bin/wifi-antenna-mon"
    "/usr/local/bin/wifi-antenna-notify"
    "/etc/systemd/system/wifimimo-check.service"
    "/etc/systemd/system/wifimimo-check.timer"
    "/etc/systemd/system/wifi-antenna-check.service"
    "/etc/systemd/system/wifi-antenna-check.timer"
    "/usr/share/applications/wifi-monitor.desktop"
)

if [[ $EUID -ne 0 ]]; then
    exec pkexec bash "$SELF" "$@"
fi

run_as_user() {
    if [[ -n "${PKEXEC_UID:-}" ]]; then
        sudo -u "#${PKEXEC_UID}" XDG_RUNTIME_DIR="/run/user/${PKEXEC_UID}" HOME="$HOME" "$@"
    else
        "$@"
    fi
}

if [[ -n "${PKEXEC_UID:-}" ]]; then
    HOME="$(getent passwd "$PKEXEC_UID" | cut -d: -f6)"
    export HOME
    export XDG_DATA_HOME="${HOME}/.local/share"
fi

install -Dm755 /dev/stdin "$TARGET_DAEMON" <<EOF2
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$ROOT_DIR/wifimimo-daemon.py" "\$@"
EOF2

install -Dm755 /dev/stdin "$TARGET_MON" <<EOF2
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$ROOT_DIR/wifimimo-mon.py" "\$@"
EOF2

install -Dm644 "$ROOT_DIR/wifimimo.desktop" "$TARGET_DESKTOP"

for old_file in "${OLD_ROOT_FILES[@]}"; do
    rm -f "$old_file"
done

systemctl disable --now wifimimo-check.timer 2>/dev/null || true
systemctl disable --now wifimimo-check.service 2>/dev/null || true
systemctl disable --now wifi-antenna-check.timer 2>/dev/null || true
systemctl disable --now wifi-antenna-check.service 2>/dev/null || true
systemctl daemon-reload

USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
USER_SERVICE_PATH="$USER_SYSTEMD_DIR/$USER_SERVICE_NAME"
OLD_USER_FILES=(
    "$USER_SYSTEMD_DIR/wifimimo-notify.service"
    "$USER_SYSTEMD_DIR/wifimimo-notify.path"
    "$USER_SYSTEMD_DIR/wifi-antenna-notify.service"
    "$USER_SYSTEMD_DIR/wifi-antenna-notify.path"
    "$HOME/.config/autostart/wifimimo-plasma.desktop"
    "$HOME/.config/autostart/wifi-antenna-plasma.desktop"
)

install -d -m 755 "$USER_SYSTEMD_DIR"
install -Dm644 "$SERVICE_DIR/wifimimo-daemon.service" "$USER_SERVICE_PATH"
for old_file in "${OLD_USER_FILES[@]}"; do
    rm -f "$old_file"
done

PLASMOID_INSTALL_DIR="$HOME/.local/share/plasma/plasmoids/org.kde.plasma.wifimimo"
rm -rf "$PLASMOID_INSTALL_DIR"
run_as_user kpackagetool6 -t Plasma/Applet -r org.kde.plasma.wifimimo 2>/dev/null || true
run_as_user kpackagetool6 -t Plasma/Applet -r org.kde.plasma.wifiantennamonitor 2>/dev/null || true
run_as_user kpackagetool6 -t Plasma/Applet -i "$PLASMOID_DIR"

run_as_user systemctl --user daemon-reload
run_as_user systemctl --user disable --now wifimimo-notify.path 2>/dev/null || true
run_as_user systemctl --user disable --now wifimimo-notify.service 2>/dev/null || true
run_as_user systemctl --user disable --now wifi-antenna-notify.path 2>/dev/null || true
run_as_user systemctl --user disable --now wifi-antenna-notify.service 2>/dev/null || true
run_as_user systemctl --user enable --now "$USER_SERVICE_NAME"

printf 'Installed:\n'
printf '  %s\n' "$TARGET_DAEMON"
printf '  %s\n' "$TARGET_MON"
printf '  %s\n' "$TARGET_DESKTOP"
printf '  %s\n' "$USER_SERVICE_PATH"
printf '\nUser service status:\n'
run_as_user systemctl --user status "$USER_SERVICE_NAME" --no-pager
printf '\nRun monitor:  wifimimo-mon\n'
printf 'Panel applet: org.kde.plasma.wifimimo\n'
printf 'View logs:    journalctl --user -u %s -f\n' "$USER_SERVICE_NAME"
