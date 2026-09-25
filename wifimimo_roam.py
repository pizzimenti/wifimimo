"""Slot planning for multipath radios: which access point each radio sits
on, which radios scout instead, and when a fading radio leaves early.

Terms
-----
slot    one BSS (BSSID) on one channel span.
span    the (lo, hi) MHz a BSS occupies: its primary channel widened to
        the operating width (80 MHz on channel 52 is 5250-5330).
tier 1  a slot whose span overlaps no other radio's slot: its own airtime.
tier 2  overlaps another radio's slot, but on a different access point:
        shared airtime, separate AP (separate per-client caps / backhaul).
scout   a radio with no strong tier 1 or tier 2 slot. It is parked
        (disconnected, autoconnect blocked) and scans, feeding fresh
        signal data to the planner, and joins when a slot opens.

Never: two radios on one BSSID (the same AP radio on the same channel).

Preference, most important first: stay connected at all; spread radios
across non-overlapping channels; then across access points; then the
most estimated capacity. A radio whose signal is heading below KEEP_DBM
within HORIZON_S is graded by where it's heading, so it moves while its
link still works instead of after it drops.

Everything here is pure (no I/O); the follower in wifimimo_nm feeds it.
"""

from __future__ import annotations

import math
import re
from collections import deque

STRONG_DBM = -70        # a slot is only joined at or above this while any radio is up
MIN_JOIN_DBM = -72      # never tried below this: APs with a minimum-signal floor
                        # (the EAP720 drops clients it hears below -75) refuse it,
                        # and the AP hears our weaker transmit (A8000: ~10 dB less)
KEEP_DBM = -75          # a held slot keeps counting as strong down to here
DEAD_DBM = -82          # a held slot below this is dead weight
FRESH_S = 10.0          # scan data older than this isn't acted on
HORIZON_S = 6.0         # how far ahead a decline is projected
TREND_WINDOW_S = 8.0
MAX_EXTRAPOLATE_DB = 10.0
DECLINE_DB_S = -0.7     # sustained slope that counts as declining
BORROW_PENALTY_DB = 3.0  # uncertainty on a reading taken by another radio
RATE_GAIN = 0.20        # with tiers tied, a change must add 20 % capacity...
MIN_GAIN_MBPS = 40.0    # ...and at least this much...
UPGRADE_DB = 6.0        # ...and a held radio only moves for capacity to a slot this much stronger
DWELL_S = 20.0          # a radio isn't moved again this soon (unless dying)
SCOUT_SCAN_MOBILE_S = 6.0
SCOUT_SCAN_STILL_S = 30.0
FADE_SCAN_S = 10.0
IDLE_SCAN_S = 120.0     # no scout: some radio still looks around this often
MOBILE_SPREAD_DB = 6.0  # a radio's level moving this much in 30 s = on the move


# ---------------------------------------------------------------------------
# Channel geometry
# ---------------------------------------------------------------------------


def chan_to_mhz(chan: int, freq_hint: int) -> int:
    """Centre frequency of channel number `chan` in the band of `freq_hint`."""
    if freq_hint >= 5925:
        return 5950 + 5 * chan
    if freq_hint >= 4900:
        return 5000 + 5 * chan
    return 2484 if chan == 14 else 2407 + 5 * chan


def channel_span(freq: int, width: int = 20, center: int = 0, ht_sec: str = "") -> tuple[int, int]:
    """(lo, hi) MHz occupied by a BSS on primary `freq` at `width`.

    `center` (MHz) is exact when the beacon states it (VHT/HE operation);
    otherwise the standard 5/6 GHz channelisation places the block. 2.4 GHz
    40 MHz follows the HT secondary-channel offset (both sides if unknown).
    """
    width = max(20, int(width or 20))
    if center and width >= 40:
        return center - width // 2, center + width // 2
    if freq < 3000:
        if width >= 40:
            if ht_sec == "above":
                return freq - 10, freq + 30
            if ht_sec == "below":
                return freq - 30, freq + 10
            return freq - 30, freq + 30
        return freq - 10, freq + 10
    if width <= 20:
        return freq - 10, freq + 10
    base = 5945 if freq >= 5925 else (5735 if freq >= 5735 else 5170)
    width = min(width, 160)
    lo = base + width * ((freq - 10 - base) // width)
    return lo, lo + width


def spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def is_radar(freq: int) -> bool:
    """5 GHz channels 52-144 (U-NII-2A / 2C): DFS, radar detection rules."""
    return 5250 <= freq <= 5730


def parse_reg_country(text: str) -> str:
    """The global country from `iw reg get` ("global" block first)."""
    in_global = False
    for line in text.splitlines():
        if line.strip() == "global":
            in_global = True
            continue
        m = re.match(r"country (\w\w):", line)
        if m and in_global:
            return m.group(1)
        if line.startswith("phy#"):
            in_global = False
    return ""


def band_of(freq: int) -> str:
    if freq >= 5925:
        return "6"
    if freq >= 4900:
        return "5"
    return "2.4"


# ---------------------------------------------------------------------------
# Scan table (`iw dev X scan dump`, unprivileged, no scan triggered)
# ---------------------------------------------------------------------------


def _vht_geometry(bss: dict) -> None:
    """width/center from the parsed HT/VHT/HE fields, where the beacon says."""
    freq = bss.get("freq", 0)
    code = bss.pop("_vht_code", None)
    c1, c2 = bss.pop("_vht_c1", 0), bss.pop("_vht_c2", 0)
    he_w, he_c0, he_c1 = bss.pop("_he_w", 0), bss.pop("_he_c0", 0), bss.pop("_he_c1", 0)
    sec = bss.get("ht_sec", "")
    width, center = 20, 0
    if sec in ("above", "below"):
        width, center = 40, freq + (10 if sec == "above" else -10)
    if he_w:
        width = he_w
        seg = he_c1 if he_w >= 160 and he_c1 else he_c0
        center = chan_to_mhz(seg, freq) if seg else 0
    elif code is not None and code >= 1 and c1:
        if code == 1 and c2 and abs(c2 - c1) == 8:
            width, center = 160, chan_to_mhz(c2, freq)
        elif code == 2:
            width, center = 160, chan_to_mhz(c1, freq)
        else:
            width, center = 80, chan_to_mhz(c1, freq)
    bss["width"], bss["center"] = width, center
    bss["span"] = channel_span(freq, width, center, sec)


def parse_scan_dump(text: str) -> list[dict]:
    """`iw dev X scan dump` -> [{bssid, ssid, freq, signal, age_s, width,
    center, span, util, stations, associated, akm}]. Hidden SSIDs are ''."""
    out: list[dict] = []
    cur: dict | None = None
    section = ""
    for raw in text.splitlines():
        m = re.match(r"BSS ([0-9a-fA-F:]{17})", raw)
        if m:
            if cur is not None:
                _vht_geometry(cur)
            cur = {"bssid": m.group(1).lower(), "ssid": "", "freq": 0, "signal": None,
                   "age_s": None, "util": None, "stations": None,
                   "associated": "-- associated" in raw, "akm": ""}
            out.append(cur)
            section = ""
            continue
        if cur is None:
            continue
        line = raw.strip()
        if raw.startswith("\t") and not raw.startswith("\t\t"):
            section = line.split(":", 1)[0].lower()
        if line.startswith("freq:"):
            cur["freq"] = int(float(line.split()[1]))
        elif line.startswith("signal:"):
            cur["signal"] = float(line.split()[1])
        elif re.match(r"last seen: \d+ ms ago", line):
            cur["age_s"] = int(line.split()[2]) / 1000.0
        elif line.startswith("SSID:") and section == "ssid":
            ssid = line[5:].strip()
            cur["ssid"] = "" if re.fullmatch(r"(\\x00)*", ssid) else ssid
        elif line.startswith("* secondary channel offset:"):
            cur["ht_sec"] = line.split(":", 1)[1].strip()
        elif section == "vht operation" and line.startswith("* channel width:"):
            code = re.match(r"\d+", line.split(":", 1)[1].strip())
            cur["_vht_code"] = int(code.group(0)) if code else 0
        elif section == "vht operation" and line.startswith("* center freq segment 1:"):
            cur["_vht_c1"] = int(line.split(":", 1)[1])
        elif section == "vht operation" and line.startswith("* center freq segment 2:"):
            cur["_vht_c2"] = int(line.split(":", 1)[1])
        # 6 GHz: HE Operation > 6 GHz Operation Information (iw 6.x wording)
        elif section == "he operation" and re.match(r"Channel Width:\s*\d+", line):
            cur["_he_w"] = int(re.search(r"(\d+)", line).group(1))
        elif section == "he operation" and line.startswith("Center Frequency Segment 0:"):
            cur["_he_c0"] = int(line.split(":", 1)[1])
        elif section == "he operation" and line.startswith("Center Frequency Segment 1:"):
            cur["_he_c1"] = int(line.split(":", 1)[1])
        elif line.startswith("* station count:"):
            cur["stations"] = int(line.split(":", 1)[1])
        elif line.startswith("* channel utilisation:"):
            cur["util"] = int(line.split(":", 1)[1].split("/")[0]) / 255.0
        elif "Authentication suites:" in line:
            cur["akm"] = line.split(":", 1)[1].strip()
    if cur is not None:
        _vht_geometry(cur)
    return out


# ---------------------------------------------------------------------------
# Signal and capacity
# ---------------------------------------------------------------------------


def pct_to_dbm(pct: int) -> float:
    """NetworkManager's signal % back to dBm (NM maps -100..-40 dBm linearly
    onto 0..100 %). Only used when no dBm reading exists."""
    return -100.0 + 0.6 * max(0, min(100, int(pct)))


# HE (802.11ax) per-stream rate at 20 MHz, 0.8 us GI, and the SNR each MCS
# roughly needs (vendor sensitivity tables, rounded).
_MCS_RATE_20 = (8.6, 17.2, 25.8, 34.4, 51.6, 68.8, 77.4, 86.0, 103.2, 114.7, 129.0, 143.4)
_MCS_SNR = (2, 5, 9, 11, 15, 18, 20, 25, 29, 31, 34, 37)
_WIDTH_FACTOR = {20: 1.0, 40: 2.0, 80: 4.1, 160: 8.2, 320: 16.4}


def est_rate_mbps(signal: float, width: int = 20, util: float | None = None, nss: int = 2) -> float:
    """Rough PHY capacity for a BSS at `signal` dBm and `width` MHz (2x2),
    scaled by the airtime its BSS Load element says is still free."""
    width = width if width in _WIDTH_FACTOR else 20
    noise = -95.0 + 10.0 * math.log10(width / 20.0)
    snr = signal - noise
    mcs = -1
    for i, need in enumerate(_MCS_SNR):
        if snr >= need:
            mcs = i
    if mcs < 0:
        return 0.0
    rate = _MCS_RATE_20[mcs] * _WIDTH_FACTOR[width] * nss
    if util is not None:
        rate *= max(0.2, 1.0 - util)
    return rate


class Trend:
    """Per-radio signal level and slope from ~1 s samples of one BSS.

    A new BSSID (roam, re-pin) starts a fresh series: slopes across two
    access points mean nothing.
    """

    def __init__(self, window_s: float = TREND_WINDOW_S) -> None:
        self.window_s = window_s
        self._series: dict[str, deque] = {}
        self._bssid: dict[str, str] = {}
        self._long: dict[str, deque] = {}

    def add(self, dev: str, t: float, dbm: float, bssid: str = "") -> None:
        if not dbm or dbm >= 0:
            return
        if self._bssid.get(dev) != bssid:
            self._series[dev] = deque()
            self._bssid[dev] = bssid
        series = self._series.setdefault(dev, deque())
        series.append((t, float(dbm)))
        while series and t - series[0][0] > self.window_s:
            series.popleft()
        long = self._long.setdefault(dev, deque())
        long.append((t, float(dbm)))
        while long and t - long[0][0] > 30.0:
            long.popleft()

    def forget(self, dev: str) -> None:
        self._series.pop(dev, None)
        self._bssid.pop(dev, None)
        self._long.pop(dev, None)

    def level(self, dev: str) -> float | None:
        series = self._series.get(dev)
        if not series:
            return None
        recent = sorted(v for _, v in list(series)[-3:])
        return recent[len(recent) // 2]

    def at(self, dev: str, t: float, tolerance_s: float = 1.5) -> float | None:
        """The radio's reading closest to time t (within tolerance)."""
        best = None
        for ts, v in self._long.get(dev, ()):
            if abs(ts - t) <= tolerance_s and (best is None or abs(ts - t) < best[0]):
                best = (abs(ts - t), v)
        return best[1] if best else None

    def slope(self, dev: str) -> float:
        """Least-squares dB/s over the window; 0 until 4 samples span 3 s."""
        series = self._series.get(dev)
        if not series or len(series) < 4 or series[-1][0] - series[0][0] < 3.0:
            return 0.0
        n = len(series)
        mt = sum(t for t, _ in series) / n
        mv = sum(v for _, v in series) / n
        var = sum((t - mt) ** 2 for t, _ in series)
        if var <= 0:
            return 0.0
        return sum((t - mt) * (v - mv) for t, v in series) / var

    def predicted(self, dev: str, horizon_s: float = HORIZON_S) -> float | None:
        """Where the level is heading: only declines are projected, and at
        most MAX_EXTRAPOLATE_DB below the current level."""
        level = self.level(dev)
        if level is None:
            return None
        drop = min(0.0, self.slope(dev)) * horizon_s
        return level + max(drop, -MAX_EXTRAPOLATE_DB)

    def declining(self, dev: str) -> bool:
        return self.slope(dev) <= DECLINE_DB_S

    def mobile(self) -> bool:
        """Any radio's level moved MOBILE_SPREAD_DB within 30 s."""
        for long in self._long.values():
            values = [v for _, v in long]
            if values and max(values) - min(values) >= MOBILE_SPREAD_DB:
                return True
        return False


class Offsets:
    """How much stronger radio A hears the air than radio B, per band.

    Cards differ a lot (live 2026-09-25: the built-in mt7925e read ~20 dB
    below the A9000 on the same AP), so a reading one radio took is only
    useful to another after correction. Learned from moments when both
    radios measured the same BSS within a couple of seconds.
    """

    ALPHA = 0.3

    def __init__(self) -> None:
        self._off: dict[tuple[str, str, str], float] = {}

    def observe(self, a: str, b: str, band: str, a_dbm: float, b_dbm: float) -> None:
        sample = a_dbm - b_dbm
        for key, value in (((a, b, band), sample), ((b, a, band), -sample)):
            old = self._off.get(key)
            self._off[key] = value if old is None else old + self.ALPHA * (value - old)

    def get(self, a: str, b: str, band: str) -> float | None:
        return self._off.get((a, b, band))


def learn_offsets(offsets: Offsets, dumps: dict[str, list[dict]], trend: Trend,
                  slots: dict[str, dict], now: float, window_s: float = 2.0) -> None:
    """Feed Offsets from near-simultaneous readings of one BSS by two radios:
    two scan entries seen within `window_s` of each other, or a scan entry
    of the BSS another radio is associated to (its live level at that time)."""
    seen: dict[str, list[tuple[str, float, float, int]]] = {}
    for dev, rows in dumps.items():
        for row in rows:
            if row.get("signal") is None or row.get("age_s") is None or row.get("associated"):
                continue
            seen.setdefault(row["bssid"], []).append(
                (dev, now - row["age_s"], row["signal"], row["freq"]))
    for bssid, obs in seen.items():
        for i, (a, ta, sa, freq) in enumerate(obs):
            for b, tb, sb, _ in obs[i + 1:]:
                if a != b and abs(ta - tb) <= window_s and now - max(ta, tb) <= 30.0:
                    offsets.observe(a, b, band_of(freq), sa, sb)
            for dev, slot in slots.items():
                if dev == a or slot.get("bssid") != bssid:
                    continue
                live = trend.at(dev, ta)
                if live is not None and now - ta <= 30.0:
                    offsets.observe(a, dev, band_of(freq), sa, live)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _owe_twin(beacon: dict, rows: list[dict], ssid: str) -> dict | None:
    """The hidden OWE BSS behind an OWE transition-mode beacon: same
    channel, same first five BSSID octets, security OWE (nmcli rows)."""
    prefix = beacon["bssid"][:14]
    for other in rows:
        if other is beacon or other["bssid"][:14] != prefix or other["freq"] != beacon["freq"]:
            continue
        sec = other.get("security", "")
        if "OWE" in sec and "OWE-TM" not in sec and other.get("ssid") in ("", ssid, "OWE-" + ssid):
            return other
    return None


def candidates(dev: str, ssid: str, nm_rows: list[dict], dumps: dict[str, list[dict]],
               offsets: Offsets, avoid: set[str] | frozenset = frozenset()) -> list[dict]:
    """Joinable slots of `ssid` for radio `dev`, each with the best current
    estimate of the signal *this* radio would get.

    Joinable = in this radio's own NetworkManager scan list (for OWE
    transition networks, the hidden twin). The dBm comes from the freshest
    reading: this radio's own scan table, or another radio's corrected by
    the learned offset (less a small uncertainty penalty), or, lacking any
    dBm, NM's percentage (then treated as stale).
    """
    own_dump = {r["bssid"]: r for r in dumps.get(dev, [])}
    by_bssid: dict[str, dict] = {}
    for row in nm_rows:
        if row.get("ssid") != ssid:
            continue
        target = row
        if "OWE-TM" in row.get("security", ""):
            target = _owe_twin(row, nm_rows, ssid)
            if target is None:
                continue
        bssid = target["bssid"]
        if bssid in avoid or bssid in by_bssid:
            continue
        freq = target["freq"]
        width = int(target.get("bandwidth") or 20)
        best = None  # (age, signal, borrowed, dump row)
        mine = own_dump.get(bssid)
        if mine and mine.get("signal") is not None and mine.get("age_s") is not None:
            best = (mine["age_s"], mine["signal"], False, mine)
        for other, rows in dumps.items():
            if other == dev:
                continue
            for r in rows:
                if r["bssid"] != bssid or r.get("signal") is None or r.get("age_s") is None:
                    continue
                off = offsets.get(dev, other, band_of(freq))
                if off is None:
                    continue
                if best is None or r["age_s"] < best[0] - 1.0:
                    best = (r["age_s"], r["signal"] + off - BORROW_PENALTY_DB, True, r)
        geometry = best[3] if best else (mine or {})
        span = geometry.get("span") or channel_span(freq, width)
        if geometry.get("width"):
            width = geometry["width"]
        by_bssid[bssid] = {
            "bssid": bssid, "freq": freq, "width": width, "span": tuple(span),
            "signal": best[1] if best else pct_to_dbm(row.get("signal", 0)),
            "age": best[0] if best else 1e9,
            "borrowed": bool(best and best[2]),
            "util": geometry.get("util"),
        }
    return sorted(by_bssid.values(), key=lambda c: -c["signal"])


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def _grade(opt: tuple, radio: dict) -> str:
    """strong / usable / dead / none for one radio's option."""
    kind = opt[0]
    if kind == "stay":
        slot = radio["slot"]
        if slot.get("dead") or slot.get("level", slot["signal"]) < DEAD_DBM:
            return "dead"
        return "strong" if slot["signal"] >= KEEP_DBM else "usable"
    if kind == "go":
        return "strong" if opt[1]["signal"] >= STRONG_DBM else "usable"
    if kind == "busy":
        return "usable"
    return "none"


def _slot_of(opt: tuple, radio: dict) -> dict | None:
    kind = opt[0]
    if kind == "stay":
        return radio["slot"]
    if kind in ("go", "busy"):
        return opt[1]
    return None


def score(assignment: dict[str, tuple], radios: dict[str, dict]) -> tuple:
    """(-shared, connected, strong slots, tier-1 slots, -dead slots,
    capacity, headroom).

    `shared` counts radios doubled up on another radio's BSSID: never a
    choice, but NM or an older version can leave radios stacked, and each
    one taken off counts as progress. Strong slots of either tier count
    next, so a radio on another access point's overlapping channel beats
    a scouting radio (AP diversity is still diversity); tier 1 breaks the
    tie, so a free channel beats an overlapping one. A dead held slot
    still counts as connected: parking the only radio is never better
    than a bad link.
    """
    placed = {}
    for dev, opt in assignment.items():
        slot = _slot_of(opt, radios[dev])
        if slot is not None and slot.get("bssid"):
            placed[dev] = slot
    bssids = [s["bssid"] for s in placed.values()]
    shared = len(bssids) - len(set(bssids))
    grades = {dev: _grade(assignment[dev], radios[dev]) for dev in assignment}
    connected = int(any(grades[d] != "none" for d in placed))
    strong = tier1 = dead = 0
    capacity = headroom = 0.0
    for dev, slot in placed.items():
        sharing = [o for o in placed
                   if o != dev and spans_overlap(tuple(slot["span"]), tuple(placed[o]["span"]))]
        if grades[dev] == "strong":
            strong += 1
            tier1 += not sharing
        if grades[dev] == "dead":
            dead += 1
            continue
        capacity += est_rate_mbps(slot["signal"], slot.get("width", 20), slot.get("util")) / (1 + len(sharing))
        headroom += slot["signal"] - DEAD_DBM
    # headroom only breaks ties (the rate model saturates on strong links):
    # of two otherwise equal placements, keep the stronger radios on air
    return -shared, connected, strong, tier1, -dead, capacity, headroom


def _better(new: tuple, old: tuple) -> bool:
    if new[:5] != old[:5]:
        return new[:5] > old[:5]
    return new[5] >= old[5] * (1 + RATE_GAIN) and new[5] - old[5] >= MIN_GAIN_MBPS


def plan_slots(radios: list[dict]) -> tuple[tuple | None, str]:
    """One change per poll toward a better placement, or (None, reason).

    radios: [{dev, status, slot, cands, locked}]
      status  on (holding `slot` on the network) / free (disconnected or
              parked: may join or scout) / busy (connecting to `slot`) /
              other (on another network: not ours to move)
      slot    {bssid, span, signal (graded: min(level, predicted)), level,
               width, util, dead}
      cands   candidates() output, strongest first
      locked  True = don't change this radio this poll (dwell, settling)
    Returns ("join"|"move", dev, bssid) / ("park", dev) / None.

    Changes are one at a time and need a clear gain over the current
    placement (tier counts, else RATE_GAIN more capacity). While a radio
    is still connecting, only dead radios are parked: the next move waits
    until the last one has landed (make before break).
    """
    by_dev = {r["dev"]: r for r in radios}
    current: dict[str, tuple] = {}
    for r in radios:
        if r["status"] == "on" and r.get("slot"):
            current[r["dev"]] = ("stay",)
        elif r["status"] == "busy":
            current[r["dev"]] = ("busy", r.get("slot") or {"bssid": "", "span": (0, 0), "signal": -90})
        elif r["status"] == "free":
            current[r["dev"]] = ("scan",)
    if not current:
        return None, "no radios"
    base = score(current, by_dev)
    held = [(r["slot"] or {}).get("bssid") for r in radios if current.get(r["dev"], ("",))[0] == "stay"]
    stacked = {b for b in held if b and held.count(b) > 1}
    settling = any(o[0] == "busy" for o in current.values())
    anyone_up = any(o[0] in ("stay", "busy") for o in current.values())
    trials: list[tuple[tuple, str, tuple]] = []
    for dev, opt in current.items():
        radio = by_dev[dev]
        if opt[0] == "busy":
            continue
        dead = opt[0] == "stay" and _grade(opt, radio) == "dead"
        if radio.get("locked") and not dead:
            continue
        options: list[tuple] = []
        if opt[0] == "stay" and (dead or radio["slot"].get("bssid") in stacked):
            options.append(("scan",))
        # The only working link is never dropped for an upgrade: a move is a
        # few seconds offline. It still moves once it's in trouble.
        others_up = any(o[0] == "stay" and _grade(o, by_dev[d]) in ("strong", "usable")
                        for d, o in current.items() if d != dev)
        sole_and_fine = opt[0] == "stay" and not others_up and _grade(opt, radio) == "strong"
        if not settling and not sole_and_fine:
            for cand in radio.get("cands", []):
                if cand["age"] > FRESH_S or cand["bssid"] == (radio.get("slot") or {}).get("bssid"):
                    continue
                if cand["signal"] < MIN_JOIN_DBM:
                    continue
                if cand["signal"] < STRONG_DBM and anyone_up:
                    continue  # weak slots are only joined when nothing is up at all
                options.append(("go", cand))
        for alt in options:
            trial = dict(current)
            trial[dev] = alt
            s = score(trial, by_dev)
            if not _better(s, base):
                continue
            # Capacity alone (tiers unchanged) moves a held radio only for a
            # clearly stronger slot: the rate model steps an MCS every ~3 dB.
            if (opt[0] == "stay" and alt[0] == "go" and s[:5] == base[:5]
                    and alt[1]["signal"] - radio["slot"]["signal"] < UPGRADE_DB):
                continue
            trials.append((s, dev, alt))
    if not trials:
        return None, "waiting for a connection to land" if settling else "placement already best"
    _, dev, alt = max(trials, key=lambda t: (t[0], t[1]))
    if alt[0] == "scan":
        return ("park", dev), f"{dev}: no strong slot of its own; scouting"
    kind = "move" if current[dev][0] == "stay" else "join"
    cand = alt[1]
    return (kind, dev, cand["bssid"]), (
        f"{dev} -> {cand['bssid']} ({cand['freq']} MHz, {cand['signal']:.0f} dBm)")


def plan_scans(radios: list[dict], last_scan: dict[str, float], now: float,
               mobile: bool) -> list[str]:
    """Which radios to ask for a scan now.

    Scouts scan every SCOUT_SCAN_MOBILE_S while anything is moving
    (SCOUT_SCAN_STILL_S when still, to save power). Without a scout, a
    radio whose slot is weak or declining scans itself every FADE_SCAN_S,
    and otherwise the radio scanned longest ago scans every IDLE_SCAN_S so
    a newly strong access point is still noticed.
    """
    out = []
    scouts = [r for r in radios if r["status"] == "free"]
    interval = SCOUT_SCAN_MOBILE_S if mobile else SCOUT_SCAN_STILL_S
    for r in scouts:
        if now - last_scan.get(r["dev"], -1e18) >= interval:
            out.append(r["dev"])
    if scouts:
        return out
    held = [r for r in radios if r["status"] == "on" and r.get("slot")]
    for r in held:
        needy = r["slot"]["signal"] < STRONG_DBM or r["slot"].get("declining")
        if needy and now - last_scan.get(r["dev"], -1e18) >= FADE_SCAN_S:
            out.append(r["dev"])
    if not out and held:
        oldest = min(held, key=lambda r: (last_scan.get(r["dev"], -1e18), r["dev"]))
        if now - last_scan.get(oldest["dev"], -1e18) >= IDLE_SCAN_S:
            out.append(oldest["dev"])
    return out
