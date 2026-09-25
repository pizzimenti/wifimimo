"""NetworkManager follow: leader tracking, placement via the slot planner,
nmcli parsing, and profile tidy."""

import json

import wifimimo_nm as nm
import wifimimo_roam as roam

P = "11111111-1111-1111-1111-111111111111"   # Central_Library
P2 = "33333333-3333-3333-3333-333333333333"  # a second profile for the same network
Q = "22222222-2222-2222-2222-222222222222"   # another network


class FakeLookup:
    def __init__(self, profiles=None, scans=None, freqs=None, cloned="", signals=None, bssids=None,
                 widths=None):
        self.profiles = profiles or {}
        self.scans = scans or {}
        self.freqs = freqs or {}
        self.cloned = cloned
        self.signals = signals or {}
        self.bssids = bssids or {}
        self.widths = widths or {}

    def signal(self, dev):
        return self.signals.get(dev, 0)

    def bssid(self, dev):
        return self.bssids.get(dev, "")

    def profile(self, uuid):
        return self.profiles.get(uuid, {})

    def scan(self, dev):
        return self.scans.get(dev, [])

    def global_cloned(self):
        return self.cloned

    def freq(self, dev):
        return self.freqs.get(dev, 0)

    def width(self, dev):
        return self.widths.get(dev, 20)


def profile(uuid=P, ssid="Central_Library", **kw):
    base = {"uuid": uuid, "id": ssid, "type": "802-11-wireless", "ssid": ssid,
            "multi_connect": "0", "mac_address": "", "interface_name": "",
            "cloned_mac": "", "stable_id": "", "route_table": "0"}
    base.update(kw)
    return base


def ap(bssid, freq, signal=80, ssid="Central_Library", security="WPA2", chan=0):
    return {"ssid": ssid, "bssid": bssid, "chan": chan, "freq": freq, "signal": signal,
            "security": security, "bandwidth": 20}


def dev(name, uuid="", state=None, reason=0, connectivity=4):
    return {"device": name, "type": "wifi", "uuid": uuid,
            "state": state if state is not None else (100 if uuid else 30), "reason": reason,
            "connectivity": connectivity}


A1, A2, A3 = "aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:03"
SCANS = {"wifi0": [ap(A1, 5180), ap(A2, 5745)],
         "wifi1": [ap(A1, 5180), ap(A2, 5745)],
         "wifi2": [ap(A1, 5180), ap(A2, 5745, signal=60)]}


def lookup(**kw):
    kw.setdefault("profiles", {P: profile(), P2: profile(P2), Q: profile(Q, "Home")})
    kw.setdefault("scans", SCANS)
    kw.setdefault("freqs", {"wifi1": 5180})
    kw.setdefault("bssids", {"wifi1": A1})
    kw.setdefault("signals", {"wifi1": -50})
    return FakeLookup(**kw)


def ctx_for(scans, age=1.0, now=0.0):
    """A RoamContext whose scan tables hold a fresh dBm reading of every AP
    in `scans` (NM % converted), as a scout or recent scan would give."""
    ctx = nm.RoamContext()
    for name, rows in scans.items():
        ctx.dumps[name] = [{"bssid": r["bssid"], "ssid": r["ssid"], "freq": r["freq"],
                            "signal": roam.pct_to_dbm(r["signal"]), "age_s": age,
                            "width": 20, "span": roam.channel_span(r["freq"], 20),
                            "util": None, "associated": False} for r in rows]
    for name in scans:
        ctx.last_scan[name] = now
    return ctx


def plan(devices, memory, now, look=None, primary="", ctx=None, scans=SCANS):
    look = look or lookup()
    return nm.plan_follow(devices, memory, now, look, primary, ctx or ctx_for(scans, now=now))


def baseline(devices):
    """Memory as it looks after one quiet poll of `devices`."""
    return {"last": {d["device"]: d["uuid"] for d in devices}, "moves": {}, "modified": {},
            "devices": sorted(d["device"] for d in devices)}


def led(devices, leader_dev="wifi1", uuid=P):
    mem = baseline(devices)
    mem["leader"] = {"uuid": uuid, "device": leader_dev, "at": 0}
    return mem


def ups(actions):
    return {a[2]: a[3] for a in actions if a[0] == "up"}


def kinds(actions, kind):
    return [a[1] for a in actions if a[0] == kind]


# --- leader tracking ---------------------------------------------------------


def test_first_poll_joins_an_idle_radio_to_a_free_slot():
    devices = [dev("wifi1", P), dev("wifi0"), dev("wifi2", Q)]
    actions, memory, status = plan(devices, {}, 100.0, primary="wifi1")
    # wifi1's AP (A1) is taken: never share a BSSID; wifi2 stays on its own network
    assert ("multi", P, "manual-multiple") in actions
    assert ups(actions) == {"wifi0": A2}
    assert status["skipped"]["wifi2"] == "on another network"
    assert memory["modified"] == {P: "0"}


def test_user_activation_becomes_leader_and_brings_radios_over_one_at_a_time():
    before = [dev("wifi1", Q), dev("wifi0", Q), dev("wifi2", Q)]
    after = [dev("wifi1", P), dev("wifi0", Q), dev("wifi2", Q)]
    actions, memory, _ = plan(after, baseline(before), 200.0)
    assert memory["leader"]["device"] == "wifi1" and memory["leader"]["uuid"] == P
    assert ups(actions) == {"wifi0": A2}                       # strongest free slot first
    assert memory["moves"]["wifi0"] == {"target": P, "previous": Q, "at": 200.0, "bssid": A2}
    assert not kinds(actions, "park")                        # never park another network's radio


def test_our_own_join_is_not_a_new_leader():
    devices = [dev("wifi1", P), dev("wifi0", P)]
    mem = led([dev("wifi1", P), dev("wifi0", Q)])
    mem["moves"] = {"wifi0": {"target": P, "previous": Q, "at": 5, "bssid": A2}}
    look = lookup(freqs={"wifi1": 5180, "wifi0": 5745}, bssids={"wifi1": A1, "wifi0": A2})
    actions, memory, _ = plan(devices, mem, 6.0, look)
    assert not ups(actions) and memory["leader"]["device"] == "wifi1"


def test_fallback_to_previous_profile_is_ignored_and_cools_down():
    mem = led([dev("wifi1", P), dev("wifi0", P)])
    mem["moves"] = {"wifi0": {"target": P, "previous": Q, "at": 100, "bssid": A2}}
    # NM autoconnect put wifi0 back on Q: not a user choice
    actions, memory, status = plan([dev("wifi1", P), dev("wifi0", Q)], mem, 150.0)
    assert memory["leader"]["uuid"] == P
    assert not ups(actions)
    assert memory["moves"]["wifi0"]["failed_at"] == 150.0


def test_replugged_card_autoconnect_is_not_a_user_choice():
    # live 2026-09-25: the A9000 dropped off USB, came back disconnected, and
    # NM autoconnected it 3 s later; it must not become the leader
    mem = led([dev("wifi1", P), dev("wifi2", P)], leader_dev="wifi2")
    actions, mem, _ = plan([dev("wifi1", P), dev("wifi2", P), dev("wifi0")], mem, 10.0)
    assert mem["appeared"]["wifi0"] == 10.0
    _, mem, _ = plan([dev("wifi1", P), dev("wifi2", P), dev("wifi0", Q)], mem, 13.0)
    assert mem["leader"]["uuid"] == P and mem["leader"]["device"] == "wifi2"
    # well after it appeared, the same activation would be the user's
    mem2 = led([dev("wifi1", P), dev("wifi2", P), dev("wifi0")], leader_dev="wifi2")
    mem2["appeared"] = {"wifi0": 10.0}
    _, mem2, _ = plan([dev("wifi1", P), dev("wifi2", P), dev("wifi0", Q)], mem2, 10.0 + nm.NEW_RADIO_GRACE_S + 1)
    assert mem2["leader"]["uuid"] == Q


def test_another_profile_for_the_same_network_is_not_a_new_choice():
    mem = led([dev("wifi1", P), dev("wifi2", Q)], leader_dev="wifi1")
    actions, mem, status = plan([dev("wifi1", P), dev("wifi2", P2)], mem, 50.0,
                                lookup(freqs={"wifi1": 5180, "wifi2": 5745},
                                       bssids={"wifi1": A1, "wifi2": A2}))
    assert mem["leader"]["uuid"] == P
    assert "wifi2" in status["followers"]                    # counts as on the network
    assert not ups(actions)


def test_user_disconnect_takes_followers_down():
    mem = led([dev("wifi1", P), dev("wifi0", P)])
    devices = [dev("wifi1", "", state=30, reason=nm.REASON_USER_REQUESTED), dev("wifi0", P)]
    actions, memory, _ = plan(devices, mem, 10.0)
    assert actions == [("down", P, "wifi0")]
    assert memory["leader"] == {}


def test_our_own_park_of_a_radio_is_not_a_user_disconnect():
    mem = led([dev("wifi1", P), dev("wifi0", P)])
    mem["parked"] = {"wifi1": 5.0}
    devices = [dev("wifi1", "", state=30, reason=nm.REASON_USER_REQUESTED), dev("wifi0", P)]
    look = lookup(freqs={"wifi0": 5745}, bssids={"wifi0": A2})
    actions, memory, _ = plan(devices, mem, 10.0, look)
    assert ("down", P, "wifi0") not in actions
    assert memory["leader"]["device"] == "wifi0"             # the choice lives on


def test_leader_losing_its_link_hands_over_and_is_placed_again():
    devices = [dev("wifi1", "", state=30, reason=53), dev("wifi2", P)]
    mem = led([dev("wifi1", P), dev("wifi2", P)])
    look = lookup(freqs={"wifi2": 5745}, bssids={"wifi2": A2})
    actions, memory, _ = plan(devices, mem, 60.0, look)
    assert memory["leader"]["device"] == "wifi2"
    assert ups(actions) == {"wifi1": A1}                      # A2 is wifi2's


def test_all_radios_dropped_rejoin_one_at_a_time():
    devices = [dev("wifi1", "", state=30, reason=53), dev("wifi2", "", state=30, reason=53)]
    mem = led([dev("wifi1", P), dev("wifi2", P)])
    actions, memory, _ = plan(devices, mem, 60.0, lookup(freqs={}, bssids={}))
    assert memory["leader"] == {"uuid": P, "device": "wifi1", "at": 0}
    assert len(ups(actions)) == 1
    # the radio not joining this poll is parked, so NM can't autoconnect it
    # onto the same AP; it joins its own slot on the next poll
    (joined,) = ups(actions)
    other = ({"wifi1", "wifi2"} - {joined}).pop()
    assert kinds(actions, "park") == [other]


# --- placement -----------------------------------------------------------------


def test_two_radios_on_one_bssid_parks_one_and_it_scouts():
    devices = [dev("wifi1", P), dev("wifi0", P)]
    look = lookup(freqs={"wifi1": 5180, "wifi0": 5180}, bssids={"wifi1": A1, "wifi0": A1},
                  signals={"wifi1": -30, "wifi0": -52},
                  scans={"wifi0": [ap(A1, 5180)], "wifi1": [ap(A1, 5180)]})
    actions, memory, status = plan(devices, led(devices), 100.0, look, scans=look.scans)
    assert kinds(actions, "park") == ["wifi0"]                # the weaker card
    assert memory["parked"] == {"wifi0": 100.0}
    assert status["iface_flags"]["wifi0"][-1]["code"] == "scouting"


def test_idle_radio_without_a_strong_free_slot_parks_and_scans():
    devices = [dev("wifi1", P), dev("wifi0")]
    scans = {"wifi0": [ap(A1, 5180), ap(A2, 5745, signal=40)]}    # A2 is -76 dBm: too weak
    look = lookup(scans=scans)
    actions, memory, status = plan(devices, led(devices), 100.0, look,
                                   ctx=ctx_for(scans, now=0.0))
    assert not ups(actions)
    assert kinds(actions, "park") == ["wifi0"]
    assert ("scan", "wifi0") in actions                       # scouts scan right away
    assert status["parked"] == ["wifi0"]


def test_parked_radio_joins_when_a_slot_opens_and_is_unparked():
    devices = [dev("wifi1", P), dev("wifi0", "", state=30, reason=39)]
    mem = led(devices)
    mem["parked"] = {"wifi0": 50.0}
    actions, memory, _ = plan(devices, mem, 100.0)
    assert ups(actions) == {"wifi0": A2}
    assert ("unpark", "wifi0") in actions
    assert actions.index(("unpark", "wifi0")) > [a[0] for a in actions].index("up")
    assert "wifi0" not in memory["parked"]


def test_stale_scan_data_is_not_acted_on_but_triggers_a_scan():
    devices = [dev("wifi1", P), dev("wifi0")]
    ctx = ctx_for(SCANS, age=roam.FRESH_S + 5)
    ctx.last_scan = {}
    actions, _, _ = plan(devices, led(devices), 100.0, ctx=ctx)
    assert not ups(actions)
    assert ("scan", "wifi0") in actions


def test_declining_radio_moves_early_to_a_fresh_strong_slot():
    devices = [dev("wifi1", P), dev("wifi2", P)]
    look = lookup(freqs={"wifi1": 5180, "wifi2": 2412}, bssids={"wifi1": A1, "wifi2": A3},
                  signals={"wifi1": -45, "wifi2": -67},
                  scans={"wifi2": [ap(A1, 5180), ap(A2, 5745, signal=60), ap(A3, 2412)]})
    ctx = ctx_for(look.scans, now=100.0)
    for i, level in enumerate(range(-55, -70, -2)):          # -2 dB/s: heading below -75
        ctx.trend.add("wifi2", 92.0 + i, level, A3)
        ctx.trend.add("wifi1", 92.0 + i, -45, A1)
    actions, _, status = plan(devices, led(devices), 100.0, look, ctx=ctx)
    assert ups(actions) == {"wifi2": A2}                      # A2 -64 dBm, a free channel
    assert status["wants_fast"] is True


def test_steady_radio_stays_put():
    devices = [dev("wifi1", P), dev("wifi2", P)]
    look = lookup(freqs={"wifi1": 5180, "wifi2": 5745}, bssids={"wifi1": A1, "wifi2": A2},
                  signals={"wifi1": -45, "wifi2": -55})
    actions, _, status = plan(devices, led(devices), 100.0, look)
    assert not ups(actions) and not kinds(actions, "park")
    assert status["plan"] == "placement already best"


def test_nothing_starts_while_a_radio_is_connecting():
    devices = [dev("wifi1", P), dict(dev("wifi2", P), state=70), dev("wifi0")]
    mem = led(devices)
    mem["moves"] = {"wifi2": {"target": P, "previous": "", "at": 99.0, "bssid": A2}}
    mem["parked"] = {"wifi0": 1.0}
    look = lookup(scans={**SCANS, "wifi0": [ap(A1, 5180), ap(A2, 5745), ap(A3, 2412)]})
    actions, _, status = plan(devices, mem, 100.0, look, scans=look.scans)
    assert not ups(actions)
    assert "land" in status["plan"]


def test_locked_profile_is_skipped_and_flagged():
    look = lookup(profiles={P: profile(mac_address="28:94:01:BB:F8:96")})
    actions, _, status = plan([dev("wifi1", P), dev("wifi0")], {}, 1.0, look, "wifi1")
    assert actions == []
    assert status["iface_flags"]["wifi1"][0]["code"] == "profile_locked"
    assert status["skipped"]["wifi0"] == "profile_locked"


def test_stable_cloned_mac_is_a_duplicate_mac_risk():
    for cloned, stable_id, bad in [("stable", "", True), ("stable-ssid", "", True),
                                   ("02:11:22:33:44:55", "", True), ("stable", "${DEVICE}", False),
                                   ("random", "", False), ("preserve", "", False), ("", "", False)]:
        code = nm.followability(profile(cloned_mac=cloned, stable_id=stable_id))[0]
        assert (code == "duplicate_mac") is bad, (cloned, stable_id)
    # global default applies when the profile doesn't set one
    assert nm.followability(profile(), global_cloned="stable")[0] == "duplicate_mac"


def test_already_multi_profile_isnt_modified():
    look = lookup(profiles={P: profile(multi_connect="manual-multiple")})
    actions, memory, _ = plan([dev("wifi1", P), dev("wifi0")], {}, 1.0, look, "wifi1")
    assert not [a for a in actions if a[0] == "multi"]
    assert memory["modified"] == {}


def test_user_pinned_profile_is_left_alone():
    look = lookup(profiles={P: profile(bssid=A1)})
    actions, _, status = plan([dev("wifi1", P), dev("wifi0")], {}, 1.0, look, "wifi1")
    assert actions == []
    assert status["skipped"]["wifi0"] == "profile pinned to one access point"


def test_owe_transition_network_joins_through_the_hidden_twin():
    scan = [ap("04:cd:c0:18:0b:24", 2437, 87, security="OWE-TM"),
            ap("04:cd:c0:18:0b:2f", 2437, 85, ssid="", security="OWE")]
    look = lookup(scans={"wifi0": scan})
    actions, _, _ = plan([dev("wifi1", P), dev("wifi0")], {}, 1.0, look, "wifi1",
                         scans={"wifi0": scan})
    assert ups(actions) == {"wifi0": "04:cd:c0:18:0b:2f"}


# --- bad-AP avoidance --------------------------------------------------------


def _pinned_mem(devices, name, bssid, at, previous=""):
    mem = led(devices)
    mem["moves"] = {name: {"target": P, "previous": previous, "at": at, "bssid": bssid}}
    return mem


def test_join_that_did_not_stick_marks_ap_bad_and_picks_another():
    devices = [dev("wifi1", P), dev("wifi2")]
    scans = {"wifi2": [ap(A1, 5180), ap(A2, 5745), ap(A3, 2412)]}
    mem = _pinned_mem(devices, "wifi2", A2, at=100.0)
    actions, memory, status = plan(devices, mem, 150.0, lookup(scans=scans), scans=scans)
    assert A2 in memory["bad"]["wifi2"]
    assert ups(actions) == {"wifi2": A3}
    assert status["iface_flags"]["wifi2"][0]["code"] == "avoiding_ap"


def test_join_stuck_at_limited_connectivity_is_moved_off_it():
    limited = dev("wifi2", P, connectivity=3)
    devices = [dev("wifi1", P), limited]
    scans = {"wifi2": [ap(A1, 5180), ap(A2, 5745), ap(A3, 2412)]}
    look = lookup(scans=scans, freqs={"wifi1": 5180, "wifi2": 5745},
                  bssids={"wifi1": A1, "wifi2": A2}, signals={"wifi1": -50, "wifi2": -50})
    mem = _pinned_mem(devices, "wifi2", A2, at=100.0, previous=P)
    actions, _, _ = plan(devices, mem, 130.0, look, scans=scans)        # within the grace
    assert not ups(actions) and not kinds(actions, "park")
    actions, memory, _ = plan(devices, mem, 100.0 + nm.LIMITED_GRACE_S + 1, look, scans=scans)
    assert ups(actions) == {"wifi2": A3}
    assert A2 in memory["bad"]["wifi2"]


def test_bad_ap_marks_expire():
    devices = [dev("wifi1", P), dev("wifi0")]
    mem = led(devices)
    mem["bad"] = {"wifi0": {A2: 500.0}}
    actions, memory, _ = plan(devices, mem, 400.0)
    assert not ups(actions)                                   # A2 avoided, A1 taken
    actions, memory, _ = plan(devices, mem, 600.0)
    assert ups(actions) == {"wifi0": A2}
    assert "wifi0" not in memory["bad"]


# --- speed and release -------------------------------------------------------


def test_wants_fast_while_moving_but_not_for_a_still_scout():
    devices = [dev("wifi1", P), dev("wifi0")]
    mem = led(devices)
    mem["parked"] = {"wifi0": 1.0}
    mem["moves"] = {"wifi0": {"target": "", "previous": "", "at": 1.0, "bssid": ""}}
    scans = {"wifi0": [ap(A1, 5180)]}
    ctx = ctx_for(scans, now=100.0)
    for i in range(8):
        ctx.trend.add("wifi1", 92.0 + i, -50, A1)
    _, _, status = plan(devices, mem, 100.0, lookup(scans=scans), ctx=ctx)
    assert status["wants_fast"] is False
    for i in range(4):
        ctx.trend.add("wifi1", 100.0 + i, -50 - 3 * i, A1)
    _, _, status = plan(devices, mem, 104.0, lookup(scans=scans), ctx=ctx)
    assert status["wants_fast"] is True


def test_release_restores_multi_connect_and_unparks():
    assert nm.plan_release({"modified": {P: "0"}, "parked": {"wifi0": 1.0}}) == [
        ("multi", P, "0"), ("unpark", "wifi0")]
    assert nm.plan_release({}) == []


def test_activate_pinned_sets_then_clears_bssid_around_activation():
    calls = []

    def fake(args):
        calls.append(" ".join(args))
        return 0, ""

    assert nm.activate_pinned(P, "wifi2", "04:cd:c0:18:0b:04", nmcli=fake) == 0
    assert calls == [
        f"connection modify --temporary uuid {P} 802-11-wireless.bssid 04:cd:c0:18:0b:04",
        f"-w 0 connection up uuid {P} ifname wifi2",
        f"connection modify --temporary uuid {P} 802-11-wireless.bssid ",
    ]


def test_activate_pinned_clears_bssid_even_if_activation_raises():
    calls = []

    def fake(args):
        calls.append(args[0:2])
        if "up" in args:
            raise OSError("nmcli died")
        return 0, ""

    try:
        nm.activate_pinned(P, "wifi2", "04:cd:c0:18:0b:04", nmcli=fake)
    except OSError:
        pass
    assert calls[-1] == ["connection", "modify"]


def test_activate_without_pick_is_plain_up():
    calls = []
    nm.activate_pinned(P, "wifi2", "", nmcli=lambda a: (calls.append(" ".join(a)), (0, ""))[1])
    assert calls == [f"-w 0 connection up uuid {P} ifname wifi2"]


def test_feed_context_tracks_levels_and_scan_tables():
    ctx = nm.RoamContext()
    states = {"wifi1": {"connected": True, "signal_avg_dbm": -50, "bssid": "AA:00:00:00:00:01"},
              "wifi0": {"connected": False}}
    tables = {"wifi1": [], "wifi0": [{"bssid": "aa:00:00:00:00:01", "signal": -70.0, "age_s": 0.2,
                                       "freq": 5180, "associated": False}]}
    nm.feed_context(ctx, states, 10.0, dump=lambda d: tables[d])
    assert ctx.trend.level("wifi1") == -50
    assert ctx.dumps["wifi0"][0]["signal"] == -70.0
    assert ctx.offsets.get("wifi0", "wifi1", "5") == -20.0   # learned from the live level


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parse_dev_show():
    text = ("GENERAL.DEVICE:wifi1\nGENERAL.TYPE:wifi\nGENERAL.STATE:100 (connected)\n"
            "GENERAL.REASON:0 (No reason given)\nGENERAL.CON-UUID:" + P + "\n\n"
            "GENERAL.DEVICE:p2p-dev-wifi1\nGENERAL.TYPE:wifi-p2p\nGENERAL.STATE:30 (disconnected)\n"
            "GENERAL.REASON:0 (No reason given)\nGENERAL.CON-UUID:\n")
    devices = nm.parse_dev_show(text)
    assert devices[0] == {"device": "wifi1", "type": "wifi", "state": 100, "reason": 0, "uuid": P,
                          "connectivity": 0}
    assert devices[1]["type"] == "wifi-p2p" and devices[1]["uuid"] == ""


def test_parse_dev_show_reads_connectivity():
    text = ("GENERAL.DEVICE:wifi2\nGENERAL.TYPE:wifi\nGENERAL.STATE:100 (connected)\n"
            "GENERAL.REASON:0 (No reason given)\nGENERAL.CON-UUID:" + P + "\n"
            "GENERAL.IP4-CONNECTIVITY:3 (limited)\n")
    assert nm.parse_dev_show(text)[0]["connectivity"] == 3


def test_heal_candidates_only_waiting_radios_now_fully_connected():
    excluded = [{"iface": "wifi2", "reason": "no connectivity"},
                {"iface": "wifi0", "reason": "not connected"},
                {"iface": "wifi3", "reason": "limited connectivity"}]
    devices = [{"device": "wifi2", "connectivity": 4}, {"device": "wifi0", "connectivity": 4},
               {"device": "wifi3", "connectivity": 3}]
    assert nm.heal_candidates(excluded, devices) == ["wifi2"]


def test_degraded_member_triggers_reapply():
    # walking away: still associated, but NM rates it limited
    devices = [{"device": "wifi1", "connectivity": 3}, {"device": "wifi2", "connectivity": 4}]
    assert nm.heal_candidates([], devices, members=["wifi1", "wifi2"]) == ["wifi1"]


def test_follower_heal_is_rate_limited(tmp_path, monkeypatch):
    status = tmp_path / "multipath.json"
    status.write_text(json.dumps({"excluded": [{"iface": "wifi2", "reason": "no connectivity"}]}))
    monkeypatch.setattr(nm.shared, "MULTIPATH_STATUS", status)
    launched = []

    class FakeProc:
        def __init__(self, argv, **_kw):
            launched.append(argv)

        def poll(self):
            return 0

    monkeypatch.setattr(nm.subprocess, "Popen", FakeProc)
    follower = nm.Follower(tmp_path / "follow.json")
    devices = [{"device": "wifi2", "connectivity": 4}]
    assert follower.heal(devices, 100.0) == ["wifi2"]
    assert follower.heal(devices, 105.0) == []            # within the interval
    assert follower.heal(devices, 100.0 + nm.HEAL_INTERVAL_S + 1) == ["wifi2"]
    assert launched[0] == ["pkexec", "/usr/local/lib/wifimimo/wifimimo-helper", "multipath", "apply"]
    assert len(launched) == 2


def test_split_terse_unescapes_colons():
    assert nm.split_terse(r"Cafe\:Net:AA\:BB\:CC\:DD\:EE\:FF:36:80:WPA2") == \
        ["Cafe:Net", "AA:BB:CC:DD:EE:FF", "36", "80", "WPA2"]


def test_parse_wifi_list_lowercases_bssid_and_reads_width():
    rows = nm.parse_wifi_list(r"Central_Library:04\:CD\:C0\:18\:0B\:24:6:2437 MHz:87:OWE-TM:40 MHz")
    assert rows == [{"ssid": "Central_Library", "bssid": "04:cd:c0:18:0b:24", "chan": 6,
                     "freq": 2437, "signal": 87, "security": "OWE-TM", "bandwidth": 40}]
    # older nmcli without BANDWIDTH
    assert nm.parse_wifi_list(r"X:04\:CD\:C0\:18\:0B\:24:6:2437 MHz:87:WPA2")[0]["bandwidth"] == 20


def test_primary_wifi_device_is_lowest_metric():
    routes = [{"dst": "default", "dev": "wifi2", "metric": 700},
              {"dst": "default", "dev": "wifi1", "metric": 610},
              {"dst": "default", "dev": "eth0", "metric": 100}]
    assert nm.primary_wifi_device({"wifi1": {}, "wifi2": {}}, routes) == "wifi1"


# ---------------------------------------------------------------------------
# tidy
# ---------------------------------------------------------------------------


def prof(uuid, pid, ssid, mac="", ts=0, key="owe"):
    return {"uuid": uuid, "id": pid, "type": "802-11-wireless", "ssid": ssid,
            "mac_address": mac, "interface_name": "", "key_mgmt": key, "timestamp": ts}


def test_tidy_collapses_card_copies_keeping_the_original_name():
    plan_ = nm.plan_tidy([
        prof("u1", "Central_Library", "Central_Library", mac="28:94:01:BB:F8:96", ts=1),
        prof("u2", "Central_Library-a8000", "Central_Library", mac="28:94:01:B7:B9:1A", ts=3),
        prof("u3", "Central_Library-internal", "Central_Library", mac="3C:3B:AD:16:B7:30", ts=5),
    ])
    assert plan_ == [{"ssid": "Central_Library",
                      "keep": {"uuid": "u1", "id": "Central_Library", "clear_binding": True},
                      "delete": [{"uuid": "u2", "id": "Central_Library-a8000"},
                                 {"uuid": "u3", "id": "Central_Library-internal"}]}]


def test_tidy_leaves_plain_duplicates_alone():
    assert nm.plan_tidy([prof("a", "Phone", "Phone", ts=1), prof("b", "Phone", "Phone", ts=2)]) == []


def test_tidy_prefers_unbound_keeper_and_spares_plain_duplicates():
    plan_ = nm.plan_tidy([prof("a", "Net", "Net"), prof("b", "Net", "Net"),
                          prof("c", "Net-stick", "Net", mac="aa:bb:cc:dd:ee:ff", ts=9)])
    assert plan_[0]["keep"]["clear_binding"] is False
    assert [d["uuid"] for d in plan_[0]["delete"]] == ["c"]


def test_tidy_ignores_different_security():
    assert nm.plan_tidy([prof("a", "Net", "Net", key="sae"),
                         prof("b", "Net-x", "Net", mac="aa:bb:cc:dd:ee:ff", key="wpa-psk")]) == []
