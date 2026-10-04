"""Bus / port detection from sysfs, and the USB-speed health flags."""

from wifimimo_radio import collect_device_info, device_flags

from sysfs_fixtures import add_pci_radio, add_usb_radio


def test_pci_radio(tmp_path):
    add_pci_radio(tmp_path)
    info = collect_device_info("wifi0", tmp_path)
    assert info["bus"] == "pci"
    assert info["driver"] == "mt7925e"
    assert info["dev_id"] == "14c3:7925"
    assert info["dev_path"] == "0000:01:00.0"
    assert info["perm_mac"] == "3c:3b:ad:16:b7:30"
    assert info["usb_speed_mbps"] == 0


def test_usb_superspeed_is_clean(tmp_path):
    add_usb_radio(tmp_path, "wifi1", bus_num=8, port=1, speed=5000,
                  vendor="0846", product="9072", driver="mt7925u",
                  perm_mac="28:94:01:bb:f8:96")
    info = collect_device_info("wifi1", tmp_path)
    assert info["bus"] == "usb"
    assert info["dev_id"] == "0846:9072"
    assert info["dev_path"] == "8-1"
    assert info["usb_speed_mbps"] == 5000
    assert info["usb_port_usb3"] is True
    assert device_flags(info) == []


def test_usb3_stick_at_high_speed_on_peered_port_is_flagged(tmp_path):
    # The 2026-09-24 A8000 case: a sloppy replug landed it on the USB 2
    # half (bus 7) of a USB 3 port.
    add_usb_radio(tmp_path, "wifi2", bus_num=7, port=1, speed=480,
                  vendor="0846", product="9060", driver="mt7921u",
                  perm_mac="28:94:01:b7:b9:1a", peer=True)
    info = collect_device_info("wifi2", tmp_path)
    flags = device_flags(info)
    assert [f["code"] for f in flags] == ["usb2_on_usb3_port"]
    assert flags[0]["severity"] == "warn"


def test_usb2_only_port_is_info_not_warning(tmp_path):
    add_usb_radio(tmp_path, "wifi2", bus_num=3, port=1, speed=480,
                  vendor="0846", product="9060", driver="mt7921u",
                  perm_mac="28:94:01:b7:b9:1a", peer=False)
    flags = device_flags(collect_device_info("wifi2", tmp_path))
    assert [(f["code"], f["severity"]) for f in flags] == [("usb3_stick_on_usb2_port", "info")]


def test_port_found_behind_external_hub(tmp_path):
    add_usb_radio(tmp_path, "wifi3", bus_num=7, port=1, hub_port=2, speed=480,
                  vendor="0846", product="9060", driver="mt7921u",
                  perm_mac="28:94:01:b7:b9:1a", peer=True)
    info = collect_device_info("wifi3", tmp_path)
    assert info["dev_path"] == "7-1.2"
    assert info["usb_port_usb3"] is True
    assert [f["code"] for f in device_flags(info)] == ["usb2_on_usb3_port"]


def test_usb2_only_chip_is_never_flagged(tmp_path):
    add_usb_radio(tmp_path, "wifi4", bus_num=7, port=1, speed=480,
                  vendor="148f", product="7601", driver="mt7601u",
                  perm_mac="00:11:22:33:44:55", peer=True)
    assert device_flags(collect_device_info("wifi4", tmp_path)) == []


def test_missing_speed_file_does_not_crash(tmp_path):
    udev = add_usb_radio(tmp_path, "wifi1", bus_num=8, port=1, speed=5000,
                         vendor="0846", product="9072", driver="mt7925u",
                         perm_mac="28:94:01:bb:f8:96")
    (udev / "speed").unlink()
    info = collect_device_info("wifi1", tmp_path)
    assert info["usb_speed_mbps"] == 0
    assert device_flags(info) == []


def test_perm_mac_wins_over_randomized_address(tmp_path):
    add_pci_radio(tmp_path)
    (tmp_path / "class" / "net" / "wifi0" / "address").write_text("8a:82:17:a5:0b:76\n")
    info = collect_device_info("wifi0", tmp_path)
    assert info["perm_mac"] == "3c:3b:ad:16:b7:30"
    assert info["mac"] == "8a:82:17:a5:0b:76"


def test_missing_iface_returns_empty_info(tmp_path):
    info = collect_device_info("nope", tmp_path)
    assert info["bus"] == "" and info["perm_mac"] == ""
