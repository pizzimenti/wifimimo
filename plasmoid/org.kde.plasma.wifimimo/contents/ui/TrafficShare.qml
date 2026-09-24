pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

// Live per-radio throughput: one stacked bar split by radio colour, plus a
// row per radio. Shows at a glance whether multipath is actually spreading
// traffic, or which single radio carries it.
ColumnLayout {
    id: share

    required property var app

    readonly property var radios: app.radios.filter(r => r.connected)
    readonly property real total: radios.reduce((sum, r) => sum + r.rx + r.tx, 0)

    spacing: 2

    function fmt(v) {
        return v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
    }

    PlasmaComponents3.Label {
        Layout.fillWidth: true
        text: "TRAFFIC  " + share.app.trafficCaption
        font.bold: true
        font.family: share.app.monospaceFamily
        elide: Text.ElideRight
    }

    Item {
        Layout.fillWidth: true
        Layout.preferredHeight: 10

        Rectangle {
            anchors.fill: parent
            radius: height / 2
            color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
                           Kirigami.Theme.textColor.b, 0.12)
        }

        Row {
            anchors.fill: parent
            spacing: 2
            visible: share.total > 0.01

            Repeater {
                model: share.radios
                delegate: Rectangle {
                    required property var modelData
                    height: parent.height
                    radius: height / 2
                    width: Math.max(0, (parent.width - 2 * (share.radios.length - 1))
                                    * (modelData.rx + modelData.tx) / Math.max(share.total, 0.001))
                    color: modelData.color
                }
            }
        }
    }

    Repeater {
        model: share.radios
        delegate: RowLayout {
            id: row
            required property var modelData
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            Rectangle {
                width: 8
                height: 8
                radius: 4
                color: row.modelData.color
            }
            PlasmaComponents3.Label {
                Layout.preferredWidth: Kirigami.Units.gridUnit * 7
                text: row.modelData.name
                elide: Text.ElideRight
                font.family: share.app.monospaceFamily
                font.bold: row.modelData.selected
            }
            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: "↓ " + share.fmt(row.modelData.rx) + "  ↑ " + share.fmt(row.modelData.tx) + " Mb/s"
                font.family: share.app.monospaceFamily
            }
            PlasmaComponents3.Label {
                text: share.total > 0.01
                      ? Math.round(100 * (row.modelData.rx + row.modelData.tx) / share.total) + "%"
                      : "—"
                color: Kirigami.Theme.disabledTextColor
                font.family: share.app.monospaceFamily
            }
        }
    }

    PlasmaComponents3.Label {
        visible: share.radios.length === 0
        text: "No connected radios"
        color: Kirigami.Theme.disabledTextColor
        font.family: share.app.monospaceFamily
    }
}
