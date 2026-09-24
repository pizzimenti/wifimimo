"""Throughput, the 60 s signal ring, and persistent radio colours."""

from wifimimo_radio import RadioColors, SignalRing, ThroughputTracker


def test_throughput_first_sample_is_zero_then_mbps():
    t = ThroughputTracker()
    assert t.update("m", 1_000, 2_000, 10.0) == (0.0, 0.0)
    # 1.25 MB in 1 s down = 10 Mb/s; 125 kB up = 1 Mb/s.
    assert t.update("m", 1_251_000, 127_000, 11.0) == (10.0, 1.0)


def test_throughput_counter_reset_reads_zero():
    t = ThroughputTracker()
    t.update("m", 5_000_000, 5_000_000, 1.0)
    assert t.update("m", 100, 100, 2.0) == (0.0, 0.0)
    assert t.update("m", 125_100, 100, 3.0) == (1.0, 0.0)


def test_signal_ring_prunes_but_keeps_one_point_before_cutoff():
    ring = SignalRing(window_s=60)
    for ts in (0, 10, 20, 70, 75):
        ring.append("m", ts, -60)
    # cutoff at 75-60=15: 0 dropped, 10 kept as the edge anchor.
    assert [p[0] for p in ring.snapshot("m", 75)] == [10, 20, 70, 75]


def test_signal_ring_skips_non_negative_readings():
    ring = SignalRing()
    ring.append("m", 1, 0)
    ring.append("m", 2, -61)
    assert ring.snapshot("m", 2) == [[2, -61]]


def test_signal_ring_follows_key_not_iface():
    ring = SignalRing()
    ring.append("28:94:01:bb:f8:96", 1, -50)
    # the netdev got renamed; the key (perm MAC) did not
    ring.append("28:94:01:bb:f8:96", 2, -52)
    assert len(ring.snapshot("28:94:01:bb:f8:96", 2)) == 2
    assert ring.snapshot("other", 2) == []


def test_radio_colors_persist_first_come_first_served(tmp_path):
    path = tmp_path / "radios.json"
    colors = RadioColors(path)
    assert colors.index("a") == 0
    assert colors.index("b") == 1
    assert colors.index("a") == 0
    again = RadioColors(path)
    assert again.index("b") == 1 and again.index("c") == 2
    assert colors.index("") == -1


def test_radio_colors_wrap_palette(tmp_path):
    colors = RadioColors(tmp_path / "r.json")
    slots = [colors.index(str(i)) for i in range(7)]
    assert slots == [0, 1, 2, 3, 4, 0, 1]
