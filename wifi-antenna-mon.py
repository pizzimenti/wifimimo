#!/usr/bin/env python3
"""
wifi-antenna-mon.py — Terminal WiFi antenna monitor

Shows per-antenna signal levels, MCS index, spatial streams, TX/RX rates,
and retry rate — with live min/max tracking and graphical bars.
"""

import curses
import locale
import os
import re
import subprocess
import sys
import time
import unicodedata


# ── config ────────────────────────────────────────────────────────────────────

IFACE            = "wlp1s0"
ALERT_DIFF_DBM   = 15     # antenna divergence warning threshold
ALERT_SIGNAL_DBM = -75    # weak signal threshold
ALERT_RETRY_PCT  = 30     # TX retry rate warning threshold
SIGNAL_FLOOR     = -90    # dBm floor for bar scaling
SIGNAL_CEIL      = -20    # dBm ceiling for bar scaling

# Max MCS index per Wi-Fi generation
MCS_MAX: dict[str, int] = {"HE": 11, "VHT": 9, "HT": 7}

# Spectral efficiency per MCS index (proportional to coded bits × modulation order)
# Ratios are exact — used to back-calculate the full rate table from one known point.
EFFICIENCY: dict[str, list[float]] = {
    "HE":  [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0, 6.0, 20/3, 7.5, 25/3],
    "VHT": [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0, 6.0, 20/3],
    "HT":  [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0],
}


# ── color pairs ───────────────────────────────────────────────────────────────

COLOR_HEADER = 1
COLOR_GOOD   = 2
COLOR_WARN   = 3
COLOR_HOT    = 4
COLOR_CRIT   = 5
COLOR_DIM    = 6
COLOR_TITLE  = 7


# ── data collection ───────────────────────────────────────────────────────────

def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        return r.stdout
    except Exception:
        return ""


def _int(s, default: int = 0) -> int:
    try:
        return int(str(s).strip())
    except (ValueError, AttributeError):
        return default


def _float(s, default: float = 0.0) -> float:
    try:
        return float(str(s).strip())
    except (ValueError, AttributeError):
        return default


def compute_rates(ref_rate: float, ref_mcs: int, mode: str) -> list[float]:
    """Back-calculate all MCS rates from a known (rate, MCS) reference point."""
    eff = EFFICIENCY.get(mode, EFFICIENCY["HE"])
    if ref_mcs < 0 or ref_mcs >= len(eff) or eff[ref_mcs] == 0:
        return [0.0] * len(eff)
    return [round(ref_rate * e / eff[ref_mcs]) for e in eff]


def collect(iface: str) -> dict:
    data: dict = {
        "iface":            iface,
        "connected":        False,
        "ssid":             "",
        "bssid":            "",
        "freq_mhz":         0,
        "chan_num":          0,
        "chan_width_mhz":    0,
        "signal_dbm":       0,
        "signal_avg_dbm":   0,
        "signal_antennas":  [],
        "tx_rate_mbps":     0.0,
        "tx_mcs":           -1,
        "tx_nss":           0,
        "tx_mode":          "HE",
        "tx_gi":            -1,
        "rx_rate_mbps":     0.0,
        "rx_mcs":           -1,
        "rx_nss":           0,
        "rx_mode":          "HE",
        "rx_gi":            -1,
        "tx_packets":       0,
        "tx_retries":       0,
        "tx_failed":        0,
        "rx_packets":       0,
        "connected_time_s": 0,
    }

    # ── iw dev — channel width and number (no root required) ─────────────
    dev_out = _run(["iw", "dev", iface])
    m = re.search(r"channel\s+(\d+)\s+\(.*?\),\s*width:\s*(\d+)\s*MHz", dev_out)
    if m:
        data["chan_num"]       = _int(m.group(1))
        data["chan_width_mhz"] = _int(m.group(2))

    # ── iw dev link — no root required ───────────────────────────────────
    link = _run(["iw", "dev", iface, "link"])
    if "Not connected" in link or not link.strip():
        return data

    data["connected"] = True

    m = re.search(r"SSID:\s*(.+)", link)
    if m:
        data["ssid"] = m.group(1).strip()
    m = re.search(r"Connected to\s+([0-9a-f:]{17})", link)
    if m:
        data["bssid"] = m.group(1)
    m = re.search(r"freq:\s*(\d+)", link)
    if m:
        data["freq_mhz"] = _int(m.group(1))
    m = re.search(r"signal:\s+([-\d]+)", link)
    if m:
        data["signal_dbm"] = _int(m.group(1))

    # tx/rx bitrate from link (HE, VHT, HT)
    for direction in ("tx", "rx"):
        # HE / VHT: "tx bitrate: 600.4 MBit/s HE-MCS 11 … HE-NSS 2 …"
        m = re.search(
            rf"{direction} bitrate:\s+([\d.]+)\s+MBit/s"
            r"(?:.*?(HE|VHT)-MCS\s+(\d+))?"
            r"(?:.*?(?:HE|VHT)-NSS\s+(\d+))?",
            link,
        )
        if m:
            data[f"{direction}_rate_mbps"] = _float(m.group(1))
            if m.group(2):
                data[f"{direction}_mode"] = m.group(2)   # "HE" or "VHT"
            if m.group(3):
                data[f"{direction}_mcs"] = _int(m.group(3))
            if m.group(4):
                data[f"{direction}_nss"] = _int(m.group(4))
            gi = re.search(rf"{direction} bitrate:.*?HE-GI\s+(\d+)", link)
            if gi:
                data[f"{direction}_gi"] = _int(gi.group(1))
            # HT fallback: "tx bitrate: 144.4 MBit/s MCS 15 40MHz …"
            if not m.group(2):
                ht = re.search(
                    rf"{direction} bitrate:\s+[\d.]+\s+MBit/s.*?\bMCS\s+(\d+)",
                    link,
                )
                if ht:
                    raw = _int(ht.group(1))   # 0-31 aggregate HT MCS
                    data[f"{direction}_mode"] = "HT"
                    data[f"{direction}_mcs"]  = raw % 8
                    data[f"{direction}_nss"]  = raw // 8 + 1

    # ── iw station dump — root required; adds per-antenna + retry stats ───
    dump = _run(["iw", "dev", iface, "station", "dump"])
    if not dump:
        data["station_dump_available"] = False
        return data

    data["station_dump_available"] = True

    # per-antenna signal:  -53 [-56, -51] dBm
    m = re.search(r"signal:\s+([-\d]+)\s+\[([-\d,\s]+)\]", dump)
    if m:
        data["signal_dbm"]      = _int(m.group(1))
        data["signal_antennas"] = [_int(v) for v in m.group(2).split(",")]

    m = re.search(r"signal avg:\s+([-\d]+)", dump)
    if m:
        data["signal_avg_dbm"] = _int(m.group(1))

    for key, pat in [
        ("tx_packets",       r"tx packets:\s+(\d+)"),
        ("tx_retries",       r"tx retries:\s+(\d+)"),
        ("tx_failed",        r"tx failed:\s+(\d+)"),
        ("rx_packets",       r"rx packets:\s+(\d+)"),
        ("connected_time_s", r"connected time:\s+(\d+)\s+s"),
    ]:
        m = re.search(pat, dump)
        if m:
            data[key] = _int(m.group(1))

    return data


# ── session history (min / max tracking) ─────────────────────────────────────

class History:
    def __init__(self):
        self._min: dict[str, float] = {}
        self._max: dict[str, float] = {}

    def update(self, key: str, value: float) -> None:
        if key not in self._min or value < self._min[key]:
            self._min[key] = value
        if key not in self._max or value > self._max[key]:
            self._max[key] = value

    def min(self, key: str, default=None):
        return self._min.get(key, default)

    def max(self, key: str, default=None):
        return self._max.get(key, default)


# ── curses helpers ────────────────────────────────────────────────────────────

def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN,   -1)
    curses.init_pair(COLOR_GOOD,   curses.COLOR_GREEN,  -1)
    curses.init_pair(COLOR_WARN,   curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_HOT,    curses.COLOR_RED,    -1)
    curses.init_pair(COLOR_CRIT,   curses.COLOR_WHITE,   curses.COLOR_RED)
    curses.init_pair(COLOR_DIM,    curses.COLOR_WHITE,  -1)
    curses.init_pair(COLOR_TITLE,  curses.COLOR_BLACK,   curses.COLOR_CYAN)


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw_bar(win, y: int, x: int, width: int, fraction: float, color_pair: int) -> None:
    fraction = max(0.0, min(1.0, fraction))
    filled   = round(fraction * width)
    bar_str  = "█" * filled + "░" * (width - filled)
    try:
        win.addstr(y, x, bar_str, color_pair)
    except curses.error:
        pass


def draw_bar_annotated(
    win, y: int, x: int, width: int,
    frac: float,
    min_frac: float | None, max_frac: float | None,
    color_pair: int,
    min_str: str = "", max_str: str = "",
) -> None:
    """Bar with session min/max value labels overlaid at their positions.

    Fills █ up to current value, ▒ from current to session-max, ░ beyond.
    Overlays min_str at the session-min position and max_str ending at
    the session-max position, in bold-dim so they stand out on the bar.
    """
    frac    = max(0.0, min(1.0, frac))
    filled  = round(frac * width)
    min_pos = round(max(0.0, min(1.0, min_frac or 0.0)) * width)
    max_pos = round(max(0.0, min(1.0, max_frac or frac)) * width)

    chars = []
    for i in range(width):
        if i < filled:
            chars.append("█")
        elif i <= max_pos:
            chars.append("▒")
        else:
            chars.append("░")
    try:
        win.addstr(y, x, "".join(chars), color_pair)
    except curses.error:
        pass

    label_attr = curses.color_pair(COLOR_DIM) | curses.A_BOLD

    # Overlay min label left-aligned at min_pos
    if min_str and min_frac is not None and width >= 10:
        lx = x + min(min_pos, width - len(min_str))
        try:
            win.addstr(y, lx, min_str, label_attr)
        except curses.error:
            pass

    # Overlay max label right-aligned ending at max_pos
    if max_str and max_frac is not None and width >= 10:
        lx = x + max_pos - len(max_str) + 1
        lx = max(x, min(lx, x + width - len(max_str)))
        # Skip if it would clobber the min label
        min_end = x + min(min_pos, width - len(min_str)) + len(min_str)
        if lx >= min_end or not min_str:
            try:
                win.addstr(y, lx, max_str, label_attr)
            except curses.error:
                pass


def signal_fraction(dbm: int) -> float:
    return (dbm - SIGNAL_FLOOR) / (SIGNAL_CEIL - SIGNAL_FLOOR)


def signal_color(dbm: int) -> int:
    if dbm < ALERT_SIGNAL_DBM:
        return curses.color_pair(COLOR_HOT) | curses.A_BOLD
    if dbm < -65:
        return curses.color_pair(COLOR_WARN)
    return curses.color_pair(COLOR_GOOD)


def mcs_color(mcs: int) -> int:
    if mcs >= 8:
        return curses.color_pair(COLOR_GOOD)
    if mcs >= 4:
        return curses.color_pair(COLOR_WARN)
    return curses.color_pair(COLOR_HOT)


def nss_dots(nss: int) -> str:
    return "●" * nss + "○" * max(0, 2 - nss)


def safe_ssid(ssid: str) -> str:
    """Decode iw's \\xNN byte escapes into proper unicode."""
    try:
        latin = re.sub(r"\\x([0-9a-fA-F]{2})",
                       lambda m: chr(int(m.group(1), 16)), ssid)
        return latin.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return re.sub(r"\\x[0-9a-fA-F]{2}", "?", ssid)


def fmt_uptime(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h{m:02}m" if h else f"{m}m{s:02}s"


# ── MCS ruler ────────────────────────────────────────────────────────────────

def draw_mcs_ruler(stdscr, row: int, indent: int,
                   mcs: int, lo: int, hi: int,
                   label: str, rate: float,
                   rates: list, mcs_max: int,
                   max_y: int) -> int:
    """
    4-row MCS ruler:
      Row 0 — label, current MCS, current Mb/s, session min/max
      Row 1 — MCS index tick labels  (0  1  2  … <mcs_max>)
      Row 2 — bar: █ current  ▒ session-high range  ░ unseen
      Row 3 — theoretical Mb/s tick labels
    """
    cell_w  = 4
    n       = mcs_max + 1
    col     = mcs_color(mcs)

    # Row 0 — summary line
    if row < max_y - 4:
        safe_addstr(stdscr, row, indent,
                    f"{label:<4}", curses.color_pair(COLOR_DIM))
        safe_addstr(stdscr, row, indent + 4,
                    f"MCS {mcs:2}", col | curses.A_BOLD)
        safe_addstr(stdscr, row, indent + 12,
                    f"{rate:>6.0f} Mb/s", col)
        safe_addstr(stdscr, row, indent + 24,
                    f"min {lo:2}  max {hi:2}",
                    curses.color_pair(COLOR_DIM))
        row += 1

    # Row 1 — MCS number ticks
    if row < max_y - 3:
        tick_row = "".join(f"{m:>{cell_w - 1}} " for m in range(n))
        safe_addstr(stdscr, row, indent, tick_row, curses.color_pair(COLOR_DIM))
        row += 1

    # Row 2 — bar
    if row < max_y - 2:
        bar = ""
        for m in range(n):
            if m <= mcs:
                bar += "█" * cell_w
            elif m <= hi:
                bar += "▒" * cell_w
            else:
                bar += "░" * cell_w
        safe_addstr(stdscr, row, indent, bar, col)
        row += 1

    # Row 3 — Mb/s labels
    if row < max_y - 1:
        mbps_row = "".join(f"{r:>{cell_w - 1}} " for r in rates)
        safe_addstr(stdscr, row, indent, mbps_row + "Mb/s",
                    curses.color_pair(COLOR_DIM))
        row += 1

    return row


# ── drawing ───────────────────────────────────────────────────────────────────

def draw(stdscr, data: dict, hist: History, interval: float) -> None:
    max_y, max_x = stdscr.getmaxyx()
    stdscr.erase()
    row = 0

    bar_w   = max(10, min(28, max_x - 54))
    val_col = 22
    bar_col = 33

    def section(title: str) -> bool:
        nonlocal row
        if row >= max_y - 4:
            return False
        safe_addstr(stdscr, row, 0, f"  {title}",
                    curses.color_pair(COLOR_HEADER) | curses.A_BOLD)
        row += 1
        safe_addstr(stdscr, row, 0, "  " + "─" * min(max_x - 4, 64),
                    curses.color_pair(COLOR_DIM))
        row += 1
        return True

    def metric(label: str, value_str: str, val_color: int,
                frac: float, bar_color: int,
                min_frac: float | None = None, max_frac: float | None = None,
                min_str: str = "", max_str: str = "",
                suffix: str = "") -> None:
        nonlocal row
        if row >= max_y - 3:
            return
        safe_addstr(stdscr, row, 4,       f"{label:<14}", curses.color_pair(COLOR_DIM))
        safe_addstr(stdscr, row, val_col, value_str,      val_color)
        if min_frac is not None or max_frac is not None:
            draw_bar_annotated(stdscr, row, bar_col, bar_w, frac,
                               min_frac, max_frac, bar_color, min_str, max_str)
        else:
            draw_bar(stdscr, row, bar_col, bar_w, frac, bar_color)
        if suffix:
            safe_addstr(stdscr, row, bar_col + bar_w + 2, suffix,
                        curses.color_pair(COLOR_DIM))
        row += 1

    # ── title ─────────────────────────────────────────────────────────────
    safe_addstr(stdscr, row, 0,
                " WIFIMON — WiFi Antenna Monitor ".center(max_x),
                curses.color_pair(COLOR_TITLE) | curses.A_BOLD)
    row += 1
    safe_addstr(stdscr, row, 0,
                f" {time.strftime('%H:%M:%S')}  refresh {interval:.0f}s"
                f"   q quit  +/- interval  r refresh",
                curses.color_pair(COLOR_DIM))
    row += 2

    if not data.get("connected"):
        safe_addstr(stdscr, row, 4,
                    f"Not connected on {data['iface']}",
                    curses.color_pair(COLOR_WARN) | curses.A_BOLD)
        stdscr.refresh()
        return

    # ── connection summary ─────────────────────────────────────────────────
    freq      = data["freq_mhz"]
    band      = ("6 GHz" if freq >= 6000 else
                 "5 GHz" if freq >= 5000 else
                 "2.4 GHz" if freq else "?")
    uptime    = fmt_uptime(data["connected_time_s"]) if data["connected_time_s"] else "?"
    ssid      = safe_ssid(data["ssid"]) or data["bssid"]
    chan_str  = f"  ch{data['chan_num']}" if data.get("chan_num") else ""
    width_str = f"  {data['chan_width_mhz']} MHz" if data.get("chan_width_mhz") else ""
    safe_addstr(stdscr, row, 4,
                f"{ssid}  ({data['bssid']})  {freq} MHz / {band}{chan_str}{width_str}  up {uptime}",
                curses.color_pair(COLOR_GOOD) | curses.A_BOLD)
    row += 2

    # ── signal ────────────────────────────────────────────────────────────
    if section("SIGNAL"):
        def sig_row(label: str, dbm: int, hist_key: str) -> None:
            hist.update(hist_key, dbm)
            lo = hist.min(hist_key)
            hi = hist.max(hist_key)
            col = signal_color(dbm)
            metric(label,
                   f"{dbm:4} dBm",
                   col,
                   signal_fraction(dbm),
                   col,
                   min_frac=signal_fraction(lo),
                   max_frac=signal_fraction(hi),
                   min_str=f"{lo}",
                   max_str=f"{hi}")

        sig_row("Overall",   data["signal_dbm"],     "sig_overall")
        sig_row("Avg",       data["signal_avg_dbm"],  "sig_avg")
        for i, dbm in enumerate(data["signal_antennas"]):
            sig_row(f"Antenna {i + 1}", dbm, f"sig_ant{i}")

        # Spread
        ants = data["signal_antennas"]
        if len(ants) >= 2 and row < max_y - 3:
            spread = max(ants) - min(ants)
            hist.update("sig_spread", spread)
            hi_sp  = hist.max("sig_spread", spread)
            col    = (curses.color_pair(COLOR_HOT) | curses.A_BOLD
                      if spread > ALERT_DIFF_DBM else curses.color_pair(COLOR_GOOD))
            metric("Spread",
                   f"{spread:4} dBm",
                   col,
                   min(1.0, spread / (ALERT_DIFF_DBM * 2)),
                   col,
                   max_frac=min(1.0, hi_sp / (ALERT_DIFF_DBM * 2)),
                   max_str=f"{hi_sp:.0f}",
                   suffix=f"warn >{ALERT_DIFF_DBM}")
        row += 1

    # ── rates ─────────────────────────────────────────────────────────────
    if section("RATES"):
        _GI_LABELS = {0: "0.8µs", 1: "1.6µs", 2: "3.2µs"}
        for label, rate_key, nss_key, mcs_key, mode_key, gi_key in [
            ("TX", "tx_rate_mbps", "tx_nss", "tx_mcs", "tx_mode", "tx_gi"),
            ("RX", "rx_rate_mbps", "rx_nss", "rx_mcs", "rx_mode", "rx_gi"),
        ]:
            rate = data[rate_key]
            nss  = data[nss_key]
            mcs  = data[mcs_key]
            mode = data.get(mode_key, "HE")
            gi   = data.get(gi_key, -1)
            hist.update(rate_key, rate)
            lo = hist.min(rate_key, rate)
            hi = hist.max(rate_key, rate)
            # Dynamic ceiling: rate at MCS-max for current mode
            if mcs >= 0:
                computed = compute_rates(rate, mcs, mode)
                max_rate = float(computed[-1]) if computed else rate
            else:
                max_rate = rate or 1.0
            parts = []
            if nss:
                parts.append(f"NSS {nss} {nss_dots(nss)}")
            if gi >= 0:
                parts.append(f"GI {_GI_LABELS.get(gi, str(gi))}")
            nss_str = "  ".join(parts)
            metric(label,
                   f"{rate:6.1f} Mb/s",
                   curses.color_pair(COLOR_GOOD),
                   min(1.0, rate / max(max_rate, 1.0)),
                   curses.color_pair(COLOR_GOOD),
                   min_frac=min(1.0, lo / max(max_rate, 1.0)),
                   max_frac=min(1.0, hi / max(max_rate, 1.0)),
                   min_str=f"{lo:.0f}",
                   max_str=f"{hi:.0f}",
                   suffix=nss_str)
        row += 1

    # ── MCS ruler ─────────────────────────────────────────────────────────
    if data["tx_mcs"] >= 0 and section("MCS INDEX"):
        for label, mcs_key, rate_key, mode_key in [
            ("TX", "tx_mcs", "tx_rate_mbps", "tx_mode"),
            ("RX", "rx_mcs", "rx_rate_mbps", "rx_mode"),
        ]:
            mcs  = data[mcs_key]
            rate = data[rate_key]
            mode = data.get(mode_key, "HE")
            if mcs < 0:
                continue
            hist.update(mcs_key, mcs)
            lo = int(hist.min(mcs_key, mcs))
            hi = int(hist.max(mcs_key, mcs))
            computed  = compute_rates(rate, mcs, mode)
            mcs_max   = MCS_MAX.get(mode, 11)
            row = draw_mcs_ruler(stdscr, row, 4, mcs, lo, hi,
                                 label, rate, computed, mcs_max, max_y)
            row += 1  # blank between TX and RX

    # ── TX retries ────────────────────────────────────────────────────────
    if section("TX RETRIES"):
        tx_pkts    = data["tx_packets"]
        tx_retries = data["tx_retries"]
        tx_failed  = data["tx_failed"]
        if tx_pkts > 0:
            retry_pct = tx_retries * 100 / tx_pkts
            hist.update("retry_pct", retry_pct)
            hi_r = hist.max("retry_pct", retry_pct)
            col  = (curses.color_pair(COLOR_CRIT) | curses.A_BOLD
                    if retry_pct > ALERT_RETRY_PCT else
                    curses.color_pair(COLOR_WARN) if retry_pct > 10 else
                    curses.color_pair(COLOR_GOOD))
            metric("Retry rate",
                   f"{retry_pct:5.1f}%",
                   col,
                   min(1.0, retry_pct / 100.0),
                   col,
                   max_frac=min(1.0, hi_r / 100.0),
                   max_str=f"{hi_r:.1f}%",
                   suffix=f"warn >{ALERT_RETRY_PCT}%  ({tx_retries}/{tx_pkts}  fail {tx_failed})")
        else:
            safe_addstr(stdscr, row, 4, "No TX data yet",
                        curses.color_pair(COLOR_DIM))
            row += 1

    # ── footer ────────────────────────────────────────────────────────────
    if not data.get("station_dump_available"):
        safe_addstr(stdscr, max_y - 2, 2,
                    " run as root for per-antenna signal and retry stats ",
                    curses.color_pair(COLOR_DIM))
    safe_addstr(stdscr, max_y - 1, 0,
                " q quit | +/- interval | r refresh ".ljust(max_x - 1),
                curses.color_pair(COLOR_TITLE))
    stdscr.refresh()


# ── main loop ─────────────────────────────────────────────────────────────────

def main(stdscr) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)
    init_colors()

    iface       = sys.argv[1] if len(sys.argv) > 1 else IFACE
    interval    = 2.0
    last_update = 0.0
    data: dict  = {}
    hist        = History()

    while True:
        now = time.monotonic()
        if now - last_update >= interval:
            data       = collect(iface)
            last_update = now

        if data:
            draw(stdscr, data, hist, interval)

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        elif key == ord("+"):
            interval = min(30.0, interval + 1.0)
        elif key == ord("-"):
            interval = max(0.5, interval - 0.5)
        elif key in (ord("r"), ord("R")):
            last_update = 0


if __name__ == "__main__":
    try:
        locale.setlocale(locale.LC_ALL, "")
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
