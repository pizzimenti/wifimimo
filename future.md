# wifimimo — refactor for Wi-Fi 7 / MLO and future PHY modes

## Context

A Wi-Fi 7 AP (`carrierpidgeon-6G`, 6295 MHz, 160 MHz width, MLO) renders
broken in the plasmoid and `wifimimo-mon`:

- Header shows `0 MHz / ?` (band unknown).
- `tx_mcs = rx_mcs = -1`, rendered as a misleadingly highlighted MCS-0
  cell because `main.qml:894` clamps with `Math.max(0, mcs)`.
- `tx_nss = rx_nss = 0`, NSS dots empty.
- Bitrate values come through (mode-agnostic netlink attr), but the rate
  ruler row is all `-` because `compute_rates` short-circuits on `mcs < 0`.

Proximate cause: nl80211 reports `NL80211_RATE_INFO_EHT_MCS / _EHT_NSS /
_EHT_GI`; `iw` prints `EHT-MCS / EHT-NSS / EHT-GI`. Neither the netlink
elif-chain (`wifimimo_core.py:335-349`), the iw regex
(`wifimimo_core.py:294-298`), nor the efficiency / MCS-max tables
(`wifimimo_core.py:24-29`) know about EHT. MLO compounds it: the MLD
parent interface does not carry `NL80211_ATTR_WIPHY_FREQ`; per-link
`freq` lives under `Link N BSSID …` blocks in `iw dev <iface> link`.

The user's deeper question: **what structural changes stop this class of
blind spot.** The answer drives the phases below.

## Why this was overlooked

1. **PHY-mode literals are scattered.** `"HE" / "VHT" / "HT"` appear as
   elif branches, regex tokens, and dict keys in **22+ sites** across
   `wifimimo_core.py`, `wifimimo-mon.py`, and `main.qml`. Adding a mode
   means 5+ edits across two languages with nothing forcing them to
   agree.
2. **QML silently shadows Python tables.** `efficiencies`
   (`main.qml:491-494`), `bandLabel` (`main.qml:443-454`), `giLabel`
   (`main.qml:477-488`), `nssDots` (`main.qml:473-475`),
   `signalFraction` (`main.qml:415-417`), `signalColor`
   (`main.qml:423-431`), `computeRates` (`main.qml:490-505`), and
   `mcsGridCount` (`main.qml:551-562`) all reimplement Python logic.
   `signalColor` thresholds even disagree with the curses UI (mon
   uses -75/-65, plasmoid uses -70/-55).
3. **Silent no-op on unknown modes.** `_parse_rate_info`
   (`wifimimo_core.py:327-349`) has no `else` branch and no warning log;
   unknown rate-info attrs leave defaults in place. The iw regex
   (`wifimimo_core.py:296`) fails to match and the function bails at
   line 300.
4. **No schema, no tests, no CI.** State serialization is hand-rolled in
   triplicate (`state_to_lines`, `read_state`, plasmoid `parseState`).
   No `tests/`, no `.github/workflows/`. No fixture for "rate present
   but mode unknown" — exactly the EHT failure mode.
5. **Developed against current hardware.** Tables and regexes were
   calibrated against the user's HE Wi-Fi 6 link; EHT/MLO only became
   reachable when the AP changed.

## Phase 1 — `PhyMode` registry + EHT/MLO collection

**Goal:** single source of truth for PHY modes; EHT/MLO works after this
phase. Replaces the throwaway 5-spot patch with one structured change.

**Files:**
- `wifimimo_core.py` (new `phy_modes.py` sibling module, then rewire
  `_parse_rate_info`, `parse_link_metrics`, `compute_rates`)
- `plasmoid/.../main.qml` (Phase 1 still touches QML for `mcsGridCount`
  and `efficiencies`; Phase 3 deletes that QML logic entirely)

**Work items:**

1. **New `phy_modes.py`** — frozen dataclass `PhyMode` with fields:
   `name`, `iw_token` (e.g. `"EHT"`), `nl_rate_attr_prefix` (e.g.
   `"NL80211_RATE_INFO_EHT"`), `nl_mcs_attr` / `_nss_attr` / `_gi_attr`,
   `mcs_max`, `efficiency: tuple[float, ...]`, `has_gi: bool`. Module
   exports `PHY_MODES: tuple[PhyMode, ...]` ordered EHT, HE, VHT, HT —
   most-specific first so the netlink check picks EHT over HE on an
   EHT-capable rate info dict. EHT efficiency extends HE with
   `(9.0, 10.0)` for 4096-QAM 3/4 and 5/6 (MCS 12/13).

2. **Replace `_parse_rate_info`** (`wifimimo_core.py:327-349`):
   ```python
   for mode in PHY_MODES:
       if mode.nl_mcs_attr in attrs:
           data[f"{direction}_mode"] = mode.name
           data[f"{direction}_mcs"] = _int(attrs[mode.nl_mcs_attr], -1)
           data[f"{direction}_nss"] = _int(attrs.get(mode.nl_nss_attr), 0)
           if mode.has_gi:
               data[f"{direction}_gi"] = _int(attrs.get(mode.nl_gi_attr), -1)
           return
   logger.warning("unknown rate-info attrs for %s: %s", direction, sorted(attrs))
   ```
   HT's `NL80211_RATE_INFO_MCS` keeps its `raw % 8 / raw // 8 + 1` shim.

3. **Rewrite `parse_link_metrics`** (`wifimimo_core.py:272-324`): build
   the regex alternation from `"|".join(m.iw_token for m in PHY_MODES if
   m.name != "HT")`. HT keeps its dedicated fallback.

4. **Add MLO freq/width fallback.** After the netlink path in
   `_collect_via_netlink` (`wifimimo_core.py:352-425`), if `freq_mhz ==
   0` or `bandwidth_mhz == 0` after the WIPHY attrs read, call `iw dev
   <iface> link` and parse the first `Link N` block for `freq:` and
   `width:`. New helper `_parse_mlo_primary_link(iface) -> tuple[int,
   int]`. This is the minimal MLO fix; Phase 4 elevates `links[]` to
   first-class.

5. **Extend `NL80211_WIDTH_TO_MHZ`** (`wifimimo_core.py:35-44`): add
   `8: 320`. Leave 240 out until a driver actually reports it.

6. **`compute_rates`** (`wifimimo_core.py:253-257`): look up `PhyMode`
   by name; drop `EFFICIENCY` / `MCS_MAX` dicts at end of phase.

7. **Bridge QML** (interim, until Phase 3): extend `efficiencies` and
   `mcsGridCount` in `main.qml:491-494` / `:551-562` with the EHT entry,
   and gate the highlight clamp at `main.qml:894` on `mcs >= 0` so an
   unknown MCS doesn't pretend to be MCS 0.

**Verification:** `pytest tests/test_phy_modes.py` (Phase 5 lands tests
alongside this). Manual: restart daemon, `cat
/run/user/$UID/wifimimo-state` shows `tx_mode=EHT`, `tx_mcs=9`,
`rx_mcs=6`, `tx_nss=1`, `rx_nss=2`, `freq_mhz=6295`,
`bandwidth_mhz=160`.

**Risk:** Medium — hot collection path. Mitigated by Phase 5 fixtures.
**Size:** Medium.

## Phase 2 — Versioned `WifiState` dataclass + JSON state file

**Goal:** kill the triplicate hand-rolled (de)serializer. One schema
declaration drives Python write, Python read, and QML parse.

**Files:** `wifimimo_core.py`, `wifimimo-daemon.py`, `wifimimo-mon.py`,
`main.qml`, `wifimimo-plasmoid-source.py` (no change), `install.sh`
(restart hint).

**Work items:**

1. `@dataclass WifiState` in `wifimimo_core.py` replacing
   `default_state()` (`wifimimo_core.py:170-211`). Nested `@dataclass
   LinkInfo` placeholder (populated in Phase 4). Field `schema_version:
   int = 2`.
2. `write_state` becomes `path.write_text(json.dumps(asdict(state),
   indent=None))` via atomic rename. `read_state` becomes a thin
   wrapper that loads JSON, applies a `schema_version` migration shim
   for v1 callers (no version field → treat as v1, map known keys,
   default the rest).
3. Delete `state_to_lines` and the long elif chain in `read_state`
   (`wifimimo_core.py:469-574`).
4. `main.qml:256-413` `parseState()` collapses to `JSON.parse(rawText)`
   plus a `validateState(obj)` that fills missing keys from the QML
   default `data` object (`main.qml:31-64`).
5. `install.sh`: post-install message reminding `systemctl --user
   restart wifimimo-daemon` so daemon and UIs match.

**Risk:** High blast radius — a running v1 daemon feeding a v2 plasmoid
fails. The migration shim handles the reverse direction (v2 plasmoid
reading a leftover v1 file across a crash window). Acceptance criterion:
`cat state` is human-readable indented JSON; fields are exactly the
dataclass field set.

**Size:** Medium.

## Phase 3 — Daemon-computed derived fields (kill Python/QML duplication)

**Goal:** UIs become dumb consumers. Math lives in one place.

**Files:** new `wifimimo_core.py` helpers (or new `derived.py`),
`wifimimo-daemon.py`, `wifimimo-mon.py`, `main.qml`.

**Work items:**

1. New `derive_display(state: WifiState) -> DisplayState` (additive
   dataclass nested inside `WifiState` as `display: DisplayState`):
   - `band_label` (replaces `wifimimo-mon.py:255` + `main.qml:443-454`)
   - `tx_gi_label`, `rx_gi_label` (replaces `wifimimo-mon.py:311` +
     `main.qml:477-488`)
   - `tx_nss_dots`, `rx_nss_dots` (replaces `wifimimo-mon.py:146-147` +
     `main.qml:473-475`)
   - `signal_fraction`, `signal_avg_fraction`, `antenna_fractions:
     list[float]` (replaces `wifimimo-mon.py:126-127` +
     `main.qml:415-417`)
   - `signal_tier: Literal["good","warn","crit"]` — **resolves the
     -75/-65 vs -70/-55 divergence**; decide canonical thresholds in
     code review.
   - `tx_rates_mbps: list[float]`, `rx_rates_mbps: list[float]` —
     pre-computed full rate ruler. Empties out (`[]`) on `mcs < 0`.
   - `mcs_grid_count: int` (replaces `main.qml:551-562`).
2. Wire `derive_display(state)` into `WifimimoDaemon.run()` between
   `collect_power` and `write_state` (`wifimimo-daemon.py:62-69`).
3. `wifimimo-mon.py`: delete local `signal_fraction`, `signal_color`
   constant tables, `gi_labels`, `nss_dots`, and inline `compute_rates`
   calls. Read from `state.display.*`.
4. `main.qml`: delete `bandLabel`, `giLabel`, `nssDots`,
   `signalFraction`, `signalColor` thresholds, `computeRates`,
   `mcsGridCount`. Bind directly to `data.display.*`.
5. `main.qml:894` highlight clamp goes away — `data.display.tx_rates`
   is empty when `mcs < 0`, and the rendered MCS string is already
   `displayMcs(mcs)`.

**Risk:** Medium — UI refactor touches every panel. Phase 5 snapshot
tests catch regressions in the derived shape. **Size:** Medium-large.

## Phase 4 — MLO multi-link awareness

**Goal:** `links[]` becomes first class. Per-link rates and signals
visible when MLO is in use.

**Files:** `wifimimo_core.py`, `wifimimo-daemon.py`, `wifimimo-mon.py`,
`main.qml`, `tests/`.

**Work items:**

1. Extend collection: parse every `Link N` block in `iw dev <iface>
   link` (`bssid`, `freq`, `signal`, `width`, `tx/rx bitrate`).
   Netlink path: walk `NL80211_ATTR_MLO_LINKS` if pyroute2 surfaces it
   (gate on availability — iw fallback is the reliable path).
2. `WifiState.links: list[LinkInfo]` with `link_id`, `bssid`,
   `freq_mhz`, `bandwidth_mhz`, `signal_dbm`, plus per-direction `rate,
   mcs, nss, mode, gi`.
3. Top-level `freq_mhz / bandwidth_mhz / tx_rate_mbps / rx_rate_mbps`
   remain as the "primary link" (highest-rate link) for back-compat
   with `history_row` / CSV columns (`wifimimo_core.py:577-604`).
4. Plasmoid `connectionSummary()` (`main.qml:528-534`) shows per-link
   badges when `data.links.length > 1`.
5. Curses mon: add a "LINKS" section under "RATES" when
   `len(state.links) > 1`.
6. New fixtures in `tests/fixtures/`: `iw_link_eht_mlo.txt`.

**Risk:** Medium — pyroute2 MLO surface may need iw fallback on older
kernels. The iw-link parsing in Phase 1 already exists; this phase
extends it from "primary link only" to "all links". **Size:**
Medium-large.

## Phase 5 — Tests + CI

**Goal:** the next blind spot fails red before it ships.

**Files:** `tests/` (new), `.github/workflows/ci.yml` (new),
`requirements-dev.txt` (new).

**Work items:**

1. `tests/fixtures/`: `iw_link_he_single.txt`,
   `iw_link_eht_single.txt`, `iw_link_eht_mlo.txt`,
   `iw_link_disconnected.txt`; pickled `nl80211_*.pkl` attribute dicts
   captured from the user's machine (one-time `iw.list_dev() +
   iw.get_stations()` dump script in `tests/capture_fixtures.py`).
2. `tests/test_phy_modes.py`:
   - Every `PhyMode.name` round-trips through `_parse_rate_info` with a
     synthetic attr dict.
   - `_parse_rate_info` on an unknown attr set emits a warning and
     leaves defaults.
3. `tests/test_collect.py`:
   - For each fixture, assert the resulting `WifiState` matches a
     checked-in JSON snapshot. Snapshot is regenerated with
     `UPDATE_SNAPSHOTS=1 pytest`. Catches schema drift.
4. `tests/test_derive_display.py`: golden values for `band_label`,
   `signal_tier`, `tx_rates_mbps` shape across HE/EHT/disconnected.
5. **QML parity check** (cheap, no QML runtime): a Python test parses
   `main.qml` and confirms it no longer contains literal `"HE"`,
   `"VHT"`, `"HT"`, `"EHT"` keys (Phase 3 removed all duplicated
   tables; this catches accidental reintroduction).
6. `.github/workflows/ci.yml`: `pip install -r requirements.txt -r
   requirements-dev.txt && pytest -q` on push and PR. Use
   `ubuntu-24.04` (pinned per Manjaro user's CI guidance), Python
   3.12 pinned.

**Risk:** Zero. **Size:** Small-medium.

## Existing functions worth reusing

- `freq_to_channel` (`wifimimo_core.py:260-269`) — already 6 GHz aware,
  unchanged.
- `_TimeoutContext` (`wifimimo_core.py:52-83`) — keep, MLO parsing
  reuses it for the iw fallback subprocess.
- `_run` / `_attrs_to_dict` / `_int` / `_float` (`wifimimo_core.py:214-243`)
  — keep, used heavily by new MLO and EHT code.
- `HISTORY_COLUMNS` / `history_row` (`wifimimo_core.py:577-604`) — keep
  the schema stable for CSV back-compat; Phase 4 adds optional MLO
  columns at the end.
- `WifimimoDaemon.update_retry_window` (`wifimimo-daemon.py:173-212`) —
  unchanged; counters are mode-agnostic.

## Dependency order

Phase 1 → Phase 5 (start tests against the registry) → Phase 2 → Phase 3
→ Phase 4 → Phase 5 (extend with MLO fixtures).

Phase 5 spans two checkpoints because tests are most valuable when
written immediately after the registry lands (catches Phase 2 schema
drift) and again after MLO support (catches Phase 4 regressions).

## End-to-end verification

After each phase plus a final smoke after Phase 4:

1. `systemctl --user restart wifimimo-daemon`.
2. `cat /run/user/$UID/wifimimo-state | jq .` — valid JSON,
   `schema_version: 2`, `tx_mode: "EHT"`, `tx_mcs / rx_mcs` non-negative,
   `tx_nss / rx_nss` non-zero, `links` length matches `iw dev wlp1s0
   link` Link blocks.
3. `python3 wifimimo-mon.py` — header reads `6 GHz` band, `160 MHz`
   width (or `320 MHz` if AP supports it), MCS in range, NSS dots
   populated, rate ruler shows numbers across 14 cells for EHT.
4. Plasmoid: expand widget — same data, 14-cell EHT grid, no phantom
   MCS-0 highlight when disconnected, per-link badges when MLO is
   active.
5. `pytest -q` green; CI green on push.
6. Swap to a Wi-Fi 6 (HE) AP — daemon writes `tx_mode: "HE"`, 12-cell
   grid, `links` length 1, no schema drift.
7. Disconnect entirely — `connected: false`, `display.tx_rates: []`,
   no UI exceptions.

## Critical files

- `/home/bradley/Code/kde_apps/wifimimo/wifimimo_core.py`
- `/home/bradley/Code/kde_apps/wifimimo/wifimimo-daemon.py`
- `/home/bradley/Code/kde_apps/wifimimo/wifimimo-mon.py`
- `/home/bradley/Code/kde_apps/wifimimo/plasmoid/org.kde.plasma.wifimimo/contents/ui/main.qml`
- `/home/bradley/Code/kde_apps/wifimimo/phy_modes.py` (new, Phase 1)
- `/home/bradley/Code/kde_apps/wifimimo/tests/` (new, Phase 5)
- `/home/bradley/Code/kde_apps/wifimimo/.github/workflows/ci.yml` (new, Phase 5)
