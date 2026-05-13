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
    assert data["ssid"] == "carrierpidgeon-6G"
    assert data["bssid"] == "36:2f:d0:28:55:74"
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
    assert {l["link_id"] for l in links} == {0, 2}
    by_id = {l["link_id"]: l for l in links}
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
