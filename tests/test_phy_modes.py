"""Every registered PHY mode must round-trip through _parse_rate_info."""

from __future__ import annotations

import logging

import pytest

import wifimimo_core
from phy_modes import PHY_MODES, compute_rates, mcs_max_for, phy_mode_by_name, wifi_label


def _attr_msg(attrs: dict) -> dict:
    return {"attrs": list(attrs.items())}


@pytest.mark.parametrize("mode", PHY_MODES, ids=lambda m: m.name)
def test_parse_rate_info_round_trip(mode):
    """Each PhyMode's MCS attr should produce the expected mode/mcs/nss fields."""
    attrs = {
        "NL80211_RATE_INFO_BITRATE32": 8648,
    }
    if mode.name == "HT":
        # HT encodes mcs+nss into the same int: nss=2, mcs=7 -> raw 15
        attrs[mode.nl_mcs_attr] = 15
    else:
        attrs[mode.nl_mcs_attr] = 7
        if mode.nl_nss_attr:
            attrs[mode.nl_nss_attr] = 2
        if mode.has_gi:
            attrs[mode.nl_gi_attr] = 0

    data = wifimimo_core.default_state()
    wifimimo_core._parse_rate_info(data, "tx", _attr_msg(attrs))

    assert data["tx_mode"] == mode.name
    assert data["tx_rate_mbps"] == 864.8
    if mode.name == "HT":
        assert data["tx_mcs"] == 7
        assert data["tx_nss"] == 2
    else:
        assert data["tx_mcs"] == 7
        assert data["tx_nss"] == 2
        if mode.has_gi:
            assert data["tx_gi"] == 0


def test_parse_rate_info_unknown_attrs_warns_and_defaults(caplog):
    """An MCS attr from a PHY mode we don't recognise should warn and leave defaults."""
    attrs = {
        "NL80211_RATE_INFO_BITRATE32": 5000,
        "NL80211_RATE_INFO_FUTURE_MCS": 5,
    }
    data = wifimimo_core.default_state()
    with caplog.at_level(logging.WARNING, logger="wifimimo"):
        wifimimo_core._parse_rate_info(data, "rx", _attr_msg(attrs))

    assert data["rx_mode"] == ""
    assert data["rx_mcs"] == -1
    assert data["rx_nss"] == 0
    assert any("unknown rate-info MCS attrs" in record.message for record in caplog.records)


def test_phy_mode_order_eht_before_he():
    """EHT must come before HE so an EHT-only rate-info doesn't fall through."""
    names = [m.name for m in PHY_MODES]
    assert names.index("EHT") < names.index("HE")


def test_compute_rates_eht_length_14():
    rates = compute_rates(1373.0, 9, "EHT")
    assert len(rates) == 14
    assert rates[9] == 1373


def test_compute_rates_unknown_mode_falls_back_to_he():
    rates = compute_rates(100.0, 5, "")
    assert len(rates) == 12  # HE table length


def test_compute_rates_returns_zeros_when_mcs_out_of_range():
    rates = compute_rates(100.0, 99, "HE")
    assert rates == [0.0] * 12


def test_mcs_max_for_each_mode():
    assert mcs_max_for("EHT") == 13
    assert mcs_max_for("HE") == 11
    assert mcs_max_for("VHT") == 9
    assert mcs_max_for("HT") == 7
    assert mcs_max_for("") == 11  # HE fallback


def test_phy_mode_lookup():
    assert phy_mode_by_name("EHT").iw_token == "EHT"
    assert phy_mode_by_name("nope") is None


def test_wifi_label_covers_every_generation():
    # Wi-Fi 7 is EHT on any band.
    assert wifi_label("EHT", 6295) == "Wi-Fi 7 / EHT"
    assert wifi_label("EHT", 5180) == "Wi-Fi 7 / EHT"
    # Wi-Fi 6E is HE on 6 GHz specifically; HE on 5/2.4 GHz is plain Wi-Fi 6.
    assert wifi_label("HE", 6295) == "Wi-Fi 6E / HE"
    assert wifi_label("HE", 5180) == "Wi-Fi 6 / HE"
    assert wifi_label("HE", 2412) == "Wi-Fi 6 / HE"
    assert wifi_label("VHT", 5180) == "Wi-Fi 5 / VHT"
    assert wifi_label("HT", 2412) == "Wi-Fi 4 / HT"


def test_wifi_label_empty_for_unknown_mode():
    assert wifi_label("", 6295) == ""
    assert wifi_label("FUTURE", 6295) == ""
