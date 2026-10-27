"""The installer's system settings and the dispatcher hook, run for real
against a scratch /etc with fake system commands on PATH.

install.sh stops after its function definitions when sourced, so each test
sources it, points its target paths into tmp_path and calls one function.
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
DISPATCHER = ROOT / "nm" / "90-wifimimo"
SYSCTL_SRC = ROOT / "sysctl" / "99-wifimimo-arp-flux.conf"
REAL_INSTALL = shutil.which("install")

# Hand-placed copies as they were set up before wifimimo carried them (the
# commands are what matters; comments and spacing are ignored when matching).
HAND_ARP = """\
# ARP flux, two NICs on one subnet.
#
# arp_ignore=1     answer only for addresses on the receiving interface.
net.ipv4.conf.all.arp_ignore = 1
net.ipv4.conf.all.arp_announce = 2
"""
HAND_BGSCAN = """\
#!/bin/sh
# Set smart bgscan on every WiFi connection:
#   - signal above -70 dBm: scan every 300s (stable, don't thrash)
#   - signal below -70 dBm: scan every 30s  (degrading, hunt aggressively)

INTERFACE="$1"
EVENT="$2"

[ "$EVENT" = "up" ] || exit 0

# Only act on wifi interfaces
TYPE=$(nmcli -g GENERAL.TYPE dev show "$INTERFACE" 2>/dev/null)
[ "$TYPE" = "wifi" ] || exit 0

wpa_cli -i "$INTERFACE" set_network 0 bgscan '"simple:30:-70:300"' >/dev/null 2>&1
"""
HAND_BLACKLIST = """\
# Internal WLAN off on purpose; the USB stick is the only radio.
blacklist mt7925e
"""


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def fakes(tmp_path):
    """Fake system commands; each appends its argv (one per line) to calls/<name>."""
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "calls"
    bin_dir.mkdir()
    calls.mkdir()
    for name in ("sysctl", "pkexec", "systemctl", "udevadm", "nmcli", "wpa_cli",
                 "timeout", "systemd-run", "kpackagetool6"):
        _write_exe(bin_dir / name, f"""#!/bin/sh
{{ printf '%s\\n' "$@"; echo --; }} >> "{calls}/{name}"
case "{name}" in
    sysctl) exit "${{FAKE_SYSCTL_RC:-0}}" ;;
    pkexec) exit 1 ;;
    nmcli) [ "$1 $2 $3 $4" = "-g GENERAL.TYPE dev show" ] && echo "${{FAKE_NM_TYPE-wifi}}" ;;
    wpa_cli) exit "${{FAKE_WPA_RC:-0}}" ;;
esac
exit 0
""")
    # Unprivileged stand-in for install(1): drop the -o/-g ownership options.
    _write_exe(bin_dir / "install", f"""#!/bin/bash
args=()
while [[ $# -gt 0 ]]; do
    case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
done
exec {REAL_INSTALL} "${{args[@]}}"
""")
    return bin_dir, calls


def calls_of(calls: Path, name: str) -> list[list[str]]:
    path = calls / name
    if not path.exists():
        return []
    runs, cur = [], []
    for line in path.read_text().splitlines():
        if line == "--":
            runs.append(cur)
            cur = []
        else:
            cur.append(line)
    return runs


@pytest.fixture
def etc(tmp_path):
    root = tmp_path / "etc"
    for d in ("sysctl.d", "NetworkManager/dispatcher.d", "modprobe.d", "wifimimo"):
        (root / d).mkdir(parents=True)
    (tmp_path / "usr-lib-sysctl.d").mkdir()
    return root


def run_fn(tmp_path, fakes, etc, script: str, **env):
    """Source install.sh with targets under tmp_path, then run `script`."""
    bin_dir, _ = fakes
    prelude = f"""
source "{INSTALL}"
TARGET_SYSCTL="{etc}/sysctl.d/99-wifimimo-arp-flux.conf"
SYSCTL_CONF="{etc}/sysctl.conf"
SYSCTL_DIRS=("{etc}/sysctl.d" "{tmp_path}/usr-lib-sysctl.d")
TARGET_DISPATCHER_DIR="{etc}/NetworkManager/dispatcher.d"
TARGET_DISPATCHER="$TARGET_DISPATCHER_DIR/90-wifimimo"
WIFIMIMO_BLACKLIST="{etc}/modprobe.d/wifimimo-internal.conf"
ETC_DIR="{etc}/wifimimo"
INTERNAL_CONF="$ETC_DIR/internal.conf"
LEGACY_DIR="$ETC_DIR/legacy"
"""
    full_env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", **env}
    return subprocess.run(["bash", "-c", prelude + script], env=full_env,
                          capture_output=True, text=True)


def place_hand_copies(etc: Path) -> dict[str, Path]:
    disp = etc / "NetworkManager" / "dispatcher.d"
    paths = {
        "arp": etc / "sysctl.d" / "99-arp-flux.conf",
        "bgscan": disp / "99-bgscan",
        "blacklist": etc / "modprobe.d" / "blacklist-mt7925e.conf",
    }
    paths["arp"].write_text(HAND_ARP)
    _write_exe(paths["bgscan"], HAND_BGSCAN)
    paths["blacklist"].write_text(HAND_BLACKLIST)
    # Look-alikes that do more, or something else, and must be left alone.
    (etc / "sysctl.d" / "99-arp-plus.conf").write_text(HAND_ARP + "vm.swappiness = 80\n")
    (etc / "sysctl.d" / "99-performance.conf").write_text("net.ipv4.tcp_fastopen = 3\n")
    _write_exe(disp / "99-bgscan-tuned", HAND_BGSCAN.replace("-70", "-65"))
    _write_exe(disp / "99-disable-ipv6", "#!/bin/sh\nexit 0\n")
    (disp / "pre-up.d").mkdir()
    (disp / "99-bgscan-link").symlink_to(paths["bgscan"])
    (etc / "modprobe.d" / "blacklist-unused.conf").write_text("blacklist pcspkr\n")
    (etc / "modprobe.d" / "mixed.conf").write_text("blacklist mt7925e\nblacklist pcspkr\n")
    (etc / "modprobe.d" / "cfg80211.conf").write_text("options cfg80211 ieee80211_regdom=US\n")
    (etc / "wifimimo" / "internal.conf").write_text(
        "# Managed internal Wi-Fi cards: <vendor:device> <driver> [parent bridge]\n"
        "14c3:7925 mt7925e 0000:00:02.2\n")
    return paths


# ---------------------------------------------------------------------------
# Sourcing and help
# ---------------------------------------------------------------------------


def test_sourcing_defines_the_functions_and_runs_nothing(tmp_path, fakes, etc):
    _, calls = fakes
    result = run_fn(tmp_path, fakes, etc, "declare -F install_arp_flux_sysctl "
                    "remove_arp_flux_sysctl handle_hand_placed_copies report_kept_legacy")
    assert result.returncode == 0, result.stderr
    assert list(calls.iterdir()) == []      # no pkexec, sysctl, systemctl, ...


def test_help_prints_the_whole_option_header(fakes):
    bin_dir, calls = fakes
    result = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "wifimimo installer."
    assert lines[-1].strip().startswith("./install.sh --uninstall")
    for option in ("--manage-internal", "--migrate-legacy-rules", "--uninstall"):
        assert option in result.stdout
    assert "set -euo" not in result.stdout
    assert calls_of(calls, "pkexec") == []


# ---------------------------------------------------------------------------
# ARP flux sysctl
# ---------------------------------------------------------------------------


def _settings(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ";")):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def test_shipped_sysctl_sets_exactly_the_arp_flux_keys_on_all():
    assert _settings(SYSCTL_SRC.read_text()) == {
        "net.ipv4.conf.all.arp_ignore": "1",
        "net.ipv4.conf.all.arp_announce": "2",
    }


def test_install_sysctl_installs_the_file_and_applies_it(tmp_path, fakes, etc):
    _, calls = fakes
    result = run_fn(tmp_path, fakes, etc, "install_arp_flux_sysctl")
    assert result.returncode == 0, result.stderr
    target = etc / "sysctl.d" / "99-wifimimo-arp-flux.conf"
    assert target.read_text() == SYSCTL_SRC.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert calls_of(calls, "sysctl") == [["-q", "-p", str(target)]]


def test_install_sysctl_carries_on_when_sysctl_fails(tmp_path, fakes, etc):
    result = run_fn(tmp_path, fakes, etc, "install_arp_flux_sysctl; echo after",
                    FAKE_SYSCTL_RC="1")
    assert result.returncode == 0
    assert result.stdout.strip() == "after"
    assert "applies at the next boot" in result.stderr
    assert (etc / "sysctl.d" / "99-wifimimo-arp-flux.conf").exists()


def test_remove_sysctl_puts_back_the_kernel_default(tmp_path, fakes, etc):
    _, calls = fakes
    target = etc / "sysctl.d" / "99-wifimimo-arp-flux.conf"
    target.write_text(SYSCTL_SRC.read_text())
    (etc / "sysctl.d" / "99-performance.conf").write_text("net.ipv4.tcp_fastopen = 3\n")
    result = run_fn(tmp_path, fakes, etc, "remove_arp_flux_sysctl")
    assert result.returncode == 0, result.stderr
    assert not target.exists()
    assert calls_of(calls, "sysctl") == [[
        "-q", "-w", "net.ipv4.conf.all.arp_ignore=0", "net.ipv4.conf.all.arp_announce=0"]]


@pytest.mark.parametrize("where, line", [
    ("etc", "net.ipv4.conf.all.arp_ignore = 1"),              # a hand-placed copy kept
    ("usr", "net/ipv4/conf/all/arp_announce=2"),             # slash form, vendor dir
    ("conf", "-net.ipv4.conf.all.arp_ignore=1"),             # /etc/sysctl.conf, '-' prefix
])
def test_remove_sysctl_reapplies_when_another_file_sets_the_keys(tmp_path, fakes, etc, where, line):
    _, calls = fakes
    (etc / "sysctl.d" / "99-wifimimo-arp-flux.conf").write_text(SYSCTL_SRC.read_text())
    other = {"etc": etc / "sysctl.d" / "99-arp-flux.conf",
             "usr": tmp_path / "usr-lib-sysctl.d" / "50-vendor.conf",
             "conf": etc / "sysctl.conf"}[where]
    other.write_text(f"# kept\n{line}\n")
    result = run_fn(tmp_path, fakes, etc, "remove_arp_flux_sysctl")
    assert result.returncode == 0, result.stderr
    assert calls_of(calls, "sysctl") == [["-q", "--system"]]


def test_remove_sysctl_without_our_file_touches_nothing(tmp_path, fakes, etc):
    _, calls = fakes
    (etc / "sysctl.d" / "99-arp-flux.conf").write_text(HAND_ARP)
    result = run_fn(tmp_path, fakes, etc, "remove_arp_flux_sysctl")
    assert result.returncode == 0, result.stderr
    assert calls_of(calls, "sysctl") == []
    assert (etc / "sysctl.d" / "99-arp-flux.conf").exists()


# ---------------------------------------------------------------------------
# Hand-placed copies
# ---------------------------------------------------------------------------


def test_finds_exactly_the_hand_placed_copies(tmp_path, fakes, etc):
    paths = place_hand_copies(etc)
    result = run_fn(tmp_path, fakes, etc, "find_hand_placed_copies")
    assert result.returncode == 0, result.stderr
    assert sorted(result.stdout.splitlines()) == sorted([
        f"sysctl {paths['arp']}",
        f"dispatcher {paths['bgscan']}",
        f"blacklist {paths['blacklist']}",
    ])


def test_our_own_files_are_never_copies(tmp_path, fakes, etc):
    paths = place_hand_copies(etc)
    shutil.copy(SYSCTL_SRC, etc / "sysctl.d" / "99-wifimimo-arp-flux.conf")
    _write_exe(etc / "NetworkManager" / "dispatcher.d" / "90-wifimimo", DISPATCHER.read_text())
    (etc / "modprobe.d" / "wifimimo-internal.conf").write_text("blacklist mt7925e\n")
    result = run_fn(tmp_path, fakes, etc, "find_hand_placed_copies")
    assert result.returncode == 0, result.stderr
    found = {line.split(" ", 1)[1] for line in result.stdout.splitlines()}
    assert found == {str(p) for p in paths.values()}


def test_a_blacklist_is_only_a_copy_when_its_card_is_managed(tmp_path, fakes, etc):
    paths = place_hand_copies(etc)
    (etc / "wifimimo" / "internal.conf").unlink()
    result = run_fn(tmp_path, fakes, etc, "find_hand_placed_copies")
    assert result.returncode == 0, result.stderr
    assert f"blacklist {paths['blacklist']}" not in result.stdout.splitlines()
    assert len(result.stdout.splitlines()) == 2


def test_without_the_flag_copies_are_listed_not_moved(tmp_path, fakes, etc):
    paths = place_hand_copies(etc)
    result = run_fn(tmp_path, fakes, etc, "handle_hand_placed_copies 0")
    assert result.returncode == 0, result.stderr
    for path in paths.values():
        assert path.exists()
        assert str(path) in result.stderr
    assert "--migrate-legacy-rules" in result.stderr
    assert not (etc / "wifimimo" / "legacy").exists()
    assert not (etc / "modprobe.d" / "wifimimo-internal.conf").exists()


def test_migration_moves_copies_aside_and_adopts_the_blacklist(tmp_path, fakes, etc):
    paths = place_hand_copies(etc)
    untouched = {p: p.read_text() for p in (etc / "sysctl.d").iterdir()
                 if p != paths["arp"]}
    result = run_fn(tmp_path, fakes, etc, "handle_hand_placed_copies 1")
    assert result.returncode == 0, result.stderr
    legacy = etc / "wifimimo" / "legacy"
    for path in paths.values():
        assert not path.exists()
        kept = Path(f"{legacy}{path}")
        assert kept.is_file()
    assert Path(f"{legacy}{paths['bgscan']}").read_text() == HAND_BGSCAN
    ours = etc / "modprobe.d" / "wifimimo-internal.conf"
    assert ours.read_text() == "blacklist mt7925e\n"
    assert stat.S_IMODE(ours.stat().st_mode) == 0o644
    for path, text in untouched.items():
        assert path.read_text() == text
    disp = etc / "NetworkManager" / "dispatcher.d"
    assert (disp / "99-bgscan-tuned").is_file() and (disp / "99-disable-ipv6").is_file()
    assert (disp / "99-bgscan-link").is_symlink()     # left alone (now dangling)
    for name in ("blacklist-unused.conf", "mixed.conf", "cfg80211.conf"):
        assert (etc / "modprobe.d" / name).exists()
    # a second run finds nothing more to do
    again = run_fn(tmp_path, fakes, etc, "find_hand_placed_copies")
    assert again.stdout == ""


def test_migration_does_not_repeat_a_blacklist_line_we_already_have(tmp_path, fakes, etc):
    place_hand_copies(etc)
    ours = etc / "modprobe.d" / "wifimimo-internal.conf"
    ours.write_text("blacklist mt7925e\n")
    result = run_fn(tmp_path, fakes, etc, "handle_hand_placed_copies 1")
    assert result.returncode == 0, result.stderr
    assert ours.read_text() == "blacklist mt7925e\n"


def test_no_copies_means_no_output(tmp_path, fakes, etc):
    result = run_fn(tmp_path, fakes, etc, "handle_hand_placed_copies 0; handle_hand_placed_copies 1")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""


def test_uninstall_report_names_where_each_kept_file_came_from(tmp_path, fakes, etc):
    legacy = etc / "wifimimo" / "legacy"
    legacy.mkdir()
    (legacy / "99-remove-mt7925-pci.rules").write_text("# old\n")       # moved flat, pre-1.9
    nested = Path(f"{legacy}{etc}/sysctl.d/99-arp-flux.conf")
    nested.parent.mkdir(parents=True)
    nested.write_text(HAND_ARP)
    result = run_fn(tmp_path, fakes, etc, "report_kept_legacy")
    assert result.returncode == 0, result.stderr
    assert "(was /etc/udev/rules.d/99-remove-mt7925-pci.rules)" in result.stdout
    assert f"(was {etc}/sysctl.d/99-arp-flux.conf)" in result.stdout


def test_uninstall_report_is_false_with_nothing_kept(tmp_path, fakes, etc):
    assert run_fn(tmp_path, fakes, etc, "report_kept_legacy").returncode == 1
    (etc / "wifimimo" / "legacy").mkdir()
    assert run_fn(tmp_path, fakes, etc, "report_kept_legacy").returncode == 1


def test_install_and_uninstall_paths_call_the_new_pieces():
    text = INSTALL.read_text()
    install_part, uninstall_part = text.split("# Install / upgrade")[1], \
        text.split("if [[ $UNINSTALL == 1 ]]; then")[1].split("\nfi\n")[0]
    assert 'TARGET_SYSCTL="/etc/sysctl.d/99-wifimimo-arp-flux.conf"' in text
    assert "install_arp_flux_sysctl" in install_part
    assert 'handle_hand_placed_copies "$MIGRATE_LEGACY"' in install_part
    assert "remove_arp_flux_sysctl" in uninstall_part
    assert "report_kept_legacy" in uninstall_part


# ---------------------------------------------------------------------------
# Dispatcher: background scan
# ---------------------------------------------------------------------------


def run_dispatcher(fakes, iface: str, action: str, **env):
    bin_dir, _ = fakes
    return subprocess.run(["sh", str(DISPATCHER), iface, action], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", **env})


def test_dispatcher_sets_bgscan_on_a_wifi_connection(fakes):
    _, calls = fakes
    result = run_dispatcher(fakes, "wifi0", "up")
    assert result.returncode == 0
    assert calls_of(calls, "nmcli") == [["-g", "GENERAL.TYPE", "dev", "show", "wifi0"]]
    assert calls_of(calls, "wpa_cli") == [
        ["-i", "wifi0", "set_network", "0", "bgscan", '"simple:30:-70:300"']]


@pytest.mark.parametrize("nm_type", ["ethernet", "wifi-p2p", "tun", ""])
def test_dispatcher_leaves_other_devices_alone(fakes, nm_type):
    _, calls = fakes
    assert run_dispatcher(fakes, "ether1", "up", FAKE_NM_TYPE=nm_type).returncode == 0
    assert calls_of(calls, "wpa_cli") == []


@pytest.mark.parametrize("action", ["down", "pre-up", "dhcp4-change", "connectivity-change", "reapply"])
def test_dispatcher_sets_bgscan_only_on_up(fakes, action):
    _, calls = fakes
    assert run_dispatcher(fakes, "wifi0", action).returncode == 0
    assert calls_of(calls, "nmcli") == []
    assert calls_of(calls, "wpa_cli") == []


def test_dispatcher_exits_0_when_wpa_cli_fails(fakes):
    assert run_dispatcher(fakes, "wifi0", "up", FAKE_WPA_RC="255").returncode == 0


@pytest.mark.skipif(Path("/etc/wifimimo/multipath-enabled").exists(),
                    reason="multipath is enabled on this machine")
def test_dispatcher_sets_bgscan_with_multipath_off_and_leaves_routing_alone(fakes):
    _, calls = fakes
    assert run_dispatcher(fakes, "wifi0", "up").returncode == 0
    assert len(calls_of(calls, "wpa_cli")) == 1
    assert calls_of(calls, "timeout") == [] and calls_of(calls, "systemd-run") == []


def test_dispatcher_bgscan_matches_the_hook_it_replaces():
    """Same bgscan string as the hand-placed hook, so adopting it changes nothing."""
    pattern = re.compile(r"""bgscan '"(simple:[^"]+)"'""")
    assert pattern.findall(DISPATCHER.read_text()) == pattern.findall(HAND_BGSCAN) == \
        pattern.findall(INSTALL.read_text()) == ["simple:30:-70:300"]
