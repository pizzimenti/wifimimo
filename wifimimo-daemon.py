#!/usr/bin/env python3
"""Long-running wifimimo data daemon."""

from __future__ import annotations

import csv
import io
import os
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from wifimimo_core import (
    HISTORY_COLUMNS,
    HISTORY_DIR,
    IFACE,
    STATE_PATH,
    UI_ACTIVE_PATH,
    collect,
    collect_power,
    derive_display,
    history_row,
    write_state,
)


ALERT_DIFF_DBM = 15
ALERT_SIGNAL_DBM = -75
ALERT_RETRY_PCT = 30
POLL_FAST_S = 1.0
POLL_SLOW_S = 5.0
TRANSITION_COOLDOWN_S = 30.0
RETRY_WINDOW_S = 10.0
U32_COUNTER_MODULUS = 2 ** 32
# How recently the plasmoid must have touched UI_ACTIVE_PATH for the daemon
# to consider the popup expanded. Has to be > plasmoid's 1s expanded poll
# (so a slow tick doesn't expire it) but short enough to drop back to slow
# poll quickly after the popup closes.
UI_ACTIVE_TTL_S = 3.0


def log(message: str) -> None:
    print(message, flush=True)


class WifimimoDaemon:
    def __init__(self, iface: str, state_path: Path, history_dir: Path) -> None:
        self.iface = iface
        self.state_path = state_path
        self.history_dir = history_dir
        self.running = True
        self.retry_samples: deque[dict] = deque()
        self.last_transition_time = 0.0
        self.last_state_signature: tuple | None = None
        self._history_file = None
        self._history_writer = None
        self._history_date: str = ""

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        log(f"wifimimo-daemon starting on {self.iface}")
        while self.running:
            loop_start = time.monotonic()
            state = collect(self.iface)
            state["timestamp"] = int(time.time())
            state.update(collect_power(self.iface))
            self.update_retry_window(state, loop_start)
            issues = self.collect_issues(state)
            state["issue_count"] = len(issues)
            state["display"] = derive_display(state)
            poll_interval = self.poll_interval_for_state(state, loop_start)
            write_state(self.state_path, state)
            self.write_history(state)
            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.05, poll_interval - elapsed))

    def stop(self, *_args) -> None:
        self.running = False
        self._close_history()

    def _open_history(self, date_str: str) -> None:
        self._close_history()
        self.history_dir.mkdir(parents=True, exist_ok=True)
        path = self.history_dir / f"{date_str}.csv"
        # If today's file already exists with a different header (the daemon
        # restarted mid-day after a schema change — e.g. a new HISTORY_COLUMNS
        # entry landed in an upgrade), rotate the old file aside so we don't
        # append new-shape rows under an old-shape header. Same-day rotation
        # uses a millisecond suffix to avoid collisions if the daemon flaps.
        if path.exists() and path.stat().st_size > 0:
            try:
                with open(path, encoding="utf-8") as f:
                    existing_header = f.readline().rstrip("\n").split(",")
            except OSError:
                existing_header = []
            if existing_header and existing_header != HISTORY_COLUMNS:
                stamp = int(time.time() * 1000)
                rotated = path.with_name(f"{date_str}.pre-{stamp}.csv")
                try:
                    path.rename(rotated)
                    log(
                        f"history schema changed; rotated {path.name} -> {rotated.name}"
                    )
                except OSError as exc:
                    # If we can't rotate, refusing to write keeps the file
                    # readable. Falling through would append new-shape rows
                    # under the old-shape header — exactly the schema
                    # mismatch this guard exists to prevent.
                    log(
                        f"history schema mismatch but rotation failed ({exc}); "
                        f"skipping history writes for {date_str}"
                    )
                    self._history_date = date_str
                    return
        write_header = not path.exists() or path.stat().st_size == 0
        self._history_file = open(path, "a", newline="", encoding="utf-8")
        self._history_writer = csv.writer(self._history_file)
        if write_header:
            self._history_writer.writerow(HISTORY_COLUMNS)
            self._history_file.flush()
        self._history_date = date_str

    def _close_history(self) -> None:
        if self._history_file:
            try:
                self._history_file.close()
            except OSError:
                pass
            self._history_file = None
            self._history_writer = None

    def write_history(self, state: dict) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._history_date:
            self._open_history(today)
        if self._history_writer:
            self._history_writer.writerow(history_row(state))
            self._history_file.flush()

    def reset_retry_window(self) -> None:
        self.retry_samples.clear()

    @staticmethod
    def counter_delta(current: int, previous: int) -> int:
        if current >= previous:
            return current - previous
        return U32_COUNTER_MODULUS - previous + current

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
        # "Healthy" = at least one direction is using 2+ spatial streams,
        # i.e. the chip's antenna chains are demonstrably working. Asymmetric
        # NSS (tx=1, rx=2) is normal on MLO/EHT client links; flagging it as
        # degraded was producing constant noise on the user's healthy link.
        if not state.get("connected"):
            return False
        tx_nss = int(state.get("tx_nss", 0) or 0)
        rx_nss = int(state.get("rx_nss", 0) or 0)
        return max(tx_nss, rx_nss) >= 2

    def state_signature(self, state: dict) -> tuple:
        connected = bool(state.get("connected"))
        tx_nss = int(state.get("tx_nss", 0) or 0)
        rx_nss = int(state.get("rx_nss", 0) or 0)
        retry_pct = float(state.get("retry_10s_pct", 0.0) or 0.0)
        signal_dbm = int(state.get("signal_dbm", 0) or 0)
        effective_nss = max(tx_nss, rx_nss)
        return (
            connected,
            state.get("bssid", ""),
            effective_nss,
            retry_pct > ALERT_RETRY_PCT,
            signal_dbm < ALERT_SIGNAL_DBM,
        )

    def ui_expanded(self) -> bool:
        """True when the plasmoid touched UI_ACTIVE_PATH recently.

        The plasmoid's expanded-state polling shells out to update the
        file's mtime on every refresh; this is the daemon's signal to drop
        into fast-poll for a live view. No DBus, no IPC — just a file.
        """
        try:
            mtime = UI_ACTIVE_PATH.stat().st_mtime
        except OSError:
            return False
        return (time.time() - mtime) <= UI_ACTIVE_TTL_S

    def poll_interval_for_state(self, state: dict, now: float) -> float:
        signature = self.state_signature(state)
        if self.last_state_signature is None:
            self.last_state_signature = signature
            self.last_transition_time = now
        elif signature != self.last_state_signature:
            self.last_state_signature = signature
            self.last_transition_time = now

        degraded = (
            bool(state.get("connected"))
            and (
                not self.mimo_healthy(state)
                or float(state.get("retry_10s_pct", 0.0) or 0.0) > ALERT_RETRY_PCT
                or int(state.get("signal_dbm", 0) or 0) < ALERT_SIGNAL_DBM
            )
        )
        if (
            degraded
            or now - self.last_transition_time < TRANSITION_COOLDOWN_S
            or self.ui_expanded()
        ):
            return POLL_FAST_S
        return POLL_SLOW_S

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

        if not self.retry_samples:
            return

        base = self.retry_samples[0]
        packet_delta = self.counter_delta(sample["tx_packets"], base["tx_packets"])
        retry_delta = self.counter_delta(sample["tx_retries"], base["tx_retries"])
        failed_delta = self.counter_delta(sample["tx_failed"], base["tx_failed"])

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
        # Empty antenna list means the driver doesn't expose chain signal at
        # all (mt7925 in MLO mode aggregates everything to MLD level). That's
        # a telemetry gap, not a degraded MIMO state — only alert when the
        # list is present but short (= an antenna actually dropped offline).
        if 0 < antenna_count < 2:
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
        # Require BOTH directions to have reported NSS before flagging
        # "MIMO Degraded" — otherwise a partial association where only one
        # direction has rate-info yet (tx=1, rx=0) trips a transient
        # false-positive alert. With both populated and max<2, every
        # active stream is single-stream → genuine 1x1 collapse.
        if tx_nss > 0 and rx_nss > 0 and max(tx_nss, rx_nss) < 2:
            issues.append((
                "critical",
                "MIMO Degraded",
                f"Both directions running NSS 1  (TX {tx_nss}, RX {rx_nss}; expected 2x2)",
            ))

        retry_pct = float(state.get("retry_10s_pct", 0.0) or 0.0)
        if retry_pct > ALERT_RETRY_PCT:
            issues.append(("normal", "High Interference", f"10s TX retry rate: {retry_pct:.1f}%  (threshold {ALERT_RETRY_PCT}%)"))
        return issues

def main() -> int:
    iface = os.environ.get("WIFI_IFACE", IFACE)
    daemon = WifimimoDaemon(iface, STATE_PATH, HISTORY_DIR)
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
