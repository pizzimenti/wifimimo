pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

// Health flags as small chips (icon + title; hover for the detail) on ONE
// line: chips that don't fit are hidden and summarised by a "+N" label whose
// tooltip lists every flag. A fixed single line keeps the popup's height
// constant. The daemon decides what's wrong and how bad; this only renders.
RowLayout {
    id: chips

    required property var flags
    property bool live: true
    property string fontFamily: "monospace"

    readonly property real chipHeight: Math.round(Kirigami.Units.gridUnit * 1.3)
    readonly property real chipFont: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 1)
    readonly property int overflow: {
        let hidden = 0;
        for (const child of row.children) {
            if (child.objectName === "flagChip" && child.x + child.width > viewport.width) {
                hidden++;
            }
        }
        return hidden;
    }

    spacing: Kirigami.Units.smallSpacing

    function sevColor(sev) {
        return sev === "crit" ? Kirigami.Theme.negativeTextColor
             : sev === "warn" ? Kirigami.Theme.neutralTextColor
             : Kirigami.Theme.disabledTextColor;
    }

    function sevIcon(sev) {
        return sev === "crit" ? "dialog-error" : sev === "warn" ? "dialog-warning" : "dialog-information";
    }

    Item {
        id: viewport
        Layout.fillWidth: true
        Layout.preferredHeight: chips.chipHeight
        clip: true

        Row {
            id: row
            spacing: Kirigami.Units.smallSpacing

            Repeater {
                model: chips.flags

                delegate: Rectangle {
                    id: chip
                    required property var modelData
                    objectName: "flagChip"
                    height: chips.chipHeight
                    width: chipRow.implicitWidth + 12
                    radius: height / 2
                    // Chips that would be cut off are hidden, not clipped mid-word.
                    opacity: x + width > viewport.width ? 0 : 1
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
                            font.pixelSize: chips.chipFont
                        }
                    }

                    MouseArea {
                        id: hover
                        anchors.fill: parent
                        hoverEnabled: true
                    }

                    PlasmaComponents3.ToolTip {
                        visible: hover.containsMouse && chip.modelData.detail !== "" && chip.opacity > 0
                        text: chip.modelData.detail
                        delay: 300
                    }
                }
            }

            Rectangle {
                visible: chips.live && chips.flags.length === 0
                height: chips.chipHeight
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
                        font.pixelSize: chips.chipFont
                    }
                }
            }
        }
    }

    PlasmaComponents3.Label {
        id: moreLabel
        visible: chips.overflow > 0
        text: "+" + chips.overflow
        color: Kirigami.Theme.disabledTextColor
        font.family: chips.fontFamily
        font.pixelSize: chips.chipFont

        MouseArea {
            id: moreHover
            anchors.fill: parent
            hoverEnabled: true
        }
        PlasmaComponents3.ToolTip {
            visible: moreHover.containsMouse
            text: chips.flags.map(f => f.title + (f.detail ? ": " + f.detail : "")).join("\n")
            delay: 300
        }
    }
}
