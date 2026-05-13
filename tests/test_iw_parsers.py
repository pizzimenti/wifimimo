"""End-to-end parse of iw output fixtures into the WifiState dict shape."""

from __future__ import annotations

from pathlib import Path

import pytest

import wifimimo_core


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_link_metrics_eht_single():
    text = _load("iw_link_eht_single.txt")
    data = wifimimo_core.default_state()
    wifimimo_core.parse_link_metrics(data, text)
    assert data["connected"] is True
    assert data["ssid"] == "example-6ghz"
    assert data["bssid"] == "02:00:00:00:55:74"
    assert data["freq_mhz"] == 6295
    assert data["chan_num"] == 69
    assert data["bandwidth_mhz"] == 160
    assert data["signal_dbm"] == -59
    assert data["tx_rate_mbps"] == 648.5
    assert data["tx_mode"] == "EHT"
    assert data["tx_mcs"] == 6
    assert data["tx_nss"] == 1
    assert data["tx_gi"] == 0
    assert data["rx_mode"] == "EHT"
    assert data["rx_mcs"] == 6
    assert data["rx_nss"] == 2


def test_parse_link_metrics_he_single():
    text = _load("iw_link_he_single.txt")
    data = wifimimo_core.default_state()
    wifimimo_core.parse_link_metrics(data, text)
    assert data["connected"] is True
    assert data["tx_mode"] == "HE"
    assert data["tx_mcs"] == 11
    assert data["tx_nss"] == 1
    assert data["rx_nss"] == 2
    assert data["bandwidth_mhz"] == 160


def test_parse_link_metrics_vht():
    text = _load("iw_link_vht.txt")
    data = wifimimo_core.default_state()
    wifimimo_core.parse_link_metrics(data, text)
    assert data["tx_mode"] == "VHT"
    assert data["tx_mcs"] == 9
    assert data["tx_nss"] == 2
    assert data["bandwidth_mhz"] == 80
    assert data["tx_gi"] == -1  # VHT has no GI field


def test_parse_link_metrics_disconnected():
    text = _load("iw_link_disconnected.txt")
    data = wifimimo_core.default_state()
    wifimimo_core.parse_link_metrics(data, text)
    assert data["connected"] is False


def test_parse_link_blocks_eht_mlo():
    text = _load("iw_link_eht_mlo.txt")
    links = wifimimo_core.parse_link_blocks(text)
    assert len(links) == 2
    assert {link["link_id"] for link in links} == {0, 2}
    by_id = {link["link_id"]: link for link in links}
    assert by_id[2]["freq_mhz"] == 6295
    assert by_id[2]["chan_num"] == 69
    assert by_id[0]["freq_mhz"] == 5180


def test_mlo_primary_link_with_width_fallback(monkeypatch):
    text = _load("iw_link_eht_mlo.txt")
    monkeypatch.setattr(wifimimo_core, "_run", lambda cmd: text)
    freq, width, links = wifimimo_core._parse_mlo_primary_link("wlp1s0")
    # First Link block in the fixture is Link 2 (6 GHz), and the MLD-stats
    # bitrate line carries the 320MHz width.
    assert freq == 6295
    assert width == 320
    assert len(links) == 2


def test_mlo_primary_link_empty_on_non_mlo(monkeypatch):
    monkeypatch.setattr(wifimimo_core, "_run", lambda cmd: _load("iw_link_he_single.txt"))
    freq, width, links = wifimimo_core._parse_mlo_primary_link("wlp1s0")
    assert (freq, width, links) == (0, 0, [])


def test_320mhz_width_in_nl80211_table():
    assert wifimimo_core.NL80211_WIDTH_TO_MHZ[8] == 320


def test_fallback_via_iw_link_populates_connected_state(monkeypatch):
    text = _load("iw_link_eht_mlo.txt")
    monkeypatch.setattr(wifimimo_core, "_run", lambda cmd: text)
    data = wifimimo_core.default_state("wlp1s0")
    wifimimo_core._fallback_via_iw_link(data, "wlp1s0")
    # When netlink station info is missing, iw link must still surface
    # connected=True and the basic identity fields, not leave defaults.
    assert data["connected"] is True
    assert data["ssid"] == "example-6ghz"
    # MLD virtual MAC — doesn't match any link's BSSID, so primary
    # falls back to highest-freq (Link 2, 6 GHz).
    assert data["bssid"] == "02:00:00:01:55:74"
    assert data["freq_mhz"] == 6295
    assert data["chan_num"] == 69
    assert data["bandwidth_mhz"] == 320  # from MLD-stats bitrate width
    assert len(data["links"]) == 2


def test_fallback_via_iw_link_noop_on_disconnected(monkeypatch):
    monkeypatch.setattr(wifimimo_core, "_run", lambda cmd: _load("iw_link_disconnected.txt"))
    data = wifimimo_core.default_state("wlp1s0")
    wifimimo_core._fallback_via_iw_link(data, "wlp1s0")
    assert data["connected"] is False
    assert data["links"] == []


def test_augment_with_iw_link_skips_when_freq_and_width_populated(monkeypatch):
    # Non-MLO: netlink filled freq+width, no need to spawn iw every poll.
    calls = []
    monkeypatch.setattr(wifimimo_core, "_run", lambda cmd: (calls.append(cmd), "")[1])
    data = wifimimo_core.default_state("wlp1s0")
    data["freq_mhz"] = 5180
    data["bandwidth_mhz"] = 80
    wifimimo_core._augment_with_iw_link(data, "wlp1s0")
    assert calls == [], "iw should not run when freq/width already populated"


def test_augment_with_iw_link_runs_when_freq_missing(monkeypatch):
    monkeypatch.setattr(wifimimo_core, "_run", lambda cmd: _load("iw_link_eht_mlo.txt"))
    data = wifimimo_core.default_state("wlp1s0")
    # freq_mhz left at default 0 (MLO MLD signature)
    wifimimo_core._augment_with_iw_link(data, "wlp1s0")
    assert data["freq_mhz"] == 6295
    assert data["bandwidth_mhz"] == 320
    assert len(data["links"]) == 2


def test_collect_pure_iw_path_promotes_primary_link_freq(monkeypatch):
    # When pyroute2 is unavailable or netlink collection fails, collect()
    # falls through to the iw-only path. MLO outputs without a top-level
    # freq must still surface primary freq/chan from the Link block —
    # otherwise derived band/Wi-Fi-N labels render against freq=0.
    monkeypatch.setattr(wifimimo_core, "_collect_via_netlink", lambda iface: None)
    mlo_text = _load("iw_link_eht_mlo.txt")
    monkeypatch.setattr(wifimimo_core, "_run", lambda cmd: mlo_text if "link" in cmd else "")
    data = wifimimo_core.collect("wlp1s0")
    assert data["connected"] is True
    assert data["freq_mhz"] == 6295
    assert data["chan_num"] == 69
    assert data["bandwidth_mhz"] == 320
    assert len(data["links"]) == 2


def test_primary_link_prefers_bssid_match(monkeypatch):
    # When the connection BSSID matches one Link's BSSID (kernel's anchor
    # link), pick *that* link as primary even if iw lists a higher-freq
    # link first. This keeps state.freq_mhz and state.bssid consistent.
    monkeypatch.setattr(wifimimo_core, "_collect_via_netlink", lambda iface: None)
    text = _load("iw_link_eht_mlo_anchor.txt")
    monkeypatch.setattr(wifimimo_core, "_run", lambda cmd: text if "link" in cmd else "")
    data = wifimimo_core.collect("wlp1s0")
    # "Connected to" matches Link 0's BSSID — that link is 5180 MHz / ch36
    # even though Link 2 (6295 MHz) appears first in the iw output.
    assert data["bssid"] == "02:00:00:03:55:73"
    assert data["freq_mhz"] == 5180
    assert data["chan_num"] == 36


def test_parse_link_metrics_ssid_containing_link_substring_is_not_mlo():
    # An SSID with the literal text "Link 2 BSSID" inside it must not flip
    # parse_link_metrics into MLO-mode. Without the line-anchored guard,
    # the freq extraction would be skipped and freq_mhz/chan_num would
    # stay zero on a perfectly normal 5 GHz single-link connection.
    text = (
        "Connected to 02:00:00:06:11:22 (on wlp1s0)\n"
        "\tSSID: My Link 2 BSSID lounge\n"
        "\tfreq: 5180\n"
        "\tsignal: -55 dBm\n"
        "\ttx bitrate: 866.7 MBit/s 80MHz VHT-MCS 9 VHT-NSS 2\n"
    )
    data = wifimimo_core.default_state("wlp1s0")
    wifimimo_core.parse_link_metrics(data, text)
    assert data["ssid"] == "My Link 2 BSSID lounge"
    assert data["freq_mhz"] == 5180
    assert data["chan_num"] == 36


def test_select_primary_link_directly():
    # Direct unit test of the selection helper covers both branches in
    # isolation from the rest of the iw parsing pipeline.
    links = [
        {"link_id": 2, "bssid": "aa:aa:aa:aa:aa:aa", "freq_mhz": 6295},
        {"link_id": 0, "bssid": "bb:bb:bb:bb:bb:bb", "freq_mhz": 5180},
    ]
    # BSSID match wins over freq.
    assert wifimimo_core._select_primary_link(links, "bb:bb:bb:bb:bb:bb")["link_id"] == 0
    # Case-insensitive match.
    assert wifimimo_core._select_primary_link(links, "AA:AA:AA:AA:AA:AA")["link_id"] == 2
    # No bssid → highest-freq.
    assert wifimimo_core._select_primary_link(links, "")["link_id"] == 2
    # Non-matching bssid → highest-freq fallback.
    assert wifimimo_core._select_primary_link(links, "cc:cc:cc:cc:cc:cc")["link_id"] == 2
