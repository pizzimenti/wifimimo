"""NetworkManager follow planner, nmcli parsing, and profile tidy."""

import wifimimo_nm as nm

P = "11111111-1111-1111-1111-111111111111"   # Central_Library
Q = "22222222-2222-2222-2222-222222222222"   # another network


class FakeLookup:
    def __init__(self, profiles=None, scans=None, chans=None, cloned=""):
        self.profiles = profiles or {}
        self.scans = scans or {}
        self.chans = chans or {}
        self.cloned = cloned

    def profile(self, uuid):
        return self.profiles.get(uuid, {})

    def scan(self, dev):
        return self.scans.get(dev, [])

    def global_cloned(self):
        return self.cloned

    def chan(self, dev):
        return self.chans.get(dev, 0)


def profile(uuid=P, ssid="Central_Library", **kw):
    base = {"uuid": uuid, "id": ssid, "type": "802-11-wireless", "ssid": ssid,
            "multi_connect": "0", "mac_address": "", "interface_name": "",
            "cloned_mac": "", "stable_id": "", "route_table": "0"}
    base.update(kw)
    return base


def ap(bssid, chan, signal=80, ssid="Central_Library", security="WPA2"):
    return {"ssid": ssid, "bssid": bssid, "chan": chan, "signal": signal, "security": security}


def dev(name, uuid="", state=None, reason=0):
    return {"device": name, "type": "wifi", "uuid": uuid,
            "state": state if state is not None else (100 if uuid else 30), "reason": reason}


SCANS = {"wifi0": [ap("aa:00:00:00:00:01", 36), ap("aa:00:00:00:00:02", 149)],
         "wifi2": [ap("aa:00:00:00:00:01", 36), ap("aa:00:00:00:00:02", 149, signal=60)]}


def lookup(**kw):
    kw.setdefault("profiles", {P: profile(), Q: profile(Q, "Home")})
    kw.setdefault("scans", SCANS)
    kw.setdefault("chans", {"wifi1": 36})
    return FakeLookup(**kw)


def baseline(devices):
    """Memory as it looks after one quiet poll of `devices`."""
    return {"last": {d["device"]: d["uuid"] for d in devices}, "moves": {}, "modified": {}}


def test_first_poll_only_records_baseline_and_joins_idle_radios():
    devices = [dev("wifi1", P), dev("wifi0"), dev("wifi2", Q)]
    actions, memory, status = nm.plan_follow(devices, {}, 100.0, lookup(), primary_device="wifi1")
    # wifi2 is on another network: left alone; idle wifi0 joins the primary's profile
    assert ("multi", P, "manual-multiple") in actions
    assert [a for a in actions if a[0] == "up"] == [("up", P, "wifi0", "aa:00:00:00:00:02")]
    assert status["skipped"]["wifi2"] == "on another network"
    assert memory["modified"] == {P: "0"}


def test_user_activation_becomes_leader_and_moves_everyone():
    before = [dev("wifi1", Q), dev("wifi0", Q), dev("wifi2", Q)]
    mem = baseline(before)
    after = [dev("wifi1", P), dev("wifi0", Q), dev("wifi2", Q)]
    actions, memory, status = nm.plan_follow(after, mem, 200.0, lookup())
    ups = sorted(a[2] for a in actions if a[0] == "up")
    assert ups == ["wifi0", "wifi2"]
    assert memory["leader"]["device"] == "wifi1" and memory["leader"]["uuid"] == P
    assert memory["moves"]["wifi0"] == {"target": P, "previous": Q, "at": 200.0}


def test_channel_diversity_between_followers():
    mem = baseline([dev("wifi1", Q), dev("wifi0"), dev("wifi2")])
    after = [dev("wifi1", P), dev("wifi0"), dev("wifi2")]
    actions, _, _ = nm.plan_follow(after, mem, 1.0, lookup())
    picks = {a[2]: a[3] for a in actions if a[0] == "up"}
    # leader on chan 36 -> first follower takes 149; second has nothing unused -> NM picks
    assert picks == {"wifi0": "aa:00:00:00:00:02", "wifi2": ""}


def test_our_own_join_is_not_a_new_leader():
    mem = baseline([dev("wifi1", P), dev("wifi0", Q)])
    mem["leader"] = {"uuid": P, "device": "wifi1", "at": 0}
    mem["moves"] = {"wifi0": {"target": P, "previous": Q, "at": 5}}
    actions, memory, _ = nm.plan_follow([dev("wifi1", P), dev("wifi0", P)], mem, 6.0, lookup())
    assert actions == [] and memory["leader"]["device"] == "wifi1"


def test_fallback_to_previous_profile_is_ignored_and_cools_down():
    mem = baseline([dev("wifi1", P), dev("wifi0", P)])
    mem["leader"] = {"uuid": P, "device": "wifi1", "at": 0}
    mem["moves"] = {"wifi0": {"target": P, "previous": Q, "at": 100}}
    # NM autoconnect put wifi0 back on Q: not a user choice
    actions, memory, status = nm.plan_follow([dev("wifi1", P), dev("wifi0", Q)], mem, 150.0, lookup())
    assert memory["leader"]["uuid"] == P
    assert not [a for a in actions if a[0] == "up"]
    assert memory["moves"]["wifi0"]["failed_at"] == 150.0
    assert status["skipped"]["wifi0"] == "cooling down after a failed join"


def test_user_disconnect_takes_followers_down():
    mem = baseline([dev("wifi1", P), dev("wifi0", P)])
    mem["leader"] = {"uuid": P, "device": "wifi1", "at": 0}
    devices = [dev("wifi1", "", state=30, reason=nm.REASON_USER_REQUESTED), dev("wifi0", P)]
    actions, memory, _ = nm.plan_follow(devices, mem, 10.0, lookup())
    assert actions == [("down", P, "wifi0")]
    assert memory["leader"] == {}


def test_locked_profile_is_skipped_and_flagged():
    look = lookup(profiles={P: profile(mac_address="28:94:01:BB:F8:96")})
    actions, _, status = nm.plan_follow([dev("wifi1", P), dev("wifi0")], {}, 1.0, look, "wifi1")
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


def test_out_of_range_radio_is_skipped():
    look = lookup(scans={"wifi0": [ap("bb:00:00:00:00:01", 11, ssid="Other")]})
    actions, _, status = nm.plan_follow([dev("wifi1", P), dev("wifi0")], {}, 1.0, look, "wifi1")
    assert not [a for a in actions if a[0] == "up"]
    assert status["skipped"]["wifi0"] == "network not in range"


def test_owe_transition_networks_are_never_pinned():
    scan = [ap("aa:00:00:00:00:01", 36, security="OWE-TM"), ap("aa:00:00:00:00:02", 149, security="OWE-TM")]
    assert nm.pick_bssid(scan, "Central_Library", set()) == ""


def test_already_multi_profile_isnt_modified():
    look = lookup(profiles={P: profile(multi_connect="manual-multiple")})
    actions, memory, _ = nm.plan_follow([dev("wifi1", P), dev("wifi0")], {}, 1.0, look, "wifi1")
    assert not [a for a in actions if a[0] == "multi"]
    assert memory["modified"] == {}


def test_release_restores_original_multi_connect():
    assert nm.plan_release({"modified": {P: "0"}}) == [("multi", P, "0")]
    assert nm.plan_release({}) == []


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parse_dev_show():
    text = ("GENERAL.DEVICE:wifi1\nGENERAL.TYPE:wifi\nGENERAL.STATE:100 (connected)\n"
            "GENERAL.REASON:0 (No reason given)\nGENERAL.CON-UUID:" + P + "\n\n"
            "GENERAL.DEVICE:p2p-dev-wifi1\nGENERAL.TYPE:wifi-p2p\nGENERAL.STATE:30 (disconnected)\n"
            "GENERAL.REASON:0 (No reason given)\nGENERAL.CON-UUID:\n")
    devices = nm.parse_dev_show(text)
    assert devices[0] == {"device": "wifi1", "type": "wifi", "state": 100, "reason": 0, "uuid": P}
    assert devices[1]["type"] == "wifi-p2p" and devices[1]["uuid"] == ""


def test_split_terse_unescapes_colons():
    assert nm.split_terse(r"Cafe\:Net:AA\:BB\:CC\:DD\:EE\:FF:36:80:WPA2") == \
        ["Cafe:Net", "AA:BB:CC:DD:EE:FF", "36", "80", "WPA2"]


def test_parse_wifi_list_lowercases_bssid():
    rows = nm.parse_wifi_list(r"Central_Library:04\:CD\:C0\:18\:0B\:24:6:87:OWE-TM")
    assert rows == [{"ssid": "Central_Library", "bssid": "04:cd:c0:18:0b:24", "chan": 6,
                     "signal": 87, "security": "OWE-TM"}]


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
    plan = nm.plan_tidy([
        prof("u1", "Central_Library", "Central_Library", mac="28:94:01:BB:F8:96", ts=1),
        prof("u2", "Central_Library-a8000", "Central_Library", mac="28:94:01:B7:B9:1A", ts=3),
        prof("u3", "Central_Library-internal", "Central_Library", mac="3C:3B:AD:16:B7:30", ts=5),
    ])
    assert plan == [{"ssid": "Central_Library",
                     "keep": {"uuid": "u1", "id": "Central_Library", "clear_binding": True},
                     "delete": [{"uuid": "u2", "id": "Central_Library-a8000"},
                                {"uuid": "u3", "id": "Central_Library-internal"}]}]


def test_tidy_leaves_plain_duplicates_alone():
    assert nm.plan_tidy([prof("a", "Phone", "Phone", ts=1), prof("b", "Phone", "Phone", ts=2)]) == []


def test_tidy_prefers_unbound_keeper_and_spares_plain_duplicates():
    plan = nm.plan_tidy([prof("a", "Net", "Net"), prof("b", "Net", "Net"),
                         prof("c", "Net-stick", "Net", mac="aa:bb:cc:dd:ee:ff", ts=9)])
    assert plan[0]["keep"]["clear_binding"] is False
    assert [d["uuid"] for d in plan[0]["delete"]] == ["c"]


def test_tidy_ignores_different_security():
    assert nm.plan_tidy([prof("a", "Net", "Net", key="sae"),
                         prof("b", "Net-x", "Net", mac="aa:bb:cc:dd:ee:ff", key="wpa-psk")]) == []
