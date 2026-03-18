#!/usr/bin/env python3
"""
wifi-antenna-tray — System tray MIMO status indicator

Green  = 2×2 MIMO (TX NSS 2)
Orange = 1×1 SISO  (TX NSS 1 or degraded)
Gray   = not connected / state unknown
"""

import os
import subprocess
import sys
import time

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


# ── config ────────────────────────────────────────────────────────────────────

IFACE        = "wlp1s0"
STATE_FILE   = os.path.expanduser(
    f"/run/user/{os.getuid()}/wifi-antenna-state"
)
POLL_MS      = 30_000   # re-read state file every 30 s
STALE_SECS   = 900      # treat state as unknown if older than 15 min

COLOR_GOOD     = QColor("#4CAF50")   # green  — 2×2 MIMO
COLOR_DEGRADED = QColor("#FF9800")   # orange — 1×1 SISO
COLOR_UNKNOWN  = QColor("#9E9E9E")   # gray   — no data


# ── icon drawing ──────────────────────────────────────────────────────────────

def make_icon(color: QColor, size: int = 22) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)

    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    cx   = size / 2
    base = size * 0.82   # dot anchor — lower portion

    # WiFi arcs radiating upward from the dot
    for radius, width in [(size * 0.18, 2.2),
                           (size * 0.35, 2.2),
                           (size * 0.52, 2.2)]:
        pen = QPen(color, width, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        rect = QRectF(cx - radius, base - radius,
                      radius * 2, radius * 2)
        p.drawArc(rect, 40 * 16, 100 * 16)   # upper arc, ~100° span

    # Centre dot
    dot_r = size * 0.10
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawEllipse(QRectF(cx - dot_r, base - dot_r,
                         dot_r * 2, dot_r * 2))
    p.end()
    return QIcon(px)


ICON_GOOD     = None
ICON_DEGRADED = None
ICON_UNKNOWN  = None


# ── state reading ─────────────────────────────────────────────────────────────

def read_state() -> dict:
    """Parse the state file written by wifi-antenna-check."""
    state: dict = {
        "connected":   False,
        "tx_nss":      "",
        "rx_nss":      "",
        "signal_dbm":  0,
        "retry_pct":   0,
        "issue_count": 0,
        "timestamp":   0,
        "stale":       True,
    }
    try:
        with open(STATE_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                state[k] = v
        state["timestamp"]   = int(state.get("timestamp", 0))
        state["signal_dbm"]  = int(state.get("signal_dbm", 0))
        state["retry_pct"]   = int(state.get("retry_pct", 0))
        state["issue_count"] = int(state.get("issue_count", 0))
        state["connected"]   = state.get("connected") == "true"
        state["stale"]       = (time.time() - state["timestamp"]) > STALE_SECS
    except (FileNotFoundError, ValueError):
        pass
    return state


def icon_for(state: dict):
    if state["stale"] or not state["connected"]:
        return ICON_UNKNOWN
    tx_nss = state.get("tx_nss", "")
    if tx_nss == "2":
        return ICON_GOOD
    if tx_nss == "1":
        return ICON_DEGRADED
    return ICON_UNKNOWN


def tooltip_for(state: dict) -> str:
    if not state["connected"] or state["stale"]:
        return "WiFi Monitor — no recent data"
    tx  = state.get("tx_nss") or "?"
    rx  = state.get("rx_nss") or "?"
    sig = state["signal_dbm"]
    ret = state["retry_pct"]
    mimo = "2×2 MIMO" if tx == "2" else f"TX {tx}×1 SISO" if tx != "?" else "MIMO unknown"
    return (f"WiFi Monitor — {IFACE}\n"
            f"{mimo}  ·  RX NSS {rx}\n"
            f"Signal {sig} dBm  ·  TX retries {ret}%")


# ── tray ──────────────────────────────────────────────────────────────────────

class WifiTray:
    def __init__(self):
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(ICON_UNKNOWN)
        self.tray.setToolTip("WiFi Monitor — loading…")
        self.tray.setVisible(True)

        menu = QMenu()
        menu.addAction("Check Now", self._check_now)
        menu.addAction("View Logs", self._view_logs)
        menu.addSeparator()
        menu.addAction("Quit", QApplication.quit)
        self.tray.setContextMenu(menu)

        self._timer = QTimer()
        self._timer.timeout.connect(self._poll)
        self._timer.start(POLL_MS)
        self._poll()

    def _poll(self):
        state = read_state()
        self.tray.setIcon(icon_for(state))
        self.tray.setToolTip(tooltip_for(state))

    def _check_now(self):
        subprocess.Popen(["pkexec", "wifi-antenna-check"])
        QTimer.singleShot(3000, self._poll)   # refresh icon after 3 s

    def _view_logs(self):
        subprocess.Popen([
            "ghostty", "--", "journalctl", "-t", "wifi-antenna-check", "-n", "50", "--no-pager"
        ])


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray available.", file=sys.stderr)
        sys.exit(1)

    global ICON_GOOD, ICON_DEGRADED, ICON_UNKNOWN
    ICON_GOOD     = make_icon(COLOR_GOOD)
    ICON_DEGRADED = make_icon(COLOR_DEGRADED)
    ICON_UNKNOWN  = make_icon(COLOR_UNKNOWN)

    _tray = WifiTray()  # noqa: F841 — keep reference alive
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
