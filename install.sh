#!/usr/bin/env bash
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$ROOT_DIR/services"
PLASMOID_DIR="$ROOT_DIR/plasmoid/org.kde.plasma.wifimimo"

TARGET_LIB_DIR="/usr/local/lib/wifimimo"
TARGET_VENV_DIR="$TARGET_LIB_DIR/.venv"
TARGET_DAEMON="/usr/local/bin/wifimimo-daemon"
TARGET_MON="/usr/local/bin/wifimimo-mon"
TARGET_PLASMOID_SOURCE="/usr/local/bin/wifimimo-plasmoid-source"
TARGET_DESKTOP="/usr/share/applications/wifimimo.desktop"
USER_SERVICE_NAME="wifimimo-daemon.service"
PLASMOID_PLUGIN_ID="org.kde.plasma.wifimimo"

if [[ $EUID -ne 0 ]]; then
    exec pkexec bash "$SELF" "$@"
fi

run_as_user() {
    if [[ -n "${PKEXEC_UID:-}" ]]; then
        sudo -u "#${PKEXEC_UID}" \
            XDG_RUNTIME_DIR="/run/user/${PKEXEC_UID}" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${PKEXEC_UID}/bus" \
            HOME="$HOME" \
            "$@"
    else
        "$@"
    fi
}

reload_plasmashell() {
    # kpackagetool6 --upgrade writes new QML to disk but does NOT re-import the
    # plasmoid into a running plasmashell — the running instance keeps the old
    # compiled QML in process memory, so the schema-v2 JSON state file gets
    # parsed by the v1 plasmoid as garbage and the panel reads "Not connected".
    #
    # On systemd-managed Plasma 6 sessions (Manjaro, Arch, openSUSE TW, …)
    # plasmashell is run as the user unit plasma-plasmashell.service. Restart
    # it via systemctl so the session keeps a consistent DBus/XDG environment.
    # Do NOT use `kquitapp6 plasmashell && kstart plasmashell` from a sudo
    # context — kstart detaches but the new plasmashell inherits a broken
    # environment and frequently dies, leaving the user with no panel.
    local uid="${PKEXEC_UID:-$UID}"
    if ! run_as_user systemctl --user --quiet is-active plasma-plasmashell.service 2>/dev/null; then
        echo "plasma-plasmashell.service not active; skipping plasmashell reload."
        echo "  If your panel needs a refresh, run: systemctl --user restart plasma-plasmashell.service"
        return 0
    fi
    echo "Reloading plasmashell so the upgraded plasmoid takes effect..."
    run_as_user systemctl --user restart plasma-plasmashell.service || \
        echo "  Note: systemctl --user restart plasma-plasmashell.service failed; reload manually."
}

upgrade_or_install_plasmoid() {
    local plasmoid_dir="$1"
    local plugin_id="$2"
    local canonical_dir
    local user_plasmoid_dir="$HOME/.local/share/plasma/plasmoids/$plugin_id"
    canonical_dir="$(realpath "$plasmoid_dir")"

    # If a dev symlink at ~/.local/share/plasma/plasmoids/<id> points back at
    # *this* checkout, remove the symlink itself before invoking kpackagetool6.
    # Otherwise `kpackagetool6 --upgrade` follows the symlink and rm -rf's the
    # source repo. We only touch links pointing at this checkout — an unrelated
    # symlinked install (e.g. another working tree) is left alone and we bail
    # out so the user can resolve it manually.
    if [[ -L "$user_plasmoid_dir" ]]; then
        local installed_target
        installed_target="$(realpath "$user_plasmoid_dir")"
        if [[ "$installed_target" == "$canonical_dir" ]]; then
            echo "Removing dev symlink $user_plasmoid_dir -> $(readlink "$user_plasmoid_dir")"
            run_as_user rm -f -- "$user_plasmoid_dir"
        else
            echo "Refusing to remove unrelated symlink $user_plasmoid_dir -> $(readlink "$user_plasmoid_dir")" >&2
            echo "Resolved target ($installed_target) does not match this checkout ($canonical_dir)." >&2
            return 1
        fi
    fi

    if [[ -d "$user_plasmoid_dir" ]]; then
        run_as_user kpackagetool6 -t Plasma/Applet --upgrade "$canonical_dir"
    else
        run_as_user kpackagetool6 -t Plasma/Applet --install "$canonical_dir"
    fi
}

if [[ -n "${PKEXEC_UID:-}" ]]; then
    HOME="$(getent passwd "$PKEXEC_UID" | cut -d: -f6)"
    export HOME
    export XDG_DATA_HOME="${HOME}/.local/share"
fi

install -d -m755 "$TARGET_LIB_DIR"
install -Dm644 "$ROOT_DIR/phy_modes.py"                "$TARGET_LIB_DIR/phy_modes.py"
install -Dm644 "$ROOT_DIR/wifimimo_core.py"            "$TARGET_LIB_DIR/wifimimo_core.py"
install -Dm755 "$ROOT_DIR/wifimimo-daemon.py"          "$TARGET_LIB_DIR/wifimimo-daemon.py"
install -Dm755 "$ROOT_DIR/wifimimo-mon.py"             "$TARGET_LIB_DIR/wifimimo-mon.py"
install -Dm755 "$ROOT_DIR/wifimimo-plasmoid-source.py" "$TARGET_LIB_DIR/wifimimo-plasmoid-source.py"
install -Dm644 "$ROOT_DIR/requirements.txt"            "$TARGET_LIB_DIR/requirements.txt"

python3 -m venv "$TARGET_VENV_DIR"
HOME=/root PIP_CACHE_DIR=/root/.cache/pip "$TARGET_VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
HOME=/root PIP_CACHE_DIR=/root/.cache/pip "$TARGET_VENV_DIR/bin/python" -m pip install -r "$TARGET_LIB_DIR/requirements.txt" >/dev/null

install -Dm755 /dev/stdin "$TARGET_DAEMON" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
exec "/usr/local/lib/wifimimo/.venv/bin/python" "/usr/local/lib/wifimimo/wifimimo-daemon.py" "$@"
EOF2

install -Dm755 /dev/stdin "$TARGET_MON" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
exec "/usr/local/lib/wifimimo/.venv/bin/python" "/usr/local/lib/wifimimo/wifimimo-mon.py" "$@"
EOF2

install -Dm755 /dev/stdin "$TARGET_PLASMOID_SOURCE" <<'EOF2'
#!/usr/bin/env bash
set -euo pipefail
exec "/usr/local/lib/wifimimo/.venv/bin/python" "/usr/local/lib/wifimimo/wifimimo-plasmoid-source.py" "$@"
EOF2

install -Dm644 "$ROOT_DIR/wifimimo.desktop" "$TARGET_DESKTOP"

systemctl daemon-reload

USER_SYSTEMD_DIR="$HOME/.config/systemd/user"
USER_SERVICE_PATH="$USER_SYSTEMD_DIR/$USER_SERVICE_NAME"

run_as_user mkdir -p "$USER_SYSTEMD_DIR"
run_as_user install -Dm644 "$SERVICE_DIR/wifimimo-daemon.service" "$USER_SERVICE_PATH"

upgrade_or_install_plasmoid "$PLASMOID_DIR" "$PLASMOID_PLUGIN_ID" \
    || echo "Note: Plasma widget install/upgrade skipped (may need manual add)"

run_as_user systemctl --user daemon-reload
run_as_user systemctl --user enable "$USER_SERVICE_NAME"
run_as_user systemctl --user restart "$USER_SERVICE_NAME"

reload_plasmashell

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
printf '\nState file is JSON (schema_version 2). plasmashell was reloaded so the\n'
printf 'new plasmoid QML is active; the panel will reappear within ~1s.\n'
