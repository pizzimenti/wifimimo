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

    readonly property string currentCommand: "wifimimo-plasmoid-source"
    property int refreshMs: 1000
    property string monospaceFamily: "monospace"
    property bool pollInFlight: false
    property bool pollPending: false

    property var data: ({
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
        tx_nss: 0,
        rx_nss: 0,
        tx_rate_mbps: 0.0,
        rx_rate_mbps: 0.0,
        tx_mcs: -1,
        rx_mcs: -1,
        tx_mode: "HE",
        rx_mode: "HE",
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
        antenna_signals: [],
        timestamp: 0
    })

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
    readonly property var antennaSignals: (data && data.antenna_signals) ? data.antenna_signals : []
    readonly property bool stale: !data || !data.connected || !data.timestamp || (Math.floor(Date.now() / 1000) - data.timestamp) > 15
    readonly property bool hasRecentData: isConnected && !stale
    readonly property int effectiveNss: {
        const values = [];
        if (data.tx_nss > 0) {
            values.push(data.tx_nss);
        }
        if (data.rx_nss > 0) {
            values.push(data.rx_nss);
        }
        return values.length ? Math.min.apply(Math, values) : 0;
    }

    function finishPoll(sourceName) {
        if (sourceName) {
            executableSource.disconnectSource(sourceName);
        }
        pollTimeout.stop();
        pollInFlight = false;
        if (pollPending) {
            pollPending = false;
            pollNow();
        }
    }

    function pollNow() {
        if (pollInFlight) {
            pollPending = true;
            return;
        }
        pollInFlight = true;
        pollPending = false;
        pollTimeout.restart();
        executableSource.connectSource(currentCommand);
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
        const antennaValues = next ? (next.antenna_signals || []) : [];
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

    function parseState(rawText) {
        const previousConnected = !!(data && data.connected);
        const previousBssid = data && data.bssid ? data.bssid : "";
        const next = {
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
            tx_nss: 0,
            rx_nss: 0,
            tx_rate_mbps: 0.0,
            rx_rate_mbps: 0.0,
            tx_mcs: -1,
            rx_mcs: -1,
            tx_mode: "HE",
            rx_mode: "HE",
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
            antenna_signals: [],
            timestamp: 0
        };

        const lines = (rawText || "").split(/\r?\n/);
        for (const line of lines) {
            if (!line || line.indexOf("=") < 0) {
                continue;
            }
            const idx = line.indexOf("=");
            const key = line.slice(0, idx);
            const value = line.slice(idx + 1);
            if (key === "connected") {
                next.connected = value === "true";
            } else if (key === "iface") {
                next.iface = value || next.iface;
            } else if (key === "ssid") {
                next.ssid = value;
            } else if (key === "ssid_display") {
                next.ssid_display = value;
            } else if (key === "bssid") {
                next.bssid = value;
            } else if (key === "timestamp") {
                next.timestamp = Number(value) || 0;
            } else if (key === "freq_mhz") {
                next.freq_mhz = Number(value) || 0;
            } else if (key === "chan_num") {
                next.chan_num = Number(value) || 0;
            } else if (key === "bandwidth_mhz") {
                next.bandwidth_mhz = Number(value) || 0;
            } else if (key === "signal_dbm") {
                next.signal_dbm = Number(value) || 0;
            } else if (key === "signal_avg_dbm") {
                next.signal_avg_dbm = Number(value) || 0;
            } else if (key === "tx_nss") {
                next.tx_nss = Number(value) || 0;
            } else if (key === "rx_nss") {
                next.rx_nss = Number(value) || 0;
            } else if (key === "tx_rate_mbps") {
                next.tx_rate_mbps = Number(value) || 0.0;
            } else if (key === "rx_rate_mbps") {
                next.rx_rate_mbps = Number(value) || 0.0;
            } else if (key === "tx_mcs") {
                next.tx_mcs = Number(value);
            } else if (key === "rx_mcs") {
                next.rx_mcs = Number(value);
            } else if (key === "tx_mode") {
                next.tx_mode = value || "HE";
            } else if (key === "rx_mode") {
                next.rx_mode = value || "HE";
            } else if (key === "tx_gi") {
                next.tx_gi = Number(value);
            } else if (key === "rx_gi") {
                next.rx_gi = Number(value);
            } else if (key === "tx_packets") {
                next.tx_packets = Number(value) || 0;
            } else if (key === "tx_retries") {
                next.tx_retries = Number(value) || 0;
            } else if (key === "tx_failed") {
                next.tx_failed = Number(value) || 0;
            } else if (key === "rx_packets") {
                next.rx_packets = Number(value) || 0;
            } else if (key === "connected_time_s") {
                next.connected_time_s = Number(value) || 0;
            } else if (key === "station_dump_available") {
                next.station_dump_available = value === "true";
            } else if (key === "retry_10s_pct") {
                next.retry_10s_pct = Number(value) || 0;
            } else if (key === "retry_10s_packets") {
                next.retry_10s_packets = Number(value) || 0;
            } else if (key === "retry_10s_retries") {
                next.retry_10s_retries = Number(value) || 0;
            } else if (key === "retry_10s_failed") {
                next.retry_10s_failed = Number(value) || 0;
            } else if (/^antenna_\d+$/.test(key)) {
                next.antenna_signals.push(Number(value) || 0);
            }
        }

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

        if (!previousConnected || bssidChanged) {
            resetHistory(next);
            return;
        }

        updateHistory("sig_overall", next.signal_dbm);
        updateHistory("sig_avg", next.signal_avg_dbm);
        for (let i = 0; i < next.antenna_signals.length; ++i) {
            updateHistory("sig_ant" + i, next.antenna_signals[i]);
        }
        if (next.antenna_signals.length >= 2) {
            updateHistory("sig_spread", Math.max.apply(Math, next.antenna_signals) - Math.min.apply(Math, next.antenna_signals));
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

    function signalFraction(dbm) {
        return Math.max(0, Math.min(1, (dbm + 90) / 70));
    }

    function spreadFraction(spread) {
        return Math.max(0, Math.min(1, spread / 30));
    }

    function signalColor(dbm) {
        if (dbm < -70) {
            return Kirigami.Theme.negativeTextColor;
        }
        if (dbm < -55) {
            return Kirigami.Theme.neutralTextColor;
        }
        return Kirigami.Theme.positiveTextColor;
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

    function bandLabel(freq) {
        if (freq >= 6000) {
            return "6 GHz";
        }
        if (freq >= 5000) {
            return "5 GHz";
        }
        if (freq > 0) {
            return "2.4 GHz";
        }
        return "?";
    }

    function fmtUptime(secs) {
        const h = Math.floor(secs / 3600);
        const m = Math.floor((secs % 3600) / 60);
        const s = secs % 60;
        if (h > 0) {
            return h + "h" + String(m).padStart(2, "0") + "m";
        }
        return m + "m" + String(s).padStart(2, "0") + "s";
    }

    function fmtClock(ts) {
        if (!ts) {
            return "--:--:--";
        }
        return new Date(ts * 1000).toLocaleTimeString(Qt.locale(), "HH:mm:ss");
    }

    function nssDots(nss) {
        return "●".repeat(Math.max(0, nss)) + "○".repeat(Math.max(0, 2 - nss));
    }

    function giLabel(gi) {
        if (gi === 0) {
            return "0.8us";
        }
        if (gi === 1) {
            return "1.6us";
        }
        if (gi === 2) {
            return "3.2us";
        }
        return "";
    }

    function computeRates(refRate, refMcs, mode) {
        const efficiencies = {
            HE: [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0, 6.0, 20 / 3, 7.5, 25 / 3],
            VHT: [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0, 6.0, 20 / 3],
            HT: [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 5.0]
        };
        const table = efficiencies[mode] || efficiencies.HE;
        if (refMcs < 0 || refMcs >= table.length || table[refMcs] === 0 || refRate <= 0) {
            return [];
        }
        const rates = [];
        for (let i = 0; i < table.length; ++i) {
            rates.push(Math.round(refRate * table[i] / table[refMcs]));
        }
        return rates;
    }

    function rateCeiling(rate, mcs, mode) {
        const computed = computeRates(rate, mcs, mode);
        if (computed.length) {
            return computed[computed.length - 1];
        }
        return Math.max(rate, 1.0);
    }

    function mcsColor(index, current, lo, hi, maxIndex) {
        const t = maxIndex > 0 ? index / maxIndex : 0;
        const hue = 0.02 + (0.12 - 0.02) * t;
        const base = Qt.hsla(hue, 0.70, 0.62, 1.0);
        if (index === current) {
            return base;
        }
        if (index >= lo && index <= hi) {
            return Qt.hsla(hue, 0.55, 0.45, 0.70);
        }
        return Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.12);
    }

    function connectionSummary() {
        const ssid = data.ssid_display || data.ssid || data.bssid;
        const chan = data.chan_num > 0 ? "  ch" + data.chan_num : "";
        const width = data.bandwidth_mhz > 0 ? "  " + data.bandwidth_mhz + " MHz" : "";
        const uptime = data.connected_time_s > 0 ? fmtUptime(data.connected_time_s) : "?";
        return ssid + "  (" + data.bssid + ")  " + data.freq_mhz + " MHz / " + bandLabel(data.freq_mhz) + chan + width + "  up " + uptime;
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

    function displayMcs(value) {
        return value >= 0 ? String(value) : "-";
    }

    function mcsGridCount(ratesLength, mode) {
        if (ratesLength > 0) {
            return ratesLength;
        }
        if (mode === "HT") {
            return 8;
        }
        if (mode === "VHT") {
            return 10;
        }
        return 12;
    }

    compactRepresentation: MouseArea {
        acceptedButtons: Qt.LeftButton
        implicitWidth: Kirigami.Units.iconSizes.smallMedium
        implicitHeight: Kirigami.Units.iconSizes.smallMedium
        onClicked: root.expanded = !root.expanded

        Kirigami.Icon {
            anchors.fill: parent
            anchors.margins: 1
            source: root.hasRecentData && root.effectiveNss <= 1
                ? Qt.resolvedUrl("../icons/network-wireless-hotspot-alert.svg")
                : Qt.resolvedUrl("../icons/network-wireless-hotspot-normal.svg")
            isMask: false
            color: "transparent"
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
            root.finishPoll(sourceName);
        }
    }

    Timer {
        id: pollTimeout
        interval: Math.max(root.refreshMs * 3, 4000)
        repeat: false
        running: false
        onTriggered: {
            root.pollInFlight = false;
            root.pollPending = false;
            executableSource.disconnectSource(root.currentCommand);
        }
    }

    Timer {
        id: pollTimer
        interval: root.expanded ? root.refreshMs : 5000
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: root.pollNow()
    }

    onExpandedChanged: function() {
        if (root.expanded) {
            root.pollNow();
        }
    }

    Component.onCompleted: pollNow()

    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth:  Kirigami.Units.gridUnit * 30
        Layout.minimumHeight: 550
        Layout.maximumWidth:  Kirigami.Units.gridUnit * 30
        Layout.maximumHeight: 550
        collapseMarginsHint: true

        ColumnLayout {
            id: contentColumn
            anchors {
                fill: parent
                margins: Kirigami.Units.smallSpacing
            }
            spacing: 1

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: "wifimimo"
                font.bold: true
                font.family: root.monospaceFamily
                horizontalAlignment: Text.AlignHCenter
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: root.hasRecentData ? root.connectionSummary() : ("Not connected on " + root.data.iface)
                wrapMode: Text.Wrap
                maximumLineCount: 2
                font.bold: true
                font.family: root.monospaceFamily
                color: root.hasRecentData ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.negativeTextColor
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: "SIGNAL"
                font.bold: true
                font.family: root.monospaceFamily
                color: Kirigami.Theme.highlightColor
            }

            Repeater {
                model: [
                    { label: "Overall", value: root.data.signal_dbm, hist: "sig_overall", kind: "signal" },
                    { label: "Avg", value: root.data.signal_avg_dbm, hist: "sig_avg", kind: "signal" },
                    { label: "Antenna 1", value: root.antennaSignalAt(0), hist: "sig_ant0", kind: "signal" },
                    { label: "Antenna 2", value: root.antennaSignalAt(1), hist: "sig_ant1", kind: "signal" },
                    { label: "Spread", value: root.spreadValue(), hist: "sig_spread", kind: "spread", suffix: "warn >15" }
                ]

                delegate: ColumnLayout {
                    id: signalBlock
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 0

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
                            color: signalBlock.modelData.kind === "spread"
                                ? root.alertColor(signalBlock.modelData.value, 10, 15)
                                : root.signalColor(signalBlock.modelData.value)
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
                        Layout.preferredHeight: 8
                        Layout.minimumHeight: 8
                        Layout.maximumHeight: 8

                        Rectangle {
                            anchors.fill: parent
                            radius: height / 2
                            color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.12)
                        }

                        Rectangle {
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            width: Math.max(4, parent.width * (signalBlock.modelData.kind === "spread"
                                ? root.spreadFraction(signalBlock.modelData.value)
                                : root.signalFraction(signalBlock.modelData.value)))
                            radius: height / 2
                            color: signalBlock.modelData.kind === "spread"
                                ? root.alertColor(signalBlock.modelData.value, 10, 15)
                                : root.signalColor(signalBlock.modelData.value)
                        }

                        Rectangle {
                            width: 2
                            radius: 1
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            x: Math.max(0, Math.min(parent.width - width, parent.width * (signalBlock.modelData.kind === "spread"
                                ? root.spreadFraction(root.histMax(signalBlock.modelData.hist, signalBlock.modelData.value))
                                : root.signalFraction(root.histMax(signalBlock.modelData.hist, signalBlock.modelData.value)))))
                            color: Kirigami.Theme.textColor
                            opacity: 0.6
                        }
                    }
                }
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: "RATES"
                font.bold: true
                font.family: root.monospaceFamily
                color: Kirigami.Theme.highlightColor
            }

            Repeater {
                model: [
                    { label: "TX", rate: root.data.tx_rate_mbps, nss: root.data.tx_nss, mcs: root.data.tx_mcs, mode: root.data.tx_mode, gi: root.data.tx_gi, hist: "tx_rate" },
                    { label: "RX", rate: root.data.rx_rate_mbps, nss: root.data.rx_nss, mcs: root.data.rx_mcs, mode: root.data.rx_mode, gi: root.data.rx_gi, hist: "rx_rate" }
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
                                  + "  NSS " + rateBlock.modelData.nss + " " + root.nssDots(rateBlock.modelData.nss)
                                  + (rateBlock.modelData.gi >= 0 ? "  GI " + root.giLabel(rateBlock.modelData.gi) : "")
                            horizontalAlignment: Text.AlignRight
                            color: Kirigami.Theme.disabledTextColor
                            font.family: root.monospaceFamily
                        }
                    }

                    Item {
                        id: rateBar
                        Layout.fillWidth: true
                        Layout.preferredHeight: 8
                        Layout.minimumHeight: 8
                        Layout.maximumHeight: 8

                        readonly property real ceiling: root.rateCeiling(rateBlock.modelData.rate, rateBlock.modelData.mcs, rateBlock.modelData.mode)

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
                text: "MCS INDEX"
                font.bold: true
                font.family: root.monospaceFamily
                color: Kirigami.Theme.highlightColor
            }

            Repeater {
                model: [
                    { label: "TX", mcs: root.data.tx_mcs, rate: root.data.tx_rate_mbps, mode: root.data.tx_mode, hist: "tx_mcs" },
                    { label: "RX", mcs: root.data.rx_mcs, rate: root.data.rx_rate_mbps, mode: root.data.rx_mode, hist: "rx_mcs" }
                ]

                delegate: ColumnLayout {
                    id: mcsBlock
                    required property var modelData
                    readonly property var rates: root.computeRates(mcsBlock.modelData.rate, mcsBlock.modelData.mcs, mcsBlock.modelData.mode)
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
                                mcsBlock.modelData.mcs,
                                mcsBlock.modelData.mcs,
                                mcsBlock.modelData.mcs,
                                mcsBlock.modelData.mcs,
                                Math.max(0, mcsBlock.rates.length - 1)
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
                            model: root.mcsGridCount(mcsBlock.rates.length, mcsBlock.modelData.mode)

                            delegate: Rectangle {
                                required property int index
                                Layout.fillWidth: true
                                Layout.preferredHeight: Kirigami.Units.gridUnit * 1.1
                                radius: 3
                                color: root.mcsColor(
                                    index,
                                    Math.max(0, mcsBlock.modelData.mcs),
                                    mcsBlock.modelData.mcs >= 0 ? root.histMin(mcsBlock.modelData.hist, mcsBlock.modelData.mcs) : -1,
                                    mcsBlock.modelData.mcs >= 0 ? root.histMax(mcsBlock.modelData.hist, mcsBlock.modelData.mcs) : -1,
                                    Math.max(0, root.mcsGridCount(mcsBlock.rates.length, mcsBlock.modelData.mode) - 1)
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
                            model: root.mcsGridCount(mcsBlock.rates.length, mcsBlock.modelData.mode)

                            delegate: PlasmaComponents3.Label {
                                required property int index
                                Layout.fillWidth: true
                                horizontalAlignment: Text.AlignHCenter
                                text: index < mcsBlock.rates.length ? mcsBlock.rates[index] : "-"
                                color: Kirigami.Theme.disabledTextColor
                                font.family: root.monospaceFamily
                                font.pixelSize: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 2)
                            }
                        }
                    }
                }
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: "TX RETRIES"
                font.bold: true
                font.family: root.monospaceFamily
                color: Kirigami.Theme.highlightColor
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
                    Layout.preferredHeight: 8
                    Layout.minimumHeight: 8
                    Layout.maximumHeight: 8

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

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: root.hasRecentData
                    ? "Source: wifimimo-daemon shared state"
                    : "Waiting for wifimimo-daemon state"
                color: Kirigami.Theme.disabledTextColor
                font.family: root.monospaceFamily
            }
        }
    }

    Plasmoid.status: !data.connected || stale
        ? PlasmaCore.Types.PassiveStatus
        : (effectiveNss <= 1 ? PlasmaCore.Types.NeedsAttentionStatus : PlasmaCore.Types.ActiveStatus)
    Plasmoid.icon: hasRecentData && effectiveNss <= 1
        ? Qt.resolvedUrl("../icons/network-wireless-hotspot-alert.svg")
        : Qt.resolvedUrl("../icons/network-wireless-hotspot-normal.svg")
    toolTipMainText: "wifimimo"
    toolTipSubText: stale || !data.connected
        ? "No recent antenna data"
        : "Bandwidth " + (data.bandwidth_mhz > 0 ? data.bandwidth_mhz + " MHz" : "width unknown")
          + "  ·  " + effectiveNss + "x" + effectiveNss + " MIMO"
          + "\nOverall " + Math.min(data.tx_rate_mbps || 0, data.rx_rate_mbps || 0).toFixed(1) + " MBit/s"
          + "  ·  Signal " + data.signal_dbm + " dBm"
    toolTipTextFormat: Text.PlainText
}
