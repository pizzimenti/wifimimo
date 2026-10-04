"""Link-level and cross-radio health flags."""

from wifimimo_radio import cross_iface_flags, link_health_flags, worst_severity


def codes(flags):
    return [f["code"] for f in flags]


def up(**kw):
    base = {"connected": True, "signal_dbm": -55, "signal_antennas": [-55, -57],
            "tx_nss": 2, "rx_nss": 2, "retry_10s_pct": 0.0}
    base.update(kw)
    return base


def test_healthy_link_has_no_flags():
    assert link_health_flags(up()) == []


def test_disconnected_link_has_no_flags():
    assert link_health_flags({"connected": False, "signal_antennas": [-90]}) == []


def test_single_antenna_is_mimo_offline():
    assert "mimo_offline" in codes(link_health_flags(up(signal_antennas=[-60])))


def test_weak_antenna_below_threshold():
    assert codes(link_health_flags(up(signal_antennas=[-70, -76]))) == ["weak_antenna_2"]


def test_threshold_is_exclusive():
    assert link_health_flags(up(signal_antennas=[-60, -75])) == []


def test_antenna_imbalance_over_15_db():
    assert codes(link_health_flags(up(signal_antennas=[-50, -66]))) == ["antenna_imbalance"]
    assert link_health_flags(up(signal_antennas=[-50, -65])) == []


def test_mimo_degraded_needs_both_directions():
    flags = link_health_flags(up(tx_nss=1, rx_nss=1))
    assert codes(flags) == ["mimo_degraded"] and flags[0]["severity"] == "crit"
    assert link_health_flags(up(tx_nss=1, rx_nss=0)) == []
    assert link_health_flags(up(tx_nss=1, rx_nss=2)) == []


def test_high_retry():
    assert codes(link_health_flags(up(retry_10s_pct=31.0))) == ["high_retry"]
    assert link_health_flags(up(retry_10s_pct=30.0)) == []


def test_weak_overall_signal_when_driver_hides_chains():
    # MLD case: no per-antenna list, so only the overall signal can warn.
    assert codes(link_health_flags(up(signal_antennas=[], signal_dbm=-78))) == ["weak_signal"]
    assert link_health_flags(up(signal_antennas=[], signal_dbm=-70)) == []


def _sysctl(values):
    return lambda name, key: values.get((name, key), 0)


GOOD_ARP = _sysctl({("all", "arp_ignore"): 1, ("all", "arp_announce"): 2})


def radio(bssid, freq, subnet="172.20.176.0/22", connected=True, links=()):
    return {"connected": connected, "bssid": bssid, "freq_mhz": freq,
            "subnet": subnet, "links": list(links)}


def test_shared_bss_live_pair():
    # 2026-09-24: wifi0 and wifi1 both on 04:cd:c0:18:0b:c4 @ 6855.
    states = {
        "wifi0": radio("04:cd:c0:18:0b:c4", 6855),
        "wifi1": radio("04:cd:c0:18:0b:c4", 6855),
        "wifi2": radio("c8:78:67:39:ff:ef", 5220),
    }
    flags = cross_iface_flags(states, GOOD_ARP)
    assert codes(flags["wifi0"]) == ["shared_bss"]
    assert codes(flags["wifi1"]) == ["shared_bss"]
    assert flags["wifi2"] == []
    assert "wifi1" in flags["wifi0"][0]["detail"]


def test_shared_channel_different_bss():
    states = {"a": radio("aa:aa:aa:aa:aa:01", 5500), "b": radio("bb:bb:bb:bb:bb:02", 5500)}
    flags = cross_iface_flags(states, GOOD_ARP)
    assert codes(flags["a"]) == ["shared_channel"] and flags["a"][0]["severity"] == "warn"


def test_shared_bss_via_mlo_link():
    states = {
        "a": radio("aa:aa:aa:aa:aa:01", 6135, links=[{"bssid": "cc:cc:cc:cc:cc:03", "freq_mhz": 5180}]),
        "b": radio("cc:cc:cc:cc:cc:03", 5180),
    }
    assert codes(cross_iface_flags(states, GOOD_ARP)["b"]) == ["shared_bss"]


def test_disconnected_radios_are_ignored():
    states = {"a": radio("aa:aa:aa:aa:aa:01", 5500),
              "b": radio("aa:aa:aa:aa:aa:01", 5500, connected=False)}
    assert cross_iface_flags(states, _sysctl({})) == {"a": [], "b": []}


def test_arp_flux_uses_effective_value():
    states = {"a": radio("aa:aa:aa:aa:aa:01", 5500), "b": radio("bb:bb:bb:bb:bb:02", 6135)}
    # all=1 overrides a per-iface 0 (kernel uses the max): no flag.
    ok = _sysctl({("all", "arp_ignore"): 1, ("a", "arp_ignore"): 0, ("all", "arp_announce"): 2})
    assert cross_iface_flags(states, ok) == {"a": [], "b": []}
    # all=0 and iface=0: flagged on every radio in the subnet.
    bad = _sysctl({("all", "arp_announce"): 2})
    flags = cross_iface_flags(states, bad)
    assert codes(flags["a"]) == ["arp_flux"] and codes(flags["b"]) == ["arp_flux"]


def test_arp_flux_needs_two_radios_in_one_subnet():
    states = {"a": radio("aa:aa:aa:aa:aa:01", 5500, subnet="10.0.0.0/24"),
              "b": radio("bb:bb:bb:bb:bb:02", 6135, subnet="10.0.1.0/24")}
    assert cross_iface_flags(states, _sysctl({})) == {"a": [], "b": []}


def test_worst_severity():
    assert worst_severity([]) == ""
    assert worst_severity([{"severity": "info"}, {"severity": "crit"}, {"severity": "warn"}]) == "crit"
