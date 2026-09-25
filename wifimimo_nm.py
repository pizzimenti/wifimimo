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
* Where each radio sits is decided by the slot planner (wifimimo_roam):
  radios spread across non-overlapping channels first, then across access
  points; a radio with no strong slot of its own is *parked* (disconnected,
  autoconnect blocked) and scouts, rather than doubling up on another
  radio's access point. Joins lock the radio to the chosen access point
  (see `activate_pinned`). For OWE transition-mode networks the hidden OWE
  twin is pinned, since the open transition beacon can't be joined by BSSID.
* A radio whose signal is heading below the keep threshold moves early to
  a fresh, stronger slot; scouts scan continuously while anything moves.
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
import threading
import time
from pathlib import Path

import wifimimo_roam as roam
import wifimimo_shared as shared

STATE_DIR = Path.home() / ".local" / "state" / "wifimimo"
FOLLOW_PATH = STATE_DIR / "follow.json"
NM_CONF_PATHS = (Path("/etc/NetworkManager/NetworkManager.conf"),
                 Path("/etc/NetworkManager/conf.d"),
                 Path("/usr/lib/NetworkManager/conf.d"),
                 Path("/run/NetworkManager/conf.d"))

FLAP_WINDOW_S = 600.0
# A pinned join is judged within this window: it either sticks with full
# connectivity or its AP is marked bad for that radio.
JOIN_JUDGE_S = 180.0
LIMITED_GRACE_S = 60.0       # joined but not fully connected for this long = bad AP
# Avoiding an AP after a failed join is two strikes: the first failure avoids
# it for RETRY_AVOID_S, then it's tried again; failing that retry too avoids
# it for good (until you choose the network again in the applet). A join
# that works clears the strikes. (Live: the riverhouse AX1800 dropped DHCP
# replies for a while, then was fixed; a long first avoid would have kept
# radios off it after the fix.)
RETRY_AVOID_S = 300.0
AVOID_FOREVER = 1e12         # "until" timestamp of a permanent avoid
# A radio's first connection this soon after it appeared (stick replugged,
# internal card switched on) is NetworkManager's autoconnect, not a choice.
# Live 2026-09-25: a replugged A9000 was autoconnected 3 s after it came
# back, on a duplicate profile, and was taken for a new user choice.
NEW_RADIO_GRACE_S = 60.0
JOIN_START_S = 5.0           # a join isn't judged "didn't start" before this
SWITCH_TIMEOUT_S = 30.0      # a switch in flight counts as busy up to this long
SHARED_BAD_S = 120.0         # a strong AP that failed a join: avoided by every radio
# A failed join also counts against the AP for every radio (SHARED_BAD_S)
# only when we read it at SURE_DBM or better: below that it may be the AP's
# minimum-signal floor refusing this one radio.
SURE_DBM = -50
JOIN_LOG = STATE_DIR / "joins.jsonl"
# Traffic check (Liveness): a gateway ping per radio every 2 s; three misses
# in a row (6 s) and the link is dead however good its signal reads.
PROBE_INTERVAL_S = 2
DEAD_AFTER_S = 6.0
# A card transmitting at the lowest legacy rate is suspect (the mute A8000
# sat at 6 Mb/s): two misses are enough then.
SUSPECT_TX_MBPS = 6.5
SUSPECT_DEAD_AFTER_S = 4.0
# A join that hasn't reached full activation within JOIN_TIMEOUT_S is given
# up (NM would wait 45 s for DHCP; live, three riverhouse joins each cost
# ~50 s: associate, AP drops us 3 s later, retry, no lease).
JOIN_TIMEOUT_S = 15.0
# Per radio and AP, a failed join raises the signal we require from that AP
# to (reading at the failure + FLOOR_MARGIN_DB), for FLOOR_TTL_S: the AP
# hears each card differently weaker than the card hears it.
FLOOR_MARGIN_DB = 6.0
FLOOR_TTL_S = 1800.0
# A working radio only moves (for capacity) to an AP some radio has
# carried traffic on within PROVEN_S; a radio in trouble may go anywhere.
PROVEN_S = 1800.0
LOST_AP_S = 60.0             # the AP a radio just lost: avoided by that radio
FAST_AFTER_MOVE_S = 30.0     # keep 1 s polling this long after any change
# While following a network, NetworkManager's autoconnect is blocked on every
# radio (in memory only) so it never races the planner: live 15:18:46, the
# built-in's link flickered, NM reconnected it to the AP the planner had just
# given another radio. If no radio has been on the network for LEADER_LOST_S
# and none can see it, the network is gone (you left): the block is lifted
# and NM picks again.
LEADER_LOST_S = 120.0
REGION_SETTLE_S = 2.0        # no joins this soon after the regulatory domain changed
REASON_USER_REQUESTED = 39
STATE_DISCONNECTED = 30
STATE_IP_CONFIG = 70          # associated and authenticated; asking for an address
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
        elif key == "GENERAL.IP4-CONNECTIVITY":
            cur["connectivity"] = _lead_int(value)
        elif key == "GENERAL.AUTOCONNECT":
            cur["autoconnect"] = value.strip().lower().startswith("yes")
    if cur:
        devices.append(cur)
    for dev in devices:
        dev.setdefault("type", "")
        dev.setdefault("state", 0)
        dev.setdefault("reason", 0)
        dev.setdefault("uuid", "")
        dev.setdefault("connectivity", 0)
    return devices


DEV_FIELDS = ("GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.REASON,"
              "GENERAL.CON-UUID,GENERAL.IP4-CONNECTIVITY,GENERAL.AUTOCONNECT")
CONNECTIVITY_FULL = 4
# Helper exclusion reasons that clear up on their own once NM's per-device
# connectivity check passes (which fires no dispatcher event of its own).
HEALABLE_REASONS = frozenset({"no connectivity", "limited connectivity", "captive portal",
                              "gateway unreachable"})
HEAL_INTERVAL_S = 5.0
DEGRADED_CONNECTIVITY = (1, 2, 3)   # none / portal / limited


def heal_candidates(excluded: list[dict], devices: list[dict],
                    members: list[str] | None = None,
                    alive: dict | None = None) -> list[str]:
    """Radios whose health changed without a dispatcher event.

    Either left out by the helper but now rated fully connected by NM, or
    carrying multipath traffic but no longer fully connected (walking away
    from an AP: associated, but its share of new connections blackholes),
    or carrying multipath traffic while the traffic check (Liveness) says
    nothing gets through.
    """
    waiting = {e.get("iface") for e in excluded or [] if e.get("reason") in HEALABLE_REASONS}
    serving = set(members or [])
    alive = alive or {}
    return sorted(
        d["device"] for d in devices
        if (d.get("device") in waiting and alive.get(d["device"]) is not False
            and (d.get("connectivity") == CONNECTIVITY_FULL or alive.get(d["device"]) is True))
        or (d.get("device") in serving and (d.get("connectivity") in DEGRADED_CONNECTIVITY
                                            or alive.get(d["device"]) is False)))


SCAN_FIELDS = "SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY,BANDWIDTH"


def parse_wifi_list(text: str) -> list[dict]:
    """`nmcli -t -f SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY[,BANDWIDTH] dev wifi list`"""
    out = []
    for line in text.splitlines():
        parts = split_terse(line)
        if len(parts) < 6:
            continue
        ssid, bssid, chan, freq, signal, security = parts[:6]
        out.append({"ssid": ssid, "bssid": bssid.lower(), "chan": _lead_int(chan),
                    "freq": _lead_int(freq), "signal": _lead_int(signal),
                    "security": security,
                    "bandwidth": _lead_int(parts[6], 20) if len(parts) > 6 else 20})
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
        "bssid": info.get("802-11-wireless.bssid", ""),
        "mac_address": info.get("802-11-wireless.mac-address", ""),
        "cloned_mac": info.get("802-11-wireless.cloned-mac-address", ""),
        "key_mgmt": info.get("802-11-wireless-security.key-mgmt", ""),
        "route_table": info.get("ipv4.route-table", ""),
        "timestamp": _lead_int(info.get("connection.timestamp", "0")),
    }


PROFILE_FIELDS = ",".join([
    "connection.uuid", "connection.id", "connection.type", "connection.multi-connect",
    "connection.interface-name", "connection.stable-id", "connection.timestamp",
    "802-11-wireless.ssid", "802-11-wireless.bssid", "802-11-wireless.mac-address",
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
    "scouting": "Scanning for a free slot",
}


def band_rank(freq: int) -> int:
    """6 GHz > 5 GHz > 2.4 GHz. Frequencies, not channel numbers: 5 and 6 GHz
    channel numbers overlap."""
    if freq >= 5925:
        return 2
    if freq >= 4900:
        return 1
    return 0


class RoamContext:
    """Unpersisted roaming inputs the daemon keeps between polls: signal
    trends, learned per-card offsets, each radio's scan table (`iw scan
    dump`), and when each radio was last asked to scan."""

    def __init__(self) -> None:
        self.trend = roam.Trend()
        self.offsets = roam.Offsets()
        self.dumps: dict[str, list[dict]] = {}
        self.last_scan: dict[str, float] = {}
        self.region = ""               # `iw reg get` global country, "" unknown
        self.region_changed_at = -1e18
        self.alive: dict[str, bool | None] = {}   # Liveness verdict per radio

    @property
    def region_ok(self) -> bool:
        """Not on the "00" world default (unknown counts as fine: nothing to
        go on, and the kernel still enforces its own rules)."""
        return self.region != "00"

    def region_settling(self, now: float) -> bool:
        return now - self.region_changed_at < REGION_SETTLE_S

    def set_region(self, region: str, now: float) -> None:
        if region != self.region:
            if self.region:
                self.region_changed_at = now
            self.region = region


def plan_follow(devices: list[dict], memory: dict, now: float, lookup,
                primary_device: str = "", ctx: RoamContext | None = None,
                ) -> tuple[list[tuple], dict, dict]:
    """Decide what to do this poll.

    devices  : parse_dev_show() output
    memory   : persisted {"last": {dev: uuid}, "leader": {...}, "moves": {dev: {...}},
                          "modified": {uuid: original multi-connect},
                          "parked": {dev: since}, "appeared": {dev: since}}
    lookup   : object with .profile(uuid) -> dict, .scan(dev) -> list,
               .global_cloned() -> str, .freq(dev) -> MHz (0 if unknown),
               .width(dev) -> MHz, .signal(dev) -> dBm (0 if unknown),
               .bssid(dev) -> str
    ctx      : RoamContext (trends, offsets, scan tables)
    Returns (actions, new_memory, status). Actions:
      ("multi", uuid, value) | ("up", uuid, dev, bssid) | ("down", uuid, dev)
      | ("move", uuid, dev, bssid) | ("park", dev) | ("hold", dev) | ("release", dev)
      | ("scan", dev)
    """
    ctx = ctx or RoamContext()
    memory = json.loads(json.dumps(memory or {}))
    first_poll = "last" not in memory
    last: dict = memory.setdefault("last", {})
    moves: dict = memory.setdefault("moves", {})
    memory.setdefault("modified", {})
    parked: dict = memory.setdefault("parked", {})
    appeared: dict = memory.setdefault("appeared", {})
    leader = memory.get("leader") or {}
    wifi = [d for d in devices if d.get("type") == "wifi" and d.get("state", 0) >= STATE_DISCONNECTED]
    by_dev = {d["device"]: d for d in wifi}
    actions: list[tuple] = []
    status = {"leader": {}, "followers": [], "skipped": {}, "iface_flags": {}}

    # Radios seen last poll. A radio that just appeared (stick plugged in,
    # internal card switched on) and got auto-connected somewhere by NM has
    # made no user choice, so its first connection never counts as one.
    known = set(memory.get("devices") or last.keys())
    for name in list(parked):
        if name not in by_dev:
            del parked[name]
    for name in list(appeared):
        if name not in by_dev:
            del appeared[name]
    if not first_poll:
        for name in by_dev:
            if name not in known:
                appeared.setdefault(name, now)

    def ssid_of(uuid: str) -> str:
        return lookup.profile(uuid).get("ssid", "") if uuid else ""

    # 1. Who did the user just activate? (On the very first poll there is
    #    no baseline: record what's there instead of treating every radio
    #    as a brand-new user choice.) Not a choice: our own joins, NM falling
    #    back to the previous profile, NM autoconnecting a radio that just
    #    appeared, or another profile for the network already being followed.
    fresh_leader = False
    lead_ssid = ssid_of(leader.get("uuid", ""))
    for dev in wifi:
        name = dev["device"]
        uuid, prev = dev.get("uuid", ""), last.get(name, "")
        if first_poll or not uuid or uuid == prev or name not in known:
            continue
        move = moves.get(name)
        ours = move and move.get("target") == uuid
        fallback = move and move.get("previous") == uuid and now - move.get("at", 0) < FLAP_WINDOW_S
        if fallback:
            move["failed_at"] = now
            continue
        if ours or now - appeared.get(name, -1e18) < NEW_RADIO_GRACE_S:
            continue
        if lead_ssid and ssid_of(uuid) == lead_ssid:
            continue
        leader = {"uuid": uuid, "device": name, "at": now}
        lead_ssid = ssid_of(uuid)
        fresh_leader = True
    if not leader and primary_device in by_dev and by_dev[primary_device].get("uuid"):
        leader = {"uuid": by_dev[primary_device]["uuid"], "device": primary_device, "at": now}

    # 2. User disconnected the leader: take the followers down with it.
    #    (A radio we parked ourselves also shows reason 39: not the user.)
    lead_dev = by_dev.get(leader.get("device", ""))
    if leader and lead_dev and lead_dev.get("uuid") != leader["uuid"] \
            and leader["device"] not in parked \
            and lead_dev.get("state") == STATE_DISCONNECTED \
            and lead_dev.get("reason") == REASON_USER_REQUESTED:
        for dev in wifi:
            if dev["device"] != leader["device"] and dev.get("uuid") == leader["uuid"]:
                actions.append(("down", leader["uuid"], dev["device"]))
                moves.pop(dev["device"], None)
        memory["leader"] = {}
        memory["last"] = {d["device"]: d.get("uuid", "") for d in wifi}
        return actions, memory, status

    # 3. Topology: which radios exist now vs last poll. A radio added
    #    (stick plugged in, internal card switched on) or removed (yanked,
    #    switched off) is the moment to re-spread; so is enabling multipath
    #    (first poll) and a new user choice. Between those, assignments are
    #    left alone so nothing flaps.
    devices_now = sorted(d["device"] for d in wifi)
    prev_devices = memory.get("devices")
    new_devices = set(devices_now) - set(prev_devices or devices_now)
    memory["devices"] = devices_now

    def on_network(d: dict) -> bool:
        return bool(d.get("uuid")) and (d["uuid"] == leader.get("uuid")
                                        or ssid_of(d["uuid"]) == lead_ssid)

    def hand_over(exclude: str = "") -> None:
        """The leader's radio vanished, lost its link or is being parked: the
        best-band radio still on the network carries the choice on."""
        nonlocal leader
        heirs = [d for d in wifi if d["device"] != exclude and on_network(d)
                 and d.get("state") == STATE_ACTIVATED]
        if heirs:
            heir = max(heirs, key=lambda d: (band_rank(lookup.freq(d["device"])),
                                             lookup.signal(d["device"]) or -100, d["device"]))
            leader = dict(leader, device=heir["device"])

    # The leader's radio vanished (yanked / switched off) or lost its link
    # (walked out of range; the user didn't disconnect it, that returned
    # above): the choice survives on another radio, and the dropped radio
    # is placed again by the planner below.
    lead_dev = by_dev.get(leader.get("device", "")) if leader else None
    if leader and (lead_dev is None or not on_network(lead_dev)):
        hand_over()

    memory["leader"] = leader
    memory["last"] = {d["device"]: d.get("uuid", "") for d in wifi}
    if not leader:
        return actions, memory, status

    profile = lookup.profile(leader["uuid"])
    status["leader"] = {"uuid": leader["uuid"], "id": profile.get("id", ""),
                        "device": leader["device"], "ssid": profile.get("ssid", "")}
    status["moved"] = []
    code, severity, detail = followability(profile, lookup.global_cloned())
    if code:
        status["iface_flags"][leader["device"]] = [
            _flag(code, severity, FLAG_TITLES.get(code, code), detail)]
        status["skipped"] = {d["device"]: code for d in wifi if d["device"] != leader["device"]}
        return actions, memory, status

    ssid = profile.get("ssid", "")
    # A BSSID the user pinned on the profile themselves wins: never override it.
    user_pinned = bool(profile.get("bssid"))
    need_multi = profile.get("multi_connect", "") not in MULTI_OK

    def join(name: str, bssid: str, previous: str, signal: float = -100.0,
             kind: str = "up") -> None:
        """kind "up" joins an idle radio; "move" drops the radio's current
        link first (switching in place failed "no secrets" 2 of 2 times live,
        joins from disconnected 0 of 8)."""
        nonlocal need_multi
        if need_multi:
            memory["modified"].setdefault(leader["uuid"], profile.get("multi_connect", "") or "default")
            actions.append(("multi", leader["uuid"], "manual-multiple"))
            need_multi = False
        actions.append((kind, leader["uuid"], name, bssid))
        parked.pop(name, None)   # stays held: the join is ours, reconnects are too
        moves[name] = {"target": leader["uuid"], "previous": previous, "at": now, "bssid": bssid,
                       "signal": round(float(signal), 1)}

    def park(name: str) -> None:
        # disconnect + block autoconnect: NM mustn't put it back on another
        # radio's AP (or another saved network) behind our back
        actions.append(("park", name))
        parked[name] = now
        moves[name] = {"target": "", "previous": "", "at": now, "bssid": ""}

    # 3b. Judge recent pinned joins. A join that didn't stick (the radio is
    #     back to disconnected), or that stuck but hasn't reached full
    #     connectivity after LIMITED_GRACE_S, is a strike against that AP for
    #     that radio (RETRY_AVOID_S, then for good on the second); the radio
    #     is re-pinned elsewhere below.
    #     (Live 2026-09-24: one AP never gave the A8000 a DHCP lease, and
    #     without this the follower re-picked it forever.)
    #     A join to a strong AP that fails is the AP's doing (live 2026-09-25:
    #     the far AP's 5 GHz side timed out the handshake for both sticks at
    #     -43 dBm), so it is avoided by every radio for SHARED_BAD_S, not just
    #     the one that tried. A radio that loses the AP it was on avoids that
    #     AP for LOST_AP_S (it was sent straight back to it, and failed).
    bad: dict = memory.setdefault("bad", {})
    strikes: dict = memory.setdefault("strikes", {})
    shared_bad: dict = memory.setdefault("bad_all", {})
    if fresh_leader:
        # choosing the network in the applet is the retry for everything
        bad.clear()
        strikes.clear()
        shared_bad.clear()
    for name in list(bad):
        bad[name] = {b: until for b, until in bad[name].items() if until > now}
        if not bad[name]:
            del bad[name]
    for b in [b for b, until in shared_bad.items() if until <= now]:
        del shared_bad[b]
    floors: dict = memory.setdefault("floor", {})
    for name in list(floors):
        floors[name] = {b: v for b, v in floors[name].items() if v[1] > now}
        if not floors[name]:
            del floors[name]
    proven: dict = memory.setdefault("proven", {})
    for b in [b for b, t in proven.items() if now - t > PROVEN_S]:
        del proven[b]

    def raise_floor(name: str, bssid: str, signal) -> None:
        if signal is None:
            return
        need = float(signal) + FLOOR_MARGIN_DB
        old = floors.get(name, {}).get(bssid, [-200.0, 0])[0]
        floors.setdefault(name, {})[bssid] = [max(old, need), now + FLOOR_TTL_S]

    broken: set[str] = set()      # on its target AP but passing no traffic
    abandon: set[str] = set()     # still connecting past JOIN_TIMEOUT_S: give up
    for name, mv in moves.items():
        bssid, dev = mv.get("bssid"), by_dev.get(name)
        if not bssid or dev is None or mv.get("settled") or now - mv.get("at", 0) > JOIN_JUDGE_S:
            continue
        age = now - mv.get("at", 0)
        on_target = dev.get("uuid") == mv.get("target")
        if on_target:
            mv["max_state"] = max(mv.get("max_state", 0), dev.get("state", 0))
        activated = on_target and dev.get("state") == STATE_ACTIVATED
        dead = activated and ctx.alive.get(name) is False
        # the first seconds after `up` can still read disconnected; NM falling
        # back to another profile is a failed join too (nothing to cancel)
        elsewhere = bool(dev.get("uuid")) and not on_target
        dropped = elsewhere or (
            not on_target and dev.get("state") == STATE_DISCONNECTED and age >= JOIN_START_S)
        stalled = not activated and not dropped and age >= JOIN_TIMEOUT_S
        limited = activated and dev.get("connectivity") in (1, 2, 3) and age > LIMITED_GRACE_S
        if not (dropped or stalled or dead or limited):
            continue
        sure = mv.get("signal", -100) >= SURE_DBM
        count = strikes.setdefault(name, {}).get(bssid, 0) + 1
        strikes[name][bssid] = count
        bad.setdefault(name, {})[bssid] = AVOID_FOREVER if count >= 2 else now + RETRY_AVOID_S
        if sure:
            shared_bad[bssid] = now + SHARED_BAD_S
        # Only a join that never got past association / handshake says the AP
        # couldn't hear us. Past that (DHCP, no traffic) it isn't a signal
        # problem: live, the riverhouse AP heard us at -70 and mistral
        # answered every DHCP request; the answers never reached the card.
        got_to_ip = mv.get("max_state", 0) >= STATE_IP_CONFIG
        if not got_to_ip:
            raise_floor(name, bssid, mv.get("signal"))
        outcome = ("no traffic" if dead else "limited" if limited else
                   "no address" if stalled and got_to_ip else "timed out" if stalled else
                   "failed" if sure else "refused (floor?)")
        status.setdefault("joins", []).append({
            "t": round(now, 1), "dev": name, "bssid": bssid, "signal": mv.get("signal"),
            "outcome": outcome, "secs": round(age, 1), "nm_state": dev.get("state"),
            "nm_reason": dev.get("reason")})
        mv["bssid"] = ""
        mv["settled"] = True
        if activated:
            broken.add(name)
        elif stalled:
            abandon.add(name)
    held: dict = memory.setdefault("held", {})
    for name, bssid in list(held.items()):
        dev = by_dev.get(name)
        lost = dev is not None and dev.get("state") == STATE_DISCONNECTED and name not in parked
        in_flight = now - moves.get(name, {}).get("at", -1e18) < SWITCH_TIMEOUT_S
        if lost and not in_flight:
            bad.setdefault(name, {})[bssid] = max(bad.get(name, {}).get(bssid, 0), now + LOST_AP_S)
        if dev is not None and dev.get("state") == STATE_ACTIVATED and ctx.alive.get(name) is False:
            # a held link that stopped passing traffic: off it, and not straight back
            broken.add(name)
            bad.setdefault(name, {})[bssid] = max(bad.get(name, {}).get(bssid, 0), now + LOST_AP_S)
        if dev is None or dev.get("state") != STATE_ACTIVATED:
            del held[name]
    for name, entries in bad.items():
        if name in by_dev:
            parts = [f"{b} ({'until you choose the network again' if until >= AVOID_FOREVER else f'{int(until - now)} s more'})"
                     for b, until in sorted(entries.items())]
            status["iface_flags"].setdefault(name, []).append(_flag(
                "avoiding_ap", "info", "Avoiding an access point",
                "Recently failed to give this radio a working connection: "
                + ", ".join(parts) + "."))

    def avoid(name: str) -> set[str]:
        return set(bad.get(name, {})) | set(shared_bad)

    status["followers"] = [d["device"] for d in wifi
                           if d["device"] != leader["device"] and on_network(d)]

    # No races with NetworkManager: every radio's autoconnect stays blocked
    # while we follow a network. If the network is gone, hand back.
    held_ac: list = memory.setdefault("held_ac", [])
    if any(on_network(d) and d.get("state") == STATE_ACTIVATED for d in wifi):
        memory["leader_seen"] = now
    seen_at = memory.setdefault("leader_seen", now)
    visible = any(a.get("ssid") == ssid for d in wifi for a in lookup.scan(d["device"]))
    if now - seen_at > LEADER_LOST_S and not visible:
        actions.extend(("release", name) for name in sorted(set(held_ac) | set(parked)))
        memory.update(leader={}, held_ac=[], parked={}, leader_seen=now)
        status["plan"] = f"'{ssid}' gone for {int(now - seen_at)} s: NetworkManager picks again"
        return actions, memory, status
    for d in wifi:
        if d.get("autoconnect") is True:
            actions.append(("hold", d["device"]))
        if d["device"] not in held_ac:
            held_ac.append(d["device"])
    memory["held_ac"] = [n for n in held_ac if n in by_dev]
    if user_pinned:
        # The user locked the profile to one AP: every radio would share it.
        for d in wifi:
            if not on_network(d):
                status["skipped"][d["device"]] = "profile pinned to one access point"
        return actions, memory, status

    # 4. Placement. Every radio is described to the slot planner, which
    #    makes at most one change per poll (join / move / park).
    views: list[dict] = []
    for d in wifi:
        name, state = d["device"], d.get("state", 0)
        move = moves.get(name, {})
        cands = roam.candidates(name, ssid, lookup.scan(name), ctx.dumps, ctx.offsets, avoid(name))
        if not ctx.region_ok:
            # radar channels need the country's rules in force to be joined
            cands = [c for c in cands if not roam.is_radar(c["freq"])]
        mine = floors.get(name, {})
        cands = [dict(c, proven=c["bssid"] in proven) for c in cands
                 if c["signal"] >= mine.get(c["bssid"], [-200.0])[0]]
        view = {"dev": name, "slot": None, "locked": False, "cands": cands}
        if name in abandon:
            view.update(status="free", locked=True)   # parked below
            views.append(view)
            continue
        landed = on_network(d) and state == STATE_ACTIVATED
        if landed and move.get("target") == leader["uuid"] and not move.get("settled"):
            move["settled"] = True
            status.setdefault("joins", []).append({
                "t": round(now, 1), "dev": name, "bssid": move.get("bssid", ""),
                "signal": move.get("signal"), "outcome": "landed",
                "secs": round(now - move.get("at", now), 1)})
        # A switch in flight is busy until it lands or is judged failed: in
        # between, the radio reads deactivating / disconnected, and treating
        # it as free let two switches overlap (live: 66 s with no link).
        in_flight = (bool(move.get("target")) and not move.get("settled")
                     and now - move.get("at", -1e18) < SWITCH_TIMEOUT_S)
        if in_flight or state > STATE_ACTIVATED:
            target = next((c for c in cands if c["bssid"] == move.get("bssid")), None)
            view.update(status="busy", locked=True, slot=target)
        elif landed:
            bssid = lookup.bssid(name)
            own = next((r for r in ctx.dumps.get(name, []) if r["bssid"] == bssid), {})
            freq = lookup.freq(name)
            level = ctx.trend.level(name) or lookup.signal(name) or -60
            heading = ctx.trend.predicted(name)
            view.update(status="on", locked=now - move.get("at", -1e18) < roam.DWELL_S, slot={
                "bssid": bssid, "freq": freq, "width": own.get("width") or lookup.width(name),
                "span": tuple(own.get("span") or roam.channel_span(freq, lookup.width(name))),
                "level": level, "signal": min(level, heading if heading is not None else level),
                "util": own.get("util"), "dead": name in broken,
                "declining": ctx.trend.declining(name)})
        elif 40 <= state < STATE_ACTIVATED:
            if not (on_network(d) or move.get("target") == leader["uuid"]):
                status["skipped"][name] = "connecting"
                continue
            target = next((c for c in view["cands"] if c["bssid"] == move.get("bssid")), None)
            view.update(status="busy", locked=True, slot=target)
        elif not d.get("uuid"):
            cooling = now - move.get("failed_at", -1e18) < FLAP_WINDOW_S
            view.update(status="free", locked=cooling)
            if cooling:
                status["skipped"][name] = "cooling down after a failed join"
        elif fresh_leader or name in new_devices:
            view.update(status="free", other=True)   # may be brought over, never parked
        else:
            status["skipped"][name] = "on another network"
            continue
        views.append(view)

    for view in views:
        if view["status"] == "on":
            held[view["dev"]] = view["slot"]["bssid"]
            if ctx.alive.get(view["dev"]) is True:
                proven[view["slot"]["bssid"]] = now
                strikes.get(view["dev"], {}).pop(view["slot"]["bssid"], None)   # it works now

    def finish(views: list[dict], joining: str) -> tuple[list[tuple], dict, dict]:
        # A join that timed out is cancelled; idle radios that aren't
        # joining stay parked (scouting) rather than left to NM's autoconnect.
        for name in sorted(abandon):
            if name not in parked:
                park(name)
        for view in views:
            name = view["dev"]
            if (view["status"] == "free" and not view.get("other") and name != joining
                    and name not in parked and by_dev[name].get("state") == STATE_DISCONNECTED):
                park(name)
        for name in sorted(parked):
            status["iface_flags"].setdefault(name, []).append(_flag(
                "scouting", "info", FLAG_TITLES["scouting"],
                "No access point on a free channel, or on another access point, is strong "
                "enough for this radio, so it scans instead of sharing a channel with "
                "another radio. It joins as soon as one is."))

        # 5. Scans: scouts continuously while anything moves; a weak or
        #    fading radio scans itself when no scout exists.
        mobile = ctx.trend.mobile()
        for name in roam.plan_scans([v for v in views if not v.get("other")],
                                    ctx.last_scan, now, mobile):
            if name != joining:
                actions.append(("scan", name))

        memory["leader"] = leader
        status["leader"]["device"] = leader.get("device", "")
        status["parked"] = sorted(parked)
        status["floors"] = {n: {b: v[0] for b, v in f.items()} for n, f in floors.items()}
        # 1 s polls while anything is moving or changing; a scout sitting
        # still beside steady radios doesn't need them (it scans every 30 s).
        status["wants_fast"] = bool(
            joining or mobile or memory.get("swap")
            or any(now - mv.get("at", -1e18) < FAST_AFTER_MOVE_S for mv in moves.values())
            or any(v["status"] == "on" and (v["slot"]["signal"] < roam.STRONG_DBM
                                            or v["slot"]["declining"]) for v in views))
        return actions, memory, status

    swap = memory.get("swap")
    if swap:
        step = swap_step(swap, by_dev, ctx, now)
        if step is None:
            memory.pop("swap", None)
        else:
            kind, name, bssid = step
            if kind == "wait":
                pass
            else:
                signal = next((c["signal"] for v in views if v["dev"] == name
                               for c in v["cands"] if c["bssid"] == bssid), -60.0)
                join(name, bssid, leader["uuid"], signal, kind)
                status["moved"].append(name)
            status["plan"] = f"swap: {swap['a']} <-> {swap['b']} ({kind} {name})"
            return finish(views, name if kind in ("up", "move") else "")

    if ctx.region_settling(now):
        change, why = None, "waiting for the regulatory domain to settle"
    else:
        change, why = roam.plan_slots(views)
    status["plan"] = why
    joining = ""
    if change and change[0] in ("join", "move"):
        _, name, bssid = change
        previous = by_dev[name].get("uuid", "") if change[0] == "join" else leader["uuid"]
        view = next(v for v in views if v["dev"] == name)
        signal = next((c["signal"] for c in view["cands"] if c["bssid"] == bssid), -100.0)
        join(name, bssid, previous, signal, "move" if change[0] == "move" else "up")
        status["moved"].append(name)
        joining = name
    elif change and change[0] == "park":
        name = change[1]
        if name == leader["device"]:
            hand_over(exclude=name)
        park(name)
    elif change and change[0] == "swap":
        # a <-> b: park b, move a to b's slot, join b to a's slot; one step
        # per poll, each waiting for the last (see swap_step)
        _, a, a_to, b, b_to = change
        memory["swap"] = {"a": a, "a_to": a_to, "b": b, "b_to": b_to, "at": now, "step": 0}
        if b == leader["device"]:
            hand_over(exclude=b)
        park(b)
    return finish(views, joining)


SWAP_TIMEOUT_S = 60.0


def swap_step(swap: dict, by_dev: dict, ctx: RoamContext, now: float) -> tuple | None:
    """Next step of a two-radio swap (a takes b's slot, b takes a's), or
    None when it's done or has to be abandoned (the planner takes over).

    step 0: b was parked; once it's down, move a to b's old slot.
    step 1: once a carries traffic there, join b to a's old slot.
    step 2: once b is up, done.
    """
    a, b = by_dev.get(swap["a"]), by_dev.get(swap["b"])
    if a is None or b is None or now - swap.get("at", now) > SWAP_TIMEOUT_S:
        return None
    step = swap.get("step", 0)
    if step == 0:
        if b.get("state") != STATE_DISCONNECTED:
            return ("wait", swap["b"], "")
        swap.update(step=1, t=now)
        return ("move", swap["a"], swap["a_to"])
    if step == 1:
        age = now - swap.get("t", now)
        if a.get("state") == STATE_ACTIVATED and ctx.alive.get(swap["a"]) is not False and age >= 2:
            swap.update(step=2, t=now)
            return ("up", swap["b"], swap["b_to"])
        if age > JOIN_TIMEOUT_S or (a.get("state") == STATE_DISCONNECTED and age >= JOIN_START_S):
            return None
        return ("wait", swap["a"], "")
    age = now - swap.get("t", now)
    if b.get("state") == STATE_ACTIVATED or age > JOIN_TIMEOUT_S:
        return None
    return ("wait", swap["b"], "")


def plan_release(memory: dict) -> list[tuple]:
    """Multipath switched off: restore multi-connect on profiles we touched
    and hand every radio we held or parked back to NetworkManager."""
    memory = memory or {}
    radios = set(memory.get("parked", {})) | set(memory.get("held_ac", []))
    return ([("multi", uuid, original or "default")
             for uuid, original in sorted(memory.get("modified", {}).items())]
            + [("release", name) for name in sorted(radios)])


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
    def __init__(self, states: dict) -> None:
        self._profiles: dict[str, dict] = {}
        self._scans: dict[str, list] = {}
        self._global: str | None = None
        self._states = states

    def _state(self, dev: str, key: str, default):
        value = self._states.get(dev, {}).get(key, default)
        return default if value is None else value

    def signal(self, dev: str) -> int:
        return int(self._state(dev, "signal_dbm", 0) or 0)

    def bssid(self, dev: str) -> str:
        return str(self._state(dev, "bssid", "")).lower()

    def profile(self, uuid: str) -> dict:
        if uuid not in self._profiles:
            rc, out = run_nmcli(["-t", "-f", PROFILE_FIELDS, "connection", "show", "uuid", uuid])
            self._profiles[uuid] = parse_profile(out) if rc == 0 else {}
        return self._profiles[uuid]

    def scan(self, dev: str) -> list[dict]:
        if dev not in self._scans:
            rc, out = run_nmcli(["-t", "-f", SCAN_FIELDS,
                                 "device", "wifi", "list", "ifname", dev, "--rescan", "no"])
            self._scans[dev] = parse_wifi_list(out) if rc == 0 else []
        return self._scans[dev]

    def global_cloned(self) -> str:
        if self._global is None:
            self._global = global_wifi_cloned_mac()
        return self._global

    def freq(self, dev: str) -> int:
        return int(self._state(dev, "freq_mhz", 0) or 0)

    def width(self, dev: str) -> int:
        return int(self._state(dev, "bandwidth_mhz", 20) or 20)


class Liveness:
    """Does traffic actually flow on each radio? One `ping` of the radio's
    gateway every PROBE_INTERVAL_S, bound to that radio (-I), per connected
    radio.

    Signal alone can't tell: live 2026-09-25 the A8000 sat associated at
    -55 dBm from 14:32 on, passing nothing (every gateway ping lost, link at
    6 Mb/s), while NetworkManager's connectivity check (every few minutes)
    and the neighbour cache still called it fine.
    """

    def __init__(self, spawn=None, clock=time.time) -> None:
        self._spawn = spawn or self._spawn_ping
        self._clock = clock
        self._procs: dict[str, tuple] = {}      # dev -> (proc, gateway)
        self.started: dict[str, float] = {}
        self.last_reply: dict[str, float] = {}

    @staticmethod
    def _spawn_ping(dev: str, gw: str):
        return subprocess.Popen(
            ["ping", "-n", "-i", str(PROBE_INTERVAL_S), "-W", "1", "-I", dev, gw],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def _reader(self, dev: str, proc) -> None:
        for line in proc.stdout:
            if "bytes from" in line:
                self.last_reply[dev] = self._clock()

    def update(self, states: dict) -> None:
        """Probe every radio with an address and a gateway; stop the rest.

        A probe is keyed by (gateway, BSSID): every AP here shares one
        gateway, so a radio that switched AP would otherwise keep its old
        probe and its last reply from before the switch, and read dead the
        moment it landed (live 16:28:41, "no traffic" after 2 s)."""
        want = {dev: (s.get("gateway"), str(s.get("bssid") or "").lower())
                for dev, s in states.items()
                if s.get("connected") and s.get("ipv4") and s.get("gateway")}
        for dev in list(self._procs):
            proc, gw = self._procs[dev]
            if want.get(dev) != gw or (proc is not None and proc.poll() is not None):
                self.stop(dev)
        for dev, key in want.items():
            if dev in self._procs:
                continue
            try:
                proc = self._spawn(dev, key[0])
            except OSError:
                continue
            self._procs[dev] = (proc, key)
            self.started[dev] = self._clock()
            self.last_reply.pop(dev, None)
            if proc is not None and getattr(proc, "stdout", None) is not None:
                threading.Thread(target=self._reader, args=(dev, proc), daemon=True).start()

    def stop(self, dev: str) -> None:
        proc, _ = self._procs.pop(dev, (None, None))
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.started.pop(dev, None)
        self.last_reply.pop(dev, None)

    def alive(self, dev: str, now: float, suspect: bool = False) -> bool | None:
        """True / False, or None while not probed or still in its grace.
        `suspect` (transmit rate collapsed to the minimum) shortens the fuse."""
        start = self.started.get(dev)
        if start is None:
            return None
        last = max(self.last_reply.get(dev, start), start)
        if now - last > (SUSPECT_DEAD_AFTER_S if suspect else DEAD_AFTER_S):
            return False
        return True if dev in self.last_reply else None


def read_scan_dump(dev: str) -> list[dict]:
    """This radio's cfg80211 scan table: unprivileged, triggers no scan."""
    try:
        res = subprocess.run(["iw", "dev", dev, "scan", "dump"], capture_output=True,
                             text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return roam.parse_scan_dump(res.stdout) if res.returncode == 0 else []


def read_region() -> str:
    """Global regulatory country from `iw reg get` ("US", "00"; "" unknown)."""
    try:
        res = subprocess.run(["iw", "reg", "get"], capture_output=True, text=True,
                             timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return roam.parse_reg_country(res.stdout)


def feed_context(ctx: RoamContext, states: dict, now: float, dump=read_scan_dump,
                 region=read_region) -> None:
    """Per poll: each radio's live level into its trend, fresh scan tables,
    per-card offsets learned from them, and the regulatory country."""
    ctx.set_region(region(), now)
    slots = {}
    for dev, state in states.items():
        if state.get("connected"):
            level = state.get("signal_avg_dbm") or state.get("signal_dbm") or 0
            ctx.trend.add(dev, now, level, str(state.get("bssid", "")).lower())
            slots[dev] = {"bssid": str(state.get("bssid", "")).lower()}
        else:
            ctx.trend.forget(dev)
    ctx.dumps = {dev: dump(dev) for dev in states}
    for dev in list(ctx.last_scan):
        if dev not in states:
            del ctx.last_scan[dev]
    roam.learn_offsets(ctx.offsets, ctx.dumps, ctx.trend, slots, now)


def activate_pinned(uuid: str, dev: str, bssid: str, nmcli=None) -> int:
    """Activate profile `uuid` on `dev`, locked to `bssid` for this radio only.

    `connection up ... ap <BSSID>` is only a hint (wpa_supplicant still picks
    the strongest AP; verified live 2026-09-24). A BSSID in the *profile* is
    binding, but the profile is shared by every radio. NetworkManager copies
    the profile into a per-device "applied connection" when the activation
    starts, so: pin the BSSID in memory (--temporary), start the activation
    (-w 0 returns once it's queued, i.e. after the copy), and immediately
    clear it again. Other radios keep their own applied copies, and nothing
    is ever written to disk.
    """
    nmcli = nmcli or run_nmcli
    if not bssid:
        rc, _ = nmcli(["-w", "0", "connection", "up", "uuid", uuid, "ifname", dev])
        return rc
    rc, _ = nmcli(["connection", "modify", "--temporary", "uuid", uuid,
                   "802-11-wireless.bssid", bssid])
    if rc != 0:
        rc, _ = nmcli(["-w", "0", "connection", "up", "uuid", uuid, "ifname", dev])
        return rc
    try:
        rc, _ = nmcli(["-w", "0", "connection", "up", "uuid", uuid, "ifname", dev])
    finally:
        nmcli(["connection", "modify", "--temporary", "uuid", uuid, "802-11-wireless.bssid", ""])
    return rc


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
        self.last_heal = 0.0
        self.heal_proc: subprocess.Popen | None = None
        self.ctx = RoamContext()
        self.liveness = Liveness()
        self.wants_fast = False     # read by the daemon's poll-interval logic

    def heal(self, devices: list[dict], now: float) -> list[str]:
        """Ask the root helper to re-apply when a waiting radio became healthy.

        Fire-and-forget through the same pkexec / polkit path the widget
        uses, at most every HEAL_INTERVAL_S, so the poll loop never blocks.
        """
        if self.heal_proc is not None and self.heal_proc.poll() is None:
            return []
        if now - self.last_heal < HEAL_INTERVAL_S:
            return []
        try:
            status = json.loads(shared.MULTIPATH_STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(status, dict):
            return []
        ready = heal_candidates(status.get("excluded", []), devices, status.get("members", []),
                                self.ctx.alive)
        if ready:
            self.last_heal = now
            try:
                self.heal_proc = subprocess.Popen(
                    ["pkexec", str(shared.HELPER_PATH), "multipath", "apply"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.heal_proc = None
        return ready

    def _log_joins(self, joins: list[dict]) -> None:
        """Append join attempts and outcomes to JOIN_LOG (for tuning
        thresholds against what really happened on a walk)."""
        if not joins or self.path != FOLLOW_PATH:
            return
        try:
            JOIN_LOG.parent.mkdir(parents=True, exist_ok=True)
            with JOIN_LOG.open("a", encoding="utf-8") as f:
                for entry in joins:
                    f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

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
            rc = activate_pinned(uuid, dev, bssid)
        elif kind == "move":
            # Drop the current link and wait for it to go (-w 5), then join
            # from disconnected: switching in place lost the secrets race.
            _, uuid, dev, bssid = action
            run_nmcli(["-w", "5", "device", "disconnect", dev], timeout=7)
            rc = activate_pinned(uuid, dev, bssid)
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
        elif kind == "park":
            # Block autoconnect first (in memory only; NM forgets it on
            # restart), then drop the link if there is one.
            _, dev = action
            rc, _ = run_nmcli(["device", "set", dev, "autoconnect", "no"])
            run_nmcli(["device", "disconnect", dev])
        elif kind == "hold":
            # in memory only (NM forgets it on restart); explicit activations
            # still work, and re-enable it, so it's re-asserted every poll
            _, dev = action
            rc, _ = run_nmcli(["device", "set", dev, "autoconnect", "no"])
        elif kind == "release":
            _, dev = action
            rc, _ = run_nmcli(["device", "set", dev, "autoconnect", "yes"])
        elif kind == "scan":
            _, dev = action
            rc, _ = run_nmcli(["device", "wifi", "rescan", "ifname", dev])
            self.ctx.last_scan[dev] = time.time()
        else:
            return f"unknown action {kind}"
        return "" if rc == 0 else f"{' '.join(map(str, action))}: nmcli exit {rc}"

    def step(self, states: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        enabled = shared.MULTIPATH_FLAG.exists()
        if not enabled:
            self.wants_fast = False
            self.liveness.update({})    # stops every probe
            if (self.was_enabled or self.memory.get("modified") or self.memory.get("parked")
                    or self.memory.get("held_ac")):
                for action in plan_release(self.memory):
                    self._run(action)
                self.memory = {}
                self._save()
            self.was_enabled = False
            return {}
        self.was_enabled = True
        rc, out = run_nmcli(["-t", "-f", DEV_FIELDS, "device", "show"])
        if rc != 0:
            return {"error": "nmcli unavailable"}

        devices = parse_dev_show(out)
        feed_context(self.ctx, states, now)
        self.liveness.update(states)
        self.ctx.alive = {
            dev: self.liveness.alive(
                dev, time.time(),
                suspect=0 < float(s.get("tx_rate_mbps") or 0) <= SUSPECT_TX_MBPS)
            for dev, s in states.items()}
        actions, memory, status = plan_follow(devices, self.memory, now, _Lookup(states),
                                              primary_wifi_device(states), self.ctx)
        self.wants_fast = bool(status.pop("wants_fast", False))
        for action in actions:
            if action[0] in ("up", "move"):
                dev = action[2]
                bssid = action[3]
                mv = memory.get("moves", {}).get(dev, {})
                status.setdefault("joins", []).append({
                    "t": round(now, 1), "dev": dev, "bssid": bssid, "signal": mv.get("signal"),
                    "outcome": "tried " + action[0]})
        self._log_joins(status.get("joins", []))
        status["healed"] = self.heal(devices, now)
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
