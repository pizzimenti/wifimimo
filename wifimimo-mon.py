#!/usr/bin/env python3
"""
wifimimo-mon.py — terminal wifimimo monitor

Shows per-antenna signal levels, MCS index, spatial streams,
and 10-second TX retry rate from the shared wifimimo daemon state.
"""

from __future__ import annotations

import curses
import locale
import sys
import time

from wifimimo_core import IFACE, MCS_MAX, compute_rates, read_state, safe_ssid


ALERT_DIFF_DBM = 15
ALERT_SIGNAL_DBM = -75
ALERT_RETRY_PCT = 30
SIGNAL_FLOOR = -90
SIGNAL_CEIL = -20

COLOR_HEADER = 1
COLOR_GOOD = 2
COLOR_WARN = 3
COLOR_HOT = 4
COLOR_CRIT = 5
COLOR_DIM = 6
COLOR_TITLE = 7


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


def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_GOOD, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_HOT, curses.COLOR_RED, -1)
    curses.init_pair(COLOR_CRIT, curses.COLOR_WHITE, curses.COLOR_RED)
    curses.init_pair(COLOR_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(COLOR_TITLE, curses.COLOR_BLACK, curses.COLOR_CYAN)


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw_bar(win, y: int, x: int, width: int, fraction: float, color_pair: int) -> None:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    bar_str = "█" * filled + "░" * (width - filled)
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
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
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

    if min_str and min_frac is not None and width >= 10:
        lx = x + min(min_pos, width - len(min_str))
        try:
            win.addstr(y, lx, min_str, label_attr)
        except curses.error:
            pass

    if max_str and max_frac is not None and width >= 10:
        lx = x + max_pos - len(max_str) + 1
        lx = max(x, min(lx, x + width - len(max_str)))
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


def fmt_uptime(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02}m" if h else f"{m}m{s:02}s"


def draw_mcs_ruler(stdscr, row: int, indent: int,
                   mcs: int, lo: int, hi: int,
                   label: str, rate: float,
                   rates: list[float], mcs_max: int,
                   max_y: int) -> int:
    cell_w = 4
    n = mcs_max + 1
    col = mcs_color(mcs)

    if row < max_y - 4:
        safe_addstr(stdscr, row, indent, f"{label:<4}", curses.color_pair(COLOR_DIM))
        safe_addstr(stdscr, row, indent + 4, f"MCS {mcs:2}", col | curses.A_BOLD)
        safe_addstr(stdscr, row, indent + 12, f"{rate:>6.0f} Mb/s", col)
        safe_addstr(stdscr, row, indent + 24, f"min {lo:2}  max {hi:2}", curses.color_pair(COLOR_DIM))
        row += 1

    if row < max_y - 3:
        tick_row = "".join(f"{m:>{cell_w - 1}} " for m in range(n))
        safe_addstr(stdscr, row, indent, tick_row, curses.color_pair(COLOR_DIM))
        row += 1

    if row < max_y - 2:
        bar = ""
        for idx in range(n):
            if idx <= mcs:
                bar += "█" * cell_w
            elif idx <= hi:
                bar += "▒" * cell_w
            else:
                bar += "░" * cell_w
        safe_addstr(stdscr, row, indent, bar, col)
        row += 1

    if row < max_y - 1:
        mbps_row = "".join(f"{rate:>{cell_w - 1}} " for rate in rates)
        safe_addstr(stdscr, row, indent, mbps_row + "Mb/s", curses.color_pair(COLOR_DIM))
        row += 1

    return row


def draw(stdscr, data: dict, hist: History, interval: float) -> None:
    max_y, max_x = stdscr.getmaxyx()
    stdscr.erase()
    row = 0

    bar_w = max(10, min(28, max_x - 54))
    val_col = 22
    bar_col = 33

    def section(title: str) -> bool:
        nonlocal row
        if row >= max_y - 4:
            return False
        safe_addstr(stdscr, row, 0, f"  {title}", curses.color_pair(COLOR_HEADER) | curses.A_BOLD)
        row += 1
        safe_addstr(stdscr, row, 0, "  " + "─" * min(max_x - 4, 64), curses.color_pair(COLOR_DIM))
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
        safe_addstr(stdscr, row, 4, f"{label:<14}", curses.color_pair(COLOR_DIM))
        safe_addstr(stdscr, row, val_col, value_str, val_color)
        if min_frac is not None or max_frac is not None:
            draw_bar_annotated(stdscr, row, bar_col, bar_w, frac, min_frac, max_frac, bar_color, min_str, max_str)
        else:
            draw_bar(stdscr, row, bar_col, bar_w, frac, bar_color)
        if suffix:
            safe_addstr(stdscr, row, bar_col + bar_w + 2, suffix, curses.color_pair(COLOR_DIM))
        row += 1

    safe_addstr(stdscr, row, 0, " wifimimo ".center(max_x), curses.color_pair(COLOR_TITLE) | curses.A_BOLD)
    row += 1
    safe_addstr(
        stdscr,
        row,
        0,
        f" {time.strftime('%H:%M:%S')}  refresh {interval:.0f}s   q quit  +/- interval  r refresh",
        curses.color_pair(COLOR_DIM),
    )
    row += 2

    stale = (time.time() - float(data.get("timestamp", 0) or 0)) > 5
    if not data.get("connected"):
        safe_addstr(stdscr, row, 4, f"Not connected on {data['iface']}", curses.color_pair(COLOR_WARN) | curses.A_BOLD)
        stdscr.refresh()
        return
    if stale:
        safe_addstr(stdscr, row, 4, f"No recent data from wifimimo-daemon on {data['iface']}", curses.color_pair(COLOR_WARN) | curses.A_BOLD)
        row += 2

    freq = data["freq_mhz"]
    band = "6 GHz" if freq >= 6000 else "5 GHz" if freq >= 5000 else "2.4 GHz" if freq else "?"
    uptime = fmt_uptime(data["connected_time_s"]) if data["connected_time_s"] else "?"
    ssid = data.get("ssid_display") or safe_ssid(data["ssid"]) or data["bssid"]
    chan_str = f"  ch{data['chan_num']}" if data.get("chan_num") else ""
    width_str = f"  {data['bandwidth_mhz']} MHz" if data.get("bandwidth_mhz") else ""
    safe_addstr(
        stdscr,
        row,
        4,
        f"{ssid}  ({data['bssid']})  {freq} MHz / {band}{chan_str}{width_str}  up {uptime}",
        curses.color_pair(COLOR_GOOD) | curses.A_BOLD,
    )
    row += 2

    if section("SIGNAL"):
        def sig_row(label: str, dbm: int, hist_key: str) -> None:
            hist.update(hist_key, dbm)
            lo = hist.min(hist_key)
            hi = hist.max(hist_key)
            col = signal_color(dbm)
            metric(
                label,
                f"{dbm:4} dBm",
                col,
                signal_fraction(dbm),
                col,
                min_frac=signal_fraction(lo),
                max_frac=signal_fraction(hi),
                min_str=f"{lo}",
                max_str=f"{hi}",
            )

        sig_row("Overall", data["signal_dbm"], "sig_overall")
        sig_row("Avg", data["signal_avg_dbm"], "sig_avg")
        for index, dbm in enumerate(data["signal_antennas"]):
            sig_row(f"Antenna {index + 1}", dbm, f"sig_ant{index}")

        antennas = data["signal_antennas"]
        if len(antennas) >= 2 and row < max_y - 3:
            spread = max(antennas) - min(antennas)
            hist.update("sig_spread", spread)
            hi_spread = hist.max("sig_spread", spread)
            col = (curses.color_pair(COLOR_HOT) | curses.A_BOLD) if spread > ALERT_DIFF_DBM else curses.color_pair(COLOR_GOOD)
            metric(
                "Spread",
                f"{spread:4} dBm",
                col,
                min(1.0, spread / (ALERT_DIFF_DBM * 2)),
                col,
                max_frac=min(1.0, hi_spread / (ALERT_DIFF_DBM * 2)),
                max_str=f"{hi_spread:.0f}",
                suffix=f"warn >{ALERT_DIFF_DBM}",
            )
        row += 1

    if section("RATES"):
        gi_labels = {0: "0.8µs", 1: "1.6µs", 2: "3.2µs"}
        for label, rate_key, nss_key, mcs_key, mode_key, gi_key in [
            ("TX", "tx_rate_mbps", "tx_nss", "tx_mcs", "tx_mode", "tx_gi"),
            ("RX", "rx_rate_mbps", "rx_nss", "rx_mcs", "rx_mode", "rx_gi"),
        ]:
            rate = data[rate_key]
            nss = data[nss_key]
            mcs = data[mcs_key]
            mode = data.get(mode_key, "HE")
            gi = data.get(gi_key, -1)
            hist.update(rate_key, rate)
            lo = hist.min(rate_key, rate)
            hi = hist.max(rate_key, rate)
            computed = compute_rates(rate, mcs, mode) if mcs >= 0 else []
            max_rate = float(computed[-1]) if computed else (rate or 1.0)
            parts = []
            if nss:
                parts.append(f"NSS {nss} {nss_dots(nss)}")
            if gi >= 0:
                parts.append(f"GI {gi_labels.get(gi, str(gi))}")
            metric(
                label,
                f"{rate:6.1f} Mb/s",
                curses.color_pair(COLOR_GOOD),
                min(1.0, rate / max(max_rate, 1.0)),
                curses.color_pair(COLOR_GOOD),
                min_frac=min(1.0, lo / max(max_rate, 1.0)),
                max_frac=min(1.0, hi / max(max_rate, 1.0)),
                min_str=f"{lo:.0f}",
                max_str=f"{hi:.0f}",
                suffix="  ".join(parts),
            )
        row += 1

    if data["tx_mcs"] >= 0 and section("MCS INDEX"):
        for label, mcs_key, rate_key, mode_key in [
            ("TX", "tx_mcs", "tx_rate_mbps", "tx_mode"),
            ("RX", "rx_mcs", "rx_rate_mbps", "rx_mode"),
        ]:
            mcs = data[mcs_key]
            rate = data[rate_key]
            mode = data.get(mode_key, "HE")
            if mcs < 0:
                continue
            hist.update(mcs_key, mcs)
            lo = int(hist.min(mcs_key, mcs))
            hi = int(hist.max(mcs_key, mcs))
            computed = compute_rates(rate, mcs, mode)
            mcs_max = MCS_MAX.get(mode, 11)
            row = draw_mcs_ruler(stdscr, row, 4, mcs, lo, hi, label, rate, computed, mcs_max, max_y)
            row += 1

    if section("TX RETRIES"):
        retry_pct = float(data.get("retry_10s_pct", 0.0) or 0.0)
        retry_packets = int(data.get("retry_10s_packets", 0) or 0)
        retry_retries = int(data.get("retry_10s_retries", 0) or 0)
        retry_failed = int(data.get("retry_10s_failed", 0) or 0)
        hist.update("retry_pct", retry_pct)
        hi_retry = hist.max("retry_pct", retry_pct)
        col = (
            (curses.color_pair(COLOR_CRIT) | curses.A_BOLD) if retry_pct > ALERT_RETRY_PCT
            else curses.color_pair(COLOR_WARN) if retry_pct > 10
            else curses.color_pair(COLOR_GOOD)
        )
        metric(
            "Retry rate",
            f"{retry_pct:5.1f}%",
            col,
            min(1.0, retry_pct / 100.0),
            col,
            max_frac=min(1.0, hi_retry / 100.0),
            max_str=f"{hi_retry:.1f}%",
            suffix=f"warn >{ALERT_RETRY_PCT}%  ({retry_retries}/{retry_packets}  fail {retry_failed} over 10s)",
        )

    safe_addstr(stdscr, max_y - 2, 2, " source: wifimimo-daemon shared state ", curses.color_pair(COLOR_DIM))
    safe_addstr(stdscr, max_y - 1, 0, " q quit | +/- interval | r refresh ".ljust(max_x - 1), curses.color_pair(COLOR_TITLE))
    stdscr.refresh()


def main(stdscr) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)
    init_colors()

    interval = 2.0
    last_update = 0.0
    data: dict = read_state()
    iface = sys.argv[1] if len(sys.argv) > 1 else IFACE
    hist = History()

    while True:
        now = time.monotonic()
        if now - last_update >= interval:
            data = read_state()
            if iface and data.get("iface"):
                data["iface"] = iface
            last_update = now

        if data:
            draw(stdscr, data, hist, interval)

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        if key == ord("+"):
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
