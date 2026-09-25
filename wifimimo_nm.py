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
  to the same profile with `connection up <uuid> ifname <dev>`, locked to
  an access point on a frequency no other radio is using (higher band
  first; see `activate_pinned`). For OWE transition-mode networks the
  hidden OWE twin is pinned, since the open transition beacon itself can't
  be joined by BSSID.
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
# A pinned join is judged within this window: it either sticks with full
# connectivity or its AP is marked bad for that radio.
JOIN_JUDGE_S = 180.0
LIMITED_GRACE_S = 60.0       # joined but not fully connected for this long = bad AP
BAD_AP_TTL_S = 1800.0        # how long a bad AP is avoided for that radio
# Pinned radios can't roam on their own (the BSSID lock is in their applied
# connection), so the follower roams for them: a radio fading below FADE_DBM
# is re-pinned to a free-frequency AP at least FADE_MARGIN_PCT stronger
# (nmcli signal %, ~2 %/dB), one radio at a time while the others are up.
FADE_DBM = -72
FADE_MARGIN_PCT = 16
FADE_HOLD_S = 30.0           # no second fade move for a radio within this window


def dbm_to_pct(dbm: int) -> int:
    """NetworkManager's signal % for a dBm reading (clamped linear, 2 %/dB)."""
    return max(0, min(100, 2 * (int(dbm) + 100)))
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
        elif key == "GENERAL.IP4-CONNECTIVITY":
            cur["connectivity"] = _lead_int(value)
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
              "GENERAL.CON-UUID,GENERAL.IP4-CONNECTIVITY")
CONNECTIVITY_FULL = 4
# Helper exclusion reasons that clear up on their own once NM's per-device
# connectivity check passes (which fires no dispatcher event of its own).
HEALABLE_REASONS = frozenset({"no connectivity", "limited connectivity", "captive portal",
                              "gateway unreachable"})
HEAL_INTERVAL_S = 10.0
DEGRADED_CONNECTIVITY = (1, 2, 3)   # none / portal / limited


def heal_candidates(excluded: list[dict], devices: list[dict],
                    members: list[str] | None = None) -> list[str]:
    """Radios whose health changed without a dispatcher event.

    Either left out by the helper but now rated fully connected by NM, or
    carrying multipath traffic but no longer fully connected (walking away
    from an AP: associated, but its share of new connections blackholes).
    """
    waiting = {e.get("iface") for e in excluded or [] if e.get("reason") in HEALABLE_REASONS}
    serving = set(members or [])
    return sorted(
        d["device"] for d in devices
        if (d.get("device") in waiting and d.get("connectivity") == CONNECTIVITY_FULL)
        or (d.get("device") in serving and d.get("connectivity") in DEGRADED_CONNECTIVITY))


SCAN_FIELDS = "SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY"


def parse_wifi_list(text: str) -> list[dict]:
    """`nmcli -t -f SSID,BSSID,CHAN,FREQ,SIGNAL,SECURITY dev wifi list ...`"""
    out = []
    for line in text.splitlines():
        parts = split_terse(line)
        if len(parts) < 6:
            continue
        ssid, bssid, chan, freq, signal, security = parts[:6]
        out.append({"ssid": ssid, "bssid": bssid.lower(), "chan": _lead_int(chan),
                    "freq": _lead_int(freq), "signal": _lead_int(signal),
                    "security": security})
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
}


def band_rank(freq: int) -> int:
    """6 GHz > 5 GHz > 2.4 GHz. Frequencies, not channel numbers: 5 and 6 GHz
    channel numbers overlap."""
    if freq >= 5925:
        return 2
    if freq >= 4900:
        return 1
    return 0


def _owe_twin(beacon: dict, scan: list[dict], ssid: str) -> dict | None:
    """The hidden OWE BSS behind an OWE transition-mode beacon.

    A transition-mode network advertises an open beacon (security OWE-TM)
    whose BSSID can't be joined directly; the encrypted twin is a hidden BSS
    on the same radio: same channel, same first five BSSID octets, security
    OWE, SSID empty (or revealed as the real / "OWE-" name).
    """
    prefix = beacon["bssid"][:14]
    for other in scan:
        if other is beacon or other["bssid"][:14] != prefix:
            continue
        if other["freq"] != beacon["freq"]:
            continue
        sec = other["security"]
        if "OWE" in sec and "OWE-TM" not in sec and other["ssid"] in ("", ssid, "OWE-" + ssid):
            return other
    return None


def pick_bssid(scan: list[dict], ssid: str, used_freqs: set[int],
               avoid: set[str] | frozenset = frozenset()) -> dict | None:
    """Best AP for SSID on a frequency no other radio is using, or None to
    let NM choose. Prefers the higher band, then the stronger signal.
    `avoid`: BSSIDs that recently failed for this radio."""
    candidates: dict[str, dict] = {}
    for ap in scan:
        if ap["ssid"] != ssid or ap["signal"] < MIN_SIGNAL_PCT:
            continue
        if "OWE-TM" in ap["security"]:
            twin = _owe_twin(ap, scan, ssid)
            if twin is None:
                continue  # can't pin a transition beacon itself
            ap = dict(twin, ssid=ssid, signal=max(ap["signal"], twin["signal"]))
        if ap["bssid"] in avoid:
            continue
        candidates.setdefault(ap["bssid"], ap)
    fresh = [a for a in candidates.values() if a["freq"] not in used_freqs]
    if not fresh:
        return None
    return max(fresh, key=lambda a: (band_rank(a["freq"]), a["signal"]))


def plan_follow(devices: list[dict], memory: dict, now: float, lookup,
                primary_device: str = "") -> tuple[list[tuple], dict, dict]:
    """Decide what to do this poll.

    devices  : parse_dev_show() output
    memory   : persisted {"last": {dev: uuid}, "leader": {...}, "moves": {dev: {...}},
                          "modified": {uuid: original multi-connect}}
    lookup   : object with .profile(uuid) -> dict, .scan(dev) -> list,
               .global_cloned() -> str, .freq(dev) -> MHz (0 if unknown),
               .signal(dev) -> dBm (0 if unknown), .bssid(dev) -> str
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

    # Radios seen last poll. A radio that just appeared (stick plugged in,
    # internal card switched on) and got auto-connected somewhere by NM has
    # made no user choice, so its first connection never counts as one.
    known = set(memory.get("devices") or last.keys())

    # 1. Who did the user just activate? (On the very first poll there is
    #    no baseline: record what's there instead of treating every radio
    #    as a brand-new user choice.)
    fresh_leader = False
    for dev in wifi:
        uuid, prev = dev.get("uuid", ""), last.get(dev["device"], "")
        if first_poll or not uuid or uuid == prev or dev["device"] not in known:
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

    # 3. Topology: which radios exist now vs last poll. A radio added
    #    (stick plugged in, internal card switched on) or removed (yanked,
    #    switched off) is the moment to re-spread; so is enabling multipath
    #    (first poll) and a new user choice. Between those, assignments are
    #    left alone so nothing flaps.
    devices_now = sorted(d["device"] for d in wifi)
    prev_devices = memory.get("devices")
    new_devices = set(devices_now) - set(prev_devices or devices_now)
    topology_changed = prev_devices is not None and devices_now != prev_devices
    rebalance = first_poll or topology_changed or fresh_leader
    memory["devices"] = devices_now

    # The leader's radio vanished (yanked / switched off) or lost its link
    # (walked out of range; the user didn't disconnect it, that returned
    # above): the best-band radio still on that network takes over, so the
    # choice survives and the dropped radio rejoins as a follower below.
    lead_dev = by_dev.get(leader.get("device", "")) if leader else None
    if leader and (lead_dev is None or lead_dev.get("uuid") != leader["uuid"]):
        heirs = [d for d in wifi if d.get("uuid") == leader["uuid"]]
        if heirs:
            heir = max(heirs, key=lambda d: (band_rank(lookup.freq(d["device"])), d["device"]))
            leader = dict(leader, device=heir["device"])

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

    def join(name: str, bssid: str, previous: str) -> None:
        nonlocal need_multi
        if need_multi:
            memory["modified"].setdefault(leader["uuid"], profile.get("multi_connect", "") or "default")
            actions.append(("multi", leader["uuid"], "manual-multiple"))
            need_multi = False
        actions.append(("up", leader["uuid"], name, bssid))
        moves[name] = {"target": leader["uuid"], "previous": previous, "at": now, "bssid": bssid}

    # 3b. Judge recent pinned joins. A join that didn't stick (the radio is
    #     back to disconnected), or that stuck but hasn't reached full
    #     connectivity after LIMITED_GRACE_S, marks that AP bad for that
    #     radio for BAD_AP_TTL_S; the radio is re-pinned elsewhere below.
    #     (Live 2026-09-24: one AP never gave the A8000 a DHCP lease, and
    #     without this the follower re-picked it forever.)
    bad: dict = memory.setdefault("bad", {})
    for name in list(bad):
        bad[name] = {b: until for b, until in bad[name].items() if until > now}
        if not bad[name]:
            del bad[name]
    broken: set[str] = set()
    for name, mv in moves.items():
        bssid, dev = mv.get("bssid"), by_dev.get(name)
        if not bssid or dev is None or now - mv.get("at", 0) > JOIN_JUDGE_S:
            continue
        on_target = dev.get("uuid") == mv.get("target")
        failed = (not on_target and dev.get("state") == STATE_DISCONNECTED) or (
            on_target and dev.get("state") == STATE_ACTIVATED
            and dev.get("connectivity") in (1, 2, 3)
            and now - mv.get("at", 0) > LIMITED_GRACE_S)
        if failed:
            bad.setdefault(name, {})[bssid] = now + BAD_AP_TTL_S
            mv["bssid"] = ""
            if on_target:
                broken.add(name)
    for name, entries in bad.items():
        if name in by_dev:
            status["iface_flags"].setdefault(name, []).append(_flag(
                "avoiding_ap", "info", "Avoiding an access point",
                "Recently failed to give this radio a working connection: "
                + ", ".join(sorted(entries)) + ". Retried after 30 min."))

    def avoid(name: str) -> set[str]:
        return set(bad.get(name, {}))

    # 4. Radios already on the leader's network. The leader itself never
    #    moves (it's the user's choice). On a rebalance, a follower that
    #    shares a frequency with a radio placed before it, or that could
    #    step up to a free higher band, is re-pinned; best bands first. A
    #    follower whose AP just proved broken is re-pinned at any time.
    used: set[int] = {lookup.freq(leader["device"])} - {0}
    on_network = [d for d in wifi if d["device"] != leader["device"] and d.get("uuid") == leader["uuid"]]
    on_network.sort(key=lambda d: (-band_rank(lookup.freq(d["device"])), d["device"]))
    for dev in on_network:
        name, cur = dev["device"], lookup.freq(dev["device"])
        status["followers"].append(name)
        if name in broken and not user_pinned:
            pick = pick_bssid(lookup.scan(name), ssid, used, avoid(name))
            join(name, pick["bssid"] if pick else "", leader["uuid"])
            used.add(pick["freq"] if pick else cur)
            status["moved"].append(name)
            continue
        if not rebalance or user_pinned or dev.get("state", 0) != STATE_ACTIVATED:
            used.add(cur)
            continue
        pick = pick_bssid(lookup.scan(name), ssid, used, avoid(name))
        collides = cur == 0 or cur in used
        if pick and (collides or band_rank(pick["freq"]) > band_rank(cur)):
            join(name, pick["bssid"], leader["uuid"])
            used.add(pick["freq"])
            status["moved"].append(name)
        else:
            used.add(cur)
    used.discard(0)

    # 5b. Roam for pinned radios: one fading radio per poll (below FADE_DBM,
    #     with a clearly stronger AP free) is re-pinned while the others keep
    #     traffic flowing. Skipped while any radio on the network is still
    #     connecting or another move is already planned this poll.
    settling = any(40 <= d.get("state", 0) < STATE_ACTIVATED
                   for d in wifi if d.get("uuid") == leader["uuid"])
    if not user_pinned and not settling and not any(a[0] == "up" for a in actions):
        on_p = [d for d in wifi if d.get("uuid") == leader["uuid"] and d.get("state") == STATE_ACTIVATED]
        fading = sorted(
            (d for d in on_p if (lookup.signal(d["device"]) or 0) < 0
             and lookup.signal(d["device"]) < FADE_DBM
             and now - moves.get(d["device"], {}).get("at", -1e18) > FADE_HOLD_S),
            key=lambda d: lookup.signal(d["device"]))
        for dev in fading:
            name = dev["device"]
            others = {lookup.freq(d["device"]) for d in on_p if d["device"] != name} - {0}
            current = lookup.bssid(name)
            pick = pick_bssid(lookup.scan(name), ssid, others, avoid(name) | ({current} - {""}))
            if pick and pick["signal"] >= dbm_to_pct(lookup.signal(name)) + FADE_MARGIN_PCT:
                join(name, pick["bssid"], leader["uuid"])
                status["moved"].append(name)
                status.setdefault("fading", []).append(name)
                break

    # 5. Radios not on the network: idle ones join; ones on another network
    #    only when the user just chose this one, or the radio just appeared
    #    (NM may have auto-connected a freshly added card elsewhere). This
    #    includes the leader's own radio when every radio dropped at once.
    for dev in wifi:
        name = dev["device"]
        if dev.get("uuid") == leader["uuid"]:
            continue
        if 40 <= dev.get("state", 0) < STATE_ACTIVATED:
            status["skipped"][name] = "connecting"
            continue
        move = moves.get(name, {})
        if now - move.get("failed_at", -1e18) < FLAP_WINDOW_S:
            status["skipped"][name] = "cooling down after a failed join"
            continue
        if dev.get("uuid") and not fresh_leader and name not in new_devices:
            status["skipped"][name] = "on another network"
            continue
        scan = lookup.scan(name)
        if not any(a["ssid"] == ssid for a in scan):
            status["skipped"][name] = "network not in range"
            continue
        pick = None if user_pinned else pick_bssid(scan, ssid, used, avoid(name))
        join(name, pick["bssid"] if pick else "", dev.get("uuid", ""))
        if pick:
            used.add(pick["freq"])
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
        ready = heal_candidates(status.get("excluded", []), devices, status.get("members", []))
        if ready:
            self.last_heal = now
            try:
                self.heal_proc = subprocess.Popen(
                    ["pkexec", str(shared.HELPER_PATH), "multipath", "apply"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.heal_proc = None
        return ready

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
        rc, out = run_nmcli(["-t", "-f", DEV_FIELDS, "device", "show"])
        if rc != 0:
            return {"error": "nmcli unavailable"}

        devices = parse_dev_show(out)
        actions, memory, status = plan_follow(devices, self.memory, now,
                                              _Lookup(states), primary_wifi_device(states))
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
