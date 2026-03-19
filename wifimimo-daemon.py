#!/usr/bin/env python3
"""Long-running wifimimo data daemon."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from wifimimo_core import IFACE, STATE_PATH, collect, write_state


ALERT_DIFF_DBM = 15
ALERT_SIGNAL_DBM = -75
ALERT_RETRY_PCT = 30
POLL_INTERVAL_S = 1.0
RETRY_WINDOW_S = 10.0
ICON_NAME = "network-wireless-hotspot-symbolic"
DESKTOP_ENTRY = "wifimimo"


def log(message: str) -> None:
    print(message, flush=True)


class WifimimoDaemon:
    def __init__(self, iface: str, state_path: Path) -> None:
        self.iface = iface
        self.state_path = state_path
        self.running = True
        self.retry_samples: deque[dict] = deque()
        self.prev_connected = False
        self.prev_mimo_healthy: bool | None = None

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        log(f"wifimimo-daemon starting on {self.iface}")
        while self.running:
            loop_start = time.monotonic()
            state = collect(self.iface)
            state["timestamp"] = int(time.time())
            self.update_retry_window(state, loop_start)
            issues = self.collect_issues(state)
            state["issue_count"] = len(issues)
            self.handle_notifications(state)
            write_state(self.state_path, state)
            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.05, POLL_INTERVAL_S - elapsed))

    def stop(self, *_args) -> None:
        self.running = False

    def reset_retry_window(self) -> None:
        self.retry_samples.clear()

    def session_changed(self, state: dict) -> bool:
        if not self.retry_samples:
            return False
        last = self.retry_samples[-1]
        return (
            last["connected"] != state.get("connected")
            or last["bssid"] != state.get("bssid")
            or state.get("connected_time_s", 0) < last["connected_time_s"]
            or state.get("tx_packets", 0) < last["tx_packets"]
            or state.get("tx_retries", 0) < last["tx_retries"]
            or state.get("tx_failed", 0) < last["tx_failed"]
        )

    def mimo_healthy(self, state: dict) -> bool:
        if not state.get("connected"):
            return False
        tx_nss = int(state.get("tx_nss", 0) or 0)
        rx_nss = int(state.get("rx_nss", 0) or 0)
        values = [value for value in (tx_nss, rx_nss) if value > 0]
        return bool(values) and min(values) >= 2

    def update_retry_window(self, state: dict, now: float) -> None:
        state["retry_10s_pct"] = 0.0
        state["retry_10s_packets"] = 0
        state["retry_10s_retries"] = 0
        state["retry_10s_failed"] = 0

        if not state.get("connected"):
            self.reset_retry_window()
            return

        if self.session_changed(state):
            self.reset_retry_window()

        sample = {
            "connected": True,
            "bssid": state.get("bssid", ""),
            "connected_time_s": int(state.get("connected_time_s", 0) or 0),
            "tx_packets": int(state.get("tx_packets", 0) or 0),
            "tx_retries": int(state.get("tx_retries", 0) or 0),
            "tx_failed": int(state.get("tx_failed", 0) or 0),
            "monotonic": now,
        }
        self.retry_samples.append(sample)

        while self.retry_samples and now - self.retry_samples[0]["monotonic"] > RETRY_WINDOW_S:
            self.retry_samples.popleft()

        base = self.retry_samples[0]
        packet_delta = max(0, sample["tx_packets"] - base["tx_packets"])
        retry_delta = max(0, sample["tx_retries"] - base["tx_retries"])
        failed_delta = max(0, sample["tx_failed"] - base["tx_failed"])

        state["retry_10s_packets"] = packet_delta
        state["retry_10s_retries"] = retry_delta
        state["retry_10s_failed"] = failed_delta
        if packet_delta > 0:
            state["retry_10s_pct"] = retry_delta * 100.0 / packet_delta

    def collect_issues(self, state: dict) -> list[tuple[str, str, str]]:
        issues: list[tuple[str, str, str]] = []
        if not state.get("connected"):
            return issues

        antennas = [int(value) for value in state.get("signal_antennas", [])]
        antenna_count = len(antennas)
        if antenna_count < 2:
            issues.append(("normal", "MIMO Offline", f"Only {antenna_count}/2 antennas reporting"))
        for index, dbm in enumerate(antennas, start=1):
            if dbm < ALERT_SIGNAL_DBM:
                issues.append(("normal", f"Weak Signal — Antenna {index}", f"{dbm} dBm  (threshold {ALERT_SIGNAL_DBM} dBm)"))
        if len(antennas) >= 2:
            spread = max(antennas) - min(antennas)
            if spread > ALERT_DIFF_DBM:
                issues.append(("normal", "Antenna Imbalance", f"{spread} dBm spread  ({min(antennas)} to {max(antennas)} dBm)"))

        tx_nss = int(state.get("tx_nss", 0) or 0)
        rx_nss = int(state.get("rx_nss", 0) or 0)
        if tx_nss and tx_nss != 2:
            issues.append(("critical", "MIMO Degraded — TX", f"Dropped to {tx_nss}x1 SISO  (expected 2x2)"))
        if rx_nss and rx_nss != 2:
            issues.append(("critical", "MIMO Degraded — RX", f"Dropped to {rx_nss}x1 SISO  (expected 2x2)"))

        retry_pct = float(state.get("retry_10s_pct", 0.0) or 0.0)
        if retry_pct > ALERT_RETRY_PCT:
            issues.append(("normal", "High Interference", f"10s TX retry rate: {retry_pct:.1f}%  (threshold {ALERT_RETRY_PCT}%)"))
        return issues

    def handle_notifications(self, state: dict) -> None:
        connected = bool(state.get("connected"))
        if not connected:
            self.prev_connected = False
            return

        mimo_healthy = self.mimo_healthy(state)
        if self.prev_mimo_healthy is None:
            self.prev_mimo_healthy = mimo_healthy
            self.prev_connected = True
            return

        if mimo_healthy != self.prev_mimo_healthy:
            if mimo_healthy:
                self.notify("2x2 MIMO Restored", f"wifimimo returned to 2x2 on {self.iface}", "normal")
            else:
                tx_nss = int(state.get("tx_nss", 0) or 0)
                rx_nss = int(state.get("rx_nss", 0) or 0)
                self.notify(
                    "1x1 MIMO Detected",
                    f"wifimimo dropped to {tx_nss}x{rx_nss} on {self.iface}",
                    "normal",
                )

        self.prev_connected = True
        self.prev_mimo_healthy = mimo_healthy

    def notify(self, title: str, body: str, urgency: str) -> None:
        subprocess.run(
            [
                "notify-send",
                "--app-name=wifimimo",
                f"--urgency={urgency}",
                f"--icon={ICON_NAME}",
                f"--hint=string:desktop-entry:{DESKTOP_ENTRY}",
                title,
                body,
            ],
            check=False,
        )


def main() -> int:
    iface = os.environ.get("WIFI_IFACE", IFACE)
    daemon = WifimimoDaemon(iface, STATE_PATH)
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
