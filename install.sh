#!/usr/bin/env bash
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$ROOT_DIR/services"
PLASMOID_DIR="$ROOT_DIR/plasmoid/org.kde.plasma.wifimimo"

TARGET_LIB_DIR="/usr/local/lib/wifimimo"
TARGET_DAEMON="/usr/local/bin/wifimimo-daemon"
TARGET_MON="/usr/local/bin/wifimimo-mon"
TARGET_PLASMOID_SOURCE="/usr/local/bin/wifimimo-plasmoid-source"
TARGET_DESKTOP="/usr/share/applications/wifimimo.desktop"
USER_SERVICE_NAME="wifimimo-daemon.service"

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

install -d -m755 "$TARGET_LIB_DIR"
install -Dm644 "$ROOT_DIR/wifimimo_core.py"            "$TARGET_LIB_DIR/wifimimo_core.py"
install -Dm755 "$ROOT_DIR/wifimimo-daemon.py"          "$TARGET_LIB_DIR/wifimimo-daemon.py"
install -Dm755 "$ROOT_DIR/wifimimo-mon.py"             "$TARGET_LIB_DIR/wifimimo-mon.py"
install -Dm755 "$ROOT_DIR/wifimimo-plasmoid-source.py" "$TARGET_LIB_DIR/wifimimo-plasmoid-source.py"

install -Dm755 /dev/stdin "$TARGET_DAEMON" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
exec python3 "/usr/local/lib/wifimimo/wifimimo-daemon.py" "$@"
EOF2

install -Dm755 /dev/stdin "$TARGET_MON" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
exec python3 "/usr/local/lib/wifimimo/wifimimo-mon.py" "$@"
EOF2

install -Dm755 /dev/stdin "$TARGET_PLASMOID_SOURCE" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
exec python3 "/usr/local/lib/wifimimo/wifimimo-plasmoid-source.py" "$@"
EOF2

install -Dm644 "$ROOT_DIR/wifimimo.desktop" "$TARGET_DESKTOP"

systemctl daemon-reload

USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
USER_SERVICE_PATH="$USER_SYSTEMD_DIR/$USER_SERVICE_NAME"

install -d -m 755 "$USER_SYSTEMD_DIR"
install -Dm644 "$SERVICE_DIR/wifimimo-daemon.service" "$USER_SERVICE_PATH"

run_as_user kpackagetool6 -t Plasma/Applet --upgrade "$PLASMOID_DIR" 2>&1 \
    || run_as_user kpackagetool6 -t Plasma/Applet --install "$PLASMOID_DIR" 2>&1 \
    || echo "Note: Plasma widget install/upgrade skipped (may need manual add)"

run_as_user systemctl --user daemon-reload
run_as_user systemctl --user enable --now "$USER_SERVICE_NAME"

printf 'Installed:\n'
printf '  %s\n' "$TARGET_LIB_DIR/"
printf '  %s\n' "$TARGET_DAEMON"
printf '  %s\n' "$TARGET_MON"
printf '  %s\n' "$TARGET_PLASMOID_SOURCE"
printf '  %s\n' "$TARGET_DESKTOP"
printf '  %s\n' "$USER_SERVICE_PATH"
printf '\nUser service status:\n'
run_as_user systemctl --user status "$USER_SERVICE_NAME" --no-pager
printf '\nRun monitor:  wifimimo-mon\n'
printf 'Panel applet: org.kde.plasma.wifimimo\n'
printf 'View logs:    journalctl --user -u %s -f\n' "$USER_SERVICE_NAME"
