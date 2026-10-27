"""The installer's system settings, the internal card's driver and blacklist,
and the dispatcher hook, run for real against a scratch /etc and /sys with
fake system commands on PATH.

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


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def fakes(tmp_path):
    """Fake system commands; each appends its argv (one per line) to calls/<name>
    and its name to calls/order. `timeout` runs wpa_cli (only) after logging;
    `modprobe -R` prints $FAKE_MODALIAS_MODULES (space-separated) one per line."""
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "calls"
    bin_dir.mkdir()
    calls.mkdir()
    for name in ("sysctl", "systemd-sysctl", "pkexec", "systemctl", "udevadm", "nmcli",
                 "wpa_cli", "timeout", "systemd-run", "kpackagetool6", "modprobe"):
        _write_exe(bin_dir / name, f"""#!/bin/sh
{{ printf '%s\\n' "$@"; echo --; }} >> "{calls}/{name}"
echo "{name}" >> "{calls}/order"
case "{name}" in
    sysctl) exit "${{FAKE_SYSCTL_RC:-0}}" ;;
    pkexec) exit 1 ;;
    wpa_cli) exit "${{FAKE_WPA_RC:-0}}" ;;
    timeout)
        [ -n "${{FAKE_TIMEOUT_RC:-}}" ] && exit "$FAKE_TIMEOUT_RC"
        shift; [ "$1" = wpa_cli ] && exec "$@" ;;
    modprobe)
        [ "$1" = -R ] || exit 0
        [ -n "${{FAKE_MODPROBE_RC:-}}" ] && exit "$FAKE_MODPROBE_RC"
        for m in ${{FAKE_MODALIAS_MODULES:-}}; do echo "$m"; done ;;
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
    (tmp_path / "sys-bus-pci-devices").mkdir()
    return root


def run_fn(tmp_path, fakes, etc, script: str, **env):
    """Source install.sh with targets under tmp_path, then run `script`."""
    bin_dir, _ = fakes
    prelude = f"""
source "{INSTALL}"
TARGET_SYSCTL="{etc}/sysctl.d/99-wifimimo-arp-flux.conf"
SYSTEMD_SYSCTL="{bin_dir}/systemd-sysctl"
TARGET_DISPATCHER_DIR="{etc}/NetworkManager/dispatcher.d"
TARGET_DISPATCHER="$TARGET_DISPATCHER_DIR/90-wifimimo"
WIFIMIMO_BLACKLIST="{etc}/modprobe.d/wifimimo-internal.conf"
ETC_DIR="{etc}/wifimimo"
INTERNAL_CONF="$ETC_DIR/internal.conf"
INTERNAL_FLAG="$ETC_DIR/internal-enabled"
LEGACY_DIR="$ETC_DIR/legacy"
SYS_PCI_DIR="{tmp_path}/sys-bus-pci-devices"
"""
    full_env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", **env}
    return subprocess.run(["bash", "-c", prelude + script], env=full_env,
                          capture_output=True, text=True)


def fake_pci_card(tmp_path, addr: str, vendor: str, device: str, *, driver: str | None,
                  bridge: str = "0000:00:1c.0", cls: str = "0x028000",
                  modalias: str | None = "pci:v00008086d00002725sv00008086sd00000024bc02sc80i00"):
    """A card under a fake /sys/devices tree, linked from the fake /sys/bus/pci/devices."""
    node = tmp_path / "sys-devices" / "pci0000:00" / bridge / addr
    node.mkdir(parents=True)
    (node / "class").write_text(cls + "\n")
    (node / "vendor").write_text(f"0x{vendor}\n")
    (node / "device").write_text(f"0x{device}\n")
    if modalias is not None:
        (node / "modalias").write_text(modalias + "\n")
    if driver:
        drv = tmp_path / "sys-drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        (node / "driver").symlink_to(drv)
    (tmp_path / "sys-bus-pci-devices" / addr).symlink_to(node)
    return node


# ---------------------------------------------------------------------------
# Sourcing and help
# ---------------------------------------------------------------------------


def test_sourcing_defines_the_functions_and_runs_nothing(tmp_path, fakes, etc):
    _, calls = fakes
    result = run_fn(tmp_path, fakes, etc, "declare -F install_arp_flux_sysctl remove_arp_flux_sysctl "
                    "detect_pci_driver write_internal_conf write_internal_blacklist")
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


def test_install_and_uninstall_paths_call_the_system_pieces():
    text = INSTALL.read_text()
    install_part = text.split("# Install / upgrade")[1]
    uninstall_part = text.split("if [[ $UNINSTALL == 1 ]]; then")[1].split("\nfi\n")[0]
    assert 'TARGET_SYSCTL="/etc/sysctl.d/99-wifimimo-arp-flux.conf"' in text
    assert "install_arp_flux_sysctl" in install_part
    assert "write_internal_blacklist" in install_part
    assert "remove_arp_flux_sysctl" in uninstall_part
    assert '"$WIFIMIMO_BLACKLIST"' in uninstall_part


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


def test_remove_sysctl_resets_both_keys_then_restores_what_boot_would_give(tmp_path, fakes, etc):
    _, calls = fakes
    target = etc / "sysctl.d" / "99-wifimimo-arp-flux.conf"
    target.write_text(SYSCTL_SRC.read_text())
    result = run_fn(tmp_path, fakes, etc, "remove_arp_flux_sysctl")
    assert result.returncode == 0, result.stderr
    assert not target.exists()
    assert calls_of(calls, "sysctl") == [[
        "-q", "-w", "net.ipv4.conf.all.arp_ignore=0", "net.ipv4.conf.all.arp_announce=0"]]
    # only these two keys, so a file that sets one of them (or a glob line
    # covering it) gets back exactly that one, and nothing else is re-applied
    assert calls_of(calls, "systemd-sysctl") == [[
        "--prefix=/net/ipv4/conf/all/arp_ignore", "--prefix=/net/ipv4/conf/all/arp_announce"]]
    assert (calls / "order").read_text().split() == ["sysctl", "systemd-sysctl"]


def test_remove_sysctl_carries_on_without_systemd_sysctl(tmp_path, fakes, etc):
    _, calls = fakes
    (etc / "sysctl.d" / "99-wifimimo-arp-flux.conf").write_text(SYSCTL_SRC.read_text())
    result = run_fn(tmp_path, fakes, etc,
                    f'SYSTEMD_SYSCTL="{tmp_path}/missing"; remove_arp_flux_sysctl; echo after')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "after"
    assert len(calls_of(calls, "sysctl")) == 1


def test_remove_sysctl_without_our_file_touches_nothing(tmp_path, fakes, etc):
    _, calls = fakes
    (etc / "sysctl.d" / "50-other.conf").write_text("net.ipv4.conf.all.arp_ignore = 1\n")
    result = run_fn(tmp_path, fakes, etc, "remove_arp_flux_sysctl")
    assert result.returncode == 0, result.stderr
    assert calls_of(calls, "sysctl") == [] and calls_of(calls, "systemd-sysctl") == []
    assert (etc / "sysctl.d" / "50-other.conf").exists()


def _can_run_private_netns() -> bool:
    if not (shutil.which("unshare") and Path("/usr/lib/systemd/systemd-sysctl").exists()):
        return False
    return subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _can_run_private_netns(),
                    reason="needs unshare -rn and systemd-sysctl")
@pytest.mark.parametrize("config, ignore, announce", [
    ("net.ipv4.conf.all.arp_ignore = 1\n", "1", "0"),         # one key: the other stays 0
    ("net.ipv4.conf.*.arp_ignore = 1\n", "1", "0"),           # glob line
    ("net/ipv4/conf/all/arp_announce=2\n", "0", "2"),         # slash form
    ("net.ipv4.conf.all.rp_filter = 2\n", "0", "0"),          # other keys: not applied
])
def test_systemd_sysctl_prefixes_restore_only_these_keys(tmp_path, config, ignore, announce):
    """The real systemd-sysctl, in a private user + network namespace (the
    host's settings are never touched): what the uninstall relies on."""
    conf = tmp_path / "other.conf"
    conf.write_text(config)
    script = f"""
P=/proc/sys/net/ipv4/conf
for k in all/arp_ignore all/arp_announce default/arp_ignore all/rp_filter; do echo 2 > $P/$k; done
sysctl -q -w net.ipv4.conf.all.arp_ignore=0 net.ipv4.conf.all.arp_announce=0
/usr/lib/systemd/systemd-sysctl --prefix=/net/ipv4/conf/all/arp_ignore \\
    --prefix=/net/ipv4/conf/all/arp_announce {conf}
cat $P/all/arp_ignore $P/all/arp_announce $P/default/arp_ignore $P/all/rp_filter
"""
    result = subprocess.run(["unshare", "-rn", "sh", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    # default.arp_ignore and rp_filter keep the 2 written above: nothing else applied
    assert result.stdout.split() == [ignore, announce, "2", "2"]


# ---------------------------------------------------------------------------
# Internal card: the driver is detected, the blacklist is wifimimo's own
# ---------------------------------------------------------------------------


def test_driver_of_a_bound_card_is_the_bound_one(tmp_path, fakes, etc):
    _, calls = fakes
    fake_pci_card(tmp_path, "0000:01:00.0", "8086", "2725", driver="iwlwifi")
    result = run_fn(tmp_path, fakes, etc, "detect_pci_driver 0000:01:00.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "iwlwifi\n"
    assert calls_of(calls, "modprobe") == []


def test_driver_of_an_unbound_card_comes_from_its_modalias(tmp_path, fakes, etc):
    _, calls = fakes
    fake_pci_card(tmp_path, "0000:01:00.0", "8086", "2725", driver=None)
    result = run_fn(tmp_path, fakes, etc, "detect_pci_driver 0000:01:00.0",
                    FAKE_MODALIAS_MODULES="iwlwifi")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "iwlwifi\n"
    assert calls_of(calls, "modprobe") == [
        ["-R", "pci:v00008086d00002725sv00008086sd00000024bc02sc80i00"]]


@pytest.mark.parametrize("modules, rc, modalias", [
    ("drv_a drv_b", None, "pci:v1"),       # ambiguous: the user names it
    ("", None, "pci:v1"),                  # no module claims it
    ("iwlwifi", "1", "pci:v1"),            # modprobe fails
    ("iwlwifi", None, None),               # no modalias to resolve
])
def test_driver_detection_gives_up_rather_than_guess(tmp_path, fakes, etc, modules, rc, modalias):
    fake_pci_card(tmp_path, "0000:01:00.0", "8086", "2725", driver=None, modalias=modalias)
    env = {"FAKE_MODALIAS_MODULES": modules}
    if rc:
        env["FAKE_MODPROBE_RC"] = rc
    result = run_fn(tmp_path, fakes, etc, "detect_pci_driver 0000:01:00.0", **env)
    assert result.returncode != 0
    assert result.stdout == ""


def test_manage_internal_records_a_present_unbound_card_with_its_detected_driver(tmp_path, fakes, etc):
    fake_pci_card(tmp_path, "0000:01:00.0", "8086", "2725", driver=None, bridge="0000:00:1c.0")
    result = run_fn(tmp_path, fakes, etc, "MANAGE_INTERNAL=8086:2725; write_internal_conf",
                    FAKE_MODALIAS_MODULES="iwlwifi")
    assert result.returncode == 0, result.stderr
    lines = (etc / "wifimimo" / "internal.conf").read_text().splitlines()
    assert lines[1:] == ["8086:2725 iwlwifi 0000:00:1c.0"]
    assert (etc / "wifimimo" / "internal-enabled").exists()     # present: starts on


def test_manage_internal_auto_takes_bound_wifi_cards_only(tmp_path, fakes, etc):
    fake_pci_card(tmp_path, "0000:01:00.0", "8086", "2725", driver="iwlwifi")
    fake_pci_card(tmp_path, "0000:02:00.0", "10ec", "8168", driver="r8169", cls="0x020000",
                  bridge="0000:00:1d.0")
    result = run_fn(tmp_path, fakes, etc, "MANAGE_INTERNAL=auto; write_internal_conf")
    assert result.returncode == 0, result.stderr
    lines = (etc / "wifimimo" / "internal.conf").read_text().splitlines()
    assert lines[1:] == ["8086:2725 iwlwifi 0000:00:1c.0"]


def test_manage_internal_needs_the_driver_named_for_an_absent_card(tmp_path, fakes, etc):
    result = run_fn(tmp_path, fakes, etc, "MANAGE_INTERNAL=8086:2725; write_internal_conf")
    assert result.returncode == 3
    assert "Pass it as 8086:2725=<driver>" in result.stderr
    named = run_fn(tmp_path, fakes, etc, "MANAGE_INTERNAL=8086:2725=iwlwifi; write_internal_conf")
    assert named.returncode == 0, named.stderr
    assert (etc / "wifimimo" / "internal.conf").read_text().splitlines()[1:] == ["8086:2725 iwlwifi"]
    assert not (etc / "wifimimo" / "internal-enabled").exists()   # absent: not switched on


def test_blacklist_is_written_from_internal_conf_whatever_else_says(tmp_path, fakes, etc):
    (etc / "wifimimo" / "internal.conf").write_text(
        "# Managed internal Wi-Fi cards: <vendor:device> <driver> [parent bridge]\n"
        "8086:2725 iwlwifi 0000:00:1c.0\n"
        "17cb:1103 ath11k_pci\n"
        "8086:51f0 iwlwifi\n")
    # someone else's file blacklisting the same driver doesn't make ours optional
    (etc / "modprobe.d" / "local.conf").write_text("blacklist iwlwifi\n")
    for _ in range(2):                                  # idempotent
        result = run_fn(tmp_path, fakes, etc, "write_internal_blacklist")
        assert result.returncode == 0, result.stderr
    ours = etc / "modprobe.d" / "wifimimo-internal.conf"
    body = [line for line in ours.read_text().splitlines() if not line.startswith("#")]
    assert body == ["blacklist ath11k_pci", "blacklist iwlwifi"]
    assert stat.S_IMODE(ours.stat().st_mode) == 0o644
    assert (etc / "modprobe.d" / "local.conf").read_text() == "blacklist iwlwifi\n"


def test_no_managed_card_means_no_blacklist(tmp_path, fakes, etc):
    ours = etc / "modprobe.d" / "wifimimo-internal.conf"
    ours.write_text("blacklist iwlwifi\n")              # left from an earlier install
    result = run_fn(tmp_path, fakes, etc, "write_internal_blacklist")
    assert result.returncode == 0, result.stderr
    assert not ours.exists()


# ---------------------------------------------------------------------------
# Dispatcher: background scan
# ---------------------------------------------------------------------------


def run_dispatcher(tmp_path, fakes, iface: str, action: str, **env):
    """Run the hook against a fake /sys/class/net: wlan0 has a phy80211 link
    and a wireless/ dir, wlan1 only wireless/, wlan2 only phy80211, eth0 neither."""
    bin_dir, _ = fakes
    net = tmp_path / "sys-class-net"
    for name, marks in {"wlan0": ("phy80211", "wireless"), "wlan1": ("wireless",),
                        "wlan2": ("phy80211",), "eth0": ()}.items():
        (net / name).mkdir(parents=True, exist_ok=True)
        if "wireless" in marks:
            (net / name / "wireless").mkdir(exist_ok=True)
        if "phy80211" in marks and not (net / name / "phy80211").exists():
            (net / name / "phy80211").symlink_to(tmp_path)
    return subprocess.run(["sh", str(DISPATCHER), iface, action], capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin",
                               "WIFIMIMO_TEST_SYS_CLASS_NET": str(net), **env})


BGSCAN_ARGS = ["set_network", "0", "bgscan", '"simple:30:-70:300"']


@pytest.mark.parametrize("iface", ["wlan0", "wlan1", "wlan2"])
def test_dispatcher_sets_bgscan_on_a_wifi_connection(tmp_path, fakes, iface):
    _, calls = fakes
    result = run_dispatcher(tmp_path, fakes, iface, "up")
    assert result.returncode == 0
    assert calls_of(calls, "timeout")[0] == ["5", "wpa_cli", "-i", iface, *BGSCAN_ARGS]
    assert calls_of(calls, "wpa_cli") == [["-i", iface, *BGSCAN_ARGS]]
    assert calls_of(calls, "nmcli") == []          # no D-Bus round trip to NM


@pytest.mark.parametrize("iface", ["eth0", "tailscale0", "lo", ""])
def test_dispatcher_leaves_other_devices_alone(tmp_path, fakes, iface):
    _, calls = fakes
    assert run_dispatcher(tmp_path, fakes, iface, "up").returncode == 0
    assert calls_of(calls, "wpa_cli") == []


@pytest.mark.parametrize("action", ["down", "pre-up", "dhcp4-change", "connectivity-change", "reapply"])
def test_dispatcher_sets_bgscan_only_on_up(tmp_path, fakes, action):
    _, calls = fakes
    assert run_dispatcher(tmp_path, fakes, "wlan0", action).returncode == 0
    assert calls_of(calls, "wpa_cli") == []


@pytest.mark.parametrize("env", [{"FAKE_WPA_RC": "255"}, {"FAKE_TIMEOUT_RC": "124"}])
def test_dispatcher_exits_0_when_wpa_cli_fails_or_hangs(tmp_path, fakes, env):
    assert run_dispatcher(tmp_path, fakes, "wlan0", "up", **env).returncode == 0


@pytest.mark.skipif(Path("/etc/wifimimo/multipath-enabled").exists(),
                    reason="multipath is enabled on this machine")
def test_dispatcher_sets_bgscan_with_multipath_off_and_leaves_routing_alone(tmp_path, fakes):
    _, calls = fakes
    assert run_dispatcher(tmp_path, fakes, "wlan0", "up").returncode == 0
    assert len(calls_of(calls, "wpa_cli")) == 1
    assert len(calls_of(calls, "timeout")) == 1      # the wpa_cli cap, not the helper
    assert calls_of(calls, "systemd-run") == []


def test_dispatcher_bgscan_is_the_documented_one():
    found = re.findall(r"""bgscan '"(simple:[^"]+)"'""", DISPATCHER.read_text())
    assert found == ["simple:30:-70:300"]
    assert f"`{found[0]}`" in (ROOT / "README.md").read_text()
