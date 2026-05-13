#!/usr/bin/env python3
"""
Shared wifimimo data collection and state-file helpers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

try:
    from pyroute2 import IW
except ImportError:
    IW = None

from phy_modes import (
    PHY_MODES,
    SIX_GHZ_FLOOR_MHZ,
    compute_rates,
    default_phy_mode,
    efficiency_for,
    iw_token_alternation,
    mcs_max_for,
    phy_mode_by_name,
    wifi_label,
)


logger = logging.getLogger("wifimimo")


IFACE = "wlp1s0"
HISTORY_DIR = Path.home() / ".local" / "state" / "wifimimo" / "history"

# Canonical signal thresholds. Used by daemon alerts AND derived display tier
# so curses mon, plasmoid, and journal alerts agree.
SIGNAL_GOOD_DBM = -65
SIGNAL_WARN_DBM = -75
SIGNAL_FLOOR_DBM = -90
SIGNAL_CEIL_DBM = -20
SPREAD_FRACTION_FLOOR = 30.0  # dBm spread that maps to a full bar

STATE_PATH = Path(f"/run/user/{os.getuid()}/wifimimo-state")
SCHEMA_VERSION = 2

KNOWN_WIFI_DRIVERS = ("iwlwifi", "mt76", "mt79", "ath", "rtw", "brcm", "mwifiex")

NL80211_WIDTH_TO_MHZ = {
    0: 20,
    1: 20,
    2: 40,
    3: 80,
    4: 160,
    5: 160,
    6: 5,
    7: 10,
    8: 320,
}
NL80211_CALL_TIMEOUT_S = 1.0


class NetlinkTimeoutError(TimeoutError):
    """Raised when an nl80211 call exceeds its bounded collection window."""


class _TimeoutContext:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.previous_handler = None
        self.previous_timer: tuple[float, float] | None = None
        self.enabled = (
            seconds > 0
            and hasattr(signal, "setitimer")
            and threading.current_thread() is threading.main_thread()
        )

    def _handle_timeout(self, _signum, _frame) -> None:
        raise NetlinkTimeoutError()

    def __enter__(self) -> "_TimeoutContext":
        if self.enabled:
            self.previous_handler = signal.getsignal(signal.SIGALRM)
            self.previous_timer = signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, self._handle_timeout)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if self.enabled:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            if self.previous_timer is not None:
                delay, interval = self.previous_timer
                if delay > 0 or interval > 0:
                    signal.setitimer(signal.ITIMER_REAL, delay, interval)
            if self.previous_handler is not None:
                signal.signal(signal.SIGALRM, self.previous_handler)
        return False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class LinkInfo:
    link_id: int = 0
    bssid: str = ""
    freq_mhz: int = 0
    chan_num: int = 0
    bandwidth_mhz: int = 0
    signal_dbm: int = 0
    tx_rate_mbps: float = 0.0
    tx_mcs: int = -1
    tx_nss: int = 0
    tx_mode: str = ""
    tx_gi: int = -1
    rx_rate_mbps: float = 0.0
    rx_mcs: int = -1
    rx_nss: int = 0
    rx_mode: str = ""
    rx_gi: int = -1


@dataclass
class DisplayState:
    band_label: str = "?"
    wifi_label: str = ""  # e.g. "Wi-Fi 7 / EHT", "Wi-Fi 6E / HE"
    signal_tier: str = "crit"
    signal_fraction: float = 0.0
    signal_avg_fraction: float = 0.0
    spread_fraction: float = 0.0
    antenna_fractions: list[float] = field(default_factory=list)
    tx_nss_dots: str = "○○"
    rx_nss_dots: str = "○○"
    tx_gi_label: str = ""
    rx_gi_label: str = ""
    tx_rates_mbps: list[float] = field(default_factory=list)
    rx_rates_mbps: list[float] = field(default_factory=list)
    mcs_grid_count: int = 12


@dataclass
class WifiState:
    schema_version: int = SCHEMA_VERSION
    iface: str = IFACE
    connected: bool = False
    ssid: str = ""
    ssid_display: str = ""
    bssid: str = ""
    freq_mhz: int = 0
    chan_num: int = 0
    bandwidth_mhz: int = 0
    signal_dbm: int = 0
    signal_avg_dbm: int = 0
    signal_antennas: list[int] = field(default_factory=list)
    tx_rate_mbps: float = 0.0
    tx_mcs: int = -1
    tx_nss: int = 0
    tx_mode: str = ""
    tx_gi: int = -1
    rx_rate_mbps: float = 0.0
    rx_mcs: int = -1
    rx_nss: int = 0
    rx_mode: str = ""
    rx_gi: int = -1
    tx_packets: int = 0
    tx_retries: int = 0
    tx_failed: int = 0
    rx_packets: int = 0
    connected_time_s: int = 0
    station_dump_available: bool = False
    retry_10s_pct: float = 0.0
    retry_10s_packets: int = 0
    retry_10s_retries: int = 0
    retry_10s_failed: int = 0
    issue_count: int = 0
    timestamp: int = 0
    card_temp_c: float = 0.0
    power_save: str = ""
    pci_power_state: str = ""
    runtime_pm: str = ""
    runtime_active_ms: int = 0
    runtime_suspended_ms: int = 0
    links: list[dict] = field(default_factory=list)
    display: dict = field(default_factory=lambda: asdict(DisplayState()))


# ---------------------------------------------------------------------------
# Power / hwmon
# ---------------------------------------------------------------------------


def _find_wifi_hwmon(iface: str) -> Path | None:
    driver_link = Path(f"/sys/class/net/{iface}/device/driver")
    driver = ""
    if driver_link.is_symlink() or driver_link.exists():
        try:
            driver = driver_link.resolve().name
        except OSError:
            pass

    hwmon_base = Path("/sys/class/hwmon")
    if not hwmon_base.exists():
        return None
    for entry in sorted(hwmon_base.iterdir()):
        name_file = entry / "name"
        if not name_file.exists():
            continue
        try:
            name = name_file.read_text().strip()
        except OSError:
            continue
        if driver and driver in name:
            return entry
        if any(name.startswith(prefix) for prefix in KNOWN_WIFI_DRIVERS):
            return entry
    return None


def collect_power(iface: str) -> dict:
    info: dict = {
        "card_temp_c": 0.0,
        "power_save": "",
        "pci_power_state": "",
        "runtime_pm": "",
        "runtime_active_ms": 0,
        "runtime_suspended_ms": 0,
    }

    hwmon = _find_wifi_hwmon(iface)
    if hwmon:
        temp_file = hwmon / "temp1_input"
        if temp_file.exists():
            try:
                info["card_temp_c"] = int(temp_file.read_text().strip()) / 1000.0
            except (OSError, ValueError):
                pass

    dev_power = Path(f"/sys/class/net/{iface}/device/power")
    for key, filename in [
        ("runtime_pm", "runtime_status"),
        ("runtime_active_ms", "runtime_active_time"),
        ("runtime_suspended_ms", "runtime_suspended_time"),
    ]:
        path = dev_power / filename
        if path.exists():
            try:
                raw = path.read_text().strip()
                if key.endswith("_ms"):
                    info[key] = _int(raw)
                else:
                    info[key] = raw
            except OSError:
                pass

    power_state_file = Path(f"/sys/class/net/{iface}/device/power_state")
    if power_state_file.exists():
        try:
            info["pci_power_state"] = power_state_file.read_text().strip()
        except OSError:
            pass

    try:
        result = subprocess.run(
            ["iw", "dev", iface, "get", "power_save"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        match = re.search(r"Power save:\s*(\S+)", result.stdout)
        if match:
            info["power_save"] = match.group(1).lower()
    except Exception:
        pass

    return info


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def default_state(iface: str = IFACE) -> dict:
    """Return the WifiState dict shape with defaults, parameterized on iface."""
    return asdict(WifiState(iface=iface))


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2, check=False)
        return result.stdout
    except Exception:
        return ""


def _attrs_to_dict(message: dict | None) -> dict:
    if not message:
        return {}
    result: dict = {}
    for key, value in message.get("attrs", []):
        result[key] = value
    return result


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, AttributeError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (ValueError, AttributeError):
        return default


def safe_ssid(ssid: str) -> str:
    try:
        latin = re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match.group(1), 16)), ssid)
        return latin.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return re.sub(r"\\x[0-9a-fA-F]{2}", "?", ssid)


def freq_to_channel(freq_mhz: int) -> int:
    if freq_mhz == 2484:
        return 14
    if 2412 <= freq_mhz <= 2472:
        return (freq_mhz - 2407) // 5
    if 5000 <= freq_mhz <= 5895:
        return (freq_mhz - 5000) // 5
    if 5955 <= freq_mhz <= 7115:
        return (freq_mhz - 5950) // 5
    return 0


# ---------------------------------------------------------------------------
# iw output parsing
# ---------------------------------------------------------------------------


_HE_VHT_EHT_TOKENS = iw_token_alternation(exclude=("HT",))
_GI_TOKENS = "|".join(m.iw_token for m in PHY_MODES if m.has_gi)


def _parse_iw_rate_line(body: str, direction: str) -> dict:
    """Extract one direction's rate fields from iw `tx/rx bitrate:` line."""
    result: dict = {}
    rate_re = (
        rf"{direction} bitrate:\s+([\d.]+)\s+MBit/s"
        rf"(?:.*?({_HE_VHT_EHT_TOKENS})-MCS\s+(\d+))?"
        rf"(?:.*?(?:{_HE_VHT_EHT_TOKENS})-NSS\s+(\d+))?"
    )
    match = re.search(rate_re, body)
    if not match:
        return result
    result["rate_mbps"] = _float(match.group(1))
    width_match = re.search(rf"{direction} bitrate:.*?\b(\d+)MHz\b", body)
    if width_match:
        result["bandwidth_mhz"] = _int(width_match.group(1))
    if match.group(2):
        result["mode"] = match.group(2)
        if match.group(3) is not None:
            result["mcs"] = _int(match.group(3), -1)
        if match.group(4) is not None:
            result["nss"] = _int(match.group(4), 0)
        if _GI_TOKENS:
            gi_match = re.search(
                rf"{direction} bitrate:.*?(?:{_GI_TOKENS})-GI\s+(\d+)", body
            )
            if gi_match:
                result["gi"] = _int(gi_match.group(1), -1)
        return result
    # HT fallback: iw prints "MCS N" without a mode prefix. N encodes both
    # mcs (low 3 bits) and stream count (high bits).
    ht_match = re.search(
        rf"{direction} bitrate:\s+[\d.]+\s+MBit/s.*?\bMCS\s+(\d+)", body
    )
    if ht_match:
        raw = _int(ht_match.group(1))
        result["mode"] = "HT"
        result["mcs"] = raw % 8
        result["nss"] = raw // 8 + 1
    return result


def parse_link_metrics(data: dict, link: str) -> None:
    if "Not connected" in link or not link.strip():
        return

    data["connected"] = True

    match = re.search(r"SSID:\s*(.+)", link)
    if match:
        data["ssid"] = match.group(1).strip()
        data["ssid_display"] = safe_ssid(data["ssid"])
    match = re.search(r"Connected to\s+([0-9a-f:]{17})", link)
    if match:
        data["bssid"] = match.group(1)
    match = re.search(r"freq:\s*([\d.]+)", link)
    if match:
        data["freq_mhz"] = int(float(match.group(1)))
        data["chan_num"] = freq_to_channel(data["freq_mhz"])
    match = re.search(r"signal:\s+([-\d]+)", link)
    if match:
        data["signal_dbm"] = _int(match.group(1))

    for direction in ("tx", "rx"):
        rate = _parse_iw_rate_line(link, direction)
        if "rate_mbps" in rate:
            data[f"{direction}_rate_mbps"] = rate["rate_mbps"]
        if "mode" in rate:
            data[f"{direction}_mode"] = rate["mode"]
        if "mcs" in rate:
            data[f"{direction}_mcs"] = rate["mcs"]
        if "nss" in rate:
            data[f"{direction}_nss"] = rate["nss"]
        if "gi" in rate:
            data[f"{direction}_gi"] = rate["gi"]
        if rate.get("bandwidth_mhz") and not data.get("bandwidth_mhz"):
            data["bandwidth_mhz"] = rate["bandwidth_mhz"]


_LINK_BLOCK_RE = re.compile(
    r"Link\s+(?P<id>\d+)\s+BSSID:?\s*(?P<bssid>[0-9a-f:]{17})(?P<body>.*?)"
    r"(?=Link\s+\d+\s+BSSID|MLD\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse_link_blocks(text: str) -> list[dict]:
    """Parse 'Link N BSSID ...' MLO blocks from `iw dev <iface> link`."""
    links: list[dict] = []
    for match in _LINK_BLOCK_RE.finditer(text):
        link = asdict(LinkInfo())
        link["link_id"] = _int(match.group("id"))
        link["bssid"] = match.group("bssid").lower()
        body = match.group("body")
        m = re.search(r"freq:\s*([\d.]+)", body)
        if m:
            link["freq_mhz"] = int(float(m.group(1)))
            link["chan_num"] = freq_to_channel(link["freq_mhz"])
        m = re.search(r"width:\s*(\d+)\s*MHz", body)
        if m:
            link["bandwidth_mhz"] = _int(m.group(1))
        m = re.search(r"signal:\s+([-\d]+)", body)
        if m:
            link["signal_dbm"] = _int(m.group(1))
        for direction in ("tx", "rx"):
            rate = _parse_iw_rate_line(body, direction)
            if "rate_mbps" in rate:
                link[f"{direction}_rate_mbps"] = rate["rate_mbps"]
            if "mode" in rate:
                link[f"{direction}_mode"] = rate["mode"]
            if "mcs" in rate:
                link[f"{direction}_mcs"] = rate["mcs"]
            if "nss" in rate:
                link[f"{direction}_nss"] = rate["nss"]
            if "gi" in rate:
                link[f"{direction}_gi"] = rate["gi"]
            if rate.get("bandwidth_mhz") and not link["bandwidth_mhz"]:
                link["bandwidth_mhz"] = rate["bandwidth_mhz"]
        links.append(link)
    return links


def _parse_mlo_primary_link(iface: str) -> tuple[int, int, list[dict]]:
    """Return (freq_mhz, bandwidth_mhz, links) from `iw dev <iface> link`.

    For an MLD interface the parent frame doesn't carry freq/width — those
    live under per-link blocks (freq) and the MLD-stats bitrate line (width,
    encoded as ``160MHz`` next to the rate). Returned tuple is ``(0, 0, [])``
    when the output has no Link blocks (non-MLO connection or iw missing).
    """
    text = _run(["iw", "dev", iface, "link"])
    if not text:
        return 0, 0, []
    links = parse_link_blocks(text)
    if not links:
        return 0, 0, []
    primary = links[0]
    width = primary["bandwidth_mhz"]
    if not width:
        # MLD parent's bitrate line carries the channel width even when the
        # Link block itself only lists freq.
        m = re.search(r"(?:tx|rx) bitrate:.*?\b(\d+)MHz\b", text)
        if m:
            width = _int(m.group(1))
    return primary["freq_mhz"], width, links


# ---------------------------------------------------------------------------
# Netlink rate-info parsing
# ---------------------------------------------------------------------------


def _parse_rate_info(data: dict, direction: str, rate_info: dict | None) -> None:
    attrs = _attrs_to_dict(rate_info)
    bitrate32 = attrs.get("NL80211_RATE_INFO_BITRATE32")
    bitrate = attrs.get("NL80211_RATE_INFO_BITRATE")
    raw_rate = bitrate32 if bitrate32 is not None else bitrate
    if raw_rate is not None:
        data[f"{direction}_rate_mbps"] = _float(raw_rate) / 10.0

    for mode in PHY_MODES:
        if mode.nl_mcs_attr not in attrs:
            continue
        data[f"{direction}_mode"] = mode.name
        if mode.name == "HT":
            raw = _int(attrs.get(mode.nl_mcs_attr), -1)
            if raw >= 0:
                data[f"{direction}_mcs"] = raw % 8
                data[f"{direction}_nss"] = raw // 8 + 1
        else:
            data[f"{direction}_mcs"] = _int(attrs.get(mode.nl_mcs_attr), -1)
            data[f"{direction}_nss"] = _int(attrs.get(mode.nl_nss_attr), 0)
            if mode.has_gi:
                data[f"{direction}_gi"] = _int(attrs.get(mode.nl_gi_attr), -1)
        return

    # No mode matched. If there are MCS-shaped attrs, the kernel has surfaced
    # a PHY mode we don't recognise yet; warn so the fixture suite can be
    # updated alongside phy_modes.PHY_MODES.
    unknown_mcs = sorted(
        k for k in attrs if k.startswith("NL80211_RATE_INFO_") and "MCS" in k
    )
    if unknown_mcs:
        logger.warning("unknown rate-info MCS attrs for %s: %s", direction, unknown_mcs)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _collect_via_netlink(iface: str) -> dict | None:
    if IW is None:
        return None

    iw = IW()
    try:
        with _TimeoutContext(NL80211_CALL_TIMEOUT_S):
            ifindex = None
            interface_attrs = None
            for message in iw.list_dev():
                attrs = _attrs_to_dict(message)
                if attrs.get("NL80211_ATTR_IFNAME") == iface:
                    ifindex = _int(attrs.get("NL80211_ATTR_IFINDEX"), 0)
                    interface_attrs = attrs
                    break
            if not ifindex or not interface_attrs:
                return None

            data = default_state(iface)
            ssid = interface_attrs.get("NL80211_ATTR_SSID", "")
            freq_mhz = _int(interface_attrs.get("NL80211_ATTR_WIPHY_FREQ"), 0)
            chan_width = interface_attrs.get("NL80211_ATTR_CHANNEL_WIDTH")
            data["iface"] = iface
            data["ssid"] = ssid
            data["ssid_display"] = safe_ssid(ssid)
            data["freq_mhz"] = freq_mhz
            data["chan_num"] = freq_to_channel(freq_mhz)
            data["bandwidth_mhz"] = NL80211_WIDTH_TO_MHZ.get(_int(chan_width, -1), 0)

            station_message = None
            for message in iw.get_stations(ifindex):
                station_message = message
                break
            if station_message is None:
                _fallback_via_iw_link(data, iface)
                return data

            station_attrs = _attrs_to_dict(station_message)
            station_info = _attrs_to_dict(station_attrs.get("NL80211_ATTR_STA_INFO"))
            if not station_info:
                _fallback_via_iw_link(data, iface)
                return data

            data["connected"] = True
            data["bssid"] = station_attrs.get("NL80211_ATTR_MAC", "")
            data["station_dump_available"] = True
            data["signal_dbm"] = _int(station_info.get("NL80211_STA_INFO_SIGNAL"), 0)
            data["signal_avg_dbm"] = _int(station_info.get("NL80211_STA_INFO_SIGNAL_AVG"), 0)
            raw_chain = [
                _int(value)
                for value in station_info.get("NL80211_STA_INFO_CHAIN_SIGNAL", []) or []
            ]
            if any(v > 0 for v in raw_chain):
                # pyroute2 misreads chain signal on some drivers (e.g. mt7925);
                # fall back to parsing iw station dump for antenna values
                dump = _run(["iw", "dev", iface, "station", "dump"])
                chain_match = re.search(r"signal:\s+([-\d]+)\s+\[([-\d,\s]+)\]", dump)
                if chain_match:
                    data["signal_dbm"] = _int(chain_match.group(1))
                    raw_chain = [_int(v) for v in chain_match.group(2).split(",")]
            data["signal_antennas"] = raw_chain
            data["tx_packets"] = _int(station_info.get("NL80211_STA_INFO_TX_PACKETS"), 0)
            data["tx_retries"] = _int(station_info.get("NL80211_STA_INFO_TX_RETRIES"), 0)
            data["tx_failed"] = _int(station_info.get("NL80211_STA_INFO_TX_FAILED"), 0)
            data["rx_packets"] = _int(station_info.get("NL80211_STA_INFO_RX_PACKETS"), 0)
            data["connected_time_s"] = _int(
                station_info.get("NL80211_STA_INFO_CONNECTED_TIME"), 0
            )

            _parse_rate_info(data, "tx", station_info.get("NL80211_STA_INFO_TX_BITRATE"))
            _parse_rate_info(data, "rx", station_info.get("NL80211_STA_INFO_RX_BITRATE"))

            _augment_with_iw_link(data, iface)
            return data
    except Exception:
        return None
    finally:
        iw.close()


def _augment_with_iw_link(data: dict, iface: str) -> None:
    """Fill in MLO-specific gaps (freq/width per link) from `iw dev link`.

    The MLD parent doesn't carry NL80211_ATTR_WIPHY_FREQ; per-link freq lives
    only in the iw `Link N` blocks. We gate on freq/width missing because
    that's the MLO-MLD signature — the netlink path populates both for any
    legacy non-MLO connection, so probing iw every poll just to confirm
    `links == []` would burn one subprocess per second for no information
    gain. When iw IS run (MLO case), we surface its `links` payload too.
    """
    needs_freq = not data.get("freq_mhz")
    needs_width = not data.get("bandwidth_mhz")
    if not (needs_freq or needs_width):
        return
    freq, width, links = _parse_mlo_primary_link(iface)
    if needs_freq and freq:
        data["freq_mhz"] = freq
        data["chan_num"] = freq_to_channel(freq)
    if needs_width and width:
        data["bandwidth_mhz"] = width
    if links:
        data["links"] = links


def _fallback_via_iw_link(data: dict, iface: str) -> None:
    """Backfill connection metrics from `iw dev <iface> link` when the
    netlink station enumeration came back empty.

    Without this the early-return paths in `_collect_via_netlink` leave
    `connected`, `bssid`, `signal_dbm`, and bitrate fields at their dataclass
    defaults — surfacing as a false "Not connected" in the UI even when iw
    plainly shows an active link. Runs iw exactly once and feeds both the
    flat parser (parse_link_metrics) and the per-link parser (parse_link_blocks).
    """
    link_text = _run(["iw", "dev", iface, "link"])
    if not link_text:
        return
    parse_link_metrics(data, link_text)
    if not data.get("connected"):
        return
    if not data.get("links"):
        data["links"] = parse_link_blocks(link_text)
    # MLD parents don't carry a top-level freq line; the first Link block does.
    if not data.get("freq_mhz") and data["links"]:
        primary = data["links"][0]
        data["freq_mhz"] = primary["freq_mhz"]
        data["chan_num"] = primary["chan_num"]
        if not data.get("bandwidth_mhz") and primary["bandwidth_mhz"]:
            data["bandwidth_mhz"] = primary["bandwidth_mhz"]


def collect(iface: str) -> dict:
    netlink_data = _collect_via_netlink(iface)
    if netlink_data is not None:
        return netlink_data

    data = default_state(iface)

    link = _run(["iw", "dev", iface, "link"])
    parse_link_metrics(data, link)
    if not data["connected"]:
        return data

    data["links"] = parse_link_blocks(link)

    dump = _run(["iw", "dev", iface, "station", "dump"])
    if not dump:
        return data

    data["station_dump_available"] = True

    match = re.search(r"signal:\s+([-\d]+)\s+\[([-\d,\s]+)\]", dump)
    if match:
        data["signal_dbm"] = _int(match.group(1))
        data["signal_antennas"] = [_int(entry) for entry in match.group(2).split(",")]

    match = re.search(r"signal avg:\s+([-\d]+)", dump)
    if match:
        data["signal_avg_dbm"] = _int(match.group(1))

    for key, pattern in [
        ("tx_packets", r"tx packets:\s+(\d+)"),
        ("tx_retries", r"tx retries:\s+(\d+)"),
        ("tx_failed", r"tx failed:\s+(\d+)"),
        ("rx_packets", r"rx packets:\s+(\d+)"),
        ("connected_time_s", r"connected time:\s+(\d+)\s+s"),
    ]:
        match = re.search(pattern, dump)
        if match:
            data[key] = _int(match.group(1))

    return data


# ---------------------------------------------------------------------------
# Derived display
# ---------------------------------------------------------------------------


def _band_label(freq_mhz: int) -> str:
    if freq_mhz >= SIX_GHZ_FLOOR_MHZ:
        return "6 GHz"
    if freq_mhz >= 5000:
        return "5 GHz"
    if freq_mhz > 0:
        return "2.4 GHz"
    return "?"


def _signal_fraction(dbm: float) -> float:
    if not dbm:
        return 0.0
    span = SIGNAL_CEIL_DBM - SIGNAL_FLOOR_DBM
    return max(0.0, min(1.0, (dbm - SIGNAL_FLOOR_DBM) / span))


def _spread_fraction(spread: float) -> float:
    return max(0.0, min(1.0, spread / SPREAD_FRACTION_FLOOR))


def _signal_tier(dbm: int) -> str:
    # dbm == 0 is the dataclass default and a transient association state;
    # positive dbm only happens with a misreading driver (mt7925-style chain
    # bug). Either way, classifying as "good" is misleading — flag as crit
    # so the UI doesn't show a healthy tier on placeholder data.
    if dbm >= 0:
        return "crit"
    if dbm >= SIGNAL_GOOD_DBM:
        return "good"
    if dbm >= SIGNAL_WARN_DBM:
        return "warn"
    return "crit"


def _nss_dots(nss: int) -> str:
    nss = max(0, int(nss or 0))
    return "●" * nss + "○" * max(0, 2 - nss)


_GI_LABELS = {0: "0.8us", 1: "1.6us", 2: "3.2us"}


def _gi_label(gi: int) -> str:
    return _GI_LABELS.get(int(gi), "") if isinstance(gi, (int, float)) and gi >= 0 else ""


def derive_display(state: dict) -> dict:
    """Compute display-only fields derived from raw state. UIs render this."""
    band_label = _band_label(_int(state.get("freq_mhz"), 0))
    if not state.get("connected"):
        return asdict(DisplayState(band_label=band_label))

    signal_dbm = _int(state.get("signal_dbm"), 0)
    signal_avg = _int(state.get("signal_avg_dbm"), 0)
    antennas = [_int(v) for v in state.get("signal_antennas", []) or []]
    spread = max(antennas) - min(antennas) if len(antennas) >= 2 else 0

    tx_mode_name = state.get("tx_mode") or ""
    rx_mode_name = state.get("rx_mode") or ""
    tx_mode = phy_mode_by_name(tx_mode_name) or default_phy_mode()
    rx_mode = phy_mode_by_name(rx_mode_name) or default_phy_mode()

    tx_mcs = _int(state.get("tx_mcs"), -1)
    rx_mcs = _int(state.get("rx_mcs"), -1)
    tx_rates: list[float] = []
    rx_rates: list[float] = []
    if tx_mcs >= 0:
        tx_rates = compute_rates(_float(state.get("tx_rate_mbps")), tx_mcs, tx_mode_name)
    if rx_mcs >= 0:
        rx_rates = compute_rates(_float(state.get("rx_rate_mbps")), rx_mcs, rx_mode_name)

    # Wi-Fi N label keys off whichever direction first reports a PHY mode —
    # tx is usually first but rx may surface during association.
    label_mode = tx_mode_name or rx_mode_name
    freq_mhz = _int(state.get("freq_mhz"), 0)

    return asdict(DisplayState(
        band_label=band_label,
        wifi_label=wifi_label(label_mode, freq_mhz),
        signal_tier=_signal_tier(signal_dbm),
        signal_fraction=_signal_fraction(signal_dbm),
        signal_avg_fraction=_signal_fraction(signal_avg),
        spread_fraction=_spread_fraction(spread),
        antenna_fractions=[_signal_fraction(v) for v in antennas],
        tx_nss_dots=_nss_dots(_int(state.get("tx_nss"), 0)),
        rx_nss_dots=_nss_dots(_int(state.get("rx_nss"), 0)),
        tx_gi_label=_gi_label(_int(state.get("tx_gi"), -1)),
        rx_gi_label=_gi_label(_int(state.get("rx_gi"), -1)),
        tx_rates_mbps=tx_rates,
        rx_rates_mbps=rx_rates,
        mcs_grid_count=max(len(tx_mode.efficiency), len(rx_mode.efficiency)),
    ))


# ---------------------------------------------------------------------------
# State file I/O
# ---------------------------------------------------------------------------


_KNOWN_FIELDS = {f.name for f in fields(WifiState)}


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_state_v1(raw: str) -> dict:
    """Best-effort migration of the legacy key=value state file."""
    data = default_state()
    int_keys = {
        "freq_mhz", "chan_num", "bandwidth_mhz", "signal_dbm", "signal_avg_dbm",
        "tx_nss", "rx_nss", "tx_mcs", "rx_mcs", "tx_gi", "rx_gi",
        "tx_packets", "tx_retries", "tx_failed", "rx_packets",
        "connected_time_s", "retry_10s_packets", "retry_10s_retries",
        "retry_10s_failed", "issue_count", "timestamp",
        "runtime_active_ms", "runtime_suspended_ms",
    }
    float_keys = {"tx_rate_mbps", "rx_rate_mbps", "retry_10s_pct", "card_temp_c"}
    bool_keys = {"connected", "station_dump_available"}
    str_keys = {
        "iface", "ssid", "ssid_display", "bssid",
        "tx_mode", "rx_mode", "power_save", "pci_power_state", "runtime_pm",
    }
    for raw_line in raw.splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        if key in bool_keys:
            data[key] = value == "true"
        elif key in int_keys:
            data[key] = _int(value, data.get(key, 0))
        elif key in float_keys:
            data[key] = _float(value, data.get(key, 0.0))
        elif key in str_keys:
            data[key] = value
        elif re.fullmatch(r"antenna_\d+", key):
            data["signal_antennas"].append(_int(value))
    return data


def read_state(path: Path = STATE_PATH) -> dict:
    defaults = default_state()
    if not path.exists():
        return defaults
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return defaults
    if not raw:
        return defaults

    if raw.startswith("{"):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return defaults
        if not isinstance(loaded, dict):
            return defaults
        merged = dict(defaults)
        # Only carry over fields we recognise; tolerate forward-compat additions.
        for key, value in loaded.items():
            if key in _KNOWN_FIELDS:
                merged[key] = value
        return merged

    # Legacy v1 (key=value) — the daemon and plasmoid migrate to JSON, this
    # branch survives only the brief upgrade window before a daemon restart.
    return _read_state_v1(raw)


# ---------------------------------------------------------------------------
# History CSV
# ---------------------------------------------------------------------------


HISTORY_COLUMNS = [
    "timestamp", "iface", "ssid", "bssid",
    "freq_mhz", "bandwidth_mhz", "chan_num",
    "signal_dbm", "signal_avg_dbm", "antenna_1", "antenna_2",
    "tx_rate_mbps", "rx_rate_mbps",
    "tx_mcs", "rx_mcs", "tx_nss", "rx_nss",
    "tx_mode", "rx_mode", "tx_gi", "rx_gi",
    "retry_10s_pct", "retry_10s_retries", "retry_10s_failed",
    "tx_packets", "tx_retries", "tx_failed",
    "card_temp_c", "power_save", "pci_power_state", "runtime_pm",
    "connected", "link_count",
]


def history_row(data: dict) -> list[str]:
    row: list[str] = []
    antennas = data.get("signal_antennas", [])
    for col in HISTORY_COLUMNS:
        if col == "antenna_1":
            row.append(str(antennas[0]) if len(antennas) >= 1 else "")
        elif col == "antenna_2":
            row.append(str(antennas[1]) if len(antennas) >= 2 else "")
        elif col == "connected":
            row.append("1" if data.get("connected") else "0")
        elif col == "link_count":
            row.append(str(len(data.get("links", []))))
        else:
            value = data.get(col, "")
            row.append(str(value))
    return row
