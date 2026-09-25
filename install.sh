#!/usr/bin/env bash
# wifimimo installer.
#
#   ./install.sh                         install / upgrade
#   ./install.sh --manage-internal ID    also let wifimimo toggle an internal PCI
#                                        wifi card (ID = vendor:device, optionally
#                                        vendor:device=driver; comma-separate
#                                        several; or "auto" for present cards)
#   ./install.sh --migrate-legacy-rules  move hand-made udev rules that remove a
#                                        managed card to /etc/wifimimo/legacy/
#   ./install.sh --uninstall             remove everything wifimimo installed
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$ROOT_DIR/services"
PLASMOID_DIR="$ROOT_DIR/plasmoid/org.kde.plasma.wifimimo"

TARGET_LIB_DIR="/usr/local/lib/wifimimo"
TARGET_VENV_DIR="$TARGET_LIB_DIR/.venv"
TARGET_HELPER="$TARGET_LIB_DIR/wifimimo-helper"
TARGET_DAEMON="/usr/local/bin/wifimimo-daemon"
TARGET_MON="/usr/local/bin/wifimimo-mon"
TARGET_PLASMOID_SOURCE="/usr/local/bin/wifimimo-plasmoid-source"
TARGET_TIDY="/usr/local/bin/wifimimo-nm-tidy"
TARGET_DESKTOP="/usr/share/applications/wifimimo.desktop"
TARGET_POLICY="/usr/share/polkit-1/actions/io.github.pizzimenti.wifimimo.policy"
TARGET_DISPATCHER_DIR="/etc/NetworkManager/dispatcher.d"
TARGET_DISPATCHER="$TARGET_DISPATCHER_DIR/90-wifimimo"
TARGET_RT_PROTOS="/etc/iproute2/rt_protos.d/wifimimo.conf"
ETC_DIR="/etc/wifimimo"
INTERNAL_CONF="$ETC_DIR/internal.conf"
INTERNAL_FLAG="$ETC_DIR/internal-enabled"
INTERNAL_RULE="/etc/udev/rules.d/70-wifimimo-internal.rules"
WIFIMIMO_BLACKLIST="/etc/modprobe.d/wifimimo-internal.conf"
LEGACY_DIR="$ETC_DIR/legacy"
USER_SERVICE_NAME="wifimimo-daemon.service"
PLASMOID_PLUGIN_ID="org.kde.plasma.wifimimo"
PY_MODULES=(phy_modes.py wifimimo_core.py wifimimo_shared.py wifimimo_radio.py wifimimo_nm.py wifimimo_roam.py)
PY_SCRIPTS=(wifimimo-daemon.py wifimimo-mon.py wifimimo-plasmoid-source.py wifimimo-nm-tidy.py)

MANAGE_INTERNAL=""
MIGRATE_LEGACY=0
UNINSTALL=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --manage-internal) MANAGE_INTERNAL="${2:?--manage-internal needs an ID or 'auto'}"; shift 2 ;;
        --manage-internal=*) MANAGE_INTERNAL="${1#*=}"; shift ;;
        --migrate-legacy-rules) MIGRATE_LEGACY=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,13p' "$SELF" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    exec pkexec bash "$SELF" \
        ${MANAGE_INTERNAL:+--manage-internal "$MANAGE_INTERNAL"} \
        $([[ $MIGRATE_LEGACY == 1 ]] && echo --migrate-legacy-rules) \
        $([[ $UNINSTALL == 1 ]] && echo --uninstall)
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
    # compiled QML in process memory. On systemd-managed Plasma 6 sessions
    # plasmashell is the user unit plasma-plasmashell.service; restart it via
    # systemctl so the session keeps a consistent DBus/XDG environment. Do NOT
    # use `kquitapp6 plasmashell && kstart plasmashell` from a sudo context.
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
    # source repo. Unrelated symlinked installs are left alone.
    if [[ -L "$user_plasmoid_dir" ]]; then
        local installed_target
        installed_target="$(realpath "$user_plasmoid_dir")"
        if [[ "$installed_target" == "$canonical_dir" ]]; then
            echo "Removing dev symlink $user_plasmoid_dir -> $(readlink "$user_plasmoid_dir")"
            run_as_user rm -f -- "$user_plasmoid_dir"
        else
            echo "Refusing to remove unrelated symlink $user_plasmoid_dir -> $(readlink "$user_plasmoid_dir")" >&2
            return 1
        fi
    fi

    if [[ -d "$user_plasmoid_dir" ]]; then
        run_as_user kpackagetool6 -t Plasma/Applet --upgrade "$canonical_dir"
    else
        run_as_user kpackagetool6 -t Plasma/Applet --install "$canonical_dir"
    fi
}

# ---------------------------------------------------------------------------
# Internal card helpers
# ---------------------------------------------------------------------------

pci_dev_for_id() {  # vendor:device -> first matching wifi-class PCI address
    local want="$1" dev vendor device
    for dev in /sys/bus/pci/devices/*; do
        [[ "$(cat "$dev/class" 2>/dev/null)" == 0x0280* ]] || continue
        vendor="$(cat "$dev/vendor")"; device="$(cat "$dev/device")"
        if [[ "${vendor#0x}:${device#0x}" == "$want" ]]; then
            basename "$dev"
            return 0
        fi
    done
    return 1
}

write_internal_conf() {
    local spec ids=() lines=() id driver addr bridge any_present=0
    if [[ "$MANAGE_INTERNAL" == auto ]]; then
        for dev in /sys/bus/pci/devices/*; do
            [[ "$(cat "$dev/class" 2>/dev/null)" == 0x0280* && -e "$dev/driver" ]] || continue
            ids+=("$(cat "$dev/vendor" | sed 's/^0x//'):$(cat "$dev/device" | sed 's/^0x//')")
        done
        [[ ${#ids[@]} -gt 0 ]] || { echo "--manage-internal auto: no bound PCI wifi card found" >&2; exit 3; }
    else
        IFS=',' read -r -a ids <<< "$MANAGE_INTERNAL"
    fi
    for spec in "${ids[@]}"; do
        id="${spec%%=*}"; id="${id,,}"
        driver=""; [[ "$spec" == *=* ]] && driver="${spec#*=}"
        [[ "$id" =~ ^[0-9a-f]{4}:[0-9a-f]{4}$ ]] || { echo "bad PCI id: $spec" >&2; exit 2; }
        bridge=""
        if addr="$(pci_dev_for_id "$id")"; then
            any_present=1
            [[ -n "$driver" ]] || driver="$(basename "$(readlink -f "/sys/bus/pci/devices/$addr/driver")" 2>/dev/null || true)"
            bridge="$(basename "$(dirname "$(readlink -f "/sys/bus/pci/devices/$addr")")")"
            [[ "$bridge" =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]] || bridge=""
        fi
        [[ "$driver" =~ ^[a-z0-9_]{1,32}$ ]] || {
            echo "cannot determine the driver for $id (card not present?). Pass it as $id=<driver>." >&2
            exit 3
        }
        lines+=("$id $driver${bridge:+ $bridge}")
    done
    install -d -m755 "$ETC_DIR"
    printf '# Managed internal Wi-Fi cards: <vendor:device> <driver> [parent bridge]\n' > "$INTERNAL_CONF"
    printf '%s\n' "${lines[@]}" >> "$INTERNAL_CONF"
    chmod 644 "$INTERNAL_CONF"
    echo "Wrote $INTERNAL_CONF:"; sed 's/^/  /' "$INTERNAL_CONF"
    # Never yank a card that's working right now: if it's present, start "on".
    if [[ $any_present == 1 && ! -e "$INTERNAL_FLAG" ]]; then
        touch "$INTERNAL_FLAG"
        echo "Internal card is present now, so it starts enabled (toggle it off in the widget)."
    fi
    # Blacklist the driver so the generated udev rule is its only loader.
    for line in "${lines[@]}"; do
        driver="$(awk '{print $2}' <<< "$line")"
        if ! grep -qsE "^[[:space:]]*blacklist[[:space:]]+$driver([[:space:]]|\$)" /etc/modprobe.d/*.conf; then
            printf 'blacklist %s\n' "$driver" >> "$WIFIMIMO_BLACKLIST"
            echo "Added 'blacklist $driver' to $WIFIMIMO_BLACKLIST"
        fi
    done
}

handle_legacy_rules() {
    local legacy
    legacy="$("$TARGET_HELPER" internal status | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("legacy_rules", [])))')"
    [[ -n "$legacy" ]] || return 0
    if [[ $MIGRATE_LEGACY == 1 ]]; then
        install -d -m755 "$LEGACY_DIR"
        while IFS= read -r path; do
            mv -v -- "$path" "$LEGACY_DIR/"
        done <<< "$legacy"
        udevadm control --reload
    else
        echo
        echo "WARNING: hand-made udev rule(s) still remove the internal card at every boot," >&2
        echo "         overriding wifimimo's toggle:" >&2
        sed 's/^/           /' <<< "$legacy" >&2
        echo "         Re-run with --migrate-legacy-rules to move them to $LEGACY_DIR/." >&2
    fi
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

if [[ -n "${PKEXEC_UID:-}" ]]; then
    HOME="$(getent passwd "$PKEXEC_UID" | cut -d: -f6)"
    export HOME
    export XDG_DATA_HOME="${HOME}/.local/share"
fi

if [[ $UNINSTALL == 1 ]]; then
    if [[ -x "$TARGET_HELPER" ]]; then
        "$TARGET_HELPER" multipath disable || true
        if [[ -s "$INTERNAL_CONF" && ! -e "$INTERNAL_FLAG" ]]; then
            echo "Restoring the internal card before removing wifimimo..."
            "$TARGET_HELPER" internal enable || true
        fi
    fi
    rm -f -- "$TARGET_DISPATCHER" "$TARGET_POLICY" "$INTERNAL_RULE" "$WIFIMIMO_BLACKLIST" "$TARGET_RT_PROTOS"
    udevadm control --reload 2>/dev/null || true
    run_as_user systemctl --user disable --now "$USER_SERVICE_NAME" 2>/dev/null || true
    run_as_user rm -f -- "$HOME/.config/systemd/user/$USER_SERVICE_NAME"
    run_as_user kpackagetool6 -t Plasma/Applet --remove "$PLASMOID_PLUGIN_ID" 2>/dev/null || true
    rm -f -- "$TARGET_DAEMON" "$TARGET_MON" "$TARGET_PLASMOID_SOURCE" "$TARGET_TIDY" "$TARGET_DESKTOP"
    rm -rf -- "$TARGET_LIB_DIR"
    if [[ -d "$LEGACY_DIR" ]] && compgen -G "$LEGACY_DIR/*" >/dev/null; then
        echo "Your pre-wifimimo udev rules were kept in $LEGACY_DIR:"
        ls -1 "$LEGACY_DIR" | sed 's/^/  /'
        echo "Move them back to /etc/udev/rules.d/ if you still want them; $ETC_DIR is left in place."
    else
        rm -rf -- "$ETC_DIR"
    fi
    rm -rf -- /run/wifimimo
    echo "wifimimo uninstalled."
    exit 0
fi

# ---------------------------------------------------------------------------
# Install / upgrade
# ---------------------------------------------------------------------------

install -d -m755 "$TARGET_LIB_DIR"
for f in "${PY_MODULES[@]}"; do install -Dm644 "$ROOT_DIR/$f" "$TARGET_LIB_DIR/$f"; done
for f in "${PY_SCRIPTS[@]}"; do install -Dm755 "$ROOT_DIR/$f" "$TARGET_LIB_DIR/$f"; done
install -Dm644 "$ROOT_DIR/requirements.txt" "$TARGET_LIB_DIR/requirements.txt"
# The helper runs as root: root-owned, not a symlink (polkit's exec.path must
# match the real file), and its directory must stay root-owned too.
install -Dm755 -o root -g root "$ROOT_DIR/wifimimo-helper" "$TARGET_HELPER"
chown root:root "$TARGET_LIB_DIR" "$TARGET_LIB_DIR/wifimimo_shared.py"

python3 -m venv "$TARGET_VENV_DIR"
HOME=/root PIP_CACHE_DIR=/root/.cache/pip "$TARGET_VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
HOME=/root PIP_CACHE_DIR=/root/.cache/pip "$TARGET_VENV_DIR/bin/python" -m pip install -r "$TARGET_LIB_DIR/requirements.txt" >/dev/null

write_wrapper() {  # target script
    install -Dm755 /dev/stdin "$1" <<EOF2
#!/usr/bin/env bash
set -euo pipefail
exec "/usr/local/lib/wifimimo/.venv/bin/python" "/usr/local/lib/wifimimo/$2" "\$@"
EOF2
}
write_wrapper "$TARGET_DAEMON" wifimimo-daemon.py
write_wrapper "$TARGET_MON" wifimimo-mon.py
write_wrapper "$TARGET_PLASMOID_SOURCE" wifimimo-plasmoid-source.py
write_wrapper "$TARGET_TIDY" wifimimo-nm-tidy.py

install -Dm644 "$ROOT_DIR/wifimimo.desktop" "$TARGET_DESKTOP"
install -Dm644 -o root -g root "$ROOT_DIR/polkit/io.github.pizzimenti.wifimimo.policy" "$TARGET_POLICY"
install -d -m755 "$(dirname "$TARGET_RT_PROTOS")"
printf '211\twifimimo\n' > "$TARGET_RT_PROTOS"
chmod 644 "$TARGET_RT_PROTOS"
install -d -m755 "$ETC_DIR"
if [[ -d "$TARGET_DISPATCHER_DIR" ]]; then
    # NM ignores dispatcher scripts that aren't root-owned 0755.
    install -Dm755 -o root -g root "$ROOT_DIR/nm/90-wifimimo" "$TARGET_DISPATCHER"
else
    echo "Note: $TARGET_DISPATCHER_DIR not found; multipath won't re-apply itself on network changes."
fi

if [[ -n "$MANAGE_INTERNAL" ]]; then
    write_internal_conf
fi
"$TARGET_HELPER" internal sync-rules >/dev/null
if [[ -s "$INTERNAL_CONF" ]]; then
    handle_legacy_rules
    if command -v udevadm >/dev/null && [[ -e "$INTERNAL_RULE" ]]; then
        udevadm verify "$INTERNAL_RULE" >/dev/null 2>&1 || echo "Note: udevadm verify reported problems in $INTERNAL_RULE" >&2
    fi
fi
if [[ -e "$ETC_DIR/multipath-enabled" ]]; then
    echo "Multipath is enabled; re-applying with the new routing layout..."
    "$TARGET_HELPER" multipath apply || true
fi

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
printf '  %s\n' "$TARGET_LIB_DIR/" "$TARGET_HELPER" "$TARGET_DAEMON" "$TARGET_MON" \
    "$TARGET_PLASMOID_SOURCE" "$TARGET_TIDY" "$TARGET_DESKTOP" "$TARGET_POLICY" "$TARGET_RT_PROTOS"
[[ -e "$TARGET_DISPATCHER" ]] && printf '  %s\n' "$TARGET_DISPATCHER"
[[ -e "$INTERNAL_RULE" ]] && printf '  %s\n' "$INTERNAL_RULE"
printf '  %s\n' "$USER_SERVICE_PATH"
printf '\nUser service status:\n'
run_as_user systemctl --user status "$USER_SERVICE_NAME" --no-pager || true
printf '\nRun monitor:  wifimimo-mon\n'
printf 'Panel applet: org.kde.plasma.wifimimo\n'
printf 'View logs:    journalctl --user -u %s -f\n' "$USER_SERVICE_NAME"
printf '\nState file is JSON (schema_version 4). Multipath and the internal-card toggle\n'
printf 'live in the widget; the root helper is %s.\n' "$TARGET_HELPER"
