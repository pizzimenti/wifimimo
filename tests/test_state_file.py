"""JSON state file write/read + legacy v1 migration shim."""

from __future__ import annotations

import json
from pathlib import Path

import wifimimo_core


def test_write_state_is_valid_json(tmp_path: Path):
    state = wifimimo_core.default_state()
    state["connected"] = True
    state["display"] = wifimimo_core.derive_display(state)
    path = tmp_path / "state"
    wifimimo_core.write_state(path, state)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == wifimimo_core.SCHEMA_VERSION
    assert loaded["connected"] is True
    assert "display" in loaded


def test_round_trip_via_read_state(tmp_path: Path):
    state = wifimimo_core.default_state()
    state["connected"] = True
    state["tx_mode"] = "EHT"
    state["tx_mcs"] = 9
    state["display"] = wifimimo_core.derive_display(state)
    path = tmp_path / "state"
    wifimimo_core.write_state(path, state)
    loaded = wifimimo_core.read_state(path)
    assert loaded["tx_mode"] == "EHT"
    assert loaded["tx_mcs"] == 9
    assert loaded["display"]["band_label"] == "?"  # disconnected default, but still typed


def test_missing_state_file_yields_defaults(tmp_path: Path):
    loaded = wifimimo_core.read_state(tmp_path / "absent")
    assert loaded["connected"] is False
    assert loaded["schema_version"] == wifimimo_core.SCHEMA_VERSION


def test_v1_migration_shim(tmp_path: Path):
    v1 = "\n".join([
        "timestamp=1234",
        "connected=true",
        "iface=wlp1s0",
        "ssid=legacy",
        "ssid_display=legacy",
        "bssid=02:00:00:00:00:01",
        "freq_mhz=5180",
        "bandwidth_mhz=80",
        "signal_dbm=-55",
        "tx_rate_mbps=866.7",
        "tx_mode=HE",
        "tx_mcs=11",
        "tx_nss=2",
        "antenna_1=-54",
        "antenna_2=-56",
    ]) + "\n"
    path = tmp_path / "state"
    path.write_text(v1, encoding="utf-8")
    loaded = wifimimo_core.read_state(path)
    assert loaded["connected"] is True
    assert loaded["tx_mode"] == "HE"
    assert loaded["tx_mcs"] == 11
    assert loaded["signal_antennas"] == [-54, -56]


def test_v1_migration_antennas_sort_by_index(tmp_path: Path):
    # Pathological v1 file with antenna_2 listed before antenna_1.
    # Old parser used push() and would scramble chain order; new parser
    # sorts by numeric index so chain 1 comes out first regardless of
    # file order.
    v1 = "\n".join([
        "connected=true",
        "antenna_2=-72",
        "antenna_1=-50",
    ]) + "\n"
    path = tmp_path / "state"
    path.write_text(v1, encoding="utf-8")
    loaded = wifimimo_core.read_state(path)
    assert loaded["signal_antennas"] == [-50, -72]


def test_unknown_keys_in_v2_are_ignored(tmp_path: Path):
    payload = {
        "schema_version": 99,  # forward-compat
        "connected": True,
        "future_field": "ignore me",
        "tx_mode": "EHT",
        "tx_mcs": 9,
    }
    path = tmp_path / "state"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = wifimimo_core.read_state(path)
    assert loaded["tx_mode"] == "EHT"
    assert "future_field" not in loaded
