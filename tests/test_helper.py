"""wifimimo-helper: argv validation, multipath planning, internal-card control."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

import wifimimo_shared as shared

from sysfs_fixtures import add_pci_radio

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("wifimimo_helper", str(ROOT / "wifimimo-helper"))
_spec = importlib.util.spec_from_loader("wifimimo_helper", _loader)
helper = importlib.util.module_from_spec(_spec)
sys.modules["wifimimo_helper"] = helper  # dataclasses look the module up by name
_loader.exec_module(helper)


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv,expected", [
    (["multipath", "enable"], (False, "multipath", "enable")),
    (["--dry-run", "internal", "sync-rules"], (True, "internal", "sync-rules")),
    (["multipath", "status"], (False, "multipath", "status")),
])
def test_parse_argv_accepts(argv, expected):
    assert helper.parse_argv(argv) == expected


@pytest.mark.parametrize("argv", [
    [], ["multipath"], ["multipath", "apply", "wifi0"], ["multipath", "reboot"],
    ["internal", "enable;rm -rf /"], ["--dry-run"], ["disk", "enable"],
    ["multipath", "--dry-run", "apply"],
])
def test_parse_argv_rejects(argv):
    with pytest.raises(helper.UsageError):
        helper.parse_argv(argv)


# ---------------------------------------------------------------------------
# multipath planning
# ---------------------------------------------------------------------------

MEMBERS = [
    {"iface": "wifi2", "perm_mac": "28:94:01:b7:b9:1a", "ip": "172.20.179.66",
     "subnet": "172.20.176.0/22", "gw": "172.20.176.1"},
    {"iface": "wifi1", "perm_mac": "28:94:01:bb:f8:96", "ip": "172.20.179.76",
     "subnet": "172.20.176.0/22", "gw": "172.20.176.1"},
]
LEGACY_RULES = [
    {"priority": 0, "src": "all", "table": "local"},
    {"priority": 5270, "src": "all", "table": "52"},
    {"priority": 5300, "src": "172.20.179.201", "table": "101"},
    {"priority": 5301, "src": "172.20.179.76", "table": "102"},
    {"priority": 32766, "src": "all", "table": "main"},
]
GOOD_SYSCTLS = {k: v for k, v in helper.desired_sysctls(MEMBERS).items()}


def argvs(plan):
    return [" ".join(step[1]) for step in plan if step[0] == "cmd"]


def test_plan_apply_order_and_content():
    plan = helper.plan_apply(MEMBERS, LEGACY_RULES, [], {})
    cmds = argvs(plan)
    # legacy rules go first, before any table is reassigned
    assert cmds[0] == "ip -4 rule del priority 5300 from 172.20.179.201 lookup 101"
    assert cmds[1] == "ip -4 rule del priority 5301 from 172.20.179.76 lookup 102"
    assert "ip -4 route replace default via 172.20.176.1 dev wifi2 table 101 proto 211" in cmds
    assert "ip -4 rule add priority 32001 from 172.20.179.66 lookup 101 protocol 211" in cmds
    assert "ip -4 rule add priority 32002 from 172.20.179.76 lookup 102 protocol 211" in cmds
    swap = ("ip -4 route replace default table 100 proto 211 "
            "nexthop via 172.20.176.1 dev wifi2 weight 1 nexthop via 172.20.176.1 dev wifi1 weight 1")
    assert swap in cmds
    # the swap comes after every table build, rule add and sysctl
    swap_at = [i for i, s in enumerate(plan) if s[0] == "cmd" and " ".join(s[1]) == swap][0]
    for i, step in enumerate(plan):
        if step[0] == "sysctl" or (step[0] == "cmd" and (" add " in " ".join(step[1])
                                                          or " replace " in " ".join(step[1]))):
            assert i <= swap_at


def test_suppress_rule_precedes_radio_and_ecmp_rules():
    cmds = argvs(helper.plan_apply(MEMBERS, [], [], GOOD_SYSCTLS))
    adds = [c for c in cmds if " rule add " in c]
    assert adds[0].startswith("ip -4 rule add priority 32000 lookup main suppress_prefixlength 0")
    assert adds[-1] == "ip -4 rule add priority 32090 lookup 100 protocol 211"


def _as_live(plan_members):
    """The ip-rule / table state that a successful apply leaves behind."""
    rules = [dict(r, protocol="wifimimo") for r in helper.desired_rules(plan_members)]
    routes = []
    for i, m in enumerate(plan_members):
        table = str(shared.RADIO_TABLE_BASE + i)
        routes.append({"dst": m["subnet"], "dev": m["iface"], "table": table, "protocol": "wifimimo"})
        routes.append({"dst": "default", "gateway": m["gw"], "dev": m["iface"], "table": table,
                       "protocol": "wifimimo"})
    routes.append({"dst": "default", "table": "100", "protocol": "wifimimo",
                   "nexthops": [{"dev": m["iface"], "gateway": m["gw"]} for m in plan_members]})
    return rules, routes


def test_plan_apply_is_idempotent():
    rules, routes = _as_live(MEMBERS)
    plan = helper.plan_apply(MEMBERS, rules, routes, GOOD_SYSCTLS)
    kinds = {step[0] for step in plan}
    assert kinds == {"cmd"}
    cmds = argvs(plan)
    assert not [c for c in cmds if " rule " in c]
    assert not [c for c in cmds if " del " in c or " flush " in c]


def test_radio_leaving_cleans_its_slot():
    rules, routes = _as_live(MEMBERS + [{"iface": "wifi0", "perm_mac": "3c:3b:ad:16:b7:30",
                                         "ip": "172.20.179.201", "subnet": "172.20.176.0/22",
                                         "gw": "172.20.176.1"}])
    cmds = argvs(helper.plan_apply(MEMBERS, rules, routes, GOOD_SYSCTLS))
    assert "ip -4 rule del priority 32003 from 172.20.179.201 lookup 103" in cmds
    assert "ip -4 route del default table 103 dev wifi0" in cmds


def test_teardown_removes_ecmp_route_first_and_restores_sysctls_last():
    rules, routes = _as_live(MEMBERS)
    plan = helper.plan_teardown(rules + LEGACY_RULES, routes)
    assert " ".join(plan[0][1]) == "ip -4 route del default table 100"
    assert plan[-1] == ("restore-sysctls",)
    cmds = argvs(plan)
    assert "ip -4 rule del priority 5300 from 172.20.179.201 lookup 101" in cmds
    # 101 / 102 are adopted from the legacy rules, so flushed whole
    assert "ip -4 route flush table 101" in cmds and "ip -4 route flush table 102" in cmds
    # never touches rules it doesn't own
    assert not [c for c in cmds if "5270" in c or "32766" in c]


def test_unhealthy_radio_gets_routing_but_no_nexthop():
    # Live 2026-09-24: a radio still waiting on NM's connectivity check had
    # strict rp_filter and no source rule, so its replies were dropped and
    # the check could never pass. It must get its table, rule and loose
    # rp_filter; only the nexthop waits for health.
    third = {"iface": "wifi0", "perm_mac": "3c:3b:ad:16:b7:30", "ip": "172.20.179.201",
             "subnet": "172.20.176.0/22", "gw": "172.20.176.1"}
    routable = MEMBERS + [third]
    plan = helper.plan_apply(routable, [], [], {}, ecmp=MEMBERS)
    cmds = argvs(plan)
    assert "ip -4 rule add priority 32003 from 172.20.179.201 lookup 103 protocol 211" in cmds
    assert ("sysctl", "net/ipv4/conf/wifi0/rp_filter", "2") in plan
    swap = [c for c in cmds if c.startswith("ip -4 route replace default table 100")][0]
    assert "dev wifi0" not in swap and "dev wifi1" in swap and "dev wifi2" in swap


def test_one_healthy_radio_gets_all_new_flows():
    # failover: a single-nexthop route beats NM's default, which may point
    # at an associated-but-broken radio
    rules, routes = _as_live(MEMBERS)
    plan = helper.plan_apply(MEMBERS, rules, routes, GOOD_SYSCTLS, ecmp=MEMBERS[1:])
    cmds = argvs(plan)
    assert ("ip -4 route replace default table 100 proto 211 "
            "nexthop via 172.20.176.1 dev wifi1 weight 1") in cmds
    assert not [c for c in cmds if " rule del " in c]      # radio rules stay


def test_no_healthy_radio_removes_the_route_but_keeps_radio_routing():
    rules, routes = _as_live(MEMBERS)
    plan = helper.plan_apply(MEMBERS, rules, routes, GOOD_SYSCTLS, ecmp=[])
    cmds = argvs(plan)
    assert "ip -4 route del default table 100" in cmds
    assert not [c for c in cmds if "replace default table 100" in c]
    assert not [c for c in cmds if " rule del " in c]


def test_teardown_leaves_foreign_routes_in_radio_tables():
    # Runs every 20 s from the dead-man timer while multipath is off: a VPN's
    # policy routing in 101-116 must survive it.
    rules, routes = _as_live(MEMBERS[:1])
    routes.append({"dst": "default", "dev": "wg0", "table": "101", "protocol": "static"})
    routes.append({"dst": "10.8.0.0/24", "dev": "wg0", "table": "105", "protocol": "boot"})
    cmds = argvs(helper.plan_teardown(rules, routes))
    assert "ip -4 route flush table 101 proto 211" in cmds
    assert not [c for c in cmds if "table 105" in c]
    assert not [c for c in cmds if c.startswith("ip -4 route flush table 101") and "proto" not in c]


def test_teardown_flushes_tables_adopted_from_legacy_rules_whole():
    routes = [{"dst": "default", "dev": "wifi1", "table": "102", "protocol": "boot"}]
    cmds = argvs(helper.plan_teardown(LEGACY_RULES, routes))
    assert "ip -4 route flush table 102" in cmds


def test_apply_cleanup_never_deletes_foreign_routes():
    rules, routes = _as_live(MEMBERS)
    routes.append({"dst": "10.8.0.0/24", "dev": "wg0", "table": "103", "protocol": "static"})
    cmds = argvs(helper.plan_apply(MEMBERS, rules, routes, GOOD_SYSCTLS))
    assert not [c for c in cmds if "table 103" in c]


def test_foreign_route_in_a_radio_table_refuses_apply_but_not_teardown():
    routes = [{"dst": "default", "dev": "wg0", "table": "101", "protocol": "static"}]
    assert helper.foreign_conflicts([], routes)
    assert not helper.foreign_conflicts([], routes, radio_tables=False)
    # ...unless a pre-v1.0 rule points at it: that table is being adopted
    assert not helper.foreign_conflicts(LEGACY_RULES, routes)


def test_foreign_table_100_route_is_refused():
    routes = [{"dst": "default", "table": "100", "protocol": "static", "dev": "eth0"}]
    assert helper.foreign_conflicts([], routes)
    assert helper.foreign_conflicts([], routes, radio_tables=False)


def test_foreign_rule_at_owned_priority_is_refused():
    rules = [{"priority": 32090, "src": "all", "table": "200", "protocol": "static"}]
    assert helper.foreign_conflicts(rules, [])


# ---------------------------------------------------------------------------
# execute() semantics
# ---------------------------------------------------------------------------


class FakeCtx(helper.Ctx):
    def __init__(self, tmp_path, fail_on=None):
        super().__init__(proc_root=tmp_path / "proc", run_dir=tmp_path / "run",
                         etc_dir=tmp_path / "etc", sys_root=tmp_path / "sys")
        self.fail_on = fail_on or ""
        self.ran = []

    def mutate(self, argv):
        line = " ".join(argv)
        self.ran.append(line)
        if self.fail_on and self.fail_on in line:
            return 2, "RTNETLINK answers: Invalid argument"
        return 0, ""


def test_apply_stops_before_swap_on_error(tmp_path):
    ctx = FakeCtx(tmp_path, fail_on="rule add priority 32001")
    plan = helper.plan_apply(MEMBERS, [], [], GOOD_SYSCTLS)
    errors = helper.execute(ctx, plan)
    assert errors
    assert not [line for line in ctx.ran if "table 100" in line]


def test_teardown_is_best_effort(tmp_path):
    ctx = FakeCtx(tmp_path, fail_on="route del default table 100")
    rules, routes = _as_live(MEMBERS)
    helper.execute(ctx, helper.plan_teardown(rules, routes))
    assert any("rule del priority 32090" in line for line in ctx.ran)


def test_sysctls_saved_once_and_restored(tmp_path):
    ctx = FakeCtx(tmp_path)
    key = "net/ipv4/conf/wifi1/rp_filter"
    path = tmp_path / "proc" / "sys" / key
    path.parent.mkdir(parents=True)
    path.write_text("1\n")
    helper.execute(ctx, [("sysctl", key, "2")])
    assert path.read_text().strip() == "2"
    helper.execute(ctx, [("sysctl", key, "2")])  # original must not be overwritten by "2"
    helper.execute(ctx, [("restore-sysctls",)])
    assert path.read_text().strip() == "1"
    assert not (tmp_path / "run" / "sysctl-saved.json").exists()


# ---------------------------------------------------------------------------
# internal card
# ---------------------------------------------------------------------------

ENTRIES = shared.parse_internal_conf("14c3:7925 mt7925e 0000:00:02.2\n")


def test_render_udev_rules_snapshot():
    text = helper.render_udev_rules(ENTRIES, "/usr/bin/modprobe")
    assert text == (
        "# Generated by wifimimo-helper from /etc/wifimimo/internal.conf. Do not edit.\n"
        'ACTION!="add", GOTO="wifimimo_internal_end"\n'
        'SUBSYSTEM!="pci", GOTO="wifimimo_internal_end"\n'
        'ATTR{vendor}=="0x14c3", ATTR{device}=="0x7925", TEST!="/etc/wifimimo/internal-enabled", '
        'ATTR{remove}="1", GOTO="wifimimo_internal_end"\n'
        'ATTR{vendor}=="0x14c3", ATTR{device}=="0x7925", TEST=="/etc/wifimimo/internal-enabled", '
        'RUN+="/usr/bin/modprobe mt7925e"\n'
        'LABEL="wifimimo_internal_end"\n'
    )


@pytest.mark.parametrize("line", [
    "14c3:792 mt7925e", "14c3:7925 mt7925e; rm", "14c3:7925 MT/7925", "14c3:7925",
    "14c3:7925 mt7925e 0000:00:02", "14c3:7925 mt7925e 0000:00:02.2 extra",
    'x" RUN+="/bin/sh',
])
def test_internal_conf_rejects(line):
    with pytest.raises(shared.ConfigError):
        shared.parse_internal_conf(line)


def test_internal_conf_accepts_comments_and_optional_bridge():
    entries = shared.parse_internal_conf("# card\n\n14C3:7925 mt7925e  # upper-case ok\n")
    assert entries == [{"vendor": "14c3", "device": "7925", "id": "14c3:7925",
                        "driver": "mt7925e", "bridge": ""}]


class InternalCtx(helper.Ctx):
    def __init__(self, tmp_path):
        super().__init__(sys_root=tmp_path / "sys", etc_dir=tmp_path / "etc",
                         run_dir=tmp_path / "run", udev_rule=tmp_path / "rules" / "70-w.rules",
                         udev_rules_dir=tmp_path / "rules")
        (tmp_path / "rules").mkdir()
        self.etc_dir.mkdir()
        (self.etc_dir / "internal.conf").write_text("14c3:7925 mt7925e 0000:00:02.2\n")
        self.writes, self.cmds = [], []

    def write(self, path, value):
        self.writes.append((str(path), value))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def mutate(self, argv):
        self.cmds.append(" ".join(argv))
        return 0, ""


def test_internal_disable_removes_device_and_clears_flag(tmp_path):
    ctx = InternalCtx(tmp_path)
    add_pci_radio(ctx.sys_root)
    (ctx.etc_dir / "internal-enabled").touch()
    code, result = helper.cmd_internal(ctx, "disable")
    assert code == 0
    assert not (ctx.etc_dir / "internal-enabled").exists()
    assert (str(ctx.sys_root / "bus/pci/devices/0000:01:00.0/remove"), "1") in ctx.writes
    assert not [c for c in ctx.cmds if c.startswith("rmmod")]
    assert ctx.udev_rule.exists()


def test_internal_enable_rescans_bridge_and_loads_driver(tmp_path):
    ctx = InternalCtx(tmp_path)
    ctx.settle_s = 0
    bridge = ctx.sys_root / "bus" / "pci" / "devices" / "0000:00:02.2"
    bridge.mkdir(parents=True)
    (bridge / "rescan").write_text("")
    helper.cmd_internal(ctx, "enable")
    assert (ctx.etc_dir / "internal-enabled").exists()
    assert (str(bridge / "rescan"), "1") in ctx.writes
    assert "modprobe mt7925e" in ctx.cmds


def test_internal_unconfigured_is_exit_3(tmp_path):
    ctx = InternalCtx(tmp_path)
    (ctx.etc_dir / "internal.conf").unlink()
    assert helper.cmd_internal(ctx, "enable")[0] == helper.EXIT_UNCONFIGURED


def test_legacy_rule_detected(tmp_path):
    ctx = InternalCtx(tmp_path)
    (ctx.udev_rules_dir / "99-remove-mt7925-pci.rules").write_text(
        'ACTION=="add", SUBSYSTEM=="pci", ATTR{vendor}=="0x14c3", ATTR{device}=="0x7925", ATTR{remove}="1"\n')
    status = helper.internal_status(ctx, ENTRIES)
    assert status["legacy_rules"] == [str(ctx.udev_rules_dir / "99-remove-mt7925-pci.rules")]


# ---------------------------------------------------------------------------
# dead-man check (root timer)
# ---------------------------------------------------------------------------


def test_check_is_an_accepted_verb():
    assert helper.parse_argv(["multipath", "check"]) == (False, "multipath", "check")


def _live_snapshot(monkeypatch):
    rules, routes = _as_live(MEMBERS)
    monkeypatch.setattr(helper, "multipath_snapshot", lambda ctx: (rules, routes))


def test_check_hands_routing_back_when_applying_raises(tmp_path, monkeypatch):
    ctx = FakeCtx(tmp_path)
    _live_snapshot(monkeypatch)

    def boom(ctx, desired):
        raise RuntimeError("ip -j rule show returned garbage")

    monkeypatch.setattr(helper, "multipath_apply", boom)
    result = helper.multipath_check(ctx, True)
    assert ctx.ran[0] == "ip -4 route del default table 100"      # ECMP route first
    assert any("rule del priority 32090" in line for line in ctx.ran)
    assert result["reason"].startswith("dead-man: routing handed back to NetworkManager")
    assert "RuntimeError" in result["reason"] and not result["active"] and not result["quiet"]


def test_check_hands_routing_back_when_applying_fails(tmp_path, monkeypatch):
    ctx = FakeCtx(tmp_path)
    _live_snapshot(monkeypatch)
    monkeypatch.setattr(helper, "multipath_apply", lambda ctx, desired: {
        "desired": True, "active": False, "members": [], "excluded": [], "reason": "",
        "error": "RTNETLINK answers: No such process", "applied_at": 0, "plan": [("cmd", ["x"])]})
    result = helper.multipath_check(ctx, True)
    assert "ip -4 route del default table 100" in ctx.ran
    assert "No such process" in result["reason"]


def test_a_refused_check_hands_back_only_our_own_routing(tmp_path, monkeypatch):
    # Another tool's rule at our ECMP priority: apply refuses, and the
    # dead-man's teardown must still remove ours, but never theirs.
    ctx = FakeCtx(tmp_path)
    rules, routes = _as_live(MEMBERS)
    rules.append({"priority": 32090, "src": "all", "table": "200", "protocol": "static"})
    routes.append({"dst": "default", "dev": "wg0", "table": "101", "protocol": "static"})
    monkeypatch.setattr(helper, "multipath_snapshot", lambda ctx: (rules, routes))
    result = helper.multipath_check(ctx, True)
    assert "Refused" in result["reason"]
    assert "ip -4 rule del priority 32090 lookup 100" in ctx.ran
    assert not [line for line in ctx.ran if "lookup 200" in line]
    assert "ip -4 route flush table 101 proto 211" in ctx.ran
    assert not [line for line in ctx.ran if line == "ip -4 route flush table 101"]


def test_a_failure_that_persists_is_logged_once(tmp_path, monkeypatch):
    ctx = FakeCtx(tmp_path)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / shared.MULTIPATH_FLAG.name).touch()
    _live_snapshot(monkeypatch)

    def refused(ctx, desired):
        raise helper.Refused("rule 32090 belongs to protocol static")

    monkeypatch.setattr(helper, "multipath_apply", refused)
    _, result = helper.cmd_multipath(ctx, "check")
    assert result["quiet"] is False and "dead-man" in result["reason"]
    _, result = helper.cmd_multipath(ctx, "check")
    assert result["quiet"] is True


def test_a_healthy_check_is_quiet_and_touches_nothing(tmp_path, monkeypatch):
    ctx = FakeCtx(tmp_path)
    monkeypatch.setattr(helper, "multipath_apply", lambda ctx, desired: {
        "desired": True, "active": True, "members": ["wifi1", "wifi2"], "excluded": [],
        "reason": "", "error": "", "applied_at": 0, "plan": []})
    result = helper.multipath_check(ctx, True)
    assert result["quiet"] and ctx.ran == []


def test_the_20s_check_logs_and_writes_status_only_when_something_changed(tmp_path, monkeypatch):
    # apply always re-issues idempotent `replace`s, so a non-empty plan is not a change
    ctx = FakeCtx(tmp_path)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / shared.MULTIPATH_FLAG.name).touch()
    members = ["wifi2", "wifi1"]
    monkeypatch.setattr(helper, "multipath_apply", lambda ctx, desired: {
        "desired": True, "active": True, "members": list(members), "excluded": [],
        "reason": "", "error": "", "applied_at": 0, "plan": [("cmd", ["ip", "x"])]})
    status = tmp_path / "run" / shared.MULTIPATH_STATUS.name
    _, result = helper.cmd_multipath(ctx, "check")
    assert result["quiet"] is False and status.exists()            # first run: no baseline
    status.write_text(status.read_text())
    mtime = status.stat().st_mtime_ns
    _, result = helper.cmd_multipath(ctx, "check")
    assert result["quiet"] is True and status.stat().st_mtime_ns == mtime
    members.remove("wifi1")                                       # a radio dropped out
    _, result = helper.cmd_multipath(ctx, "check")
    assert result["quiet"] is False and '"wifi1"' not in status.read_text()
