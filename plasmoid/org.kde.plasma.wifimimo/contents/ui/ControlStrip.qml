pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

// Multipath and internal-card switches. Both run the root helper through
// pkexec (polkit action allows the active session without a prompt). A
// switch's `checked` always tracks the daemon's reported state: after a
// click we re-bind it, so the UI can never claim a state the machine isn't in.
//
// Every transient state (busy, error, "are you sure?") is shown *inside* the
// existing two rows, never as an extra row, so nothing below this block
// (graph, traffic, card selector) ever moves.
ColumnLayout {
    id: strip

    required property var app

    readonly property var mp: app.doc && app.doc.multipath ? app.doc.multipath : ({})
    readonly property var card: app.doc && app.doc.internal_card ? app.doc.internal_card : ({})
    readonly property var nm: app.doc && app.doc.nm ? app.doc.nm : ({})
    readonly property real smallFont: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 1)
    property bool confirmInternalOff: false

    // Multipath needs two radios. A switched-off managed internal card counts
    // (turning it on makes two), and the switch never hides while multipath
    // is on, so it can always be turned off.
    readonly property int possibleRadios: app.radios.length + (app.internalGhostShown ? 1 : 0)
    readonly property bool showMultipath: possibleRadios >= 2 || !!mp.desired
    // The internal row shows whenever wifimimo manages an internal card, even
    // while it's off and absent from the bus: that row is the way back on.
    readonly property bool showInternal: !!card.managed

    spacing: 2
    visible: !!(app.doc && app.doc.helper_available) && (showMultipath || showInternal)

    function busy(verb) {
        return app.helperBusy.indexOf(verb) === 0;
    }

    function failed(verb) {
        return app.helperError !== "" && app.helperErrorVerb === verb;
    }

    function multipathSubtitle() {
        if (busy("multipath")) {
            return "applying…";
        }
        if (failed("multipath")) {
            return app.helperError;
        }
        if (mp.error) {
            return mp.error;
        }
        if (!mp.desired) {
            return "off";
        }
        if (mp.active) {
            const n = (mp.members || []).length;
            let text = "active · " + n + " radios · per-connection balancing";
            if (nm.leader && nm.leader.id) {
                text += " · following '" + nm.leader.id + "'";
            }
            return text;
        }
        return "on · waiting: " + (mp.reason || "re-applying");
    }

    function internalSubtitle() {
        if (confirmInternalOff) {
            return "Your only connected radio. Turn it off anyway?";
        }
        if (busy("internal")) {
            return app.helperBusy.endsWith("enable") ? "turning on…" : "turning off…";
        }
        if (failed("internal")) {
            return app.helperError;
        }
        if (card.present) {
            return "on" + (card.iface ? " · " + card.iface : "") + (card.bound ? "" : " · no driver");
        }
        return card.desired ? "on · waiting for the card" : "off · removed from the PCI bus";
    }

    component RowTitle: PlasmaComponents3.Label {
        font.bold: true
        font.family: strip.app.monospaceFamily
    }

    component RowSubtitle: PlasmaComponents3.Label {
        id: sub
        property bool warn: false
        property bool bad: false
        Layout.fillWidth: true
        elide: Text.ElideRight
        color: bad ? Kirigami.Theme.negativeTextColor
             : warn ? Kirigami.Theme.neutralTextColor
             : Kirigami.Theme.disabledTextColor
        font.family: strip.app.monospaceFamily
        font.pixelSize: strip.smallFont

        MouseArea {
            id: subHover
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.NoButton
        }
        PlasmaComponents3.ToolTip {
            visible: subHover.containsMouse && sub.truncated
            text: sub.text
        }
    }

    // Row: Multipath
    RowLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: Kirigami.Units.gridUnit * 2.2
        visible: strip.showMultipath
        spacing: Kirigami.Units.smallSpacing

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            RowTitle {
                text: "Multipath"
            }
            RowSubtitle {
                text: strip.multipathSubtitle()
                bad: strip.failed("multipath") || !!strip.mp.error
            }
        }
        PlasmaComponents3.BusyIndicator {
            visible: strip.busy("multipath")
            Layout.preferredWidth: Kirigami.Units.iconSizes.medium
            Layout.preferredHeight: Kirigami.Units.iconSizes.medium
        }
        PlasmaComponents3.Switch {
            visible: !strip.busy("multipath")
            enabled: strip.app.helperBusy === ""
            checked: !!strip.mp.desired
            onToggled: {
                strip.app.runHelper("multipath", checked ? "enable" : "disable");
                checked = Qt.binding(() => !!strip.mp.desired);
            }
        }
    }

    // Row: Internal Wi-Fi (only when install.sh --manage-internal set it up)
    RowLayout {
        Layout.fillWidth: true
        Layout.preferredHeight: Kirigami.Units.gridUnit * 2.2
        visible: strip.showInternal
        spacing: Kirigami.Units.smallSpacing

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            RowTitle {
                text: "Internal Wi-Fi"
            }
            RowSubtitle {
                text: strip.internalSubtitle()
                warn: strip.confirmInternalOff
                bad: strip.failed("internal")
            }
        }
        PlasmaComponents3.BusyIndicator {
            visible: strip.busy("internal")
            Layout.preferredWidth: Kirigami.Units.iconSizes.medium
            Layout.preferredHeight: Kirigami.Units.iconSizes.medium
        }
        // Confirmation replaces the switch in place (no extra row).
        PlasmaComponents3.ToolButton {
            visible: strip.confirmInternalOff
            text: "Turn off"
            onClicked: {
                strip.confirmInternalOff = false;
                strip.app.runHelper("internal", "disable");
            }
        }
        PlasmaComponents3.ToolButton {
            visible: strip.confirmInternalOff
            text: "Cancel"
            onClicked: strip.confirmInternalOff = false
        }
        PlasmaComponents3.Switch {
            visible: !strip.busy("internal") && !strip.confirmInternalOff
            enabled: strip.app.helperBusy === ""
            checked: !!strip.card.desired || !!strip.card.present
            onToggled: {
                if (!checked && strip.app.onlyConnectedIsInternal) {
                    strip.confirmInternalOff = true;
                } else {
                    strip.app.runHelper("internal", checked ? "enable" : "disable");
                }
                checked = Qt.binding(() => !!strip.card.desired || !!strip.card.present);
            }
        }
    }
}
