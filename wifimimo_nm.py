"""NetworkManager "follow": one click in the Plasma network applet drives
every radio, so multipath needs no per-card profile copies.

Runs inside the unprivileged daemon, as the desktop user, so NetworkManager
asks the user's own secret agent (plasma-nm / KWallet) for passwords.
Nothing persistent is changed in NM: profiles are only ever modified with
`--temporary` (in-memory), and never created.

Model
-----
* The **leader** is the most recent profile the *user* activated on any
  radio (an activation wifimimo didn't start itself).
* Every other managed wifi device that can see the leader's SSID is joined
  to the same profile with `connection up <uuid> ifname <dev>`, preferring
  an access point on a channel no other radio is using.
* A radio that NetworkManager drops back to its previous profile within
  FLAP_WINDOW_S is not a new leader, and isn't retried until the window
  passes (no fight with NM's autoconnect).
* When the user disconnects the leader (reason 39), the followers are
  brought down with it.

`plan_follow` is pure (snapshot + memory -> actions); `Follower` does the
nmcli I/O around it.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import wifimimo_shared as shared

STATE_DIR = Path.home() / ".local" / "state" / "wifimimo"
FOLLOW_PATH = STATE_DIR / "follow.json"
NM_CONF_PATHS = (Path("/etc/NetworkManager/NetworkManager.conf"),
                 Path("/etc/NetworkManager/conf.d"),
                 Path("/usr/lib/NetworkManager/conf.d"),
                 Path("/run/NetworkManager/conf.d"))

FLAP_WINDOW_S = 600.0
MIN_SIGNAL_PCT = 40          # nmcli SIGNAL is 0-100; ~40 is roughly -70 dBm
REASON_USER_REQUESTED = 39
STATE_DISCONNECTED = 30
STATE_ACTIVATED = 100
MULTI_OK = {"manual-multiple", "multiple", "2", "3"}


# ---------------------------------------------------------------------------
# nmcli terse parsing
# ---------------------------------------------------------------------------


def split_terse(line: str) -> list[str]:
    """Split an `nmcli -t` line on unescaped ':' and unescape '\\:' / '\\\\'."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def _lead_int(value: str, default: int = 0) -> int:
    match = re.match(r"\s*(-?\d+)", value or "")
    return int(match.group(1)) if match else default


def parse_dev_show(text: str) -> list[dict]:
    """`nmcli -t -f GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.REASON,
    GENERAL.CON-UUID dev show` -> list of devices."""
    devices, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                devices.append(cur)
                cur = {}
            continue
        key, _, value = line.partition(":")
        value = value.replace("\\:", ":")
        if key == "GENERAL.DEVICE":
            if cur:
                devices.append(cur)
            cur = {"device": value}
        elif key == "GENERAL.TYPE":
            cur["type"] = value
        elif key == "GENERAL.STATE":
            cur["state"] = _lead_int(value)
        elif key == "GENERAL.REASON":
            cur["reason"] = _lead_int(value)
        elif key == "GENERAL.CON-UUID":
            cur["uuid"] = value
    if cur:
        devices.append(cur)
    for dev in devices:
        dev.setdefault("type", "")
        dev.setdefault("state", 0)
        dev.setdefault("reason", 0)
        dev.setdefault("uuid", "")
    return devices


def parse_wifi_list(text: str) -> list[dict]:
    """`nmcli -t -f SSID,BSSID,CHAN,SIGNAL,SECURITY dev wifi list ...`"""
    out = []
    for line in text.splitlines():
        parts = split_terse(line)
        if len(parts) < 5:
            continue
        ssid, bssid, chan, signal, security = parts[:5]
        out.append({"ssid": ssid, "bssid": bssid.lower(), "chan": _lead_int(chan),
                    "signal": _lead_int(signal), "security": security})
    return out


def parse_profile(text: str) -> dict:
    """`nmcli -t -f <fields> connection show <uuid>` (key:value lines)."""
    info = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        info[key] = value.replace("\\:", ":")
    return {
        "uuid": info.get("connection.uuid", ""),
        "id": info.get("connection.id", ""),
        "type": info.get("connection.type", ""),
        "multi_connect": info.get("connection.multi-connect", ""),
        "interface_name": info.get("connection.interface-name", ""),
        "stable_id": info.get("connection.stable-id", ""),
        "ssid": info.get("802-11-wireless.ssid", ""),
        "mac_address": info.get("802-11-wireless.mac-address", ""),
        "cloned_mac": info.get("802-11-wireless.cloned-mac-address", ""),
        "key_mgmt": info.get("802-11-wireless-security.key-mgmt", ""),
        "route_table": info.get("ipv4.route-table", ""),
        "timestamp": _lead_int(info.get("connection.timestamp", "0")),
    }


PROFILE_FIELDS = ",".join([
    "connection.uuid", "connection.id", "connection.type", "connection.multi-connect",
    "connection.interface-name", "connection.stable-id", "connection.timestamp",
    "802-11-wireless.ssid", "802-11-wireless.mac-address",
    "802-11-wireless.cloned-mac-address", "802-11-wireless-security.key-mgmt",
    "ipv4.route-table",
])


def global_wifi_cloned_mac(paths=NM_CONF_PATHS) -> str:
    """wifi.cloned-mac-address default from NetworkManager.conf (last wins)."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.conf")))
        elif path.is_file():
            files.append(path)
    value = ""
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("wifi.cloned-mac-address"):
                value = line.split("=", 1)[-1].strip()
    return value


def followability(profile: dict, global_cloned: str = "") -> tuple[str, str, str]:
    """(code, severity, detail) explaining why a profile can't be followed, or ('','','')."""
    if profile.get("type") and profile["type"] != "802-11-wireless":
        return "not_wifi", "info", "Leader profile isn't a Wi-Fi profile."
    if profile.get("mac_address") or profile.get("interface_name"):
        return ("profile_locked", "warn",
                f"'{profile.get('id')}' is tied to one card (mac-address / interface-name), "
                "so other radios can't join it. Run wifimimo-nm-tidy to unify per-card copies.")
    if profile.get("route_table") not in ("", "0"):
        return ("custom_route_table", "info",
                f"'{profile.get('id')}' uses its own route table; multipath follow is skipped.")
    cloned = (profile.get("cloned_mac") or global_cloned or "preserve").lower()
    per_device_id = any(tok in profile.get("stable_id", "") for tok in ("${DEVICE}", "${MAC}"))
    if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", cloned) or cloned == "stable-ssid" or (
            cloned == "stable" and not per_device_id):
        return ("duplicate_mac", "crit",
                f"'{profile.get('id')}' clones MAC '{cloned}': every radio would share one "
                "address on the same network. Use preserve/permanent/random, or add "
                "${DEVICE} to connection.stable-id.")
    return "", "", ""


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def _flag(code: str, severity: str, title: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "title": title, "detail": detail}


FLAG_TITLES = {
    "profile_locked": "Profile tied to one card",
    "duplicate_mac": "Shared cloned MAC",
    "custom_route_table": "Custom route table",
    "not_wifi": "Not a Wi-Fi profile",
}


def pick_bssid(scan: list[dict], ssid: str, used_chans: set[int]) -> str:
    """Best AP for SSID on an unused channel, or '' to let NM choose.

    OWE transition-mode networks advertise an open beacon whose BSSID can't
    be joined directly (the encrypted twin is hidden), so never pin those.
    """
    candidates = [a for a in scan if a["ssid"] == ssid and a["signal"] >= MIN_SIGNAL_PCT]
    if not candidates or any("OWE" in a["security"] for a in candidates):
        return ""
    fresh = [a for a in candidates if a["chan"] not in used_chans]
    if not fresh:
        return ""
    return max(fresh, key=lambda a: a["signal"])["bssid"]


def plan_follow(devices: list[dict], memory: dict, now: float, lookup,
                primary_device: str = "") -> tuple[list[tuple], dict, dict]:
    """Decide what to do this poll.

    devices  : parse_dev_show() output
    memory   : persisted {"last": {dev: uuid}, "leader": {...}, "moves": {dev: {...}},
                          "modified": {uuid: original multi-connect}}
    lookup   : object with .profile(uuid) -> dict, .scan(dev) -> list,
               .global_cloned() -> str, .chan(dev) -> int (0 if unknown)
    Returns (actions, new_memory, status). Actions:
      ("multi", uuid, value) | ("up", uuid, dev, bssid) | ("down", uuid, dev)
    """
    memory = json.loads(json.dumps(memory or {}))
    first_poll = "last" not in memory
    last: dict = memory.setdefault("last", {})
    moves: dict = memory.setdefault("moves", {})
    memory.setdefault("modified", {})
    leader = memory.get("leader") or {}
    wifi = [d for d in devices if d.get("type") == "wifi" and d.get("state", 0) >= STATE_DISCONNECTED]
    by_dev = {d["device"]: d for d in wifi}
    actions: list[tuple] = []
    status = {"leader": {}, "followers": [], "skipped": {}, "iface_flags": {}}

    # 1. Who did the user just activate? (On the very first poll there is
    #    no baseline: record what's there instead of treating every radio
    #    as a brand-new user choice.)
    fresh_leader = False
    for dev in wifi:
        uuid, prev = dev.get("uuid", ""), last.get(dev["device"], "")
        if first_poll or not uuid or uuid == prev:
            continue
        move = moves.get(dev["device"])
        ours = move and move.get("target") == uuid
        fallback = move and move.get("previous") == uuid and now - move.get("at", 0) < FLAP_WINDOW_S
        if fallback:
            move["failed_at"] = now
            continue
        if not ours:
            leader = {"uuid": uuid, "device": dev["device"], "at": now}
            fresh_leader = True
    if not leader and primary_device in by_dev and by_dev[primary_device].get("uuid"):
        leader = {"uuid": by_dev[primary_device]["uuid"], "device": primary_device, "at": now}

    # 2. User disconnected the leader: take the followers down with it.
    lead_dev = by_dev.get(leader.get("device", ""))
    if leader and lead_dev and lead_dev.get("uuid") != leader["uuid"] \
            and lead_dev.get("state") == STATE_DISCONNECTED \
            and lead_dev.get("reason") == REASON_USER_REQUESTED:
        for dev in wifi:
            if dev["device"] != leader["device"] and dev.get("uuid") == leader["uuid"]:
                actions.append(("down", leader["uuid"], dev["device"]))
                moves.pop(dev["device"], None)
        memory["leader"] = {}
        memory["last"] = {d["device"]: d.get("uuid", "") for d in wifi}
        return actions, memory, status

    memory["leader"] = leader
    memory["last"] = {d["device"]: d.get("uuid", "") for d in wifi}
    if not leader:
        return actions, memory, status

    profile = lookup.profile(leader["uuid"])
    status["leader"] = {"uuid": leader["uuid"], "id": profile.get("id", ""),
                        "device": leader["device"], "ssid": profile.get("ssid", "")}
    code, severity, detail = followability(profile, lookup.global_cloned())
    if code:
        status["iface_flags"][leader["device"]] = [
            _flag(code, severity, FLAG_TITLES.get(code, code), detail)]
        status["skipped"] = {d["device"]: code for d in wifi if d["device"] != leader["device"]}
        return actions, memory, status

    used_chans = {lookup.chan(d["device"]) for d in wifi if d.get("uuid") == leader["uuid"]}
    used_chans.discard(0)
    need_multi = profile.get("multi_connect", "") not in MULTI_OK
    for dev in wifi:
        name = dev["device"]
        if name == leader["device"]:
            continue
        if dev.get("uuid") == leader["uuid"]:
            status["followers"].append(name)
            continue
        if 40 <= dev.get("state", 0) < STATE_ACTIVATED:
            status["skipped"][name] = "connecting"
            continue
        move = moves.get(name, {})
        if now - move.get("failed_at", -1e18) < FLAP_WINDOW_S:
            status["skipped"][name] = "cooling down after a failed join"
            continue
        if dev.get("uuid") and not fresh_leader:
            status["skipped"][name] = "on another network"
            continue
        scan = lookup.scan(name)
        if not any(a["ssid"] == profile.get("ssid") for a in scan):
            status["skipped"][name] = "network not in range"
            continue
        bssid = pick_bssid(scan, profile.get("ssid", ""), used_chans)
        if need_multi:
            memory["modified"].setdefault(leader["uuid"], profile.get("multi_connect", "") or "default")
            actions.append(("multi", leader["uuid"], "manual-multiple"))
            need_multi = False
        actions.append(("up", leader["uuid"], name, bssid))
        moves[name] = {"target": leader["uuid"], "previous": dev.get("uuid", ""), "at": now}
        if bssid:
            chan = next((a["chan"] for a in scan if a["bssid"] == bssid), 0)
            if chan:
                used_chans.add(chan)
    return actions, memory, status


def plan_release(memory: dict) -> list[tuple]:
    """Multipath switched off: restore multi-connect on profiles we touched."""
    return [("multi", uuid, original or "default")
            for uuid, original in sorted((memory or {}).get("modified", {}).items())]


# ---------------------------------------------------------------------------
# I/O wrapper
# ---------------------------------------------------------------------------


def run_nmcli(args: list[str], timeout: float = 5) -> tuple[int, str]:
    try:
        res = subprocess.run(["nmcli", *args], capture_output=True, text=True,
                             timeout=timeout, check=False)
        return res.returncode, res.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


class _Lookup:
    def __init__(self, chan_of) -> None:
        self._profiles: dict[str, dict] = {}
        self._scans: dict[str, list] = {}
        self._global: str | None = None
        self._chan_of = chan_of

    def profile(self, uuid: str) -> dict:
        if uuid not in self._profiles:
            rc, out = run_nmcli(["-t", "-f", PROFILE_FIELDS, "connection", "show", "uuid", uuid])
            self._profiles[uuid] = parse_profile(out) if rc == 0 else {}
        return self._profiles[uuid]

    def scan(self, dev: str) -> list[dict]:
        if dev not in self._scans:
            rc, out = run_nmcli(["-t", "-f", "SSID,BSSID,CHAN,SIGNAL,SECURITY",
                              "device", "wifi", "list", "ifname", dev, "--rescan", "no"])
            self._scans[dev] = parse_wifi_list(out) if rc == 0 else []
        return self._scans[dev]

    def global_cloned(self) -> str:
        if self._global is None:
            self._global = global_wifi_cloned_mac()
        return self._global

    def chan(self, dev: str) -> int:
        return self._chan_of(dev)


def primary_wifi_device(states: dict, routes=None) -> str:
    """The wifi device carrying NM's lowest-metric default route in `main`."""
    if routes is None:
        try:
            res = subprocess.run(["ip", "-j", "-4", "route", "show", "default", "table", "main"],
                                 capture_output=True, text=True, timeout=2, check=False)
            routes = json.loads(res.stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            routes = []
    best: tuple[int, str] | None = None
    for route in routes if isinstance(routes, list) else []:
        dev = route.get("dev", "")
        if route.get("dst") != "default" or dev not in states:
            continue
        rank = (int(route.get("metric", 0) or 0), dev)
        if best is None or rank < best:
            best = rank
    return best[1] if best else ""


class Follower:
    """Daemon-side driver. step() is called every poll."""

    def __init__(self, path: Path = FOLLOW_PATH) -> None:
        self.path = path
        try:
            self.memory = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self.memory, dict):
                self.memory = {}
        except (OSError, ValueError):
            self.memory = {}
        self.was_enabled = False

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.memory, indent=1) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def _run(self, action: tuple) -> str:
        kind = action[0]
        if kind == "multi":
            _, uuid, value = action
            rc, _ = run_nmcli(["connection", "modify", "--temporary", "uuid", uuid,
                            "connection.multi-connect", value])
        elif kind == "up":
            _, uuid, dev, bssid = action
            args = ["-w", "0", "connection", "up", "uuid", uuid, "ifname", dev]
            if bssid:
                args += ["ap", bssid]
            rc, _ = run_nmcli(args)
        elif kind == "down":
            # Deactivate this device's instance of the profile only; never
            # `device disconnect` (that blocks the device's autoconnect).
            _, uuid, dev = action
            _, out = run_nmcli(["-t", "-f", "UUID,DEVICE,ACTIVE-PATH", "connection", "show", "--active"])
            rc = 1
            for line in out.splitlines():
                parts = split_terse(line)
                if len(parts) >= 3 and parts[0] == uuid and parts[1] == dev:
                    rc, _ = run_nmcli(["connection", "down", "apath", parts[2]])
                    break
        else:
            return f"unknown action {kind}"
        return "" if rc == 0 else f"{' '.join(map(str, action))}: nmcli exit {rc}"

    def step(self, states: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        enabled = shared.MULTIPATH_FLAG.exists()
        if not enabled:
            if self.was_enabled or self.memory.get("modified"):
                for action in plan_release(self.memory):
                    self._run(action)
                self.memory = {}
                self._save()
            self.was_enabled = False
            return {}
        self.was_enabled = True
        rc, out = run_nmcli(["-t", "-f", "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,"
                          "GENERAL.REASON,GENERAL.CON-UUID", "device", "show"])
        if rc != 0:
            return {"error": "nmcli unavailable"}

        def chan_of(dev: str) -> int:
            return int(states.get(dev, {}).get("chan_num", 0) or 0)

        actions, memory, status = plan_follow(parse_dev_show(out), self.memory, now,
                                              _Lookup(chan_of), primary_wifi_device(states))
        errors = [err for err in (self._run(a) for a in actions) if err]
        if memory != self.memory:
            self.memory = memory
            self._save()
        status["actions"] = [list(a) for a in actions]
        if errors:
            status["error"] = "; ".join(errors)
        return status


# ---------------------------------------------------------------------------
# Tidy: collapse per-card profile copies into one profile per network
# ---------------------------------------------------------------------------


def _bound(p: dict) -> bool:
    return bool(p.get("mac_address") or p.get("interface_name"))


def plan_tidy(profiles: list[dict]) -> list[dict]:
    """Collapse *card-bound* copies of a Wi-Fi profile.

    Groups profiles by (SSID, key-mgmt). Only groups that contain a
    card-bound profile (mac-address / interface-name set) are touched, and
    only bound profiles are ever deleted: plain duplicates the user made for
    their own reasons (different passwords, settings) are left alone.
    Keeper preference: unbound, then named exactly like the SSID (the
    original rather than a '-a8000' style copy), then most recently used.
    """
    groups: dict[tuple, list[dict]] = {}
    for p in profiles:
        if p.get("type") != "802-11-wireless" or not p.get("ssid"):
            continue
        groups.setdefault((p["ssid"], p.get("key_mgmt", "")), []).append(p)
    plan = []
    for (ssid, _key_mgmt), members in sorted(groups.items()):
        if len(members) < 2 or not any(_bound(p) for p in members):
            continue
        keep = max(members, key=lambda p: (not _bound(p), p.get("id") == ssid,
                                           p.get("timestamp", 0), p.get("id", "")))
        doomed = [p for p in members if p is not keep and _bound(p)]
        if not doomed and not _bound(keep):
            continue
        plan.append({
            "ssid": ssid,
            "keep": {"uuid": keep["uuid"], "id": keep["id"], "clear_binding": _bound(keep)},
            "delete": [{"uuid": p["uuid"], "id": p["id"]} for p in doomed],
        })
    return plan
