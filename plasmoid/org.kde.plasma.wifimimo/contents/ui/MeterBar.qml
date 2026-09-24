import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami

// Pill-shaped meter: track, fill, and an optional high-water marker.
// `placeholder` draws only the empty track (no data yet / radio down).
Item {
    id: bar

    property real fraction: 0
    property real markerFraction: -1
    property color fillColor: Kirigami.Theme.positiveTextColor
    property bool placeholder: false

    readonly property real clampedFraction: Math.max(0, Math.min(1, fraction))

    Layout.fillWidth: true
    Layout.preferredHeight: 12
    Layout.minimumHeight: 12
    Layout.maximumHeight: 12
    implicitHeight: 12

    Rectangle {
        anchors.fill: parent
        radius: height / 2
        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
                       Kirigami.Theme.textColor.b, 0.12)
    }

    Rectangle {
        visible: !bar.placeholder
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: Math.max(4, parent.width * bar.clampedFraction)
        radius: height / 2
        color: bar.fillColor
    }

    Rectangle {
        visible: !bar.placeholder && bar.markerFraction >= 0
        width: 2
        radius: 1
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        x: Math.max(0, Math.min(parent.width - width,
                                parent.width * Math.max(0, Math.min(1, bar.markerFraction))))
        color: Kirigami.Theme.textColor
        opacity: 0.6
    }
}
