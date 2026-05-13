pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import QtCore

import org.kde.kirigami as Kirigami
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    preferredRepresentation: compactRepresentation

    // Build "cat /run/user/<uid>/wifimimo-state" once at startup. cat is a
    // few-millisecond fork (no Python interpreter, no venv) so the executable
    // engine stays cheap. We can't use XMLHttpRequest against file:// URLs in
    // Qt 6 — it's blocked unless QML_XHR_ALLOW_FILE_READ=1 is set in
    // plasmashell's environment, which would be a global side effect.
    readonly property string runtimeDir: StandardPaths.writableLocation(StandardPaths.RuntimeLocation).toString().replace(/^file:\/\//, "")
    readonly property string statePath: runtimeDir ? (runtimeDir + "/wifimimo-state") : ""
    readonly property string uiActivePath: runtimeDir ? (runtimeDir + "/wifimimo-ui-active") : ""
    // When the popup is expanded, the poll command also touches the
    // ui-active marker so the daemon knows to drop into fast-poll (1 s)
    // mode. Collapsed polls only read the state file — the marker ages
    // out and the daemon returns to slow-poll (5 s) on its own.
    readonly property string currentCommand: root.expanded
        ? "sh -c 'touch \"$1\"; if [ -f \"$2\" ]; then cat \"$2\"; fi' _ \"" + uiActivePath + "\" \"" + statePath + "\""
        : "sh -c 'if [ -f \"$1\" ]; then cat \"$1\"; fi' _ \"" + statePath + "\""
    property int refreshMs: 1000
    property int compactRefreshMs: 15000
    property string monospaceFamily: "monospace"

    readonly property var defaultDisplay: ({
        band_label: "?",
        signal_tier: "crit",
        signal_fraction: 0.0,
        signal_avg_fraction: 0.0,
        spread_fraction: 0.0,
        antenna_fractions: [],
        tx_nss_dots: "○○",
        rx_nss_dots: "○○",
        tx_gi_label: "",
        rx_gi_label: "",
        tx_rates_mbps: [],
        rx_rates_mbps: [],
        mcs_grid_count: 12
    })

    readonly property var defaultData: ({
        schema_version: 2,
        connected: false,
        iface: "wlp1s0",
        ssid: "",
        ssid_display: "",
        bssid: "",
        freq_mhz: 0,
        chan_num: 0,
        bandwidth_mhz: 0,
        signal_dbm: 0,
        signal_avg_dbm: 0,
        signal_antennas: [],
        tx_nss: 0,
        rx_nss: 0,
        tx_rate_mbps: 0.0,
        rx_rate_mbps: 0.0,
        tx_mcs: -1,
        rx_mcs: -1,
        tx_mode: "",
        rx_mode: "",
        tx_gi: -1,
        rx_gi: -1,
        tx_packets: 0,
        tx_retries: 0,
        tx_failed: 0,
        rx_packets: 0,
        connected_time_s: 0,
        station_dump_available: false,
        retry_10s_pct: 0.0,
        retry_10s_packets: 0,
        retry_10s_retries: 0,
        retry_10s_failed: 0,
        timestamp: 0,
        links: [],
        display: defaultDisplay
    })

    property var data: defaultData

    property real histSigOverallMinValue: 0
    property real histSigOverallMaxValue: 0
    property real histSigAvgMinValue: 0
    property real histSigAvgMaxValue: 0
    property real histSigAnt0MinValue: 0
    property real histSigAnt0MaxValue: 0
    property real histSigAnt1MinValue: 0
    property real histSigAnt1MaxValue: 0
    property real histSigSpreadMinValue: 0
    property real histSigSpreadMaxValue: 0
    property real histTxRateMinValue: 0
    property real histTxRateMaxValue: 0
    property real histRxRateMinValue: 0
    property real histRxRateMaxValue: 0
    property real histTxMcsMinValue: -1
    property real histTxMcsMaxValue: -1
    property real histRxMcsMinValue: -1
    property real histRxMcsMaxValue: -1
    property real histRetryPctMinValue: 0
    property real histRetryPctMaxValue: 0

    readonly property bool isConnected: !!(data && data.connected)
    readonly property var antennaSignals: (data && data.signal_antennas) ? data.signal_antennas : []
    readonly property var display: (data && data.display) ? data.display : defaultDisplay
    readonly property bool stale: !data || !data.connected || !data.timestamp || (Math.floor(Date.now() / 1000) - data.timestamp) > 15
    readonly property bool hasRecentData: isConnected && !stale
    readonly property int effectiveNss: {
        // Best observed NSS across either direction. Asymmetric NSS is normal
        // on MLO/EHT client links — uplink frequently sticks at NSS 1 while
        // downlink uses NSS 2. min(tx, rx) treats that as 1x1 MIMO and turns
        // the icon red even though the chip's antenna chains are healthy.
        // max(tx, rx) keeps the alert firing only when *both* directions
        // collapse to a single stream, which is the actual chain-failure
        // signature worth flagging.
        const tx = data.tx_nss > 0 ? data.tx_nss : 0;
        const rx = data.rx_nss > 0 ? data.rx_nss : 0;
        return Math.max(tx, rx);
    }
    readonly property int linkCount: (data && data.links) ? data.links.length : 0
    readonly property bool mloMultiLink: linkCount > 1
    // Read the band tier from the daemon-computed label so the 6 GHz floor
    // (5955 MHz, the UNII-5 boundary) is defined in one place
    // (phy_modes.SIX_GHZ_FLOOR_MHZ) — not duplicated as a literal here.
    readonly property bool onSixGhz: display.band_label === "6 GHz"

    // Five-tier icon state:
    //   disabled – no link / wifi off / stale data        (grey, normal SVG @ 45% opacity)
    //   alert    – connected, max(tx,rx) NSS < 2         (red)
    //   good     – connected, 2x2, multi-link MLO        (gold)
    //   wifi6e   – connected, 2x2, 6 GHz, non-MLO        (blue)
    //   normal   – connected, 2x2, everything else       (white / theme default)
    readonly property string iconTier: {
        if (!hasRecentData) {
            return "disabled";
        }
        // effectiveNss === 0 is "unknown" (partial payload during association),
        // not "degraded". Don't flip the icon red just because the rate-info
        // attrs haven't landed yet — that gives a transient red flash on
        // every reconnect. Real degradation requires NSS to be reported AND
        // be < 2.
        if (effectiveNss > 0 && effectiveNss < 2) {
            return "alert";
        }
        if (mloMultiLink) {
            return "good";
        }
        if (onSixGhz) {
            return "wifi6e";
        }
        return "normal";
    }
    readonly property url iconSource: {
        if (iconTier === "alert") {
            return Qt.resolvedUrl("../icons/network-wireless-hotspot-alert.svg");
        }
        if (iconTier === "good") {
            return Qt.resolvedUrl("../icons/network-wireless-hotspot-good.svg");
        }
        if (iconTier === "wifi6e") {
            return Qt.resolvedUrl("../icons/network-wireless-hotspot-wifi6e.svg");
        }
        // "normal" and "disabled" share the white SVG; opacity differentiates.
        return Qt.resolvedUrl("../icons/network-wireless-hotspot-normal.svg");
    }
    readonly property real iconOpacity: iconTier === "disabled" ? 0.45 : 1.0

    function pollNow() {
        if (!runtimeDir) {
            return;
        }
        executableSource.disconnectSource(currentCommand);
        executableSource.connectSource(currentCommand);
    }

    Component.onCompleted: {
        if (!runtimeDir) {
            console.warn("wifimimo: StandardPaths.RuntimeLocation is empty;",
                         "state polling disabled until plasmashell restart");
        }
    }

    function updateHistory(key, value) {
        switch (key) {
        case "sig_overall":
            histSigOverallMinValue = Math.min(histSigOverallMinValue, value);
            histSigOverallMaxValue = Math.max(histSigOverallMaxValue, value);
            break;
        case "sig_avg":
            histSigAvgMinValue = Math.min(histSigAvgMinValue, value);
            histSigAvgMaxValue = Math.max(histSigAvgMaxValue, value);
            break;
        case "sig_ant0":
            histSigAnt0MinValue = Math.min(histSigAnt0MinValue, value);
            histSigAnt0MaxValue = Math.max(histSigAnt0MaxValue, value);
            break;
        case "sig_ant1":
            histSigAnt1MinValue = Math.min(histSigAnt1MinValue, value);
            histSigAnt1MaxValue = Math.max(histSigAnt1MaxValue, value);
            break;
        case "sig_spread":
            histSigSpreadMinValue = Math.min(histSigSpreadMinValue, value);
            histSigSpreadMaxValue = Math.max(histSigSpreadMaxValue, value);
            break;
        case "tx_rate":
            histTxRateMinValue = Math.min(histTxRateMinValue, value);
            histTxRateMaxValue = Math.max(histTxRateMaxValue, value);
            break;
        case "rx_rate":
            histRxRateMinValue = Math.min(histRxRateMinValue, value);
            histRxRateMaxValue = Math.max(histRxRateMaxValue, value);
            break;
        case "tx_mcs":
            if (histTxMcsMinValue < 0) {
                histTxMcsMinValue = value;
                histTxMcsMaxValue = value;
            } else {
                histTxMcsMinValue = Math.min(histTxMcsMinValue, value);
                histTxMcsMaxValue = Math.max(histTxMcsMaxValue, value);
            }
            break;
        case "rx_mcs":
            if (histRxMcsMinValue < 0) {
                histRxMcsMinValue = value;
                histRxMcsMaxValue = value;
            } else {
                histRxMcsMinValue = Math.min(histRxMcsMinValue, value);
                histRxMcsMaxValue = Math.max(histRxMcsMaxValue, value);
            }
            break;
        case "retry_pct":
            histRetryPctMinValue = Math.min(histRetryPctMinValue, value);
            histRetryPctMaxValue = Math.max(histRetryPctMaxValue, value);
            break;
        }
    }

    function resetHistory(sample) {
        const next = sample || null;
        const antennaValues = next ? (next.signal_antennas || []) : [];
        const hasSpread = antennaValues.length >= 2;
        const spread = hasSpread ? Math.max.apply(Math, antennaValues) - Math.min.apply(Math, antennaValues) : 0;

        histSigOverallMinValue = next ? next.signal_dbm : 0;
        histSigOverallMaxValue = next ? next.signal_dbm : 0;
        histSigAvgMinValue = next ? next.signal_avg_dbm : 0;
        histSigAvgMaxValue = next ? next.signal_avg_dbm : 0;
        histSigAnt0MinValue = antennaValues.length >= 1 ? antennaValues[0] : 0;
        histSigAnt0MaxValue = antennaValues.length >= 1 ? antennaValues[0] : 0;
        histSigAnt1MinValue = antennaValues.length >= 2 ? antennaValues[1] : 0;
        histSigAnt1MaxValue = antennaValues.length >= 2 ? antennaValues[1] : 0;
        histSigSpreadMinValue = spread;
        histSigSpreadMaxValue = spread;
        histTxRateMinValue = next ? next.tx_rate_mbps : 0;
        histTxRateMaxValue = next ? next.tx_rate_mbps : 0;
        histRxRateMinValue = next ? next.rx_rate_mbps : 0;
        histRxRateMaxValue = next ? next.rx_rate_mbps : 0;
        histTxMcsMinValue = next && next.tx_mcs >= 0 ? next.tx_mcs : -1;
        histTxMcsMaxValue = next && next.tx_mcs >= 0 ? next.tx_mcs : -1;
        histRxMcsMinValue = next && next.rx_mcs >= 0 ? next.rx_mcs : -1;
        histRxMcsMaxValue = next && next.rx_mcs >= 0 ? next.rx_mcs : -1;
        histRetryPctMinValue = next ? next.retry_10s_pct : 0;
        histRetryPctMaxValue = next ? next.retry_10s_pct : 0;
    }

    function histMin(key, fallback) {
        switch (key) {
        case "sig_overall":
            return histSigOverallMinValue;
        case "sig_avg":
            return histSigAvgMinValue;
        case "sig_ant0":
            return histSigAnt0MinValue;
        case "sig_ant1":
            return histSigAnt1MinValue;
        case "sig_spread":
            return histSigSpreadMinValue;
        case "tx_rate":
            return histTxRateMinValue;
        case "rx_rate":
            return histRxRateMinValue;
        case "tx_mcs":
            return histTxMcsMinValue >= 0 ? histTxMcsMinValue : fallback;
        case "rx_mcs":
            return histRxMcsMinValue >= 0 ? histRxMcsMinValue : fallback;
        case "retry_pct":
            return histRetryPctMinValue;
        default:
            return fallback;
        }
    }

    function histMax(key, fallback) {
        switch (key) {
        case "sig_overall":
            return histSigOverallMaxValue;
        case "sig_avg":
            return histSigAvgMaxValue;
        case "sig_ant0":
            return histSigAnt0MaxValue;
        case "sig_ant1":
            return histSigAnt1MaxValue;
        case "sig_spread":
            return histSigSpreadMaxValue;
        case "tx_rate":
            return histTxRateMaxValue;
        case "rx_rate":
            return histRxRateMaxValue;
        case "tx_mcs":
            return histTxMcsMaxValue >= 0 ? histTxMcsMaxValue : fallback;
        case "rx_mcs":
            return histRxMcsMaxValue >= 0 ? histRxMcsMaxValue : fallback;
        case "retry_pct":
            return histRetryPctMaxValue;
        default:
            return fallback;
        }
    }

    function validateState(obj) {
        // Merge incoming JSON with defaults so missing keys (older daemon,
        // partial payload, schema additions) don't NPE in bindings.
        const merged = JSON.parse(JSON.stringify(defaultData));
        if (obj && typeof obj === "object") {
            for (const k in obj) {
                if (Object.prototype.hasOwnProperty.call(obj, k)) {
                    merged[k] = obj[k];
                }
            }
        }
        if (!merged.display || typeof merged.display !== "object") {
            merged.display = JSON.parse(JSON.stringify(defaultDisplay));
        } else {
            const d = JSON.parse(JSON.stringify(defaultDisplay));
            for (const k in merged.display) {
                if (Object.prototype.hasOwnProperty.call(merged.display, k)) {
                    d[k] = merged.display[k];
                }
            }
            merged.display = d;
        }
        if (!Array.isArray(merged.signal_antennas)) {
            merged.signal_antennas = [];
        }
        if (!Array.isArray(merged.links)) {
            merged.links = [];
        }
        return merged;
    }

    function parseState(rawText) {
        const previousConnected = !!(data && data.connected);
        const previousBssid = data && data.bssid ? data.bssid : "";
        let parsed = null;
        const trimmed = (rawText || "").trim();
        if (trimmed.length > 0 && trimmed.charAt(0) === "{") {
            try {
                parsed = JSON.parse(trimmed);
            } catch (e) {
                console.warn("wifimimo: state JSON parse failed:", e);
                parsed = null;
            }
        } else if (trimmed.length > 0) {
            // Legacy v1 (key=value) — survives the upgrade window before the
            // daemon restarts onto the new JSON format.
            parsed = parseStateV1Lines(trimmed);
        }

        const next = validateState(parsed);

        const bssidChanged = previousConnected && next.connected
            && previousBssid.length > 0
            && next.bssid.length > 0
            && previousBssid !== next.bssid;

        data = next;

        if (!next.connected) {
            if (previousConnected) {
                resetHistory(null);
            }
            return;
        }

        if (!root.expanded) {
            if (!previousConnected || bssidChanged) {
                resetHistory(next);
            }
            return;
        }

        if (!previousConnected || bssidChanged) {
            resetHistory(next);
            return;
        }

        updateHistory("sig_overall", next.signal_dbm);
        updateHistory("sig_avg", next.signal_avg_dbm);
        for (let i = 0; i < next.signal_antennas.length; ++i) {
            updateHistory("sig_ant" + i, next.signal_antennas[i]);
        }
        if (next.signal_antennas.length >= 2) {
            updateHistory("sig_spread", Math.max.apply(Math, next.signal_antennas) - Math.min.apply(Math, next.signal_antennas));
        }
        updateHistory("tx_rate", next.tx_rate_mbps);
        updateHistory("rx_rate", next.rx_rate_mbps);
        if (next.tx_mcs >= 0) {
            updateHistory("tx_mcs", next.tx_mcs);
        }
        if (next.rx_mcs >= 0) {
            updateHistory("rx_mcs", next.rx_mcs);
        }
        updateHistory("retry_pct", next.retry_10s_pct);
    }

    function parseStateV1Lines(rawText) {
        const obj = {};
        // Index antennas by their numeric suffix so a v1 file with
        // reordered or sparse `antenna_N` keys still produces the right
        // chain order. push() would silently scramble the chains if iw
        // emitted them out of order.
        const antennaByIndex = {};
        const lines = rawText.split(/\r?\n/);
        for (const line of lines) {
            const idx = line.indexOf("=");
            if (idx < 0) {
                continue;
            }
            const key = line.slice(0, idx);
            const value = line.slice(idx + 1);
            if (key === "connected" || key === "station_dump_available") {
                obj[key] = value === "true";
            } else {
                const antennaMatch = key.match(/^antenna_(\d+)$/);
                if (antennaMatch) {
                    antennaByIndex[parseInt(antennaMatch[1], 10)] = Number(value) || 0;
                } else if (/^(freq_mhz|chan_num|bandwidth_mhz|signal_dbm|signal_avg_dbm|tx_nss|rx_nss|tx_mcs|rx_mcs|tx_gi|rx_gi|tx_packets|tx_retries|tx_failed|rx_packets|connected_time_s|retry_10s_packets|retry_10s_retries|retry_10s_failed|timestamp)$/.test(key)) {
                    obj[key] = Number(value) || 0;
                } else if (/^(tx_rate_mbps|rx_rate_mbps|retry_10s_pct|card_temp_c)$/.test(key)) {
                    obj[key] = Number(value) || 0;
                } else {
                    obj[key] = value;
                }
            }
        }
        const indices = Object.keys(antennaByIndex);
        if (indices.length) {
            obj.signal_antennas = indices
                .map(i => parseInt(i, 10))
                .sort((a, b) => a - b)
                .map(i => antennaByIndex[i]);
        }
        return obj;
    }

    function alertColor(value, warnThreshold, critThreshold) {
        if (value > critThreshold) {
            return Kirigami.Theme.negativeTextColor;
        }
        if (value > warnThreshold) {
            return Kirigami.Theme.neutralTextColor;
        }
        return Kirigami.Theme.positiveTextColor;
    }

    function tierColor(tier) {
        if (tier === "crit") {
            return Kirigami.Theme.negativeTextColor;
        }
        if (tier === "warn") {
            return Kirigami.Theme.neutralTextColor;
        }
        return Kirigami.Theme.positiveTextColor;
    }

    function signalColorForDbm(dbm) {
        // Used for historical-low markers where we only have the raw dBm value;
        // mirrors the canonical tier thresholds from wifimimo_core.SIGNAL_*_DBM.
        // dbm >= 0 is the dataclass default or a chain-misreading driver bug —
        // either way it's not a healthy reading, so flag negative (matches the
        // Python _signal_tier short-circuit).
        if (dbm >= 0) {
            return Kirigami.Theme.negativeTextColor;
        }
        if (dbm < -75) {
            return Kirigami.Theme.negativeTextColor;
        }
        if (dbm < -65) {
            return Kirigami.Theme.neutralTextColor;
        }
        return Kirigami.Theme.positiveTextColor;
    }

    function signalFractionForDbm(dbm) {
        // Mirror of wifimimo_core._signal_fraction so historical markers (min/max)
        // can be positioned on the bar. The *current* value's fraction comes from
        // display.signal_fraction. dbm >= 0 collapses to 0 so the misreading
        // doesn't paint a deceptively full bar.
        if (dbm >= 0) {
            return 0;
        }
        return Math.max(0, Math.min(1, (dbm + 90) / 70));
    }

    function spreadFraction(spread) {
        return Math.max(0, Math.min(1, spread / 30));
    }

    function fmtUptime(secs) {
        const h = Math.floor(secs / 3600);
        const m = Math.floor((secs % 3600) / 60);
        const s = secs % 60;
        if (h > 0) {
            return h + "h " + String(m).padStart(2, "0") + "m " + String(s).padStart(2, "0") + "s";
        }
        return m + "m " + String(s).padStart(2, "0") + "s";
    }

    function fmtClock(ts) {
        if (!ts) {
            return "--:--:--";
        }
        return new Date(ts * 1000).toLocaleTimeString(Qt.locale(), "HH:mm:ss");
    }

    function freqLine() {
        // Freq, channel, and width per link. Drop the band label (6 GHz /
        // 5 GHz / 2.4 GHz) — the freq number already encodes the band, and
        // duplicating it is what the user called out as inconsistent. The
        // overall channel width applies to the current rate; we attach it
        // once at the end (true for both single- and multi-link cases).
        const links = data.links || [];
        const width = data.bandwidth_mhz > 0 ? "   " + data.bandwidth_mhz + " MHz" : "";
        if (mloMultiLink) {
            const parts = [];
            for (let i = 0; i < links.length; ++i) {
                const l = links[i];
                const ch = l.chan_num > 0 ? " ch" + l.chan_num : "";
                parts.push(l.freq_mhz + " MHz" + ch);
            }
            return parts.join("  +  ") + width;
        }
        const ch = data.chan_num > 0 ? " ch" + data.chan_num : "";
        return data.freq_mhz + " MHz" + ch + width;
    }

    function linkStatusLine() {
        // Wi-Fi N / IEEE-PHY label comes from the daemon (display.wifi_label)
        // so the QML doesn't carry PHY-mode strings itself.
        const wifi = display.wifi_label || "";
        const sep = wifi ? "   " : "";
        switch (iconTier) {
        case "alert":
            return (wifi ? wifi + sep : "") + "Degraded (" + effectiveNss + "x" + effectiveNss + " MIMO)";
        case "good":
            return wifi + sep + "MLO " + linkCount + " links aggregated";
        default:  // "wifi6e", "normal"
            return wifi ? wifi + sep + "single link" : "Single link";
        }
    }

    function antennaSignalAt(index) {
        return index < antennaSignals.length ? antennaSignals[index] : 0;
    }

    function spreadValue() {
        if (antennaSignals.length >= 2) {
            return Math.max.apply(Math, antennaSignals) - Math.min.apply(Math, antennaSignals);
        }
        return 0;
    }

    function buildSignalModel() {
        // Only include per-antenna rows when the driver actually exposes a
        // chain-signal list. mt7925 in MLO mode aggregates to MLD-level and
        // leaves NL80211_STA_INFO_CHAIN_SIGNAL empty; rendering "0 dBm" rows
        // for absent antennas is more misleading than just omitting them.
        const rows = [
            { label: "Overall", value: root.data.signal_dbm, hist: "sig_overall", kind: "signal" },
            { label: "Avg", value: root.data.signal_avg_dbm, hist: "sig_avg", kind: "signal" }
        ];
        for (let i = 0; i < antennaSignals.length; ++i) {
            rows.push({ label: "Antenna " + (i + 1), value: antennaSignals[i], hist: "sig_ant" + i, kind: "signal" });
        }
        if (antennaSignals.length >= 2) {
            rows.push({ label: "Spread", value: spreadValue(), hist: "sig_spread", kind: "spread", suffix: "warn >15" });
        }
        return rows;
    }

    function displayMcs(value) {
        return value >= 0 ? String(value) : "-";
    }

    function mcsColor(index, current, lo, hi, maxIndex) {
        const t = maxIndex > 0 ? index / maxIndex : 0;
        const hue = 0.02 + (0.12 - 0.02) * t;
        const base = Qt.hsla(hue, 0.70, 0.62, 1.0);
        if (current >= 0 && index === current) {
            return base;
        }
        if (lo >= 0 && hi >= 0 && index >= lo && index <= hi) {
            return Qt.hsla(hue, 0.55, 0.45, 0.70);
        }
        return Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.12);
    }

    compactRepresentation: MouseArea {
        acceptedButtons: Qt.LeftButton
        implicitWidth: Kirigami.Units.iconSizes.smallMedium
        implicitHeight: Kirigami.Units.iconSizes.smallMedium
        onClicked: root.expanded = !root.expanded

        Kirigami.Icon {
            anchors.fill: parent
            anchors.margins: 1
            source: root.iconSource
            isMask: false
            color: "transparent"
            opacity: root.iconOpacity
            active: root.expanded
        }
    }

    Plasma5Support.DataSource {
        id: executableSource
        engine: "executable"
        interval: 0
        onNewData: (sourceName, sourceData) => {
            if (sourceName !== root.currentCommand) {
                return;
            }
            root.parseState(sourceData.stdout || "");
            executableSource.disconnectSource(sourceName);
        }
    }

    Timer {
        id: pollTimer
        interval: root.expanded ? root.refreshMs : root.compactRefreshMs
        repeat: true
        running: !!root.runtimeDir
        triggeredOnStart: true
        onTriggered: root.pollNow()
    }

    onExpandedChanged: function() {
        if (root.expanded) {
            resetHistory(root.data && root.data.connected ? root.data : null);
            root.pollNow();
        }
    }

    fullRepresentation: PlasmaExtras.Representation {
        // No fixed height — the panel sizes to its content so we get
        // uniform spacing between every section instead of a single
        // fillHeight-driven slack pocket above SIGNAL.
        Layout.minimumWidth:  Kirigami.Units.gridUnit * 30
        Layout.maximumWidth:  Kirigami.Units.gridUnit * 30
        collapseMarginsHint: true

        ColumnLayout {
            id: contentColumn
            anchors {
                fill: parent
                margins: Kirigami.Units.smallSpacing
            }
            spacing: 3

            // Title row: "wifimimo v0.2.0      link uptime: 7h 09m 23s"
            // All one font size; "wifimimo" bold, version regular, uptime dim.
            // Version is pulled from the plasmoid's metadata.json so the
            // string moves in lockstep with `Version` there.
            RowLayout {
                Layout.fillWidth: true
                spacing: 0

                PlasmaComponents3.Label {
                    text: "wifimimo"
                    font.bold: true
                    font.pixelSize: Math.round(Kirigami.Theme.defaultFont.pixelSize * 1.5)
                    font.family: root.monospaceFamily
                }

                PlasmaComponents3.Label {
                    text: "  v" + (Plasmoid.metaData && Plasmoid.metaData.version ? Plasmoid.metaData.version : "")
                    font.pixelSize: Math.round(Kirigami.Theme.defaultFont.pixelSize * 1.5)
                    font.family: root.monospaceFamily
                }

                Item {
                    Layout.fillWidth: true
                }

                PlasmaComponents3.Label {
                    visible: root.hasRecentData
                    text: "link uptime: " + (root.data.connected_time_s > 0 ? root.fmtUptime(root.data.connected_time_s) : "?")
                    font.pixelSize: Math.round(Kirigami.Theme.defaultFont.pixelSize * 1.5)
                    font.family: root.monospaceFamily
                    color: Kirigami.Theme.disabledTextColor
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1
                visible: root.hasRecentData

                PlasmaComponents3.Label {
                    Layout.fillWidth: true
                    text: (root.data.ssid_display || root.data.ssid || root.data.bssid) + "  (" + root.data.bssid + ")"
                    elide: Text.ElideRight
                    font.bold: true
                    font.pixelSize: Math.round(Kirigami.Theme.defaultFont.pixelSize * 1.20)
                    font.family: root.monospaceFamily
                    color: Kirigami.Theme.positiveTextColor
                }

                PlasmaComponents3.Label {
                    Layout.fillWidth: true
                    text: root.freqLine()
                    font.pixelSize: Math.round(Kirigami.Theme.defaultFont.pixelSize * 1.10)
                    font.family: root.monospaceFamily
                    color: Kirigami.Theme.textColor
                }

                PlasmaComponents3.Label {
                    Layout.fillWidth: true
                    text: root.linkStatusLine()
                    font.pixelSize: Math.round(Kirigami.Theme.defaultFont.pixelSize * 1.10)
                    font.family: root.monospaceFamily
                    color: Kirigami.Theme.textColor
                }
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                visible: !root.hasRecentData
                text: "Not connected on " + root.data.iface
                font.bold: true
                font.family: root.monospaceFamily
                color: Kirigami.Theme.negativeTextColor
            }

            // Telemetry sections are hidden when there's no recent sample
            // — otherwise the panel renders default 0 dBm / 0 Mb/s / 0% rows
            // under "Not connected", which look like real-but-zero readings.
            // Negative top-margin tightens the gap between the link header
            // and the first section (SIGNAL); per-section topMargin on
            // RATES / MCS INDEX / TX RETRIES adds breathing room between
            // the four telemetry blocks.
            ColumnLayout {
                Layout.fillWidth: true
                Layout.topMargin: -2
                visible: root.hasRecentData
                spacing: 2

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: "SIGNAL"
                font.bold: true
                font.family: root.monospaceFamily
                color: Kirigami.Theme.textColor
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                // Visible whenever the kernel has reported the link is up but
                // the chain-signal list is empty — the per-antenna telemetry
                // gap is structural (driver doesn't surface it for MLD
                // stations), not transient.
                visible: root.hasRecentData && root.antennaSignals.length === 0
                text: "Per-antenna data unavailable (MLD-level signal only)"
                wrapMode: Text.Wrap
                color: Kirigami.Theme.disabledTextColor
                font.family: root.monospaceFamily
                font.italic: true
                font.pixelSize: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 2)
            }

            Repeater {
                model: root.buildSignalModel()

                delegate: ColumnLayout {
                    id: signalBlock
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 0

                    readonly property real frac: signalBlock.modelData.kind === "spread"
                        ? root.spreadFraction(signalBlock.modelData.value)
                        : root.signalFractionForDbm(signalBlock.modelData.value)
                    readonly property var fillColor: signalBlock.modelData.kind === "spread"
                        ? root.alertColor(signalBlock.modelData.value, 10, 15)
                        : root.signalColorForDbm(signalBlock.modelData.value)

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.largeSpacing

                        PlasmaComponents3.Label {
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 6
                            text: signalBlock.modelData.label
                            color: Kirigami.Theme.disabledTextColor
                            font.family: root.monospaceFamily
                        }

                        PlasmaComponents3.Label {
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 5
                            text: Number(signalBlock.modelData.value).toFixed(0) + " dBm"
                            color: signalBlock.fillColor
                            font.family: root.monospaceFamily
                        }

                        PlasmaComponents3.Label {
                            Layout.fillWidth: true
                            text: Number(root.histMin(signalBlock.modelData.hist, signalBlock.modelData.value)).toFixed(0)
                                  + " .. "
                                  + Number(root.histMax(signalBlock.modelData.hist, signalBlock.modelData.value)).toFixed(0)
                                  + (signalBlock.modelData.suffix ? "  " + signalBlock.modelData.suffix : "")
                            horizontalAlignment: Text.AlignRight
                            color: Kirigami.Theme.disabledTextColor
                            font.family: root.monospaceFamily
                        }
                    }

                    Item {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 12
                        Layout.minimumHeight: 12
                        Layout.maximumHeight: 12

                        Rectangle {
                            anchors.fill: parent
                            radius: height / 2
                            color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.12)
                        }

                        Rectangle {
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            width: Math.max(4, parent.width * signalBlock.frac)
                            radius: height / 2
                            color: signalBlock.fillColor
                        }

                        Rectangle {
                            width: 2
                            radius: 1
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            x: Math.max(0, Math.min(parent.width - width, parent.width * (signalBlock.modelData.kind === "spread"
                                ? root.spreadFraction(root.histMax(signalBlock.modelData.hist, signalBlock.modelData.value))
                                : root.signalFractionForDbm(root.histMax(signalBlock.modelData.hist, signalBlock.modelData.value)))))
                            color: Kirigami.Theme.textColor
                            opacity: 0.6
                        }
                    }
                }
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                Layout.topMargin: 10
                text: "RATES"
                font.bold: true
                font.family: root.monospaceFamily
                color: Kirigami.Theme.textColor
            }

            Repeater {
                model: [
                    { label: "TX", rate: root.data.tx_rate_mbps, nss: root.data.tx_nss, mcs: root.data.tx_mcs, rates: root.display.tx_rates_mbps, nss_dots: root.display.tx_nss_dots, gi_label: root.display.tx_gi_label, hist: "tx_rate" },
                    { label: "RX", rate: root.data.rx_rate_mbps, nss: root.data.rx_nss, mcs: root.data.rx_mcs, rates: root.display.rx_rates_mbps, nss_dots: root.display.rx_nss_dots, gi_label: root.display.rx_gi_label, hist: "rx_rate" }
                ]

                delegate: ColumnLayout {
                    id: rateBlock
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 0

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.largeSpacing

                        PlasmaComponents3.Label {
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 3
                            text: rateBlock.modelData.label
                            color: Kirigami.Theme.disabledTextColor
                            font.family: root.monospaceFamily
                        }

                        PlasmaComponents3.Label {
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 6
                            text: Number(rateBlock.modelData.rate).toFixed(1) + " Mb/s"
                            font.family: root.monospaceFamily
                        }

                        PlasmaComponents3.Label {
                            Layout.fillWidth: true
                            text: Number(root.histMin(rateBlock.modelData.hist, rateBlock.modelData.rate)).toFixed(0)
                                  + " .. "
                                  + Number(root.histMax(rateBlock.modelData.hist, rateBlock.modelData.rate)).toFixed(0)
                                  + "  NSS " + rateBlock.modelData.nss + " " + rateBlock.modelData.nss_dots
                                  + (rateBlock.modelData.gi_label ? "  GI " + rateBlock.modelData.gi_label : "")
                            horizontalAlignment: Text.AlignRight
                            color: Kirigami.Theme.disabledTextColor
                            font.family: root.monospaceFamily
                        }
                    }

                    Item {
                        id: rateBar
                        Layout.fillWidth: true
                        Layout.preferredHeight: 12
                        Layout.minimumHeight: 12
                        Layout.maximumHeight: 12

                        readonly property real ceiling: {
                            const rates = rateBlock.modelData.rates;
                            if (rates && rates.length > 0) {
                                return Math.max(rates[rates.length - 1], 1.0);
                            }
                            return Math.max(rateBlock.modelData.rate, 1.0);
                        }

                        Rectangle {
                            anchors.fill: parent
                            radius: height / 2
                            color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.12)
                        }

                        Rectangle {
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            width: Math.max(4, parent.width * Math.max(0, Math.min(1, rateBlock.modelData.rate / rateBar.ceiling)))
                            radius: height / 2
                            color: Kirigami.Theme.positiveTextColor
                        }

                        Rectangle {
                            width: 2
                            radius: 1
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            x: Math.max(0, Math.min(parent.width - width, parent.width * Math.max(0, Math.min(1, root.histMax(rateBlock.modelData.hist, rateBlock.modelData.rate) / rateBar.ceiling))))
                            color: Kirigami.Theme.textColor
                            opacity: 0.6
                        }
                    }
                }
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                Layout.topMargin: 10
                text: "MCS INDEX"
                font.bold: true
                font.family: root.monospaceFamily
                color: Kirigami.Theme.textColor
            }

            Repeater {
                model: [
                    { label: "TX", mcs: root.data.tx_mcs, rate: root.data.tx_rate_mbps, rates: root.display.tx_rates_mbps, hist: "tx_mcs" },
                    { label: "RX", mcs: root.data.rx_mcs, rate: root.data.rx_rate_mbps, rates: root.display.rx_rates_mbps, hist: "rx_mcs" }
                ]

                delegate: ColumnLayout {
                    id: mcsBlock
                    required property var modelData
                    readonly property var rates: mcsBlock.modelData.rates || []
                    readonly property int gridCount: mcsBlock.rates.length > 0 ? mcsBlock.rates.length : root.display.mcs_grid_count
                    Layout.fillWidth: true
                    spacing: 1

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        PlasmaComponents3.Label {
                            text: mcsBlock.modelData.label + "  MCS " + root.displayMcs(mcsBlock.modelData.mcs)
                            font.family: root.monospaceFamily
                        }

                        PlasmaComponents3.Label {
                            text: Number(mcsBlock.modelData.rate).toFixed(0) + " Mb/s"
                            color: root.mcsColor(
                                Math.max(0, mcsBlock.modelData.mcs),
                                mcsBlock.modelData.mcs,
                                mcsBlock.modelData.mcs,
                                mcsBlock.modelData.mcs,
                                Math.max(0, mcsBlock.gridCount - 1)
                            )
                            font.family: root.monospaceFamily
                        }

                        Item {
                            Layout.fillWidth: true
                        }

                        PlasmaComponents3.Label {
                            text: mcsBlock.modelData.mcs >= 0
                                  ? ("min " + Number(root.histMin(mcsBlock.modelData.hist, mcsBlock.modelData.mcs)).toFixed(0)
                                     + "  max " + Number(root.histMax(mcsBlock.modelData.hist, mcsBlock.modelData.mcs)).toFixed(0))
                                  : "min -  max -"
                            font.family: root.monospaceFamily
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 1

                        Repeater {
                            model: mcsBlock.gridCount

                            delegate: Rectangle {
                                required property int index
                                Layout.fillWidth: true
                                Layout.preferredHeight: Kirigami.Units.gridUnit * 1.4
                                radius: 3
                                color: root.mcsColor(
                                    index,
                                    mcsBlock.modelData.mcs,
                                    mcsBlock.modelData.mcs >= 0 ? root.histMin(mcsBlock.modelData.hist, mcsBlock.modelData.mcs) : -1,
                                    mcsBlock.modelData.mcs >= 0 ? root.histMax(mcsBlock.modelData.hist, mcsBlock.modelData.mcs) : -1,
                                    Math.max(0, mcsBlock.gridCount - 1)
                                )

                                PlasmaComponents3.Label {
                                    anchors.centerIn: parent
                                    text: index
                                    color: index === mcsBlock.modelData.mcs ? Kirigami.Theme.backgroundColor : Kirigami.Theme.textColor
                                    font.family: root.monospaceFamily
                                    font.pixelSize: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 2)
                                }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 1

                        Repeater {
                            model: mcsBlock.gridCount

                            // Wrap each rate label in an Item so the row's
                            // Layout.fillWidth distributes equal widths (Item
                            // has implicitWidth 0) — using a bare Label gave
                            // proportional widths based on text content, so
                            // "144" claimed less width than "2882" and the
                            // rate centers drifted off the MCS cell centers.
                            delegate: Item {
                                required property int index
                                Layout.fillWidth: true
                                Layout.preferredHeight: rateLabel.implicitHeight

                                PlasmaComponents3.Label {
                                    id: rateLabel
                                    anchors.centerIn: parent
                                    text: parent.index < mcsBlock.rates.length ? mcsBlock.rates[parent.index] : "-"
                                    color: Kirigami.Theme.disabledTextColor
                                    font.family: root.monospaceFamily
                                    font.pixelSize: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 2)
                                }
                            }
                        }
                    }
                }
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                Layout.topMargin: 10
                text: "TX RETRIES"
                font.bold: true
                font.family: root.monospaceFamily
                color: Kirigami.Theme.textColor
            }

            ColumnLayout {
                id: retryBlock
                Layout.fillWidth: true
                spacing: 0

                readonly property real retryPct: Number(root.data && root.data.retry_10s_pct !== undefined ? root.data.retry_10s_pct : 0)

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.mediumSpacing

                    PlasmaComponents3.Label {
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 6
                        text: "Retry rate"
                        color: Kirigami.Theme.disabledTextColor
                        font.family: root.monospaceFamily
                    }

                    PlasmaComponents3.Label {
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 5
                        text: retryBlock.retryPct.toFixed(1) + "%"
                        color: root.alertColor(retryBlock.retryPct, 10, 30)
                        font.family: root.monospaceFamily
                    }

                    PlasmaComponents3.Label {
                        Layout.fillWidth: true
                        text: root.histMax("retry_pct", retryBlock.retryPct).toFixed(1) + "% max  "
                              + root.data.retry_10s_retries + "/" + root.data.retry_10s_packets + " retries  fail " + root.data.retry_10s_failed
                        horizontalAlignment: Text.AlignRight
                        color: Kirigami.Theme.disabledTextColor
                        font.family: root.monospaceFamily
                    }
                }

                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 12
                    Layout.minimumHeight: 12
                    Layout.maximumHeight: 12

                    Rectangle {
                        anchors.fill: parent
                        radius: height / 2
                        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.12)
                    }

                    Rectangle {
                        anchors.left: parent.left
                        anchors.top: parent.top
                        anchors.bottom: parent.bottom
                        width: Math.max(4, parent.width * Math.max(0, Math.min(1, retryBlock.retryPct / 100.0)))
                        radius: height / 2
                        color: root.alertColor(retryBlock.retryPct, 10, 30)
                    }

                    Rectangle {
                        width: 2
                        radius: 1
                        anchors.top: parent.top
                        anchors.bottom: parent.bottom
                        x: Math.max(0, Math.min(parent.width - width, parent.width * Math.max(0, Math.min(1, root.histMax("retry_pct", retryBlock.retryPct) / 100.0))))
                        color: Kirigami.Theme.textColor
                        opacity: 0.6
                    }
                }
            }

            }  // end of "telemetry sections visible only when hasRecentData"
        }
    }

    // Keep the icon visible at all times — PassiveStatus would auto-hide it
    // in the collapsed tray, but the user wants a greyed icon they can see
    // (so they know the daemon is running and the link is just down).
    Plasmoid.status: iconTier === "alert"
        ? PlasmaCore.Types.NeedsAttentionStatus
        : PlasmaCore.Types.ActiveStatus
    Plasmoid.icon: iconSource
    toolTipMainText: "wifimimo"
    toolTipSubText: stale || !data.connected
        ? "No recent antenna data"
        : "Bandwidth " + (data.bandwidth_mhz > 0 ? data.bandwidth_mhz + " MHz" : "width unknown")
          + "  ·  " + effectiveNss + "x" + effectiveNss + " MIMO"
          + (mloMultiLink ? ("  ·  MLO " + linkCount + " links") : "")
          + "\nOverall " + Math.min(data.tx_rate_mbps || 0, data.rx_rate_mbps || 0).toFixed(1) + " MBit/s"
          + "  ·  Signal " + data.signal_dbm + " dBm"
    toolTipTextFormat: Text.PlainText
}
