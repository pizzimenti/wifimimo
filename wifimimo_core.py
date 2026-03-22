#!/usr/bin/env python3
"""
Shared wifimimo data collection and state-file helpers.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from pathlib import Path

try:
    from pyroute2 import IW
except ImportError:
    IW = None


IFACE = "wlp1s0"

MCS_MAX: dict[str, int] = {"HE": 11, "VHT": 9, "HT": 7}
EFFICIENCY: dict[str, list[float]] = {
    "HE": [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0, 6.0, 20 / 3, 7.5, 25 / 3],
    "VHT": [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0, 6.0, 20 / 3],
    "HT": [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0],
}

STATE_PATH = Path(f"/run/user/{os.getuid()}/wifimimo-state")

NL80211_WIDTH_TO_MHZ = {
    0: 20,
    1: 20,
    2: 40,
    3: 80,
    4: 160,
    5: 160,
    6: 5,
    7: 10,
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


def default_state(iface: str = IFACE) -> dict:
    return {
        "iface": iface,
        "connected": False,
        "ssid": "",
        "ssid_display": "",
        "bssid": "",
        "freq_mhz": 0,
        "chan_num": 0,
        "bandwidth_mhz": 0,
        "signal_dbm": 0,
        "signal_avg_dbm": 0,
        "signal_antennas": [],
        "tx_rate_mbps": 0.0,
        "tx_mcs": -1,
        "tx_nss": 0,
        "tx_mode": "HE",
        "tx_gi": -1,
        "rx_rate_mbps": 0.0,
        "rx_mcs": -1,
        "rx_nss": 0,
        "rx_mode": "HE",
        "rx_gi": -1,
        "tx_packets": 0,
        "tx_retries": 0,
        "tx_failed": 0,
        "rx_packets": 0,
        "connected_time_s": 0,
        "station_dump_available": False,
        "retry_10s_pct": 0.0,
        "retry_10s_packets": 0,
        "retry_10s_retries": 0,
        "retry_10s_failed": 0,
        "issue_count": 0,
        "timestamp": 0,
    }


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


def compute_rates(ref_rate: float, ref_mcs: int, mode: str) -> list[float]:
    eff = EFFICIENCY.get(mode, EFFICIENCY["HE"])
    if ref_mcs < 0 or ref_mcs >= len(eff) or eff[ref_mcs] == 0:
        return [0.0] * len(eff)
    return [round(ref_rate * entry / eff[ref_mcs]) for entry in eff]


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
    match = re.search(r"freq:\s*(\d+)", link)
    if match:
        data["freq_mhz"] = _int(match.group(1))
        data["chan_num"] = freq_to_channel(data["freq_mhz"])
    match = re.search(r"signal:\s+([-\d]+)", link)
    if match:
        data["signal_dbm"] = _int(match.group(1))

    for direction in ("tx", "rx"):
        match = re.search(
            rf"{direction} bitrate:\s+([\d.]+)\s+MBit/s"
            r"(?:.*?(HE|VHT)-MCS\s+(\d+))?"
            r"(?:.*?(?:HE|VHT)-NSS\s+(\d+))?",
            link,
        )
        if not match:
            continue
        data[f"{direction}_rate_mbps"] = _float(match.group(1))
        if match.group(2):
            data[f"{direction}_mode"] = match.group(2)
        if match.group(3):
            data[f"{direction}_mcs"] = _int(match.group(3))
        if match.group(4):
            data[f"{direction}_nss"] = _int(match.group(4))
        if not data["bandwidth_mhz"]:
            width_match = re.search(rf"{direction} bitrate:.*?\b(\d+)MHz\b", link)
            if width_match:
                data["bandwidth_mhz"] = _int(width_match.group(1))
        gi_match = re.search(rf"{direction} bitrate:.*?HE-GI\s+(\d+)", link)
        if gi_match:
            data[f"{direction}_gi"] = _int(gi_match.group(1))
        if match.group(2):
            continue
        ht_match = re.search(rf"{direction} bitrate:\s+[\d.]+\s+MBit/s.*?\bMCS\s+(\d+)", link)
        if not ht_match:
            continue
        raw = _int(ht_match.group(1))
        data[f"{direction}_mode"] = "HT"
        data[f"{direction}_mcs"] = raw % 8
        data[f"{direction}_nss"] = raw // 8 + 1


def _parse_rate_info(data: dict, direction: str, rate_info: dict | None) -> None:
    attrs = _attrs_to_dict(rate_info)
    bitrate32 = attrs.get("NL80211_RATE_INFO_BITRATE32")
    bitrate = attrs.get("NL80211_RATE_INFO_BITRATE")
    raw_rate = bitrate32 if bitrate32 is not None else bitrate
    if raw_rate is not None:
        data[f"{direction}_rate_mbps"] = _float(raw_rate) / 10.0

    if "NL80211_RATE_INFO_HE_MCS" in attrs:
        data[f"{direction}_mode"] = "HE"
        data[f"{direction}_mcs"] = _int(attrs.get("NL80211_RATE_INFO_HE_MCS"), -1)
        data[f"{direction}_nss"] = _int(attrs.get("NL80211_RATE_INFO_HE_NSS"), 0)
        data[f"{direction}_gi"] = _int(attrs.get("NL80211_RATE_INFO_HE_GI"), -1)
    elif "NL80211_RATE_INFO_VHT_MCS" in attrs:
        data[f"{direction}_mode"] = "VHT"
        data[f"{direction}_mcs"] = _int(attrs.get("NL80211_RATE_INFO_VHT_MCS"), -1)
        data[f"{direction}_nss"] = _int(attrs.get("NL80211_RATE_INFO_VHT_NSS"), 0)
    elif "NL80211_RATE_INFO_MCS" in attrs:
        data[f"{direction}_mode"] = "HT"
        raw = _int(attrs.get("NL80211_RATE_INFO_MCS"), -1)
        if raw >= 0:
            data[f"{direction}_mcs"] = raw % 8
            data[f"{direction}_nss"] = raw // 8 + 1


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
                return data

            station_attrs = _attrs_to_dict(station_message)
            station_info = _attrs_to_dict(station_attrs.get("NL80211_ATTR_STA_INFO"))
            if not station_info:
                return data

            data["connected"] = True
            data["bssid"] = station_attrs.get("NL80211_ATTR_MAC", "")
            data["station_dump_available"] = True
            data["signal_dbm"] = _int(station_info.get("NL80211_STA_INFO_SIGNAL"), 0)
            data["signal_avg_dbm"] = _int(station_info.get("NL80211_STA_INFO_SIGNAL_AVG"), 0)
            data["signal_antennas"] = [
                _int(value)
                for value in station_info.get("NL80211_STA_INFO_CHAIN_SIGNAL", []) or []
            ]
            data["tx_packets"] = _int(station_info.get("NL80211_STA_INFO_TX_PACKETS"), 0)
            data["tx_retries"] = _int(station_info.get("NL80211_STA_INFO_TX_RETRIES"), 0)
            data["tx_failed"] = _int(station_info.get("NL80211_STA_INFO_TX_FAILED"), 0)
            data["rx_packets"] = _int(station_info.get("NL80211_STA_INFO_RX_PACKETS"), 0)
            data["connected_time_s"] = _int(
                station_info.get("NL80211_STA_INFO_CONNECTED_TIME"), 0
            )

            _parse_rate_info(data, "tx", station_info.get("NL80211_STA_INFO_TX_BITRATE"))
            _parse_rate_info(data, "rx", station_info.get("NL80211_STA_INFO_RX_BITRATE"))
            return data
    except Exception:
        return None
    finally:
        iw.close()


def collect(iface: str) -> dict:
    netlink_data = _collect_via_netlink(iface)
    if netlink_data is not None:
        return netlink_data

    data = default_state(iface)

    link = _run(["iw", "dev", iface, "link"])
    parse_link_metrics(data, link)
    if not data["connected"]:
        return data

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


def state_to_lines(data: dict) -> list[str]:
    lines = []
    for key in [
        "timestamp",
        "connected",
        "iface",
        "ssid",
        "ssid_display",
        "bssid",
        "freq_mhz",
        "chan_num",
        "bandwidth_mhz",
        "signal_dbm",
        "signal_avg_dbm",
        "tx_nss",
        "rx_nss",
        "tx_rate_mbps",
        "rx_rate_mbps",
        "tx_mcs",
        "rx_mcs",
        "tx_mode",
        "rx_mode",
        "tx_gi",
        "rx_gi",
        "tx_packets",
        "tx_retries",
        "tx_failed",
        "rx_packets",
        "connected_time_s",
        "station_dump_available",
        "retry_10s_pct",
        "retry_10s_packets",
        "retry_10s_retries",
        "retry_10s_failed",
        "issue_count",
    ]:
        value = data.get(key)
        if isinstance(value, bool):
            encoded = "true" if value else "false"
        else:
            encoded = str(value)
        lines.append(f"{key}={encoded}")
    for index, value in enumerate(data.get("signal_antennas", []), start=1):
        lines.append(f"antenna_{index}={int(value)}")
    return lines


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(state_to_lines(data)) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_state(path: Path = STATE_PATH) -> dict:
    data = default_state()
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        if key in {"connected", "station_dump_available"}:
            data[key] = value == "true"
        elif key in {
            "freq_mhz",
            "chan_num",
            "bandwidth_mhz",
            "signal_dbm",
            "signal_avg_dbm",
            "tx_nss",
            "rx_nss",
            "tx_mcs",
            "rx_mcs",
            "tx_gi",
            "rx_gi",
            "tx_packets",
            "tx_retries",
            "tx_failed",
            "rx_packets",
            "connected_time_s",
            "retry_10s_packets",
            "retry_10s_retries",
            "retry_10s_failed",
            "issue_count",
            "timestamp",
        }:
            data[key] = _int(value, data.get(key, 0))
        elif key in {"tx_rate_mbps", "rx_rate_mbps", "retry_10s_pct"}:
            data[key] = _float(value, data.get(key, 0.0))
        elif key in {"iface", "ssid", "ssid_display", "bssid", "tx_mode", "rx_mode"}:
            data[key] = value
        elif re.fullmatch(r"antenna_\d+", key):
            data["signal_antennas"].append(_int(value))
    return data
