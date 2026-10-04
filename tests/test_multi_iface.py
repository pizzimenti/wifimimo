"""Multi-card support: discovery, primary selection, schema-v3 document."""

from __future__ import annotations

import importlib.util
import types
from collections import deque
from pathlib import Path

import wifimimo_core


def _load_daemon() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "wifimimo_daemon", Path(__file__).parent.parent / "wifimimo-daemon.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_sysfs(tmp_path: Path, wifi: list[str], wired: list[str]) -> Path:
    base = tmp_path / "net"
    base.mkdir()
    for name in wifi:
        (base / name / "phy80211").mkdir(parents=True)
    for name in wired:
        (base / name).mkdir()
    return base


def _state(iface: str, connected: bool) -> dict:
    state = wifimimo_core.default_state(iface)
    state["connected"] = connected
    return state


# ---------------------------------------------------------------------------
# discover_wifi_ifaces
# ---------------------------------------------------------------------------


def test_discovery_finds_only_wifi_netdevs(tmp_path: Path):
    base = _fake_sysfs(tmp_path, wifi=["wlp3s0f3u2", "wlp1s0"], wired=["lo", "enp2s0"])
    assert wifimimo_core.discover_wifi_ifaces(base, iftypes={}) == [
        "wlp1s0", "wlp3s0f3u2",
    ]


def test_discovery_accepts_legacy_wireless_dir(tmp_path: Path):
    base = tmp_path / "net"
    (base / "wlan0" / "wireless").mkdir(parents=True)
    (base / "eth0").mkdir()
    assert wifimimo_core.discover_wifi_ifaces(base, iftypes={}) == ["wlan0"]


def test_discovery_missing_base_yields_empty(tmp_path: Path):
    assert wifimimo_core.discover_wifi_ifaces(tmp_path / "absent", iftypes={}) == []


def test_discovery_excludes_non_station_iftypes(tmp_path: Path):
    # One radio exposing AP + monitor + managed netdevs: all three carry
    # phy80211, but only the station may be polled — an AP netdev would
    # surface a random associated client as "our" uplink.
    base = _fake_sysfs(
        tmp_path, wifi=["wlan0", "wlan0-ap", "mon0"], wired=["eth0"]
    )
    iftypes = {
        "wlan0": wifimimo_core.NL80211_IFTYPE_STATION,
        "wlan0-ap": 3,  # NL80211_IFTYPE_AP
        "mon0": 6,      # NL80211_IFTYPE_MONITOR
    }
    assert wifimimo_core.discover_wifi_ifaces(base, iftypes=iftypes) == ["wlan0"]


def test_discovery_keeps_ifaces_unknown_to_nl80211(tmp_path: Path):
    # Legacy WEXT-only drivers never appear in the nl80211 dump; absence
    # must not exclude them.
    base = _fake_sysfs(tmp_path, wifi=["wlan9"], wired=[])
    assert wifimimo_core.discover_wifi_ifaces(base, iftypes={"other0": 3}) == ["wlan9"]


# ---------------------------------------------------------------------------
# select_primary_iface / build_multi_state
# ---------------------------------------------------------------------------


def test_primary_prefers_connected_card():
    states = {
        "wlp1s0": _state("wlp1s0", connected=False),
        "wlp3s0f3u2": _state("wlp3s0f3u2", connected=True),
    }
    assert wifimimo_core.select_primary_iface(states) == "wlp3s0f3u2"


def test_primary_falls_back_alphabetical_when_nothing_connected():
    states = {
        "wlp3s0f3u2": _state("wlp3s0f3u2", connected=False),
        "wlp1s0": _state("wlp1s0", connected=False),
    }
    assert wifimimo_core.select_primary_iface(states) == "wlp1s0"


def test_build_multi_state_mirrors_primary_at_top_level():
    usb = _state("wlp3s0f3u2", connected=True)
    usb["ssid"] = "Anglers Keep 5"
    states = {"wlp1s0": _state("wlp1s0", connected=False), "wlp3s0f3u2": usb}
    doc = wifimimo_core.build_multi_state(states)
    assert doc["iface"] == "wlp3s0f3u2"
    assert doc["connected"] is True
    assert doc["ssid"] == "Anglers Keep 5"
    assert doc["ifaces"] == ["wlp1s0", "wlp3s0f3u2"]
    assert set(doc["interfaces"]) == {"wlp1s0", "wlp3s0f3u2"}
    # Sub-states don't nest the multi-card keys again.
    assert "interfaces" not in doc["interfaces"]["wlp3s0f3u2"]
    assert "ifaces" not in doc["interfaces"]["wlp3s0f3u2"]


def test_build_multi_state_empty_input_is_disconnected_defaults():
    doc = wifimimo_core.build_multi_state({})
    assert doc["connected"] is False
    assert doc["iface"] == ""
    assert doc["ifaces"] == []
    assert doc["interfaces"] == {}


# ---------------------------------------------------------------------------
# State-file round trip
# ---------------------------------------------------------------------------


def test_interfaces_survive_state_round_trip(tmp_path: Path):
    usb = _state("wlp3s0f3u2", connected=True)
    usb["tx_mode"] = "EHT"
    usb["display"] = wifimimo_core.derive_display(usb)
    doc = wifimimo_core.build_multi_state(
        {"wlp1s0": _state("wlp1s0", connected=False), "wlp3s0f3u2": usb}
    )
    path = tmp_path / "state"
    wifimimo_core.write_state(path, doc)
    loaded = wifimimo_core.read_state(path)
    assert loaded["ifaces"] == ["wlp1s0", "wlp3s0f3u2"]
    assert loaded["interfaces"]["wlp3s0f3u2"]["tx_mode"] == "EHT"
    assert loaded["interfaces"]["wlp1s0"]["connected"] is False


def test_partial_interface_substate_deep_merges(tmp_path: Path):
    # A sub-state missing most keys (older daemon / half-flushed write) must
    # come back fully typed, same guarantee the top level already has.
    path = tmp_path / "state"
    path.write_text(
        '{"schema_version": 3, "interfaces": {"wlanX": {"connected": true, "tx_mcs": 7}}}',
        encoding="utf-8",
    )
    loaded = wifimimo_core.read_state(path)
    sub = loaded["interfaces"]["wlanX"]
    assert sub["connected"] is True
    assert sub["tx_mcs"] == 7
    assert sub["display"]["mcs_grid_count"] == 12
    assert sub["signal_antennas"] == []


def test_malformed_structured_fields_keep_defaults(tmp_path: Path):
    # A truthy non-dict `interfaces` (or non-list `ifaces`, non-dict
    # `display`) must not survive the merge — consumers call .get() on
    # interfaces and would crash on a stray string.
    path = tmp_path / "state"
    path.write_text(
        '{"schema_version": 3, "connected": true,'
        ' "interfaces": "garbage", "ifaces": "also-garbage", "display": 7}',
        encoding="utf-8",
    )
    loaded = wifimimo_core.read_state(path)
    assert loaded["connected"] is True
    assert loaded["interfaces"] == {}
    assert loaded["ifaces"] == []
    assert loaded["display"]["signal_tier"] == "crit"


def test_malformed_v4_structured_fields_keep_defaults(tmp_path: Path):
    path = tmp_path / "state"
    path.write_text(
        '{"schema_version": 4, "flags": "x", "signal_history": 3,'
        ' "multipath": [1], "internal_card": "on", "nm": null}',
        encoding="utf-8",
    )
    loaded = wifimimo_core.read_state(path)
    assert loaded["flags"] == [] and loaded["signal_history"] == []
    assert loaded["multipath"] == {} and loaded["internal_card"] == {} and loaded["nm"] == {}


def test_doc_only_keys_never_leak_into_interfaces():
    usb = _state("wifi1", connected=True)
    usb.update(multipath={"active": True}, internal_card={"managed": True},
               helper_available=True, sampled_at=1.0, nm={"x": 1})
    doc = wifimimo_core.build_multi_state({"wifi1": usb})
    assert doc["multipath"] == {"active": True}
    assert not set(wifimimo_core.DOC_ONLY_KEYS) & set(doc["interfaces"]["wifi1"])


def test_v4_per_radio_fields_round_trip(tmp_path: Path):
    usb = _state("wifi1", connected=True)
    usb.update(card_name="A9000", flags=[{"code": "shared_bss"}],
               signal_history=[[1.0, -55]], usb_speed_mbps=5000)
    path = tmp_path / "state"
    wifimimo_core.write_state(path, wifimimo_core.build_multi_state({"wifi1": usb}))
    sub = wifimimo_core.read_state(path)["interfaces"]["wifi1"]
    assert sub["card_name"] == "A9000"
    assert sub["flags"] == [{"code": "shared_bss"}]
    assert sub["signal_history"] == [[1.0, -55]]
    assert sub["usb_speed_mbps"] == 5000


# ---------------------------------------------------------------------------
# Daemon per-iface tracking
# ---------------------------------------------------------------------------


def test_retry_windows_are_independent_per_iface(tmp_path: Path):
    daemon_module = _load_daemon()
    daemon = daemon_module.WifimimoDaemon("", tmp_path / "state", tmp_path / "hist")

    def sample(iface: str, packets: int, retries: int) -> dict:
        state = _state(iface, connected=True)
        state["bssid"] = "02:00:00:00:00:01"
        state["connected_time_s"] = 100
        state["tx_packets"] = packets
        state["tx_retries"] = retries
        return state

    a0 = sample("wlanA", 1000, 10)
    daemon.update_retry_window("wlanA", a0, 0.0)
    b0 = sample("wlanB", 500, 0)
    daemon.update_retry_window("wlanB", b0, 0.0)
    a1 = sample("wlanA", 1100, 60)
    daemon.update_retry_window("wlanA", a1, 1.0)
    b1 = sample("wlanB", 600, 0)
    daemon.update_retry_window("wlanB", b1, 1.0)

    assert a1["retry_10s_pct"] == 50.0  # 50 retries over 100 packets
    assert b1["retry_10s_pct"] == 0.0  # unaffected by wlanA's churn


def test_disconnect_resets_only_that_ifaces_window(tmp_path: Path):
    daemon_module = _load_daemon()
    daemon = daemon_module.WifimimoDaemon("", tmp_path / "state", tmp_path / "hist")
    daemon.retry_samples["wlanA"] = deque([{"connected": True}])
    daemon.retry_samples["wlanB"] = deque([{"connected": True}])

    state = _state("wlanA", connected=False)
    daemon.update_retry_window("wlanA", state, 5.0)

    assert "wlanA" not in daemon.retry_samples
    assert "wlanB" in daemon.retry_samples


def test_vanished_iface_tracker_state_is_pruned(tmp_path: Path, monkeypatch):
    daemon_module = _load_daemon()
    daemon = daemon_module.WifimimoDaemon("", tmp_path / "state", tmp_path / "hist")

    monkeypatch.setattr(
        daemon_module, "discover_wifi_ifaces", lambda: ["wlanA", "wlanB"]
    )
    assert daemon.current_ifaces() == ["wlanA", "wlanB"]
    daemon.retry_samples["wlanB"] = deque([{"connected": True}])
    daemon.last_state_signature["wlanB"] = (True,)
    daemon.last_transition_time["wlanB"] = 1.0

    monkeypatch.setattr(daemon_module, "discover_wifi_ifaces", lambda: ["wlanA"])
    assert daemon.current_ifaces() == ["wlanA"]
    assert "wlanB" not in daemon.retry_samples
    assert "wlanB" not in daemon.last_state_signature
    assert "wlanB" not in daemon.last_transition_time


def test_pinned_iface_skips_discovery(tmp_path: Path, monkeypatch):
    daemon_module = _load_daemon()
    daemon = daemon_module.WifimimoDaemon("wlanX", tmp_path / "state", tmp_path / "hist")
    monkeypatch.setattr(
        daemon_module, "discover_wifi_ifaces",
        lambda: (_ for _ in ()).throw(AssertionError("discovery must not run")),
    )
    assert daemon.current_ifaces() == ["wlanX"]
