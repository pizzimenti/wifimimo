"""Friendly card names: override > known model > Built-in > vendor + chip."""

import json
import os

from wifimimo_radio import NameOverrides, card_name, lookup_usb_vendor


USB_IDS = """\
# comment line
0846  NetGear, Inc.
\t9060  Some product
148f  Ralink Technology, Corp.
"""


def ids_file(tmp_path):
    path = tmp_path / "usb.ids"
    path.write_text(USB_IDS, encoding="utf-8")
    return (path,)


def test_known_models():
    assert card_name({"bus": "usb", "dev_id": "0846:9072"}) == "A9000"
    assert card_name({"bus": "usb", "dev_id": "0846:9060"}) == "A8000"


def test_pci_card_is_builtin():
    assert card_name({"bus": "pci", "dev_id": "14c3:7925", "driver": "mt7925e"}) == "Built-in"


def test_override_wins_over_known_model():
    info = {"bus": "usb", "dev_id": "0846:9072", "perm_mac": "28:94:01:bb:f8:96"}
    assert card_name(info, {"28:94:01:bb:f8:96": "Travel stick"}) == "Travel stick"


def test_unknown_usb_uses_vendor_and_chip(tmp_path):
    info = {"bus": "usb", "dev_id": "148f:7601", "driver": "mt7601u"}
    assert card_name(info, usb_ids_paths=ids_file(tmp_path)) == "Ralink Technology MT7601U"


def test_unknown_usb_without_usb_ids_falls_back_to_chip(tmp_path):
    info = {"bus": "usb", "dev_id": "abcd:1234", "driver": "rtw89_8852au"}
    assert card_name(info, usb_ids_paths=(tmp_path / "missing",)) == "RTW89_8852AU"


def test_vendor_lookup_ignores_product_lines(tmp_path):
    assert lookup_usb_vendor("0846", ids_file(tmp_path)) == "NetGear"
    assert lookup_usb_vendor("9060", ids_file(tmp_path)) == ""


def test_name_overrides_reload_and_reject_junk(tmp_path):
    path = tmp_path / "names.json"
    overrides = NameOverrides(path)
    assert overrides.get() == {}
    path.write_text(json.dumps({"28:94:01:BB:F8:96": "Stick", "x": 5, "y": "  "}))
    assert overrides.get() == {"28:94:01:bb:f8:96": "Stick"}
    path.write_text("{not json")
    os.utime(path, (1, 1))
    assert overrides.get() == {}
