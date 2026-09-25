"""Slot planner: channel geometry, scan tables, trends, and placement.

Scenarios follow the Anglers Keep walk (2026-09-25): three radios, one
access point near the house with 2.4 GHz ch 11 + 5 GHz ch 52/80, weaker
APs on ch 36/80 and ch 100/80. Names and addresses are anonymised.
"""

from pathlib import Path

import pytest

import wifimimo_roam as roam

FIXTURES = Path(__file__).parent / "fixtures"


# --- geometry --------------------------------------------------------------

@pytest.mark.parametrize("freq,width,span", [
    (5260, 80, (5250, 5330)),    # ch 52 in the 52-64 block
    (5180, 80, (5170, 5250)),    # ch 36
    (5500, 80, (5490, 5570)),    # ch 100
    (5745, 80, (5735, 5815)),    # ch 149 (UNII-3 grid starts at 5735)
    (5825, 80, (5815, 5895)),    # ch 165
    (5180, 160, (5170, 5330)),
    (5220, 40, (5210, 5250)),    # ch 44 pairs with 48
    (6135, 160, (6105, 6265)),   # 6 GHz ch 37
    (2462, 20, (2452, 2472)),
])
def test_channel_span_from_channelisation(freq, width, span):
    assert roam.channel_span(freq, width) == span


def test_channel_span_prefers_stated_centre_and_ht_offset():
    assert roam.channel_span(5260, 80, center=5290) == (5250, 5330)
    assert roam.channel_span(2412, 40, ht_sec="above") == (2402, 2442)
    assert roam.channel_span(2462, 40, ht_sec="below") == (2432, 2472)
    assert roam.channel_span(2437, 40) == (2407, 2467)   # unknown side: both


def test_adjacent_80_mhz_blocks_do_not_overlap_but_nested_ones_do():
    ch36, ch52 = roam.channel_span(5180, 80), roam.channel_span(5260, 80)
    assert not roam.spans_overlap(ch36, ch52)
    assert roam.spans_overlap(ch36, roam.channel_span(5220, 20))       # ch 44 inside 36/80
    assert not roam.spans_overlap(roam.channel_span(2412, 20), roam.channel_span(2462, 20))
    assert roam.spans_overlap(roam.channel_span(2412, 20), roam.channel_span(2422, 20))


# --- scan table ------------------------------------------------------------

def test_parse_scan_dump_fixture():
    rows = {r["bssid"]: r for r in roam.parse_scan_dump((FIXTURES / "iw_scan_dump_multi.txt").read_text())}
    assert len(rows) == 5
    home5 = rows["02:00:00:00:00:52"]
    assert home5["associated"] and home5["ssid"] == "example-home"
    assert (home5["freq"], home5["width"], home5["center"], home5["span"]) == (5260, 80, 5290, (5250, 5330))
    assert home5["signal"] == -29.0 and home5["age_s"] == pytest.approx(15.544)
    assert home5["stations"] == 2 and home5["util"] == 0.0 and home5["akm"] == "PSK"
    home24 = rows["02:00:00:00:00:11"]
    assert (home24["width"], home24["span"]) == (20, (2452, 2472))
    assert home24["util"] == pytest.approx(0.2)
    assert rows["02:00:00:00:01:36"]["span"] == (5170, 5250)
    hidden = rows["02:00:00:00:01:49"]
    assert hidden["ssid"] == "" and hidden["span"] == (5735, 5815)
    six = rows["02:00:00:00:06:37"]
    assert (six["width"], six["center"], six["span"]) == (160, 6185, (6105, 6265))
    assert six["akm"] == "SAE"


def test_parse_scan_dump_empty():
    assert roam.parse_scan_dump("") == []


# --- signal ----------------------------------------------------------------

def test_pct_to_dbm_inverts_networkmanager_scale():
    assert roam.pct_to_dbm(100) == -40
    assert roam.pct_to_dbm(50) == -70
    assert roam.pct_to_dbm(0) == -100


def test_est_rate_grows_with_signal_and_width():
    assert roam.est_rate_mbps(-90, 80) < roam.est_rate_mbps(-70, 80) < roam.est_rate_mbps(-50, 80)
    assert roam.est_rate_mbps(-50, 20) < roam.est_rate_mbps(-50, 80)
    assert roam.est_rate_mbps(-100, 20) == 0.0
    assert roam.est_rate_mbps(-50, 80, util=0.5) == pytest.approx(roam.est_rate_mbps(-50, 80) / 2)


def feed(trend, dev, values, bssid="b1", t0=0.0):
    for i, v in enumerate(values):
        trend.add(dev, t0 + i, v, bssid)


def test_trend_slope_and_projection():
    t = roam.Trend()
    feed(t, "wifi1", [-60, -61, -62, -63, -64, -65, -66, -67])
    assert t.slope("wifi1") == pytest.approx(-1.0)
    assert t.declining("wifi1")
    assert t.predicted("wifi1", 6) == pytest.approx(-72.0)
    feed(t, "wifi2", [-60] * 8)
    assert t.slope("wifi2") == 0 and t.predicted("wifi2") == -60


def test_trend_projection_is_capped_and_only_for_declines():
    t = roam.Trend()
    feed(t, "wifi1", [-50, -55, -60, -65, -70, -75])
    assert t.predicted("wifi1", 60) == pytest.approx(t.level("wifi1") - roam.MAX_EXTRAPOLATE_DB)
    feed(t, "wifi2", [-80, -75, -70, -65])
    assert t.predicted("wifi2") == t.level("wifi2")


def test_trend_restarts_on_a_new_bssid_and_ignores_spikes():
    t = roam.Trend()
    feed(t, "wifi1", [-60, -62, -64, -66, -68])
    t.add("wifi1", 5, -50, "b2")
    assert t.slope("wifi1") == 0 and t.level("wifi1") == -50
    feed(t, "wifi2", [-60, -60, -90])
    assert t.level("wifi2") == -60   # median of the last three


def test_mobile_when_any_radio_moves():
    t = roam.Trend()
    feed(t, "wifi1", [-60, -60, -61])
    assert not t.mobile()
    feed(t, "wifi1", [-60, -64, -68], t0=3)
    assert t.mobile()


def test_offsets_learn_from_simultaneous_readings():
    trend, offsets = roam.Trend(), roam.Offsets()
    feed(trend, "wifi1", [-30] * 5, bssid="aa:00:00:00:00:52")
    dumps = {"wifi0": [{"bssid": "aa:00:00:00:00:52", "signal": -50.0, "age_s": 1.0, "freq": 5260,
                        "associated": False}],
             "wifi2": [{"bssid": "aa:00:00:00:00:11", "signal": -30.0, "age_s": 0.5, "freq": 2462,
                        "associated": False}],
             "wifi1": [{"bssid": "aa:00:00:00:00:11", "signal": -35.0, "age_s": 1.0, "freq": 2462,
                        "associated": False}]}
    slots = {"wifi1": {"bssid": "aa:00:00:00:00:52"}}
    roam.learn_offsets(offsets, dumps, trend, slots, now=5.0)
    assert offsets.get("wifi0", "wifi1", "5") == pytest.approx(-20.0)   # built-in hears 20 dB less
    assert offsets.get("wifi1", "wifi0", "5") == pytest.approx(20.0)
    assert offsets.get("wifi2", "wifi1", "2.4") == pytest.approx(5.0)
    assert offsets.get("wifi2", "wifi1", "5") is None


def test_offsets_ignore_readings_far_apart_in_time():
    offsets = roam.Offsets()
    dumps = {"a": [{"bssid": "x", "signal": -50.0, "age_s": 1.0, "freq": 5180, "associated": False}],
             "b": [{"bssid": "x", "signal": -60.0, "age_s": 9.0, "freq": 5180, "associated": False}]}
    roam.learn_offsets(offsets, dumps, roam.Trend(), {}, now=10.0)
    assert offsets.get("a", "b", "5") is None


# --- candidates ------------------------------------------------------------

def nmrow(bssid, freq, pct=80, ssid="home", security="WPA2", bandwidth=80):
    return {"ssid": ssid, "bssid": bssid, "chan": 0, "freq": freq, "signal": pct,
            "security": security, "bandwidth": bandwidth}


def dumprow(bssid, freq, signal, age=1.0, width=80):
    return {"bssid": bssid, "freq": freq, "signal": float(signal), "age_s": age, "width": width,
            "span": roam.channel_span(freq, width), "util": None, "associated": False}


def test_candidates_use_own_fresh_reading_and_nm_list_for_joinability():
    nm_rows = [nmrow("aa:52", 5260), nmrow("aa:36", 5180, pct=30), nmrow("zz", 5180, ssid="other")]
    dumps = {"wifi1": [dumprow("aa:52", 5260, -31), dumprow("aa:99", 5500, -40)]}
    cands = roam.candidates("wifi1", "home", nm_rows, dumps, roam.Offsets())
    assert [c["bssid"] for c in cands] == ["aa:52", "aa:36"]    # aa:99 isn't in NM's list
    assert cands[0]["signal"] == -31 and cands[0]["age"] == 1.0 and cands[0]["span"] == (5250, 5330)
    assert cands[1]["signal"] == roam.pct_to_dbm(30) and cands[1]["age"] > roam.FRESH_S


def test_candidates_borrow_a_fresher_reading_with_offset():
    offsets = roam.Offsets()
    offsets.observe("wifi0", "wifi2", "5", -60.0, -45.0)   # wifi0 hears 15 dB less than wifi2
    dumps = {"wifi0": [dumprow("aa:36", 5180, -80, age=25.0)],
             "wifi2": [dumprow("aa:36", 5180, -50, age=2.0)]}
    (cand,) = roam.candidates("wifi0", "home", [nmrow("aa:36", 5180)], dumps, offsets)
    assert cand["borrowed"] and cand["age"] == 2.0
    assert cand["signal"] == pytest.approx(-50 - 15 - roam.BORROW_PENALTY_DB)


def test_candidates_never_borrow_without_a_learned_offset():
    dumps = {"wifi2": [dumprow("aa:36", 5180, -50, age=2.0)]}
    (cand,) = roam.candidates("wifi0", "home", [nmrow("aa:36", 5180, pct=40)], dumps, roam.Offsets())
    assert not cand["borrowed"] and cand["age"] > roam.FRESH_S


def test_candidates_pin_the_owe_twin_and_skip_avoided():
    rows = [nmrow("aa:bb:cc:dd:ee:01", 5180, ssid="lib", security="OWE-TM"),
            nmrow("aa:bb:cc:dd:ee:02", 5180, ssid="", security="OWE"),
            nmrow("aa:bb:cc:dd:ff:01", 5745, ssid="lib", security="OWE-TM")]   # no twin
    cands = roam.candidates("wifi1", "lib", rows, {}, roam.Offsets())
    assert [c["bssid"] for c in cands] == ["aa:bb:cc:dd:ee:02"]
    assert roam.candidates("wifi1", "lib", rows, {}, roam.Offsets(), avoid={"aa:bb:cc:dd:ee:02"}) == []


# --- planner ---------------------------------------------------------------

def cand(bssid, freq, signal, width=80, age=2.0):
    return {"bssid": bssid, "freq": freq, "width": width, "span": roam.channel_span(freq, width),
            "signal": float(signal), "age": age, "borrowed": False, "util": None}


def slot(bssid, freq, signal, width=80, level=None, dead=False):
    return {"bssid": bssid, "freq": freq, "width": width, "span": roam.channel_span(freq, width),
            "signal": float(signal), "level": float(level if level is not None else signal),
            "util": None, "dead": dead}


def on(dev, s, cands=(), locked=False):
    return {"dev": dev, "status": "on", "slot": s, "cands": list(cands), "locked": locked}


def free(dev, cands=(), locked=False):
    return {"dev": dev, "status": "free", "slot": None, "cands": list(cands), "locked": locked}


H52 = ("aa:52", 5260)     # house AP, 5 GHz ch 52/80
H11 = ("aa:11", 2462)     # house AP, 2.4 GHz ch 11
F36 = ("bb:36", 5180)     # far AP, ch 36/80
G52 = ("cc:52", 5260)     # another AP, also ch 52/80


def test_two_radios_on_one_bssid_parks_the_weaker_card():
    radios = [on("wifi1", slot(*H52, -30)), on("wifi0", slot(*H52, -52)),
              on("wifi2", slot(*H11, -28, width=20))]
    change, why = roam.plan_slots(radios)
    assert change == ("park", "wifi0"), why


def test_three_radios_on_one_bssid_unstack_one_park_at_a_time():
    # live 2026-09-25, after the old follower chased a replugged card
    radios = [on("wifi1", slot(*H52, -32)), on("wifi0", slot(*H52, -51)), on("wifi2", slot(*H52, -35))]
    assert roam.plan_slots(radios)[0] == ("park", "wifi0")
    radios[1] = free("wifi0")
    assert roam.plan_slots(radios)[0] == ("park", "wifi2")


def test_a_stacked_radio_moves_to_a_free_slot_rather_than_parking():
    radios = [on("wifi1", slot(*H52, -30)), on("wifi2", slot(*H52, -35), [cand(*H11, -30, width=20)])]
    assert roam.plan_slots(radios)[0] == ("move", "wifi2", "aa:11")


def test_scout_stays_free_when_only_weak_or_occupied_slots_exist():
    radios = [on("wifi1", slot(*H52, -30)), on("wifi2", slot(*H11, -28, width=20)),
              free("wifi0", [cand(*H52, -50), cand(*F36, -78)])]
    change, why = roam.plan_slots(radios)
    assert change is None, why


def test_scout_joins_a_free_channel_once_strong():
    radios = [on("wifi1", slot(*H52, -30)), on("wifi2", slot(*H11, -28, width=20)),
              free("wifi0", [cand(*H52, -50), cand(*F36, -66)])]
    assert roam.plan_slots(radios)[0] == ("join", "wifi0", "bb:36")


def test_overlapping_channel_on_another_ap_beats_scouting():
    radios = [on("wifi1", slot(*H52, -40)), free("wifi2", [cand(*H52, -35), cand(*G52, -60)])]
    assert roam.plan_slots(radios)[0] == ("join", "wifi2", "cc:52")


def test_free_channel_beats_overlapping_channel_even_if_weaker():
    radios = [on("wifi1", slot(*H52, -40)),
              free("wifi2", [cand(*G52, -45), cand(*H11, -60, width=20)])]
    assert roam.plan_slots(radios)[0] == ("join", "wifi2", "aa:11")


def test_same_ap_other_band_counts_as_a_free_channel():
    radios = [on("wifi1", slot(*H52, -30)), free("wifi2", [cand(*H52, -28), cand(*H11, -28, width=20)])]
    assert roam.plan_slots(radios)[0] == ("join", "wifi2", "aa:11")


def test_declining_radio_moves_early_to_a_fresh_strong_slot():
    # level -68 but heading to -78 within the horizon: graded by where it's heading
    radios = [on("wifi1", slot(*H52, -78, level=-68), [cand(*F36, -62)]),
              on("wifi2", slot(*H11, -40, width=20))]
    assert roam.plan_slots(radios)[0] == ("move", "wifi1", "bb:36")


def test_steady_radio_does_not_chase_a_marginally_better_ap():
    radios = [on("wifi1", slot(*H52, -60), [cand(*F36, -57)]), on("wifi2", slot(*H11, -40, width=20))]
    assert roam.plan_slots(radios)[0] is None


def test_stale_candidates_are_not_acted_on():
    radios = [on("wifi1", slot(*H52, -78, level=-68), [cand(*F36, -55, age=roam.FRESH_S + 1)]),
              on("wifi2", slot(*H11, -40, width=20))]
    assert roam.plan_slots(radios)[0] is None


def test_dwell_locks_a_radio_but_a_dead_one_still_parks():
    radios = [on("wifi1", slot(*H52, -78, level=-68), [cand(*F36, -55)], locked=True),
              on("wifi2", slot(*H11, -40, width=20))]
    assert roam.plan_slots(radios)[0] is None
    radios[0] = on("wifi1", slot(*H52, -88), locked=True)
    assert roam.plan_slots(radios)[0] == ("park", "wifi1")


def test_a_dead_only_radio_keeps_its_link():
    assert roam.plan_slots([on("wifi1", slot(*H52, -88))])[0] is None


def test_weak_slot_is_joined_only_when_nothing_else_is_up():
    assert roam.plan_slots([free("wifi1", [cand(*F36, -78)])])[0] == ("join", "wifi1", "bb:36")
    radios = [on("wifi2", slot(*H11, -40, width=20)), free("wifi1", [cand(*F36, -78)])]
    assert roam.plan_slots(radios)[0] is None


def test_nothing_new_starts_while_a_radio_is_connecting():
    radios = [on("wifi1", slot(*H52, -78, level=-68), [cand(*F36, -55)]),
              {"dev": "wifi0", "status": "busy", "slot": slot(*H11, -60, width=20), "cands": [], "locked": True},
              free("wifi2", [cand(*G52, -50)])]
    change, why = roam.plan_slots(radios)
    assert change is None and "land" in why


def test_never_two_radios_on_one_bssid():
    radios = [on("wifi1", slot(*H52, -30)), free("wifi2", [cand(*H52, -25)])]
    assert roam.plan_slots(radios)[0] is None


def test_other_network_radios_are_ignored():
    radios = [on("wifi1", slot(*H52, -30)),
              {"dev": "wifi2", "status": "other", "slot": None, "cands": [cand(*H11, -30, width=20)],
               "locked": False}]
    assert roam.plan_slots(radios)[0] is None


def test_upgrade_needs_a_clear_capacity_gain():
    # same tiers either way; -45 on 80 MHz is far more than -67 on 20 MHz
    radios = [on("wifi1", slot(*H52, -40)),
              on("wifi2", slot("dd:01", 2412, -67, width=20), [cand(*F36, -45)])]
    assert roam.plan_slots(radios)[0] == ("move", "wifi2", "bb:36")


# --- scans -----------------------------------------------------------------

def test_scouts_scan_fast_when_moving_and_slow_when_still():
    radios = [on("wifi1", slot(*H52, -40)), free("wifi0")]
    assert roam.plan_scans(radios, {"wifi0": 95.0}, 100.0, mobile=True) == []
    assert roam.plan_scans(radios, {"wifi0": 90.0}, 100.0, mobile=True) == ["wifi0"]
    assert roam.plan_scans(radios, {"wifi0": 90.0}, 100.0, mobile=False) == []
    assert roam.plan_scans(radios, {}, 100.0, mobile=False) == ["wifi0"]


def test_without_a_scout_the_longest_unscanned_radio_looks_around_slowly():
    radios = [on("wifi1", slot(*H52, -40)), on("wifi2", slot(*H11, -40, width=20))]
    last = {"wifi1": 50.0, "wifi2": 10.0}
    assert roam.plan_scans(radios, last, 100.0, mobile=False) == []
    assert roam.plan_scans(radios, last, 10.0 + roam.IDLE_SCAN_S, mobile=False) == ["wifi2"]


def test_weak_radio_scans_itself_only_without_a_scout():
    weak = on("wifi1", slot(*H52, -74))
    assert roam.plan_scans([weak], {}, 100.0, mobile=True) == ["wifi1"]
    assert roam.plan_scans([weak], {"wifi1": 95.0}, 100.0, mobile=True) == []
    assert roam.plan_scans([weak, free("wifi0")], {"wifi0": 99.0}, 100.0, mobile=True) == []
    steady = on("wifi1", slot(*H52, -50))
    assert roam.plan_scans([steady], {"wifi1": 50.0}, 100.0, mobile=True) == []
    falling = on("wifi1", dict(slot(*H52, -60), declining=True))
    assert roam.plan_scans([falling], {}, 100.0, mobile=True) == ["wifi1"]
