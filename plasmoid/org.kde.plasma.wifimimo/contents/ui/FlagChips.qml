pragma ComponentBehavior: Bound

import QtQuick

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

// Health flags as small chips (icon + title; hover for the detail). The
// daemon decides what's wrong and how bad; this only renders it.
Flow {
    id: chips

    required property var flags
    property bool live: true
    property string fontFamily: "monospace"

    spacing: Kirigami.Units.smallSpacing

    function sevColor(sev) {
        return sev === "crit" ? Kirigami.Theme.negativeTextColor
             : sev === "warn" ? Kirigami.Theme.neutralTextColor
             : Kirigami.Theme.disabledTextColor;
    }

    function sevIcon(sev) {
        return sev === "crit" ? "dialog-error" : sev === "warn" ? "dialog-warning" : "dialog-information";
    }

    Repeater {
        model: chips.flags

        delegate: Rectangle {
            id: chip
            required property var modelData
            height: chipRow.implicitHeight + 4
            width: chipRow.implicitWidth + 12
            radius: height / 2
            color: Qt.rgba(chips.sevColor(modelData.severity).r, chips.sevColor(modelData.severity).g,
                           chips.sevColor(modelData.severity).b, 0.15)
            border.width: 1
            border.color: Qt.rgba(chips.sevColor(modelData.severity).r, chips.sevColor(modelData.severity).g,
                                  chips.sevColor(modelData.severity).b, 0.5)

            Row {
                id: chipRow
                anchors.centerIn: parent
                spacing: 4

                Kirigami.Icon {
                    width: Kirigami.Units.iconSizes.small
                    height: width
                    anchors.verticalCenter: parent.verticalCenter
                    source: chips.sevIcon(chip.modelData.severity)
                }
                PlasmaComponents3.Label {
                    anchors.verticalCenter: parent.verticalCenter
                    text: chip.modelData.title
                    font.family: chips.fontFamily
                    font.pixelSize: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 1)
                }
            }

            MouseArea {
                id: hover
                anchors.fill: parent
                hoverEnabled: true
            }

            PlasmaComponents3.ToolTip {
                visible: hover.containsMouse && chip.modelData.detail !== ""
                text: chip.modelData.detail
                delay: 300
            }
        }
    }

    Rectangle {
        visible: chips.live && chips.flags.length === 0
        height: okRow.implicitHeight + 4
        width: okRow.implicitWidth + 12
        radius: height / 2
        color: Qt.rgba(Kirigami.Theme.positiveTextColor.r, Kirigami.Theme.positiveTextColor.g,
                       Kirigami.Theme.positiveTextColor.b, 0.15)

        Row {
            id: okRow
            anchors.centerIn: parent
            spacing: 4
            Kirigami.Icon {
                width: Kirigami.Units.iconSizes.small
                height: width
                anchors.verticalCenter: parent.verticalCenter
                source: "dialog-ok-apply"
            }
            PlasmaComponents3.Label {
                anchors.verticalCenter: parent.verticalCenter
                text: "No issues"
                font.family: chips.fontFamily
                font.pixelSize: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 1)
            }
        }
    }
}
