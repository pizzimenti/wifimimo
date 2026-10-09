# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).
Each version's heading says whether it was a major, minor or patch change.

## [Unreleased]

## [1.8.3] - 2026-10-09 (patch)

### Fixed

- `wifimimo-nm-tidy` also collapses unbound duplicates that share the keeper's
  exact name (the applet lists them as "Net (wifi0)" twice), keeping the most
  recently used. Differently named copies are still left alone.

## [1.8.2] - 2026-10-03 (patch)

### Fixed

- **Light themes** (Breeze Light, Breath Light): the panel icon was a fixed
  white SVG and all but vanished on a light panel. It's now drawn in the
  panel's own text colour, so it's dark on a light panel and light on a dark
  one; the gold (MLO) and blue (6 GHz) tiers each have a light-panel and a
  dark-panel shade (at least 3:1 on Breeze / Breath panels), and whichever
  measures more contrast against the actual panel colour is used, so a mid-tone
  panel gets the better one. The degraded tier uses the theme's negative colour. Where Plasma shows the applet's icon itself, it's the themed
  `network-wireless-hotspot-symbolic`.
- The MCS grid on a light theme: cells seen since the popup opened were a muddy
  tan that outweighed the current cell, the current cell's number was white on
  light gold, and the rate beside "MCS n" was light gold text on white. Seen
  cells are now the current cell's colour at low alpha (pastel on light, deep
  on dark), the current cell's number is always dark, and the rate text is a
  deep shade of its hue on light themes (at least 4.5:1).

### Removed

- The four per-tier icon SVGs (`contents/icons/`), replaced by `PanelIcon.qml`.

## [1.8.1] - 2026-10-03 (patch)

### Fixed

- **Foreign routing is never touched.** The root helper's teardown (run every
  20 s by the dead-man timer, even with multipath off) flushed tables 101–116
  whole and deleted any rule at its priorities, so a VPN's policy routing there
  was wiped. It now removes only what it tagged (`proto 211`); a table adopted
  from pre-v1.0 hand-made rules is still flushed whole. Turning multipath on is
  refused, not applied over, when another tool's routes sit in tables 100–116.
- A refused or failing dead-man check is logged once, not every 20 s, and its
  log lines now reach the journal (they were INFO under `LogLevelMax=notice`).
- A release NetworkManager couldn't take (nmcli timing out, NM restarting) is
  kept and retried, instead of being forgotten with the radio's autoconnect
  still off: on daemon stop, on the fail-safe, and when multipath is turned off.
- Hand-back no longer turns autoconnect on for a radio whose autoconnect was
  already off before wifimimo held it.
- Unbinding a profile from one radio is tried once; if it doesn't stick
  (polkit refused the on-disk change) the profile gets the "tied to one card"
  flag instead of nmcli running every poll.
- `wifimimo-nm-tidy --apply` no longer resets the kept profile's
  `ipv4.route-metric`, and keeps a network's per-card copies if clearing the
  kept profile's binding fails.
- The widget's helper watchdog drops a timed-out run, so a retry of the same
  switch starts a new run and a late result can't land on a later click.
- The daemon survives a non-dict result from the follower.
- Tools: `walk-log` refuses a directory that already holds a recording, stops
  only its own recorders, and copies the join log into the walk (which
  `walk-summary` now reads); the new-connection probe bypasses proxies.
- Tests: the fail-safe test can no longer start the real root helper.

## [1.8.0] - 2026-09-28 (minor)

### Changed

- The follower now saves one change to a profile: a network saved bound to one
  radio by name (`connection.interface-name`, as the applet can save a newly
  joined network) has that binding cleared, so the other radios, and its saved
  password, can join it; it follows from the next poll. MAC bindings are still
  only flagged.
- `wifimimo-nm-tidy` also clears a lone profile's interface-name binding (it
  used to act only on networks with several profiles).

## [1.7.1] - 2026-09-28 (patch)

### Fixed

- The Internal Wi-Fi and Multipath switches no longer flip back to the old state
  after a toggle. The widget showed the daemon's last sample (taken before the
  helper acted) for up to a poll, so a click meant to correct the switch ran the
  opposite action. The helper's own fresh report now holds the switch until the
  daemon has sampled since.

## [1.7.0] - 2026-09-25 (minor)

### Added

- **Dead-man switches**, so any failure of wifimimo's control returns the radios
  and routing to NetworkManager:
  - a systemd watchdog on the daemon (45 s without a heartbeat → killed, radios
    handed back by `ExecStopPost`, restarted);
  - a root timer, `wifimimo-deadman.timer`, running `wifimimo-helper multipath
    check` every 20 s: re-checks routing with fresh gateway pings independently of
    the daemon, and tears wifimimo's routing down if checking or applying fails
    (quiet unless membership or the verdict changes);
  - a planner fail-safe: nothing through for 60 s → every radio back to
    NetworkManager for 5 min, then retry; shown in the widget's Multipath tooltip.

### Fixed

- A gateway that never answers pings no longer marks a radio dead while
  NetworkManager's connectivity check passes (public networks often drop ping).

## [1.6.1] - 2026-09-25 (patch)

### Fixed

- The widget no longer shadows Qt's `Item.data` with its own `data` property
  (renamed `cardData`), which drew a Qt warning on every load.

## [1.6.0] - 2026-09-25 (minor)

### Added

- **Per-site tuning**: `~/.config/wifimimo/roam.json` overrides the roaming
  thresholds (join / keep / dead signal levels, look-ahead, upgrade margin, join
  timeout, avoid time, traffic-check fuse, network-gone time). Validated against
  safe ranges, re-read when it changes; removing a key restores its default.
  Applied values and any problems show in the state file's `nm` block.

## [1.5.2] - 2026-09-25 (patch)

### Fixed

- A stopped or crashed daemon no longer leaves radios with NetworkManager's
  autoconnect blocked: the user service's `ExecStopPost` runs
  `wifimimo-daemon --release`, which hands every held or parked radio back. The
  next start re-holds them within a poll.

## [1.5.1] - 2026-09-25 (patch)

### Fixed

- The traffic check restarts, with its grace period, when a radio changes access
  point. Every AP here shares one gateway, so a radio that switched AP kept its
  pre-switch last reply and was called "no traffic" 2 s after landing.

## [1.5.0] - 2026-09-25 (minor)

### Changed

- **Avoiding an access point is two strikes**: the first failed join avoids it
  for 5 min, then it's tried again; a second failure avoids it until you choose
  the network again in the applet. A join that works clears the strikes. (Was a
  flat 30 min, or 2 min below -50 dBm: long enough to keep radios off an AP
  after it was fixed, short enough to keep retrying one that never works.)
  The widget's flag says which avoids are timed and which are for good.

## [1.4.0] - 2026-09-25 (minor)

### Changed

- **No races with NetworkManager**: while following a network, NetworkManager's
  autoconnect is blocked (in memory only) on every radio and re-asserted every
  poll, so only the planner connects radios. Live, a flickering radio was
  reconnected by NM to the access point the planner had just given another
  radio. If no radio has been on the network for 2 min and none can see it, the
  block is lifted and NetworkManager picks again; turning multipath off lifts it
  too. (If the daemon ever dies while holding, reboot or
  `nmcli device set <radio> autoconnect yes` restores NM's behaviour.)

## [1.3.1] - 2026-09-25 (patch)

### Fixed

- A per-card signal floor is learned only from joins that never got past
  association / handshake (the AP couldn't hear us). A join stuck waiting for
  an address is logged as "no address" and cancelled, and teaches no floor:
  the riverhouse AP heard us at -70 and every DHCP request was answered, the
  answers never reached the card.
- A card transmitting at the lowest legacy rate (the mute A8000 sat at 6 Mb/s)
  is declared dead after two missed gateway pings (4 s) instead of three.

## [1.3.0] - 2026-09-25 (minor)

### Added

- **Traffic check per radio**: a gateway ping bound to each connected radio every
  2 s; three misses (6 s) and the link is dead however strong its signal. A dead
  radio is taken off its access point, no longer counts as a working radio, and
  triggers a multipath re-apply (at most every 5 s).
- **Fast join give-up**: a join not fully up within 15 s is cancelled instead of
  waiting out NetworkManager's 45 s DHCP timeout.
- **Per-card signal floors**: a failed join raises the signal that card needs from
  that access point (reading + 6 dB) for 30 min.
- **Proven access points**: a working radio moves for capacity only to an access
  point some radio carried traffic on in the last 30 min.
- **Swaps**: two working radios trade access points when clearly better, only
  while a third radio carries traffic.

### Fixed

- The root helper's gateway check always pings through the radio; the neighbour
  cache kept a dead link's gateway "reachable" and it stayed a multipath member.

## [1.2.0] - 2026-09-25 (minor)

### Added

- Every join attempt and its outcome (target signal, landed / failed / refused /
  timed out, seconds, NetworkManager state and reason) is logged to
  `~/.local/state/wifimimo/joins.jsonl`.

### Changed

- No access point read below -72 dBm is ever tried (access points with a
  minimum-signal floor, e.g. the EAP720 at -75, refuse it). A failed join counts
  against an access point for every radio only at -50 dBm or better.

## [1.1.1] - 2026-09-25 (patch)

### Fixed

- One switch at a time: a radio mid-switch counts as busy until it lands or is
  judged failed (two overlapping switches left all three radios down for 66 s).
- A move drops the current link before joining (switching in place failed
  "no secrets").
- A strong access point that fails a join is avoided by every radio for a while;
  a radio that loses its access point avoids it for 60 s; joins aren't judged in
  their first 5 s; the only working radio is never moved for an upgrade.
- Radar (DFS) channels aren't joined while the regulatory domain is the world
  default, and nothing is joined within 2 s of a change.

## [1.1.0] - 2026-09-25 (minor)

### Added

- **Slot planning and early roaming** (`wifimimo_roam.py`): radios spread across
  non-overlapping channels first, then across access points; a radio with no
  strong slot of its own scouts (parked and scanning) instead of sharing another
  radio's access point. Signal trends move a fading radio before it crosses
  −75 dBm; scan tables give dBm, age, channel width and BSS load; per-card
  signal offsets are learned. The daemon polls every second while anything is
  moving.

### Fixed

- A replugged card's autoconnect, or a second saved profile for the same
  network, is no longer taken for a new network choice.

## [1.0.0] - 2026-09-25 (major)

### Added

- **Multipath**: a widget switch that balances new connections across every
  connected radio (L4 ECMP). Routes live outside the main table (rules 32000 /
  32001+ / 32090, tables 100-116, proto `wifimimo`), are rebuilt by a
  NetworkManager dispatcher hook on every network change, admit only radios with
  full connectivity and a reachable gateway, and survive reboots.
- **NetworkManager follow**: while multipath is on, choosing a network in the
  Plasma applet joins every radio to it (in-memory `multi-connect` only; saved
  profiles untouched), and disconnecting drops them all. `wifimimo-nm-tidy`
  removes hand-made per-card profile copies.
- **Internal card toggle** (`install.sh --manage-internal`): switch a built-in PCI
  card off (removed from the bus) or on (rescan + driver load) from the widget;
  persists across reboots via a generated udev rule.
- **Root helper** `wifimimo-helper` with a fixed verb set, polkit action allowing
  the active session without a password, dry-run mode.
- **Health flags**: USB 3 stick running at USB 2, USB 2-only port, radios sharing
  an access point or channel, ARP-flux risk, weak overall signal on MLD links,
  plus the existing link checks, as chips in the widget and lines in the monitor.
- **Signal graph**: last 60 s of every radio's signal on a fixed −90…−30 dBm axis.
- **Card names**: `A9000`, `A8000`, `Built-in`, vendor + chip otherwise; override in
  `~/.config/wifimimo/names.json`.
- `install.sh --migrate-legacy-rules` and `--uninstall`.

### Changed

- Popup is sized to its content with no scrolling and denser one-line meters; a
  disconnected card keeps the full layout with placeholders, so the card selector
  never moves.
- Card selector: one button per card, the first selected by default; the `auto`
  button is gone.
- State schema v4 (per-radio bus / USB speed / addressing / throughput / history /
  flags / colour; document-level multipath, internal card, NM, helper status).
- Daily history CSV gains `rx_mbps`, `tx_mbps`, `usb_speed_mbps`, `flags`.
- Alert thresholds consolidated in `wifimimo_core`.

### Removed

- `future.md` (the v0.3 refactor plan, fully implemented).

## [0.4.0] - 2026-09-07

### Added

- **Automatic wifi-card detection** — the daemon no longer hardwires
  `wlp1s0`. Every 802.11 station netdev under `/sys/class/net` is
  discovered on each poll, so swapping the internal card for a USB
  adapter (or hotplugging one mid-session) just works, no restart or
  configuration needed. `WIFI_IFACE=<name>` pins the daemon to one card
  when you want that; discovery filters to nl80211 *station* iftype so
  AP / monitor / mesh netdevs on the same radio are never polled as an
  uplink.
- **Multi-card polling + JSON state schema v3** — all discovered cards
  are collected every cycle, each with its own retry window, transition
  cooldown, and daily history-CSV rows. The state document's top level
  mirrors the *primary* card (connected wins) so schema-v2 readers keep
  working; new `ifaces` list and `interfaces` map carry every card's
  full state.
- **Plasmoid card selector** — a button row at the top of the popup
  (visible only with 2+ cards) picks which card is displayed, plus an
  `auto` button that returns to following the daemon's primary card.
  Min/max history resets on card switches as well as BSSID roams.
- **`wifimimo-mon <iface>`** now renders that card's own sub-state, and
  resets its history when the primary fails over in no-argument mode.
- **Per-adapter temperature** — hwmon sensors resolve through each
  interface's own device tree first, so two cards no longer share one
  reading.

### Fixed

- "Not connected" forever after a wifi card swap (the daemon kept
  polling the hardcoded, now-absent interface).
- `read_state` keeps defaults when `interfaces` / `ifaces` / `display`
  have the wrong JSON shape instead of letting malformed payloads crash
  consumers.

## [0.3.0] - 2026-05-13

### Added

- **Wi-Fi 7 / EHT support** end-to-end. New `phy_modes.py` registry is the
  single source of truth for HT / VHT / HE / EHT — netlink rate-info attrs,
  iw tokens, MCS max, efficiency tables, and Wi-Fi-N generation labels all
  flow from one place. EHT rate ladder includes 4096-QAM MCS 12 / 13.
- **MLO multi-link awareness**. `links[]` populated from `iw dev <iface> link`
  Link blocks; per-link freq / channel / width / BSSID surfaced to the UI.
  Primary-link selection prefers the BSSID matching `Connected to`, falls
  back to the highest-frequency link.
- **320 MHz bandwidth** mapped from `NL80211_CHANNEL_WIDTH` value 8.
- **JSON state schema (`schema_version: 2`)**. Versioned `WifiState` /
  `LinkInfo` / `DisplayState` dataclasses replace the hand-rolled
  key=value text format. v1 files still parse during the upgrade window
  via a migration shim in both Python and QML readers.
- **Daemon-computed `display` block** — band label, Wi-Fi-N label
  (Wi-Fi 7 / EHT, Wi-Fi 6E / HE, etc.), signal tier, signal/avg/spread
  fractions, NSS dots, GI labels, full per-MCS rate ladders,
  `mcs_grid_count`. UIs are dumb consumers; math lives in one place.
- **Five-tier panel icon** — grey (no link), red (degraded 1x1), white
  (2x2 single-link), blue (6 GHz non-MLO), gold (multi-link MLO active).
- **Adaptive expanded-poll cadence** — daemon drops from 5 s to 1 s while
  the popup is expanded (mediated by an XDG runtime marker file).
- **Per-line connection header** in the plasmoid: SSID + BSSID, freq(s)
  + channel + width, Wi-Fi N + IEEE PHY + link status, plus title bar
  combining `wifimimo vX.Y.Z` and `link uptime: Hh Mm Ss`.
- **Curses monitor parity** — same Wi-Fi-N label, MLO `LINKS` section
  when multi-link, identical uptime formatting.
- **Honest "data unavailable" hints** under SIGNAL and TX RETRIES when
  the driver can't surface per-antenna chain signal or per-MLD retry
  counters (mt7925 MLO firmware gap, confirmed dead at every kernel
  surface). Stops a static 0 from masquerading as a perfect link.
- **Same-day history CSV rotation** — when `HISTORY_COLUMNS` schema
  changes mid-day, the daemon rotates the existing file aside instead
  of appending new-shape rows under an old-shape header.
- **Tests + CI** — 52 pytest cases covering PHY-mode round-tripping,
  iw output fixtures, JSON state I/O + v1 migration, derived display
  shape, history rotation, and a QML parity check that catches
  reintroduced PHY-mode literals. GitHub Actions on `ubuntu-24.04` /
  Python 3.12.7.
- **Top-of-README screenshot** showing the expanded plasmoid panel.

### Changed

- `effectiveNss` (plasmoid) and `mimo_healthy` (daemon) now use
  `max(tx_nss, rx_nss)` instead of `min(...)`. Asymmetric NSS on
  MLO/EHT links (TX NSS 1 / RX NSS 2) is normal and no longer
  triggers a red icon or "MIMO Degraded" alert. The alert now requires
  *both* directions to have reported NSS and *both* to be < 2.
- `install.sh` reloads plasmashell via
  `systemctl --user restart plasma-plasmashell.service` instead of
  `kquitapp6 + kstart` (which left the panel dead from a `pkexec`
  context). The helper exports `DBUS_SESSION_BUS_ADDRESS` so user-bus
  systemd commands resolve correctly when re-executed via sudo.
- Section headings in the plasmoid use `Kirigami.Theme.textColor` for
  better readability on dark themes; gaps between sections widened
  via `Layout.topMargin`; gap between the link header and the SIGNAL
  section tightened.
- README rewritten end-to-end to match the actual capability surface.

### Fixed

- MLO MLD parents missing `NL80211_ATTR_WIPHY_FREQ` now surface
  freq / chan / bandwidth via the iw fallback path instead of
  rendering as `0 MHz / ?`.
- `_signal_tier` and `_signal_fraction` (Python and QML) treat
  `dbm >= 0` as invalid (default 0 / chain misreading) and return
  `crit` / `0.0` instead of mis-classifying as `good` / near-full bar.
- Telemetry sections hidden in the popup when no recent data — stops
  fake `0 dBm` / `0 Mb/s` rows from rendering under "Not connected".
- `parse_link_metrics` strips Link-block bodies before extracting
  top-level scalars (`signal:`, `freq:`, `tx/rx bitrate:`), so MLO
  outputs with per-Link content lines don't bind top-level fields
  to the wrong link.
- MLO Link-block detection anchored on line start (`re.MULTILINE`) so
  an SSID literally containing `Link N BSSID` doesn't mis-classify
  a non-MLO connection.
- `read_state` deep-merges the nested `display` dict on partial v2
  payloads instead of replacing it whole and dropping unspecified keys.
- History CSV writes safely no-op (don't append new-shape rows) when
  the schema-mismatch rotation fails.

[Unreleased]: https://github.com/pizzimenti/wifimimo/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/pizzimenti/wifimimo/releases/tag/v0.4.0
[0.3.0]: https://github.com/pizzimenti/wifimimo/releases/tag/v0.3.0
[0.2.0]: https://github.com/pizzimenti/wifimimo/releases/tag/v0.2.0
