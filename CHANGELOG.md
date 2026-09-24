# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

Planned as 1.0.0.

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
