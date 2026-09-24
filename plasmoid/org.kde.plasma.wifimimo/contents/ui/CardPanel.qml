pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

// The selected card. Identical structure whether the card is up, down, or
// switched off: without a recent sample, values read "—", meters are empty
// tracks and the telemetry is dimmed, so nothing masquerades as a real zero
// and the popup never changes height when a card drops.
//
// Dense by design (the popup must fit the screen without scrolling): every
// meter is one line — label, value, bar, range — and per-direction PHY
// details (NSS, guard interval) sit on the MCS lines.
ColumnLayout {
    id: panel

    required property var app

    readonly property var d: app.data
    readonly property var disp: app.display
    readonly property bool live: app.hasRecentData
    readonly property string mono: app.monospaceFamily
    readonly property real smallFont: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 2)
    readonly property real labelW: Kirigami.Units.gridUnit * 5.2
    readonly property real valueW: Kirigami.Units.gridUnit * 5
    readonly property real rangeW: Kirigami.Units.gridUnit * 4.6

    spacing: 1

    // Section title with an optional dim note on the same line (driver gaps,
    // counters) so notes never add a line.
    component SectionHeader: RowLayout {
        id: header
        property string title: ""
        property string note: ""
        Layout.fillWidth: true
        Layout.topMargin: Kirigami.Units.smallSpacing
        spacing: Kirigami.Units.largeSpacing

        PlasmaComponents3.Label {
            text: header.title
            font.bold: true
            font.family: panel.mono
        }
        PlasmaComponents3.Label {
            Layout.fillWidth: true
            text: header.note
            elide: Text.ElideRight
            color: Kirigami.Theme.disabledTextColor
            font.family: panel.mono
            font.italic: true
            font.pixelSize: panel.smallFont
        }
    }

    // One meter on one line: label | value | bar | range.
    component MeterRow: RowLayout {
        id: meter
        property string label: ""
        property string valueText: ""
        property color valueColor: Kirigami.Theme.textColor
        property real fraction: 0
        property real markerFraction: -1
        property color fillColor: Kirigami.Theme.positiveTextColor
        property bool available: true
        property string rangeText: ""
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PlasmaComponents3.Label {
            Layout.preferredWidth: panel.labelW
            text: meter.label
            elide: Text.ElideRight
            color: Kirigami.Theme.disabledTextColor
            font.family: panel.mono
        }
        PlasmaComponents3.Label {
            Layout.preferredWidth: panel.valueW
            text: meter.valueText
            color: meter.available ? meter.valueColor : Kirigami.Theme.disabledTextColor
            font.family: panel.mono
        }
        MeterBar {
            Layout.alignment: Qt.AlignVCenter
            placeholder: !meter.available
            fraction: meter.fraction
            markerFraction: meter.markerFraction
            fillColor: meter.fillColor
        }
        PlasmaComponents3.Label {
            Layout.preferredWidth: panel.rangeW
            text: meter.rangeText
            horizontalAlignment: Text.AlignRight
            color: Kirigami.Theme.disabledTextColor
            font.family: panel.mono
            font.pixelSize: panel.smallFont
        }
    }

    // --- identity -----------------------------------------------------------
    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PlasmaComponents3.Label {
            text: panel.d.card_name || panel.d.iface || "—"
            font.bold: true
            font.pixelSize: Math.round(Kirigami.Theme.defaultFont.pixelSize * 1.35)
            font.family: panel.mono
        }
        PlasmaComponents3.Label {
            text: panel.d.iface || ""
            color: Kirigami.Theme.disabledTextColor
            font.family: panel.mono
        }
        Item {
            Layout.fillWidth: true
        }
        PlasmaComponents3.Label {
            visible: panel.live
            text: "up " + (panel.d.connected_time_s > 0 ? panel.app.fmtUptime(panel.d.connected_time_s) : "?")
            color: Kirigami.Theme.disabledTextColor
            font.family: panel.mono
        }
    }

    PlasmaComponents3.Label {
        Layout.fillWidth: true
        text: panel.live
              ? (panel.d.ssid_display || panel.d.ssid || panel.d.bssid) + "  (" + panel.d.bssid + ")"
              : panel.app.statusReason()
        elide: Text.ElideRight
        font.bold: true
        font.family: panel.mono
        color: panel.live ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.disabledTextColor
    }

    PlasmaComponents3.Label {
        Layout.fillWidth: true
        text: panel.live ? panel.app.freqLine() + "   " + panel.app.linkStatusLine() : "—"
        elide: Text.ElideRight
        font.family: panel.mono
    }

    PlasmaComponents3.Label {
        Layout.fillWidth: true
        text: panel.app.deviceLine() || "—"
        elide: Text.ElideRight
        color: Kirigami.Theme.disabledTextColor
        font.family: panel.mono
        font.pixelSize: panel.smallFont
    }

    FlagChips {
        Layout.fillWidth: true
        Layout.topMargin: 2
        flags: panel.d.flags || []
        live: panel.live
        fontFamily: panel.mono
    }

    // --- telemetry ----------------------------------------------------------
    ColumnLayout {
        Layout.fillWidth: true
        opacity: panel.live ? 1.0 : 0.45
        spacing: 1

        SectionHeader {
            title: "SIGNAL"
            // Driver gap, not a fault: mt7925 MLD stations report no chain signal.
            note: panel.live && panel.app.antennaSignals.length === 0 ? "per-antenna n/a (MLD)" : ""
        }

        Repeater {
            model: panel.app.buildSignalModel()

            delegate: MeterRow {
                id: sig
                required property var modelData
                readonly property bool spread: modelData.kind === "spread"
                label: modelData.label
                available: modelData.available
                valueText: modelData.available
                           ? Number(modelData.value).toFixed(0) + (spread ? " dB" : " dBm")
                           : (panel.live ? "n/a" : "—")
                valueColor: spread ? panel.app.alertColor(modelData.value, 10, 15)
                                   : panel.app.signalColorForDbm(modelData.value)
                fillColor: valueColor
                fraction: spread ? panel.app.spreadFraction(modelData.value)
                                 : panel.app.signalFractionForDbm(modelData.value)
                markerFraction: spread
                    ? panel.app.spreadFraction(panel.app.histMax(modelData.hist, modelData.value))
                    : panel.app.signalFractionForDbm(panel.app.histMax(modelData.hist, modelData.value))
                rangeText: modelData.available
                           ? Number(panel.app.histMin(modelData.hist, modelData.value)).toFixed(0) + " .. "
                             + Number(panel.app.histMax(modelData.hist, modelData.value)).toFixed(0)
                           : ""
            }
        }

        SectionHeader {
            title: "RATES"
        }

        Repeater {
            model: [
                { label: "TX", rate: panel.d.tx_rate_mbps, rates: panel.disp.tx_rates_mbps, hist: "tx_rate" },
                { label: "RX", rate: panel.d.rx_rate_mbps, rates: panel.disp.rx_rates_mbps, hist: "rx_rate" }
            ]

            delegate: MeterRow {
                required property var modelData
                readonly property real ceiling: modelData.rates && modelData.rates.length > 0
                    ? Math.max(modelData.rates[modelData.rates.length - 1], 1.0)
                    : Math.max(modelData.rate, 1.0)
                label: modelData.label
                available: panel.live
                valueText: panel.live ? Number(modelData.rate).toFixed(1) + " Mb/s" : "—"
                fraction: modelData.rate / ceiling
                markerFraction: panel.app.histMax(modelData.hist, modelData.rate) / ceiling
                fillColor: Kirigami.Theme.positiveTextColor
                rangeText: panel.live
                           ? Number(panel.app.histMin(modelData.hist, modelData.rate)).toFixed(0) + " .. "
                             + Number(panel.app.histMax(modelData.hist, modelData.rate)).toFixed(0)
                           : ""
            }
        }

        SectionHeader {
            title: "MCS INDEX"
        }

        Repeater {
            model: [
                { label: "TX", mcs: panel.d.tx_mcs, rate: panel.d.tx_rate_mbps, rates: panel.disp.tx_rates_mbps,
                  nss: panel.d.tx_nss, dots: panel.disp.tx_nss_dots, gi: panel.disp.tx_gi_label, hist: "tx_mcs" },
                { label: "RX", mcs: panel.d.rx_mcs, rate: panel.d.rx_rate_mbps, rates: panel.disp.rx_rates_mbps,
                  nss: panel.d.rx_nss, dots: panel.disp.rx_nss_dots, gi: panel.disp.rx_gi_label, hist: "rx_mcs" }
            ]

            delegate: ColumnLayout {
                id: mcsBlock
                required property var modelData
                readonly property var rates: modelData.rates || []
                readonly property int gridCount: rates.length > 0 ? rates.length : panel.disp.mcs_grid_count
                Layout.fillWidth: true
                spacing: 1

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    PlasmaComponents3.Label {
                        text: mcsBlock.modelData.label + "  MCS " + panel.app.displayMcs(mcsBlock.modelData.mcs)
                        font.family: panel.mono
                    }
                    PlasmaComponents3.Label {
                        text: panel.live ? Number(mcsBlock.modelData.rate).toFixed(0) + " Mb/s" : "—"
                        color: panel.app.mcsColor(Math.max(0, mcsBlock.modelData.mcs), mcsBlock.modelData.mcs,
                                                  mcsBlock.modelData.mcs, mcsBlock.modelData.mcs,
                                                  Math.max(0, mcsBlock.gridCount - 1))
                        font.family: panel.mono
                    }
                    PlasmaComponents3.Label {
                        text: panel.live
                              ? "NSS " + mcsBlock.modelData.nss + " " + mcsBlock.modelData.dots
                                + (mcsBlock.modelData.gi ? "  GI " + mcsBlock.modelData.gi : "")
                              : ""
                        color: Kirigami.Theme.disabledTextColor
                        font.family: panel.mono
                        font.pixelSize: panel.smallFont
                    }
                    Item {
                        Layout.fillWidth: true
                    }
                    PlasmaComponents3.Label {
                        text: mcsBlock.modelData.mcs >= 0
                              ? "min " + Number(panel.app.histMin(mcsBlock.modelData.hist, mcsBlock.modelData.mcs)).toFixed(0)
                                + "  max " + Number(panel.app.histMax(mcsBlock.modelData.hist, mcsBlock.modelData.mcs)).toFixed(0)
                              : "min -  max -"
                        color: Kirigami.Theme.disabledTextColor
                        font.family: panel.mono
                        font.pixelSize: panel.smallFont
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
                            Layout.preferredHeight: Kirigami.Units.gridUnit * 1.15
                            radius: 3
                            color: panel.app.mcsColor(
                                index,
                                mcsBlock.modelData.mcs,
                                mcsBlock.modelData.mcs >= 0 ? panel.app.histMin(mcsBlock.modelData.hist, mcsBlock.modelData.mcs) : -1,
                                mcsBlock.modelData.mcs >= 0 ? panel.app.histMax(mcsBlock.modelData.hist, mcsBlock.modelData.mcs) : -1,
                                Math.max(0, mcsBlock.gridCount - 1))

                            PlasmaComponents3.Label {
                                anchors.centerIn: parent
                                text: parent.index
                                color: parent.index === mcsBlock.modelData.mcs
                                       ? Kirigami.Theme.backgroundColor : Kirigami.Theme.textColor
                                font.family: panel.mono
                                font.pixelSize: panel.smallFont
                            }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 1

                    Repeater {
                        model: mcsBlock.gridCount

                        // Items (implicitWidth 0) give every cell the same
                        // width, so rate labels stay centred under their
                        // MCS cells whatever the text length.
                        delegate: Item {
                            required property int index
                            Layout.fillWidth: true
                            Layout.preferredHeight: rateLabel.implicitHeight

                            PlasmaComponents3.Label {
                                id: rateLabel
                                anchors.centerIn: parent
                                text: parent.index < mcsBlock.rates.length ? mcsBlock.rates[parent.index] : "-"
                                color: Kirigami.Theme.disabledTextColor
                                font.family: panel.mono
                                font.pixelSize: panel.smallFont
                            }
                        }
                    }
                }
            }
        }

        SectionHeader {
            title: "TX RETRIES"
            // mt7925 MLD stations don't surface retry counters at any kernel
            // level, so a flat 0% there is a driver gap, not a clean link.
            note: panel.app.linkCount > 0
                  ? "unreliable on MLD link"
                  : (panel.live ? panel.d.retry_10s_retries + "/" + panel.d.retry_10s_packets
                                  + " · " + panel.d.retry_10s_failed + " failed · 10 s" : "")
        }

        MeterRow {
            readonly property real pct: Number(panel.d.retry_10s_pct || 0)
            label: "Retry rate"
            available: panel.live
            valueText: panel.live ? pct.toFixed(1) + "%" : "—"
            valueColor: panel.app.alertColor(pct, 10, 30)
            fillColor: valueColor
            fraction: pct / 100.0
            markerFraction: panel.app.histMax("retry_pct", pct) / 100.0
            rangeText: panel.live ? "max " + panel.app.histMax("retry_pct", pct).toFixed(1) + "%" : ""
        }
    }
}
