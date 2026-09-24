pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

// Multipath and internal-card switches. Both run the root helper through
// pkexec (polkit action allows the active session without a prompt). A
// switch's `checked` always tracks the daemon's reported state: after a
// click we re-bind it, so the UI can never claim a state the machine isn't in.
ColumnLayout {
    id: strip

    required property var app

    readonly property var mp: app.doc && app.doc.multipath ? app.doc.multipath : ({})
    readonly property var card: app.doc && app.doc.internal_card ? app.doc.internal_card : ({})
    readonly property var nm: app.doc && app.doc.nm ? app.doc.nm : ({})
    property bool confirmInternalOff: false

    spacing: 2
    visible: !!(app.doc && app.doc.helper_available)

    function multipathSubtitle() {
        if (app.helperBusy.indexOf("multipath") === 0) {
            return "applying…";
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
        if (app.helperBusy.indexOf("internal") === 0) {
            return app.helperBusy.endsWith("enable") ? "turning on…" : "turning off…";
        }
        if (card.present) {
            return "on" + (card.iface ? " · " + card.iface : "") + (card.bound ? "" : " · no driver");
        }
        return card.desired ? "on · waiting for the card" : "off · removed from the PCI bus";
    }

    // Row: Multipath
    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            PlasmaComponents3.Label {
                text: "Multipath"
                font.bold: true
                font.family: strip.app.monospaceFamily
            }
            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: strip.multipathSubtitle()
                elide: Text.ElideRight
                color: strip.mp.error ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.disabledTextColor
                font.family: strip.app.monospaceFamily
                font.pixelSize: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 1)
            }
        }
        PlasmaComponents3.BusyIndicator {
            visible: strip.app.helperBusy.indexOf("multipath") === 0
            implicitWidth: Kirigami.Units.iconSizes.medium
            implicitHeight: implicitWidth
        }
        PlasmaComponents3.Switch {
            id: mpSwitch
            visible: strip.app.helperBusy.indexOf("multipath") !== 0
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
        visible: !!strip.card.managed
        spacing: Kirigami.Units.smallSpacing

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            PlasmaComponents3.Label {
                text: "Internal Wi-Fi"
                font.bold: true
                font.family: strip.app.monospaceFamily
            }
            PlasmaComponents3.Label {
                Layout.fillWidth: true
                text: strip.internalSubtitle()
                elide: Text.ElideRight
                color: Kirigami.Theme.disabledTextColor
                font.family: strip.app.monospaceFamily
                font.pixelSize: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 1)
            }
        }
        PlasmaComponents3.BusyIndicator {
            visible: strip.app.helperBusy.indexOf("internal") === 0
            implicitWidth: Kirigami.Units.iconSizes.medium
            implicitHeight: implicitWidth
        }
        PlasmaComponents3.Switch {
            visible: strip.app.helperBusy.indexOf("internal") !== 0
            enabled: strip.app.helperBusy === "" && !strip.confirmInternalOff
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

    // Guard: turning off the only connected radio disconnects you.
    RowLayout {
        Layout.fillWidth: true
        visible: strip.confirmInternalOff
        spacing: Kirigami.Units.smallSpacing

        PlasmaComponents3.Label {
            Layout.fillWidth: true
            text: "It's your only connected radio. Turn it off anyway?"
            wrapMode: Text.Wrap
            color: Kirigami.Theme.neutralTextColor
            font.family: strip.app.monospaceFamily
        }
        PlasmaComponents3.Button {
            text: "Turn off"
            onClicked: {
                strip.confirmInternalOff = false;
                strip.app.runHelper("internal", "disable");
            }
        }
        PlasmaComponents3.Button {
            text: "Cancel"
            onClicked: strip.confirmInternalOff = false
        }
    }

    PlasmaComponents3.Label {
        Layout.fillWidth: true
        visible: strip.app.helperError !== ""
        text: strip.app.helperError
        wrapMode: Text.Wrap
        color: Kirigami.Theme.negativeTextColor
        font.family: strip.app.monospaceFamily
        font.pixelSize: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 1)
    }
}
