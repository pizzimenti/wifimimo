"""Constants and parsers shared by the unprivileged daemon and the root helper.

Standard library only: `wifimimo-helper` runs as root under `python3 -I`
and imports this module from its own (root-owned) install directory, so
nothing here may pull in third-party packages or read user-controlled paths.
Keeping one copy of these facts means the helper that *writes* routing state
and the daemon that *reads* it can never disagree about what wifimimo owns.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LIB_DIR = Path("/usr/local/lib/wifimimo")
HELPER_PATH = LIB_DIR / "wifimimo-helper"
ETC_DIR = Path("/etc/wifimimo")
RUN_DIR = Path("/run/wifimimo")

MULTIPATH_FLAG = ETC_DIR / "multipath-enabled"
MULTIPATH_STATUS = RUN_DIR / "multipath.json"
SYSCTL_SAVED = RUN_DIR / "sysctl-saved.json"
HELPER_LOCK = RUN_DIR / "helper.lock"

INTERNAL_CONF = ETC_DIR / "internal.conf"
INTERNAL_FLAG = ETC_DIR / "internal-enabled"
INTERNAL_UDEV_RULE = Path("/etc/udev/rules.d/70-wifimimo-internal.rules")

POLKIT_ACTION = "io.github.pizzimenti.wifimimo.helper"

# ---------------------------------------------------------------------------
# Routing ownership
#
# Routes live OUTSIDE the main table: NetworkManager deletes extraneous
# routes from `main` whenever it reconfigures a device, which is exactly how
# a hand-made ECMP default route disappears while its rules stay behind.
# Layout (wg-quick style), all tagged `proto RT_PROTO`:
#
#   5210-5270  Tailscale                       (untouched, stays ahead)
#   32000      lookup main suppress_prefixlength 0
#              -> NM's specific routes (LAN, docker, VPN /1s) keep winning,
#                 main's default routes are skipped
#   32001+i    from <radio i ip> lookup TABLE_BASE+1+i
#   32090      lookup ECMP_TABLE               (the one multipath default)
#   32766      main                            (NM defaults = fallback)
#
# 32000+ also sits after NM's WireGuard default-route rules (~31610).
# ---------------------------------------------------------------------------

RT_PROTO = 211
RT_PROTO_NAME = "wifimimo"
ECMP_TABLE = 100
RADIO_TABLE_BASE = 101
MAX_RADIOS = 16
RADIO_TABLE_MAX = RADIO_TABLE_BASE + MAX_RADIOS - 1  # 116
PRIO_SUPPRESS_MAIN = 32000
PRIO_RADIO_BASE = 32001
PRIO_RADIO_MAX = PRIO_RADIO_BASE + MAX_RADIOS - 1  # 32016
PRIO_ECMP = 32090
# Hand-made rules from before v1.0 (priorities 5300-5399 pointing at the
# radio tables) are adopted and removed on the first apply.
LEGACY_PRIO_MIN = 5300
LEGACY_PRIO_MAX = 5399


def owned_rule_priority(prio: int, table: int | None) -> bool:
    """True when an `ip rule` entry belongs to wifimimo."""
    if prio == PRIO_SUPPRESS_MAIN or prio == PRIO_ECMP:
        return True
    if PRIO_RADIO_BASE <= prio <= PRIO_RADIO_MAX:
        return True
    if LEGACY_PRIO_MIN <= prio <= LEGACY_PRIO_MAX and table is not None:
        return RADIO_TABLE_BASE <= table <= RADIO_TABLE_MAX
    return False


def proto_is_ours(value) -> bool:
    """`ip -j` renders a registered protocol by name, an unknown one as a number."""
    return str(value) in (str(RT_PROTO), RT_PROTO_NAME)


def route_table(route: dict) -> int | None:
    """Numeric table of an `ip -j route show table all` entry (None for main/local)."""
    try:
        return int(route.get("table", "main"))
    except (TypeError, ValueError):
        return None


def ecmp_members(routes) -> list[str]:
    """Interfaces in wifimimo's ECMP default route.

    Accepts either `ip -j route show table all` output or `... table 100`
    output (entries without a `table` key are taken to be from table 100).
    """
    devs: set[str] = set()
    for route in routes if isinstance(routes, list) else []:
        table = route_table(route) if "table" in route else ECMP_TABLE
        if table != ECMP_TABLE or route.get("dst") != "default":
            continue
        if not proto_is_ours(route.get("protocol")):
            continue
        for hop in route.get("nexthops") or [route]:
            if hop.get("dev"):
                devs.add(hop["dev"])
    return sorted(devs)


# ---------------------------------------------------------------------------
# Internal-card config
#
# One managed PCI wifi device per line:
#   <vendor>:<device> <driver> [<parent bridge address>]
#   14c3:7925 mt7925e 0000:00:02.2
# Comments (#) and blank lines are allowed. Anything else is rejected, so a
# malformed file can never smuggle text into the generated udev rule.
# ---------------------------------------------------------------------------

_CONF_LINE = re.compile(
    r"^([0-9a-f]{4}):([0-9a-f]{4})\s+([a-z0-9_]{1,32})"
    r"(?:\s+([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]))?$"
)


class ConfigError(ValueError):
    """internal.conf contains a line that doesn't match the strict format."""


def parse_internal_conf(text: str) -> list[dict]:
    entries: list[dict] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip().lower()
        if not line:
            continue
        match = _CONF_LINE.match(line)
        if not match:
            raise ConfigError(f"internal.conf line {lineno}: {raw.strip()!r}")
        vendor, device, driver, bridge = match.groups()
        entries.append({
            "vendor": vendor,
            "device": device,
            "id": f"{vendor}:{device}",
            "driver": driver,
            "bridge": bridge or "",
        })
    return entries


def read_internal_conf(path: Path = INTERNAL_CONF) -> list[dict]:
    """Managed internal cards, or [] when unconfigured or unreadable."""
    try:
        return parse_internal_conf(path.read_text(encoding="utf-8"))
    except (OSError, ConfigError):
        return []


# PCI class 0x0280xx = "Network controller: other" — where 802.11 cards live.
WIFI_PCI_CLASS_PREFIX = "0x0280"


def _sysfs(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def find_internal_devices(entries: list[dict], sys_root: Path = Path("/sys")) -> list[dict]:
    """PCI devices present right now that match a managed internal.conf entry.

    Only wifi-class devices (0x0280xx) qualify, so a typo in internal.conf
    can never target e.g. a disk controller that happens to share an id.
    """
    wanted = {e["id"]: e for e in entries}
    found: list[dict] = []
    devices = sys_root / "bus" / "pci" / "devices"
    try:
        candidates = sorted(devices.iterdir())
    except OSError:
        return found
    for dev in candidates:
        dev_id = f"{_sysfs(dev / 'vendor').removeprefix('0x')}:{_sysfs(dev / 'device').removeprefix('0x')}".lower()
        entry = wanted.get(dev_id)
        if entry is None or not _sysfs(dev / "class").startswith(WIFI_PCI_CLASS_PREFIX):
            continue
        driver_link = dev / "driver"
        bound = ""
        if driver_link.exists():
            try:
                bound = driver_link.resolve().name
            except OSError:
                bound = ""
        try:
            nets = sorted(p.name for p in (dev / "net").iterdir())
        except OSError:
            nets = []
        found.append({
            "addr": dev.name,
            "id": dev_id,
            "driver": entry["driver"],
            "bound_driver": bound,
            "iface": nets[0] if nets else "",
        })
    return found
