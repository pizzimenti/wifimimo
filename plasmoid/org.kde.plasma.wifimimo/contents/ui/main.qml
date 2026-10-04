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
        schema_version: 4,
        connected: false,
        iface: "",
        card_name: "",
        perm_mac: "",
        bus: "",
        driver: "",
        dev_id: "",
        usb_speed_mbps: 0,
        operstate: "",
        ipv4: "",
        prefixlen: 0,
        rx_mbps: 0.0,
        tx_mbps: 0.0,
        signal_history: [],
        flags: [],
        color_index: -1,
        internal: false,
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

    // Not `data`: that name is Item's default property (children), and
    // shadowing it drew a Qt warning on every load.
    property var cardData: defaultData

    // Multi-card (schema v3) support. `cardData` above always holds the state of
    // the *displayed* card. The daemon's document carries every discovered
    // card under `interfaces`; the selector row (visible when 2+ cards are
    // present) pins one, and empty selection follows the daemon's primary
    // (the connected card).
    property var ifaceList: []
    property string selectedIface: ""   // the card the user picked (defaults to the first)
    property string shownIface: ""      // card actually rendered this poll
    property string lastInternalIface: ""  // internal card's iface while it was present
    readonly property string internalGhost: "@internal"  // selector id for a switched-off internal card

    // Schema v4: the whole document (multipath, internal_card, nm, every
    // card) and a per-radio summary model for the graph and the selector.
    property var doc: ({})
    property var radios: []
    readonly property var internalCard: doc && doc.internal_card ? doc.internal_card : ({})
    readonly property bool internalGhostShown: !!internalCard.managed && !internalCard.present
    readonly property bool onlyConnectedIsInternal: {
        const up = radios.filter(r => r.connected);
        return up.length === 1 && up[0].internal;
    }

    // Root helper (pkexec; polkit allows the active session without a prompt).
    readonly property string helperPath: "/usr/local/lib/wifimimo/wifimimo-helper"
    readonly property var helperActions: ({
        "multipath": ["enable", "disable"],
        "internal": ["enable", "disable"]
    })
    property string helperBusy: ""
    property string helperCommand: ""   // the run in flight (the engine's source name)
    property string helperError: ""
    property string helperErrorVerb: ""   // which row shows the error
    // What the last helper run reported (it reads the machine as root right
    // after acting), laid over the daemon's document until the daemon has
    // sampled since. Without it a switch flips back to the old state for up
    // to a poll, and a click meant to fix that lands as the opposite action.
    property var helperFresh: null   // {key, fields, at (wall s)}
    readonly property var helperFreshFields: ({
        "internal": ["desired", "present", "bound", "iface"],
        "multipath": ["desired", "active", "members", "reason"]
    })

    // Radio palette (validated for colour-vision-deficiency separation).
    // Red / yellow / green are left out: they mean good / warn / bad here.
    readonly property bool darkTheme: {
        const c = Kirigami.Theme.backgroundColor;
        return (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) < 0.5;
    }
    readonly property var paletteLight: ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7", "#e87ba4"]
    readonly property var paletteDark: ["#3987e5", "#d95926", "#199e70", "#9085e9", "#d55181"]

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

    readonly property bool isConnected: !!(cardData && cardData.connected)
    readonly property var antennaSignals: (cardData && cardData.signal_antennas) ? cardData.signal_antennas : []
    readonly property var display: (cardData && cardData.display) ? cardData.display : defaultDisplay
    readonly property bool stale: !cardData || !cardData.connected || !cardData.timestamp || (Math.floor(Date.now() / 1000) - cardData.timestamp) > 15
    readonly property bool hasRecentData: isConnected && !stale
    readonly property int effectiveNss: {
        // Best observed NSS across either direction. Asymmetric NSS is normal
        // on MLO/EHT client links — uplink frequently sticks at NSS 1 while
        // downlink uses NSS 2. min(tx, rx) treats that as 1x1 MIMO and turns
        // the icon red even though the chip's antenna chains are healthy.
        // max(tx, rx) keeps the alert firing only when *both* directions
        // collapse to a single stream, which is the actual chain-failure
        // signature worth flagging.
        const tx = cardData.tx_nss > 0 ? cardData.tx_nss : 0;
        const rx = cardData.rx_nss > 0 ? cardData.rx_nss : 0;
        return Math.max(tx, rx);
    }
    readonly property int linkCount: (cardData && cardData.links) ? cardData.links.length : 0
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
        if (!Array.isArray(merged.flags)) {
            merged.flags = [];
        }
        if (!Array.isArray(merged.signal_history)) {
            merged.signal_history = [];
        }
        return merged;
    }

    function radioColor(index) {
        if (index === undefined || index < 0) {
            return Kirigami.Theme.disabledTextColor;
        }
        const pal = darkTheme ? paletteDark : paletteLight;
        return pal[index % pal.length];
    }

    function worstSeverity(flags) {
        const rank = { info: 0, warn: 1, crit: 2 };
        let worst = "";
        for (const f of (flags || [])) {
            if (worst === "" || (rank[f.severity] || 0) > (rank[worst] || 0)) {
                worst = f.severity || "";
            }
        }
        return worst;
    }

    function buildRadios(list, map) {
        const out = [];
        for (const name of list) {
            const s = map[name];
            if (!s || typeof s !== "object") {
                continue;
            }
            out.push({
                iface: name,
                name: s.card_name || name,
                color: radioColor(s.color_index),
                connected: !!s.connected,
                internal: !!s.internal,
                history: Array.isArray(s.signal_history) ? s.signal_history : [],
                worst: worstSeverity(s.flags),
                selected: name === shownIface
            });
        }
        return out;
    }

    // Allow-listed helper verbs only; the command string is never built
    // from anything but these literals.
    function runHelper(verb, action) {
        const allowed = helperActions[verb];
        if (!allowed || allowed.indexOf(action) < 0 || helperBusy !== "") {
            return;
        }
        helperBusy = verb + " " + action;
        helperError = "";
        helperErrorVerb = verb;
        helperWatchdog.restart();
        helperCommand = "pkexec " + helperPath + " " + verb + " " + action;
        helperSource.connectSource(helperCommand);
    }

    function finishHelper(sourceData) {
        const code = sourceData["exit code"];
        let result = null;
        try {
            result = JSON.parse((sourceData.stdout || "").trim().split("\n").pop() || "{}");
        } catch (e) {
            result = null;
        }
        if (code === 126 || code === 127) {
            helperError = "Not authorized, or the helper isn't installed (re-run install.sh).";
        } else if (code !== 0 || (result && result.error)) {
            helperError = (result && result.error) ? result.error : ("helper exited " + code);
        }
        const verb = helperBusy.split(" ")[0];
        if (result && typeof result === "object" && helperFreshFields[verb]) {
            const fields = {};
            for (const name of helperFreshFields[verb]) {
                if (name in result) {
                    fields[name] = result[name];
                }
            }
            helperFresh = {
                key: verb === "internal" ? "internal_card" : verb,
                fields: fields,
                at: Date.now() / 1000
            };
            doc = withHelperFresh(Object.assign({}, doc));
        }
        helperBusy = "";
        helperWatchdog.stop();
        pollNow();
    }

    // Lays helperFresh over a parsed document (in place) until the daemon
    // has sampled after the helper ran, or 10 s if it isn't writing.
    function withHelperFresh(parsed) {
        if (!helperFresh || !parsed || typeof parsed !== "object") {
            return parsed;
        }
        if ((parsed.sampled_at || 0) >= helperFresh.at || Date.now() / 1000 - helperFresh.at > 10) {
            helperFresh = null;
            return parsed;
        }
        parsed[helperFresh.key] = Object.assign({}, parsed[helperFresh.key] || {}, helperFresh.fields);
        return parsed;
    }

    function statusReason() {
        if (selectedIface === internalGhost && internalGhostShown) {
            return "Internal card is off (removed from the PCI bus)";
        }
        if (!cardData.iface) {
            return "No wifi interface detected";
        }
        if (cardData.connected && stale) {
            return "Stale data (daemon last seen " + fmtClock(cardData.timestamp) + ")";
        }
        if (cardData.operstate === "dormant") {
            return "Connecting…";
        }
        // Parked by multipath: no strong free channel or other AP for it.
        if (doc && doc.nm && (doc.nm.parked || []).indexOf(cardData.iface) >= 0) {
            return "Scouting: scanning for a free channel";
        }
        return "Not associated";
    }

    function deviceLine() {
        const parts = [];
        if (cardData.bus === "usb") {
            const gen = cardData.usb_speed_mbps >= 5000 ? "USB 3" : cardData.usb_speed_mbps > 0 ? "USB 2" : "USB";
            parts.push(gen + (cardData.usb_speed_mbps > 0
                ? " · " + (cardData.usb_speed_mbps >= 1000 ? (cardData.usb_speed_mbps / 1000) + " Gb/s" : cardData.usb_speed_mbps + " Mb/s")
                : ""));
        } else if (cardData.bus === "pci") {
            parts.push("PCIe");
        }
        if (cardData.driver) {
            parts.push(cardData.driver);
        }
        if (cardData.perm_mac) {
            parts.push(cardData.perm_mac);
        }
        if (cardData.ipv4) {
            parts.push(cardData.ipv4 + "/" + cardData.prefixlen);
        }
        return parts.join(" · ");
    }

    function parseState(rawText) {
        const previousConnected = !!(cardData && cardData.connected);
        const previousBssid = cardData && cardData.bssid ? cardData.bssid : "";
        const previousIface = shownIface;
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
        parsed = withHelperFresh(parsed);

        // Schema v3: the document's top level is the primary card's state and
        // `interfaces` maps every card to its own. Pick the user-selected
        // card when it exists; a selection whose card vanished (USB unplug)
        // falls back to the primary until the card returns.
        let view = parsed;
        let list = [];
        let map = {};
        if (parsed && typeof parsed === "object") {
            map = (parsed.interfaces && typeof parsed.interfaces === "object")
                ? parsed.interfaces : {};
            if (Array.isArray(parsed.ifaces) && parsed.ifaces.length > 0) {
                list = parsed.ifaces;
            } else if (parsed.iface) {
                list = [parsed.iface];
            }
            // Selection is always an explicit card (no "auto"): default to
            // the first card; follow the internal card between its live
            // button and its ghost as it's switched off / on; when a
            // selected stick is unplugged, fall back to the first card.
            const card = parsed.internal_card || {};
            if (card.present && card.iface) {
                lastInternalIface = card.iface;
            }
            if (selectedIface === internalGhost && card.present && card.iface && map[card.iface]) {
                selectedIface = card.iface;
            } else if (selectedIface !== internalGhost && !map[selectedIface]) {
                selectedIface = (card.managed && !card.present && selectedIface !== ""
                                 && selectedIface === lastInternalIface)
                    ? internalGhost : (list.length > 0 ? list[0] : "");
            }
            if (selectedIface === internalGhost) {
                // The internal card's panel: live card if it's back, else a
                // named, empty skeleton so the layout keeps its shape.
                const card = parsed.internal_card || {};
                view = card.iface && map[card.iface]
                    ? map[card.iface]
                    : { iface: "", card_name: "Built-in", connected: false,
                        timestamp: parsed.timestamp || 0 };
            } else if (selectedIface && map[selectedIface]
                    && typeof map[selectedIface] === "object") {
                view = map[selectedIface];
            }
        }
        ifaceList = list;
        doc = (parsed && typeof parsed === "object") ? parsed : {};

        const next = validateState(view);
        shownIface = next.iface || "";
        radios = buildRadios(list, map);

        // Switching cards invalidates min/max history even when both cards
        // are associated to the same BSSID, so track it as its own reset
        // trigger alongside roams.
        const ifaceChanged = previousIface.length > 0
            && shownIface.length > 0
            && previousIface !== shownIface;
        const bssidChanged = previousConnected && next.connected
            && previousBssid.length > 0
            && next.bssid.length > 0
            && previousBssid !== next.bssid;

        cardData = next;

        if (!next.connected) {
            if (previousConnected) {
                resetHistory(null);
            }
            return;
        }

        if (!root.expanded) {
            if (!previousConnected || bssidChanged || ifaceChanged) {
                resetHistory(next);
            }
            return;
        }

        if (!previousConnected || bssidChanged || ifaceChanged) {
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
        const links = cardData.links || [];
        const width = cardData.bandwidth_mhz > 0 ? "   " + cardData.bandwidth_mhz + " MHz" : "";
        if (mloMultiLink) {
            const parts = [];
            for (let i = 0; i < links.length; ++i) {
                const l = links[i];
                const ch = l.chan_num > 0 ? " ch" + l.chan_num : "";
                parts.push(l.freq_mhz + " MHz" + ch);
            }
            return parts.join("  +  ") + width;
        }
        const ch = cardData.chan_num > 0 ? " ch" + cardData.chan_num : "";
        return cardData.freq_mhz + " MHz" + ch + width;
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
        // Always the same five rows so the panel never changes height.
        // Rows the driver can't fill (mt7925 in MLO mode leaves the chain
        // signal list empty; avg 0 is a pre-association placeholder) render
        // as "n/a" with an empty track rather than a fake "0 dBm".
        const live = root.hasRecentData;
        const n = antennaSignals.length;
        return [
            { label: "Overall", value: root.cardData.signal_dbm, hist: "sig_overall", kind: "signal", available: live },
            { label: "Avg", value: root.cardData.signal_avg_dbm, hist: "sig_avg", kind: "signal",
              available: live && !!root.cardData.signal_avg_dbm },
            { label: "Antenna 1", value: antennaSignalAt(0), hist: "sig_ant0", kind: "signal", available: live && n >= 1 },
            { label: "Antenna 2", value: antennaSignalAt(1), hist: "sig_ant1", kind: "signal", available: live && n >= 2 },
            { label: "Spread", value: spreadValue(), hist: "sig_spread", kind: "spread",
              available: live && n >= 2 }
        ];
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

    Plasma5Support.DataSource {
        id: helperSource
        engine: "executable"
        interval: 0
        onNewData: (sourceName, sourceData) => {
            // The engine keys sources by command string: disconnect so the
            // same toggle can run again later.
            helperSource.disconnectSource(sourceName);
            // A run the watchdog gave up on may still finish: its result
            // belongs to no current click, so it must not clear a later
            // run's busy state or land on the wrong switch.
            if (sourceName !== root.helperCommand) {
                return;
            }
            root.helperCommand = "";
            root.finishHelper(sourceData);
        }
    }

    Timer {
        id: helperWatchdog
        interval: 30000
        onTriggered: {
            // Disconnect, or a retry of the same toggle (same source name)
            // would never start a new run.
            helperSource.disconnectSource(root.helperCommand);
            root.helperCommand = "";
            root.helperError = "The helper didn't answer within 30 s.";
            root.helperBusy = "";
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
            resetHistory(root.cardData && root.cardData.connected ? root.cardData : null);
            root.pollNow();
        }
    }

    fullRepresentation: PlasmaExtras.Representation {
        // Sized exactly to the content, no scrolling. The content's height
        // is constant: every card (up, down, or switched off) renders the
        // same skeleton, notes live on section-header lines and flag chips
        // on one line, so switching cards never resizes the popup or moves
        // the card buttons. Dense rows keep it well under the screen height
        // (~740 px at 18 px/gridUnit vs 879 px available on a 1200p/130%
        // laptop with a 44 px panel).
        readonly property real fitHeight: contentColumn.implicitHeight + 2 * Kirigami.Units.smallSpacing
        Layout.minimumWidth:  Kirigami.Units.gridUnit * 30
        Layout.maximumWidth:  Kirigami.Units.gridUnit * 30
        Layout.preferredWidth: Kirigami.Units.gridUnit * 30
        Layout.minimumHeight: fitHeight
        Layout.maximumHeight: fitHeight
        Layout.preferredHeight: fitHeight
        collapseMarginsHint: true

        ColumnLayout {
            id: contentColumn
            anchors {
                fill: parent
                margins: Kirigami.Units.smallSpacing
            }
            spacing: Kirigami.Units.smallSpacing

            // Title row: "wifimimo v1.0.0      ● 12:04:31". Version comes from
            // metadata.json; the dot says whether the daemon is alive.
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

                Rectangle {
                    readonly property bool fresh: !!root.doc.timestamp
                        && (Math.floor(Date.now() / 1000) - root.doc.timestamp) <= 15
                    width: 8
                    height: 8
                    radius: 4
                    color: fresh ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.negativeTextColor
                }

                PlasmaComponents3.Label {
                    text: " " + root.fmtClock(root.doc.timestamp || 0)
                    font.family: root.monospaceFamily
                    color: Kirigami.Theme.disabledTextColor
                }
            }

            ControlStrip {
                Layout.fillWidth: true
                app: root
            }

            SignalGraph {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                Layout.preferredHeight: Kirigami.Units.gridUnit * 4.5
                app: root
            }

            Kirigami.Separator {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
            }

            // Card selector: one button per card, always shown (even for one
            // card) so it never appears / disappears and shifts the layout.
            // Highlight uses `highlighted:` (not `checked:`) because a click
            // would break a `checked` binding. Unselected buttons are flat so
            // the selected card stands out raised; `highlighted` alone is
            // invisible under Breeze.
            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                Repeater {
                    model: root.radios

                    delegate: PlasmaComponents3.Button {
                        id: cardButton
                        required property var modelData
                        font.family: root.monospaceFamily
                        highlighted: modelData.iface === root.shownIface
                        flat: !highlighted
                        contentItem: Row {
                            spacing: 5
                            Rectangle {
                                anchors.verticalCenter: parent.verticalCenter
                                width: 9
                                height: 9
                                radius: 4.5
                                // filled = connected, hollow ring = down
                                color: cardButton.modelData.connected ? cardButton.modelData.color : "transparent"
                                border.width: cardButton.modelData.connected ? 0 : 1.5
                                border.color: Kirigami.Theme.disabledTextColor
                            }
                            PlasmaComponents3.Label {
                                anchors.verticalCenter: parent.verticalCenter
                                text: cardButton.modelData.iface
                                font.family: root.monospaceFamily
                            }
                            PlasmaComponents3.Label {
                                anchors.verticalCenter: parent.verticalCenter
                                visible: cardButton.modelData.worst === "warn" || cardButton.modelData.worst === "crit"
                                text: "!"
                                font.bold: true
                                color: cardButton.modelData.worst === "crit"
                                       ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.neutralTextColor
                            }
                        }
                        onClicked: {
                            if (modelData.iface !== root.selectedIface) {
                                root.selectedIface = modelData.iface;
                                root.resetHistory(null);
                                root.pollNow();
                            }
                        }
                    }
                }

                // Switched-off internal card: a ghost button so its panel
                // (and the way back) stays reachable.
                PlasmaComponents3.Button {
                    visible: root.internalGhostShown
                    highlighted: root.selectedIface === root.internalGhost
                    flat: !highlighted
                    font.family: root.monospaceFamily
                    contentItem: Row {
                        spacing: 5
                        Rectangle {
                            anchors.verticalCenter: parent.verticalCenter
                            width: 9
                            height: 9
                            radius: 4.5
                            color: "transparent"
                            border.width: 1.5
                            border.color: Kirigami.Theme.disabledTextColor
                        }
                        PlasmaComponents3.Label {
                            anchors.verticalCenter: parent.verticalCenter
                            text: "internal"
                            color: Kirigami.Theme.disabledTextColor
                            font.family: root.monospaceFamily
                        }
                    }
                    onClicked: {
                        root.selectedIface = root.internalGhost;
                        root.resetHistory(null);
                        root.pollNow();
                    }
                }

                Item {
                    Layout.fillWidth: true
                }
            }

            CardPanel {
                Layout.fillWidth: true
                app: root
            }
        }  // end of contentColumn
    }

    // Keep the icon visible at all times — PassiveStatus would auto-hide it
    // in the collapsed tray, but the user wants a greyed icon they can see
    // (so they know the daemon is running and the link is just down).
    Plasmoid.status: iconTier === "alert"
        ? PlasmaCore.Types.NeedsAttentionStatus
        : PlasmaCore.Types.ActiveStatus
    Plasmoid.icon: iconSource
    toolTipMainText: "wifimimo"
    toolTipSubText: stale || !cardData.connected
        ? "No recent antenna data"
        : "Bandwidth " + (cardData.bandwidth_mhz > 0 ? cardData.bandwidth_mhz + " MHz" : "width unknown")
          + "  ·  " + effectiveNss + "x" + effectiveNss + " MIMO"
          + (mloMultiLink ? ("  ·  MLO " + linkCount + " links") : "")
          + "\nOverall " + Math.min(cardData.tx_rate_mbps || 0, cardData.rx_rate_mbps || 0).toFixed(1) + " MBit/s"
          + "  ·  Signal " + cardData.signal_dbm + " dBm"
          + (doc.multipath && doc.multipath.active
             ? "\nMultipath: active (" + (doc.multipath.members || []).length + " radios)" : "")
          + (radios.filter(r => r.worst === "warn" || r.worst === "crit").length > 0
             ? "\n" + radios.filter(r => r.worst === "warn" || r.worst === "crit").length + " radio(s) flagged" : "")
    toolTipTextFormat: Text.PlainText
}
