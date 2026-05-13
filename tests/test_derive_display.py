"""Derived display fields keep UIs as dumb consumers."""

from __future__ import annotations

import wifimimo_core


def _make_state(**overrides):
    state = wifimimo_core.default_state()
    state["connected"] = True
    state.update(overrides)
    return state


def test_disconnected_returns_default_display_with_band():
    state = wifimimo_core.default_state()
    display = wifimimo_core.derive_display(state)
    assert display["band_label"] == "?"
    assert display["tx_rates_mbps"] == []
    assert display["signal_tier"] == "crit"


def test_eht_state_populates_14_rate_cells():
    state = _make_state(
        freq_mhz=6295, bandwidth_mhz=160,
        signal_dbm=-59, signal_avg_dbm=-58,
        signal_antennas=[-58, -60],
        tx_rate_mbps=648.5, tx_mcs=6, tx_nss=1, tx_mode="EHT", tx_gi=0,
        rx_rate_mbps=1297.1, rx_mcs=6, rx_nss=2, rx_mode="EHT", rx_gi=0,
    )
    display = wifimimo_core.derive_display(state)
    assert display["band_label"] == "6 GHz"
    assert display["mcs_grid_count"] == 14
    assert len(display["tx_rates_mbps"]) == 14
    assert len(display["rx_rates_mbps"]) == 14
    assert display["tx_nss_dots"] == "●○"
    assert display["rx_nss_dots"] == "●●"
    assert display["tx_gi_label"] == "0.8us"


def test_he_state_yields_12_cell_grid():
    state = _make_state(
        freq_mhz=5180, bandwidth_mhz=80,
        signal_dbm=-50,
        tx_mcs=11, tx_mode="HE", tx_rate_mbps=600.0, tx_nss=1, tx_gi=1,
        rx_mcs=11, rx_mode="HE", rx_rate_mbps=600.0, rx_nss=2, rx_gi=1,
    )
    display = wifimimo_core.derive_display(state)
    assert display["band_label"] == "5 GHz"
    assert display["mcs_grid_count"] == 12
    assert len(display["tx_rates_mbps"]) == 12
    assert display["tx_gi_label"] == "1.6us"


def test_signal_tier_thresholds_match_alert():
    assert wifimimo_core.derive_display(_make_state(signal_dbm=-50))["signal_tier"] == "good"
    assert wifimimo_core.derive_display(_make_state(signal_dbm=-70))["signal_tier"] == "warn"
    assert wifimimo_core.derive_display(_make_state(signal_dbm=-80))["signal_tier"] == "crit"


def test_signal_tier_treats_zero_or_positive_as_crit():
    # 0 dBm is the dataclass default (transient association state); positive
    # values can show up via mt7925-style chain misreading. Both should fall
    # to crit so the UI doesn't paint a healthy tier on placeholder data.
    assert wifimimo_core.derive_display(_make_state(signal_dbm=0))["signal_tier"] == "crit"
    assert wifimimo_core.derive_display(_make_state(signal_dbm=5))["signal_tier"] == "crit"


def test_band_label_lower_6ghz_boundary():
    # UNII-5 ch1 = 5955 MHz is the 6 GHz floor; the previous >=6000 cutoff
    # mis-labelled 5955-5995 MHz as 5 GHz.
    assert wifimimo_core.derive_display(_make_state(freq_mhz=5955))["band_label"] == "6 GHz"
    assert wifimimo_core.derive_display(_make_state(freq_mhz=5950))["band_label"] == "5 GHz"


def test_mcs_negative_yields_empty_rates():
    state = _make_state(
        freq_mhz=5180, signal_dbm=-50,
        tx_mcs=-1, rx_mcs=-1, tx_mode="HE", rx_mode="HE",
    )
    display = wifimimo_core.derive_display(state)
    assert display["tx_rates_mbps"] == []
    assert display["rx_rates_mbps"] == []
    # grid count still falls back to the mode's table so the UI shows an empty 12-cell row
    assert display["mcs_grid_count"] == 12


def test_signal_fraction_within_bounds():
    display = wifimimo_core.derive_display(_make_state(signal_dbm=-50, signal_avg_dbm=-90))
    assert 0.0 <= display["signal_fraction"] <= 1.0
    assert display["signal_avg_fraction"] == 0.0


def test_signal_fraction_clamps_invalid_non_negative_dbm():
    # The (-50 - -90) / 70 math would map +5 dBm to a near-full bar
    # (~1.36, clamped to 1.0). _signal_fraction must reject dbm >= 0
    # so the bar matches the "crit" tier classification.
    assert wifimimo_core._signal_fraction(0) == 0.0
    assert wifimimo_core._signal_fraction(5) == 0.0
    assert wifimimo_core._signal_fraction(99) == 0.0
    # Real negative readings still scale.
    assert wifimimo_core._signal_fraction(-50) > 0.5


def test_antenna_fractions_match_count():
    display = wifimimo_core.derive_display(
        _make_state(signal_dbm=-50, signal_antennas=[-58, -60, -62])
    )
    assert len(display["antenna_fractions"]) == 3
