"""`ip -j` parsers and the read-only multipath / internal-card status views.

Fixtures follow the real iproute2 JSON shape captured on 2026-09-24,
trimmed to the wifi interfaces.
"""

import json

import wifimimo_shared as shared
from wifimimo_radio import (
    parse_default_routes,
    parse_ip_addr,
    parse_multipath_live,
    read_internal_status,
    read_multipath_status,
)

from sysfs_fixtures import add_pci_radio

ADDR = [
    {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host"}]},
    {"ifname": "wifi1", "addr_info": [{"family": "inet", "local": "172.20.179.76", "prefixlen": 22,
                                       "scope": "global", "dynamic": True}]},
    {"ifname": "wifi2", "addr_info": [{"family": "inet", "local": "172.20.179.66", "prefixlen": 22,
                                       "scope": "global"}]},
]
DEFAULTS_NM = [
    {"dst": "default", "gateway": "172.20.176.1", "dev": "wifi1", "protocol": "dhcp", "metric": 610},
    {"dst": "default", "gateway": "172.20.176.1", "dev": "wifi2", "protocol": "dhcp", "metric": 700},
]
RULES_ACTIVE = [
    {"priority": 0, "src": "all", "table": "local"},
    {"priority": 5270, "src": "all", "table": "52"},
    {"priority": 32000, "src": "all", "table": "main", "suppress_prefixlen": 0, "protocol": "wifimimo"},
    {"priority": 32001, "src": "172.20.179.76", "table": "101", "protocol": "wifimimo"},
    {"priority": 32002, "src": "172.20.179.66", "table": "102", "protocol": "wifimimo"},
    {"priority": 32090, "src": "all", "table": "100", "protocol": "wifimimo"},
    {"priority": 32766, "src": "all", "table": "main"},
]
TABLE100 = [{"dst": "default", "protocol": "wifimimo", "flags": [], "nexthops": [
    {"gateway": "172.20.176.1", "dev": "wifi1", "weight": 1, "flags": []},
    {"gateway": "172.20.176.1", "dev": "wifi2", "weight": 1, "flags": []},
]}]


def test_parse_ip_addr_global_only():
    assert parse_ip_addr(ADDR) == {
        "wifi1": {"ipv4": "172.20.179.76", "prefixlen": 22, "subnet": "172.20.176.0/22"},
        "wifi2": {"ipv4": "172.20.179.66", "prefixlen": 22, "subnet": "172.20.176.0/22"},
    }
    assert parse_ip_addr("garbage") == {}


def test_parse_default_routes_single_and_multipath():
    assert parse_default_routes(DEFAULTS_NM) == {"wifi1": "172.20.176.1", "wifi2": "172.20.176.1"}
    assert parse_default_routes(TABLE100) == {"wifi1": "172.20.176.1", "wifi2": "172.20.176.1"}


def test_multipath_live_active():
    assert parse_multipath_live(RULES_ACTIVE, TABLE100) == {"active": True, "members": ["wifi1", "wifi2"]}


def test_multipath_live_route_without_rules_is_inactive():
    assert parse_multipath_live(RULES_ACTIVE[:2], TABLE100)["active"] is False


def test_multipath_live_ignores_foreign_routes():
    foreign = [{"dst": "default", "protocol": "static", "gateway": "10.0.0.1", "dev": "eth0"}]
    assert parse_multipath_live(RULES_ACTIVE, foreign) == {"active": False, "members": []}


def test_numeric_proto_is_recognised():
    route = [dict(TABLE100[0], protocol=str(shared.RT_PROTO))]
    assert parse_multipath_live(RULES_ACTIVE, route)["active"] is True


def test_multipath_status_merges_helper_result(tmp_path):
    etc, run = tmp_path / "etc", tmp_path / "run"
    etc.mkdir(), run.mkdir()
    (etc / "multipath-enabled").touch()
    (run / "multipath.json").write_text(json.dumps({
        "reason": "needs ≥2 radios", "excluded": [{"iface": "wifi0", "reason": "captive portal"}],
    }))
    status = read_multipath_status(etc, run, live={"active": False, "members": []})
    assert status["desired"] is True and status["active"] is False
    assert status["reason"] == "needs ≥2 radios"
    assert status["excluded"][0]["iface"] == "wifi0"


def test_multipath_status_off_has_no_reason(tmp_path):
    (tmp_path / "multipath.json").write_text(json.dumps({"reason": "stale"}))
    status = read_multipath_status(tmp_path, tmp_path, live={"active": False, "members": []})
    assert status["desired"] is False and status["reason"] == ""


def test_internal_status_unmanaged(tmp_path):
    assert read_internal_status(tmp_path, tmp_path)["managed"] is False


def test_internal_status_present_and_absent(tmp_path):
    sys_root, etc = tmp_path / "sys", tmp_path / "etc"
    etc.mkdir()
    (etc / "internal.conf").write_text("14c3:7925 mt7925e 0000:00:02.2\n")
    status = read_internal_status(sys_root, etc)
    assert status["managed"] and not status["present"] and not status["desired"]
    add_pci_radio(sys_root)
    (etc / "internal-enabled").touch()
    status = read_internal_status(sys_root, etc)
    assert status["present"] and status["bound"] and status["desired"]
    assert status["iface"] == "wifi0" and status["pci_addr"] == "0000:01:00.0"
