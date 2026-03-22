# wifimimo

Wi-Fi MIMO antenna and link quality monitor for Linux.

Includes a background daemon, a terminal monitor, and a KDE Plasma 6 panel widget.

## Requirements

- Linux with `iw` and `nl80211`-based Wi-Fi driver
- Python 3
- KDE Plasma 6 (optional, for the panel widget)

## Install

```bash
git clone <this-repo> && cd wifimimo
./install.sh
```

This installs:

| File | Purpose |
|------|---------|
| `/usr/local/bin/wifimimo-daemon` | Background daemon that polls Wi-Fi station data |
| `/usr/local/bin/wifimimo-mon` | Terminal monitor launcher |
| `~/.config/systemd/user/wifimimo-daemon.service` | User systemd service |

## Daemon

The daemon (`wifimimo-daemon.py`) polls `iw` station dump at 1-second intervals and writes parsed state to a shared file. It tracks:

- Signal strength (overall, average, per-antenna)
- TX/RX rates, MCS index, NSS (spatial streams), guard interval
- TX retry rate over a 10-second sliding window
- Channel, bandwidth, frequency band

## Terminal Monitor

```bash
wifimimo-mon
```

Curses TUI showing live Wi-Fi link quality metrics.

## Plasma Widget

A KDE Plasma 6 panel widget that shows MIMO antenna state at a glance.

- **Panel icon**: Wi-Fi hotspot icon (alert variant when running single-stream)
- **Tooltip**: bandwidth, MIMO config, rate, and signal summary
- **Popup**: signal bars (overall, average, per-antenna, spread), TX/RX rate bars, MCS index grid with rate table, TX retry bar

Signal colors: green (>= -55 dBm), yellow (-55 to -70 dBm), red (< -70 dBm).

To add it to your panel after install: right-click panel > Add Widgets > search "wifimimo".

To manually install/upgrade the widget:

```bash
kpackagetool6 -t Plasma/Applet --upgrade plasmoid/org.kde.plasma.wifimimo
```

## Data Sources

- `iw dev <iface> station dump` — signal, per-antenna levels, rates, MCS, NSS, retries
- `iw dev <iface> link` — SSID, BSSID, frequency, bandwidth
- shared runtime state file — plasmoid reads `/run/user/$UID/wifimimo-state` directly

## License

MIT
