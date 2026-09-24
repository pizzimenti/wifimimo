"""Per-radio facts that live outside nl80211: bus, naming, addressing,
traffic, history, health flags, and the multipath / internal-card status.

Everything that touches the filesystem takes a root path (`sys_root`,
`etc_dir`, ...) so tests can point it at a fixture tree. Nothing here needs
root; the privileged side is `wifimimo-helper`.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
from collections import deque
from pathlib import Path

from wifimimo_core import ALERT_DIFF_DBM, ALERT_RETRY_PCT, ALERT_SIGNAL_DBM
import wifimimo_shared as shared

SYS_ROOT = Path("/sys")
PROC_ROOT = Path("/proc")
STATE_DIR = Path.home() / ".local" / "state" / "wifimimo"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "wifimimo"
NAMES_PATH = CONFIG_DIR / "names.json"
RADIOS_PATH = STATE_DIR / "radios.json"

SIGNAL_HISTORY_S = 60.0
PALETTE_SIZE = 5

# USB wifi drivers whose chips are USB 3 capable. A stick bound to one of
# these and running at <= 480 Mb/s on a port that has a SuperSpeed peer was
# (almost always) inserted so the SuperSpeed pins never mated.
USB3_CAPABLE_DRIVERS = frozenset({
    "mt7921u", "mt7925u", "mt76x2u",
    "rtw88_8812au", "rtw88_8821au", "rtw88_8822bu", "rtw88_8822cu",
    "rtw89_8852au", "rtw89_8852bu", "rtw89_8852cu", "rtw89_8922au",
    "8812au", "88XXau", "88x2bu",
})

# Friendly names for known models. USB product strings are useless for this
# ("Wireless_Device" from "MediaTek Inc." on both Netgear sticks), so the
# vendor:product id is the only reliable key.
KNOWN_MODELS = {
    "0846:9072": "A9000",   # Netgear Nighthawk BE6500 (mt7925u)
    "0846:9060": "A8000",   # Netgear Nighthawk AXE3000 (mt7921u)
}
BUILTIN_NAME = "Built-in"

USB_IDS_PATHS = (
    Path("/usr/share/hwdata/usb.ids"),
    Path("/usr/share/misc/usb.ids"),
    Path("/var/lib/usbutils/usb.ids"),
)


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(_read(path), 0)
    except ValueError:
        return default


def _resolve(path: Path) -> Path | None:
    try:
        if not (path.exists() or path.is_symlink()):
            return None
        return path.resolve()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Device / bus info
# ---------------------------------------------------------------------------


def _usb_port_for(udev: Path) -> Path | None:
    """The hub port a USB device is plugged into.

    Works for root-hub ports (usb8/8-0:1.0/usb8-port1) and external hubs
    (7-1/7-1:1.0/7-1-port2): the port directory's `device` link points back
    at the child device.
    """
    parent = udev.parent
    try:
        candidates = sorted(parent.glob("*:1.0/*-port*"))
    except OSError:
        return None
    for port in candidates:
        if _resolve(port / "device") == udev:
            return port
    return None


def collect_device_info(iface: str, sys_root: Path = SYS_ROOT) -> dict:
    """Bus, driver, ids, MACs and (for USB) link speed + port capability."""
    net = sys_root / "class" / "net" / iface
    info = {
        "perm_mac": (_read(net / "phy80211" / "macaddress") or _read(net / "address")).lower(),
        "mac": _read(net / "address").lower(),
        "operstate": _read(net / "operstate"),
        "bus": "",
        "driver": "",
        "dev_id": "",
        "dev_path": "",
        "usb_speed_mbps": 0,
        "usb_port_usb3": False,
    }
    dev = _resolve(net / "device")
    if dev is None:
        return info
    subsystem = _resolve(dev / "subsystem")
    info["bus"] = subsystem.name if subsystem else ""
    driver = _resolve(dev / "driver")
    info["driver"] = driver.name if driver else ""

    if info["bus"] == "pci":
        info["dev_path"] = dev.name
        vendor = _read(dev / "vendor").removeprefix("0x")
        device = _read(dev / "device").removeprefix("0x")
        if vendor and device:
            info["dev_id"] = f"{vendor}:{device}".lower()
    elif info["bus"] == "usb":
        # net/X/device is the USB *interface* node ("2-2:1.0"); the device
        # with speed and ids is its parent ("2-2").
        udev = dev.parent if ":" in dev.name else dev
        info["dev_path"] = udev.name
        vendor = _read(udev / "idVendor")
        product = _read(udev / "idProduct")
        if vendor and product:
            info["dev_id"] = f"{vendor}:{product}".lower()
        info["usb_speed_mbps"] = _read_int(udev / "speed")
        port = _usb_port_for(udev)
        info["usb_port_usb3"] = bool(port and (port / "peer").exists())
    return info


# ---------------------------------------------------------------------------
# Card names
# ---------------------------------------------------------------------------


def lookup_usb_vendor(vendor_id: str, paths=USB_IDS_PATHS) -> str:
    """Short vendor name from the usb.ids database ('NetGear, Inc.' -> 'NetGear')."""
    vendor_id = vendor_id.lower()
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line[:4].lower() == vendor_id and line[4:6] == "  ":
                        name = line[6:].strip()
                        return name.split(",", 1)[0].strip()
        except OSError:
            continue
    return ""


def card_name(info: dict, overrides: dict | None = None, usb_ids_paths=USB_IDS_PATHS) -> str:
    """Human name for a radio: override > known model > Built-in > vendor+chip."""
    perm_mac = info.get("perm_mac", "")
    if overrides and perm_mac in overrides:
        return overrides[perm_mac]
    dev_id = info.get("dev_id", "")
    if dev_id in KNOWN_MODELS:
        return KNOWN_MODELS[dev_id]
    bus = info.get("bus", "")
    if bus in ("pci", "sdio", "platform"):
        return BUILTIN_NAME
    if bus == "usb":
        chip = info.get("driver", "").upper()
        vendor = lookup_usb_vendor(dev_id.split(":", 1)[0], usb_ids_paths) if dev_id else ""
        label = " ".join(part for part in (vendor, chip) if part)
        return label or dev_id or "USB Wi-Fi"
    return info.get("iface", "") or "Wi-Fi"


class NameOverrides:
    """~/.config/wifimimo/names.json ({"<perm mac>": "name"}), re-read on change."""

    def __init__(self, path: Path = NAMES_PATH) -> None:
        self.path = path
        self._mtime: float | None = None
        self._names: dict[str, str] = {}

    def get(self) -> dict[str, str]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._names = None, {}
            return self._names
        if mtime != self._mtime:
            self._mtime = mtime
            self._names = {}
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                self._names = {
                    str(mac).lower(): str(name).strip()[:32]
                    for mac, name in loaded.items()
                    if isinstance(name, str) and name.strip()
                }
        return self._names


# ---------------------------------------------------------------------------
# IPv4 addressing
# ---------------------------------------------------------------------------


def _run_json(cmd: list[str]):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2, check=False)
        return json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def parse_ip_addr(entries) -> dict[str, dict]:
    """`ip -j -4 addr show` -> {iface: {ipv4, prefixlen, subnet}} (global scope)."""
    result: dict[str, dict] = {}
    for link in entries if isinstance(entries, list) else []:
        name = link.get("ifname")
        for addr in link.get("addr_info", []) or []:
            if addr.get("family") != "inet" or addr.get("scope") != "global":
                continue
            local, prefix = addr.get("local"), addr.get("prefixlen")
            if not name or not local or prefix is None:
                continue
            try:
                subnet = str(ipaddress.ip_interface(f"{local}/{prefix}").network)
            except ValueError:
                continue
            result.setdefault(name, {"ipv4": local, "prefixlen": int(prefix), "subnet": subnet})
    return result


def parse_default_routes(entries) -> dict[str, str]:
    """`ip -j -4 route show default` (main table) -> {iface: gateway}."""
    gateways: dict[str, str] = {}
    for route in entries if isinstance(entries, list) else []:
        if route.get("dst") != "default":
            continue
        if "dev" in route and route.get("gateway"):
            gateways.setdefault(route["dev"], route["gateway"])
        for hop in route.get("nexthops", []) or []:
            if hop.get("dev") and hop.get("gateway"):
                gateways.setdefault(hop["dev"], hop["gateway"])
    return gateways


def collect_ipv4() -> dict[str, dict]:
    """{iface: {ipv4, prefixlen, subnet, gateway}} with two forks per poll."""
    addrs = parse_ip_addr(_run_json(["ip", "-j", "-4", "addr", "show"]))
    gws = parse_default_routes(_run_json(["ip", "-j", "-4", "route", "show", "default"]))
    for name, gw in gws.items():
        addrs.setdefault(name, {"ipv4": "", "prefixlen": 0, "subnet": ""})["gateway"] = gw
    return addrs


# ---------------------------------------------------------------------------
# Throughput, signal history, stable colors
# ---------------------------------------------------------------------------


class ThroughputTracker:
    """Mb/s from netdev byte counters, keyed by permanent MAC."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[int, int, float]] = {}

    def update(self, key: str, rx_bytes: int, tx_bytes: int, now: float) -> tuple[float, float]:
        previous = self._last.get(key)
        self._last[key] = (rx_bytes, tx_bytes, now)
        if previous is None:
            return 0.0, 0.0
        prx, ptx, pnow = previous
        dt = now - pnow
        if dt <= 0 or rx_bytes < prx or tx_bytes < ptx:
            return 0.0, 0.0
        return (
            round((rx_bytes - prx) * 8 / dt / 1e6, 2),
            round((tx_bytes - ptx) * 8 / dt / 1e6, 2),
        )

    def sample(self, key: str, iface: str, now: float, sys_root: Path = SYS_ROOT) -> tuple[float, float]:
        stats = sys_root / "class" / "net" / iface / "statistics"
        return self.update(key, _read_int(stats / "rx_bytes"), _read_int(stats / "tx_bytes"), now)


class SignalRing:
    """Last SIGNAL_HISTORY_S seconds of (wall time, dBm) per radio."""

    def __init__(self, window_s: float = SIGNAL_HISTORY_S) -> None:
        self.window_s = window_s
        self._rings: dict[str, deque] = {}

    def append(self, key: str, ts: float, dbm: int) -> None:
        ring = self._rings.setdefault(key, deque())
        if dbm < 0:
            ring.append((round(ts, 1), int(dbm)))
        self._prune(ring, ts)

    def _prune(self, ring: deque, now: float) -> None:
        cutoff = now - self.window_s
        # Keep one point just before the cutoff so the line reaches the
        # graph's left edge instead of starting mid-air.
        while len(ring) >= 2 and ring[1][0] <= cutoff:
            ring.popleft()

    def snapshot(self, key: str, now: float) -> list[list]:
        ring = self._rings.get(key)
        if not ring:
            return []
        self._prune(ring, now)
        return [[ts, dbm] for ts, dbm in ring]


class RadioColors:
    """Persistent perm-MAC -> palette slot, first come first served."""

    def __init__(self, path: Path = RADIOS_PATH) -> None:
        self.path = path
        self._slots: dict[str, int] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._slots = {str(k): int(v) for k, v in loaded.items() if isinstance(v, int)}
        except (OSError, ValueError):
            pass

    def index(self, key: str) -> int:
        if not key:
            return -1
        if key not in self._slots:
            self._slots[key] = len(self._slots)
            self._save()
        return self._slots[key] % PALETTE_SIZE

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._slots, indent=1) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Health flags
# ---------------------------------------------------------------------------


def _flag(code: str, severity: str, title: str, detail: str = "") -> dict:
    return {"code": code, "severity": severity, "title": title, "detail": detail}


SEVERITY_RANK = {"info": 0, "warn": 1, "crit": 2}


def link_health_flags(state: dict) -> list[dict]:
    """Per-link checks (the pre-v1.0 `collect_issues`, plus MLD weak signal)."""
    flags: list[dict] = []
    if not state.get("connected"):
        return flags
    antennas = [int(v) for v in state.get("signal_antennas", []) or []]
    # Empty antenna list = driver doesn't expose chain signal (mt7925 MLO
    # aggregates to MLD level): a telemetry gap, not an offline chain.
    if 0 < len(antennas) < 2:
        flags.append(_flag("mimo_offline", "warn", "MIMO offline",
                           f"Only {len(antennas)}/2 antennas reporting"))
    for index, dbm in enumerate(antennas, start=1):
        if dbm < ALERT_SIGNAL_DBM:
            flags.append(_flag(f"weak_antenna_{index}", "warn", f"Weak antenna {index}",
                               f"{dbm} dBm (threshold {ALERT_SIGNAL_DBM} dBm)"))
    if len(antennas) >= 2:
        spread = max(antennas) - min(antennas)
        if spread > ALERT_DIFF_DBM:
            flags.append(_flag("antenna_imbalance", "warn", "Antenna imbalance",
                               f"{spread} dB spread ({min(antennas)} to {max(antennas)} dBm)"))
    signal = int(state.get("signal_dbm", 0) or 0)
    if not antennas and signal < 0 and signal < ALERT_SIGNAL_DBM:
        flags.append(_flag("weak_signal", "warn", "Weak signal",
                           f"{signal} dBm (threshold {ALERT_SIGNAL_DBM} dBm)"))
    tx_nss = int(state.get("tx_nss", 0) or 0)
    rx_nss = int(state.get("rx_nss", 0) or 0)
    # Both directions must report NSS before calling it a 1x1 collapse —
    # half-populated rate info during association is not a fault.
    if tx_nss > 0 and rx_nss > 0 and max(tx_nss, rx_nss) < 2:
        flags.append(_flag("mimo_degraded", "crit", "MIMO degraded",
                           f"Both directions at NSS 1 (TX {tx_nss}, RX {rx_nss})"))
    retry = float(state.get("retry_10s_pct", 0.0) or 0.0)
    if retry > ALERT_RETRY_PCT:
        flags.append(_flag("high_retry", "warn", "High interference",
                           f"10 s TX retry rate {retry:.1f}% (threshold {ALERT_RETRY_PCT}%)"))
    return flags


def device_flags(state: dict) -> list[dict]:
    """Bus-level problems: a USB 3 stick that negotiated USB 2."""
    if state.get("bus") != "usb":
        return []
    speed = int(state.get("usb_speed_mbps", 0) or 0)
    if not speed or speed > 480:
        return []
    if state.get("driver") not in USB3_CAPABLE_DRIVERS:
        return []
    if state.get("usb_port_usb3"):
        return [_flag("usb2_on_usb3_port", "warn", "Running at USB 2",
                      f"USB 3 stick negotiated {speed} Mb/s on a USB 3 port. "
                      "Reseat it firmly so the SuperSpeed pins mate.")]
    return [_flag("usb3_stick_on_usb2_port", "info", "USB 2 port",
                  f"USB 3 stick on a USB 2-only port ({speed} Mb/s).")]


def read_sysctl(name: str, key: str, proc_root: Path = PROC_ROOT) -> int:
    return _read_int(proc_root / "sys" / "net" / "ipv4" / "conf" / name / key, 0)


def effective_sysctl(iface: str, key: str, reader=read_sysctl) -> int:
    """arp_ignore / arp_announce / rp_filter use max(all, iface)."""
    return max(reader("all", key), reader(iface, key))


def cross_iface_flags(states: dict[str, dict], reader=read_sysctl) -> dict[str, list[dict]]:
    """Problems that only exist between radios: shared airtime, ARP flux."""
    result: dict[str, list[dict]] = {name: [] for name in states}
    up = {name: s for name, s in states.items() if s.get("connected")}

    def bssids(s: dict) -> set[str]:
        found = {s.get("bssid", "").lower()} | {
            (link.get("bssid") or "").lower() for link in s.get("links", []) or []
        }
        found.discard("")
        return found

    def freqs(s: dict) -> set[int]:
        found = {int(s.get("freq_mhz", 0) or 0)} | {
            int(link.get("freq_mhz", 0) or 0) for link in s.get("links", []) or []
        }
        found.discard(0)
        return found

    names = sorted(up)
    for name in names:
        me = up[name]
        same_bss = [o for o in names if o != name and bssids(me) & bssids(up[o])]
        same_chan = [o for o in names if o != name and o not in same_bss and freqs(me) & freqs(up[o])]
        if same_bss:
            result[name].append(_flag("shared_bss", "warn", "Shares an access point",
                                      f"Same BSS as {', '.join(same_bss)}: the radios split one "
                                      "airtime budget, so multipath gains nothing between them."))
        if same_chan:
            result[name].append(_flag("shared_channel", "info", "Shares a channel",
                                      f"Same channel as {', '.join(same_chan)}: they contend for airtime."))

    by_subnet: dict[str, list[str]] = {}
    for name in names:
        subnet = up[name].get("subnet", "")
        if subnet:
            by_subnet.setdefault(subnet, []).append(name)
    for subnet, members in by_subnet.items():
        if len(members) < 2:
            continue
        risky = [
            m for m in members
            if effective_sysctl(m, "arp_ignore", reader) < 1
            or effective_sysctl(m, "arp_announce", reader) < 2
        ]
        if risky:
            for m in members:
                result[m].append(_flag("arp_flux", "warn", "ARP flux risk",
                                       f"{len(members)} radios on {subnet} without "
                                       "arp_ignore=1 / arp_announce=2: replies can leave "
                                       "the wrong radio. Enabling multipath sets these."))
    return result


def worst_severity(flags: list[dict]) -> str:
    worst = ""
    for flag in flags:
        sev = flag.get("severity", "")
        if SEVERITY_RANK.get(sev, -1) > SEVERITY_RANK.get(worst, -1):
            worst = sev
    return worst


# ---------------------------------------------------------------------------
# Multipath + internal-card status (read-only views of root-owned state)
# ---------------------------------------------------------------------------


def parse_multipath_live(rules, table_routes) -> dict:
    """Is the ECMP layout actually installed? From `ip -j rule` + table 100."""
    rule_prios = {
        int(r.get("priority", -1))
        for r in (rules if isinstance(rules, list) else [])
    }
    members: list[str] = []
    installed = False
    for route in table_routes if isinstance(table_routes, list) else []:
        if route.get("dst") != "default" or not shared.proto_is_ours(route.get("protocol")):
            continue
        installed = True
        if route.get("dev"):
            members.append(route["dev"])
        members.extend(h["dev"] for h in route.get("nexthops", []) or [] if h.get("dev"))
    active = installed and shared.PRIO_ECMP in rule_prios and shared.PRIO_SUPPRESS_MAIN in rule_prios
    return {"active": active, "members": sorted(set(members))}


def read_multipath_status(etc_dir: Path = shared.ETC_DIR, run_dir: Path = shared.RUN_DIR,
                          live: dict | None = None) -> dict:
    desired = (etc_dir / shared.MULTIPATH_FLAG.name).exists()
    if live is None:
        live = parse_multipath_live(
            _run_json(["ip", "-j", "-4", "rule", "show"]),
            _run_json(["ip", "-j", "-4", "route", "show", "table", str(shared.ECMP_TABLE)]),
        )
    status = {
        "desired": desired,
        "active": bool(live.get("active")),
        "members": live.get("members", []),
        "excluded": [],
        "reason": "",
        "error": "",
        "applied_at": 0,
    }
    try:
        saved = json.loads((run_dir / shared.MULTIPATH_STATUS.name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        for key in ("excluded", "reason", "error", "applied_at"):
            if key in saved:
                status[key] = saved[key]
    if desired and not status["active"] and not status["reason"]:
        status["reason"] = "waiting for re-apply"
    if not desired:
        status["reason"] = ""
    return status


def read_internal_status(sys_root: Path = SYS_ROOT, etc_dir: Path = shared.ETC_DIR) -> dict:
    entries = shared.read_internal_conf(etc_dir / shared.INTERNAL_CONF.name)
    status = {
        "managed": bool(entries),
        "desired": (etc_dir / shared.INTERNAL_FLAG.name).exists(),
        "present": False,
        "bound": False,
        "driver": entries[0]["driver"] if entries else "",
        "dev_id": entries[0]["id"] if entries else "",
        "pci_addr": "",
        "iface": "",
    }
    if not entries:
        return status
    ids = {e["id"] for e in entries}
    devices = sys_root / "bus" / "pci" / "devices"
    try:
        candidates = sorted(devices.iterdir())
    except OSError:
        candidates = []
    for dev in candidates:
        vendor = _read(dev / "vendor").removeprefix("0x")
        device = _read(dev / "device").removeprefix("0x")
        if f"{vendor}:{device}".lower() not in ids:
            continue
        if not _read(dev / "class").startswith(shared.WIFI_PCI_CLASS_PREFIX):
            continue
        status["present"] = True
        status["pci_addr"] = dev.name
        driver = _resolve(dev / "driver")
        status["bound"] = driver is not None
        if driver is not None:
            status["driver"] = driver.name
        try:
            nets = sorted((dev / "net").iterdir())
        except OSError:
            nets = []
        if nets:
            status["iface"] = nets[0].name
        break
    return status


def helper_available(helper: Path = shared.HELPER_PATH) -> bool:
    return helper.is_file() and os.access(helper, os.X_OK)
