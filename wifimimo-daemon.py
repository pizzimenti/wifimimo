#!/usr/bin/env python3
"""Long-running wifimimo data daemon."""

from __future__ import annotations

import csv
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import wifimimo_radio as radio
import wifimimo_shared as shared
from wifimimo_core import (
    ALERT_RETRY_PCT,
    ALERT_SIGNAL_DBM,
    HISTORY_COLUMNS,
    HISTORY_DIR,
    STATE_PATH,
    UI_ACTIVE_PATH,
    build_multi_state,
    collect,
    collect_power,
    derive_display,
    discover_wifi_ifaces,
    history_row,
    write_state,
)


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
    def __init__(self, pinned_iface: str, state_path: Path, history_dir: Path) -> None:
        # Empty pinned_iface means auto-discover every poll, so a USB card
        # hotplugged mid-run shows up without a daemon restart.
        self.pinned_iface = pinned_iface
        self.state_path = state_path
        self.history_dir = history_dir
        self.running = True
        self.retry_samples: dict[str, deque[dict]] = {}
        self.last_transition_time: dict[str, float] = {}
        self.last_state_signature: dict[str, tuple] = {}
        self.known_ifaces: list[str] = []
        self._history_file = None
        self._history_writer = None
        self._history_date: str = ""
        # Per-radio memory keyed by permanent MAC, so a rename or replug
        # keeps a radio's graph line and colour.
        self.throughput = radio.ThroughputTracker()
        self.signal_rings = radio.SignalRing()
        self.radio_colors = radio.RadioColors()
        self.name_overrides = radio.NameOverrides()
        # Optional NetworkManager follower (multipath); injected by main().
        self.follower = None

    def current_ifaces(self) -> list[str]:
        ifaces = [self.pinned_iface] if self.pinned_iface else discover_wifi_ifaces()
        if ifaces != self.known_ifaces:
            log(f"wifi interfaces: {', '.join(ifaces) if ifaces else '(none)'}")
            for stale in set(self.known_ifaces) - set(ifaces):
                self.retry_samples.pop(stale, None)
                self.last_transition_time.pop(stale, None)
                self.last_state_signature.pop(stale, None)
            self.known_ifaces = ifaces
        return ifaces

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        log(
            "wifimimo-daemon starting "
            + (f"pinned to {self.pinned_iface}" if self.pinned_iface
               else "(auto-discovering wifi interfaces)")
        )
        while self.running:
            loop_start = time.monotonic()
            doc, states = self.poll_once(loop_start)
            poll_interval = self.poll_interval_for_states(states, loop_start)
            write_state(self.state_path, doc)
            for state in states.values():
                self.write_history(state)
            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.05, poll_interval - elapsed))

    def enrich(self, iface: str, state: dict, ipv4: dict, managed_ids: set,
               wall: float, mono: float) -> None:
        # A handful of sysfs reads; re-read every poll so a stick that
        # re-enumerates at a different USB speed is noticed immediately.
        info = radio.collect_device_info(iface)
        state.update(info)
        state.update(ipv4.get(iface, {}))
        key = info.get("perm_mac") or iface
        state["card_name"] = radio.card_name(dict(info, iface=iface), self.name_overrides.get())
        state["internal"] = info.get("bus") == "pci" and info.get("dev_id") in managed_ids
        state["rx_mbps"], state["tx_mbps"] = self.throughput.sample(key, iface, mono)
        state["color_index"] = self.radio_colors.index(key)
        if state.get("connected"):
            self.signal_rings.append(key, wall, int(state.get("signal_dbm", 0) or 0))
        state["signal_history"] = self.signal_rings.snapshot(key, wall)

    def poll_once(self, loop_start: float) -> tuple[dict, dict[str, dict]]:
        wall = time.time()
        ipv4 = radio.collect_ipv4()
        internal = radio.read_internal_status()
        managed_ids = {e["id"] for e in shared.read_internal_conf()}
        states: dict[str, dict] = {}
        for iface in self.current_ifaces():
            state = collect(iface)
            state["timestamp"] = int(wall)
            state.update(collect_power(iface))
            self.update_retry_window(iface, state, loop_start)
            self.enrich(iface, state, ipv4, managed_ids, wall, loop_start)
            states[iface] = state

        cross = radio.cross_iface_flags(states)
        for iface, state in states.items():
            state["flags"] = (
                radio.link_health_flags(state)
                + radio.device_flags(state)
                + cross.get(iface, [])
            )
            state["issue_count"] = len(state["flags"])
            state["display"] = derive_display(state)

        nm_status: dict = {}
        if self.follower is not None:
            try:
                nm_status = self.follower.step(states, wall)
            except Exception as exc:  # never let NM trouble stop telemetry
                nm_status = {"error": str(exc)}
            for iface, extra in (nm_status.pop("iface_flags", {}) or {}).items():
                if iface in states:
                    states[iface]["flags"].extend(extra)
                    states[iface]["issue_count"] = len(states[iface]["flags"])

        doc = build_multi_state(states)
        # With zero cards the primary defaults carry timestamp 0; stamp
        # the document anyway so consumers can tell the daemon is alive.
        doc["timestamp"] = int(wall)
        doc["sampled_at"] = round(wall, 1)
        multipath_desired = shared.MULTIPATH_FLAG.exists()
        doc["multipath"] = radio.read_multipath_status(
            live=None if multipath_desired else {"active": False, "members": []}
        )
        doc["internal_card"] = internal
        doc["helper_available"] = radio.helper_available()
        doc["nm"] = nm_status
        return doc, states

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

    def reset_retry_window(self, iface: str) -> None:
        self.retry_samples.pop(iface, None)

    @staticmethod
    def counter_delta(current: int, previous: int) -> int:
        if current >= previous:
            return current - previous
        return U32_COUNTER_MODULUS - previous + current

    def session_changed(self, iface: str, state: dict) -> bool:
        samples = self.retry_samples.get(iface)
        if not samples:
            return False
        last = samples[-1]
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

    def poll_interval_for_states(self, states: dict[str, dict], now: float) -> float:
        fast = self.ui_expanded()
        for iface, state in states.items():
            signature = self.state_signature(state)
            if self.last_state_signature.get(iface) != signature:
                self.last_state_signature[iface] = signature
                self.last_transition_time[iface] = now

            degraded = (
                bool(state.get("connected"))
                and (
                    not self.mimo_healthy(state)
                    or float(state.get("retry_10s_pct", 0.0) or 0.0) > ALERT_RETRY_PCT
                    or int(state.get("signal_dbm", 0) or 0) < ALERT_SIGNAL_DBM
                )
            )
            if degraded or now - self.last_transition_time.get(iface, now) < TRANSITION_COOLDOWN_S:
                fast = True
        return POLL_FAST_S if fast else POLL_SLOW_S

    def update_retry_window(self, iface: str, state: dict, now: float) -> None:
        state["retry_10s_pct"] = 0.0
        state["retry_10s_packets"] = 0
        state["retry_10s_retries"] = 0
        state["retry_10s_failed"] = 0

        if not state.get("connected"):
            self.reset_retry_window(iface)
            return

        if self.session_changed(iface, state):
            self.reset_retry_window(iface)

        sample = {
            "connected": True,
            "bssid": state.get("bssid", ""),
            "connected_time_s": int(state.get("connected_time_s", 0) or 0),
            "tx_packets": int(state.get("tx_packets", 0) or 0),
            "tx_retries": int(state.get("tx_retries", 0) or 0),
            "tx_failed": int(state.get("tx_failed", 0) or 0),
            "monotonic": now,
        }
        samples = self.retry_samples.setdefault(iface, deque())
        samples.append(sample)

        while samples and now - samples[0]["monotonic"] > RETRY_WINDOW_S:
            samples.popleft()

        if not samples:
            return

        base = samples[0]
        packet_delta = self.counter_delta(sample["tx_packets"], base["tx_packets"])
        retry_delta = self.counter_delta(sample["tx_retries"], base["tx_retries"])
        failed_delta = self.counter_delta(sample["tx_failed"], base["tx_failed"])

        state["retry_10s_packets"] = packet_delta
        state["retry_10s_retries"] = retry_delta
        state["retry_10s_failed"] = failed_delta
        if packet_delta > 0:
            state["retry_10s_pct"] = retry_delta * 100.0 / packet_delta


def main() -> int:
    # WIFI_IFACE pins the daemon to one card; unset/empty auto-discovers
    # all wifi netdevs each poll (hotplug-friendly).
    iface = os.environ.get("WIFI_IFACE", "").strip()
    daemon = WifimimoDaemon(iface, STATE_PATH, HISTORY_DIR)
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
