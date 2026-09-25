pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

// Multipath and Internal Wi-Fi switches on ONE line. Both run the root helper
// through pkexec (the polkit action allows the active session without a
// prompt). A switch's `checked` always tracks the daemon's reported state:
// after a click it is re-bound, so the UI never claims a state the machine
// isn't in.
//
// No subtitles: state shows as label colour (neutral = on but waiting,
// negative = error) with the details in the label's tooltip; busy and
// "are you sure?" replace the switch in place. Nothing ever adds a line.
RowLayout {
    id: strip

    required property var app

    readonly property var mp: app.doc && app.doc.multipath ? app.doc.multipath : ({})
    readonly property var card: app.doc && app.doc.internal_card ? app.doc.internal_card : ({})
    readonly property var nm: app.doc && app.doc.nm ? app.doc.nm : ({})
    property bool confirmInternalOff: false

    // Multipath needs two radios; with one it's meaningless, so the switch
    // hides. A switched-off managed internal card counts (turning it on makes
    // two). The setting itself persists: plug in a second radio and the
    // switch reappears in the state it was left in.
    readonly property int possibleRadios: app.radios.length + (app.internalGhostShown ? 1 : 0)
    readonly property bool showMultipath: possibleRadios >= 2
    // Shown whenever wifimimo manages an internal card, even while it's off
    // and absent from the PCI bus: this switch is the way back on.
    readonly property bool showInternal: !!card.managed

    visible: !!(app.doc && app.doc.helper_available) && (showMultipath || showInternal)
    spacing: Kirigami.Units.smallSpacing

    function busy(verb) {
        return app.helperBusy.indexOf(verb) === 0;
    }

    function failed(verb) {
        return app.helperError !== "" && app.helperErrorVerb === verb;
    }

    function multipathDetail() {
        if (failed("multipath")) {
            return app.helperError;
        }
        if (mp.error) {
            return mp.error;
        }
        if (!mp.desired) {
            return "Off. Turn on to balance connections across all connected radios.";
        }
        if (mp.active) {
            let text = "Active on " + (mp.members || []).join(", ") + " (per-connection balancing)";
            if (nm.leader && nm.leader.id) {
                text += ". Following '" + nm.leader.id + "'";
            }
            return text + ".";
        }
        return "On, waiting: " + (mp.reason || "re-applying") + ".";
    }

    function internalDetail() {
        if (failed("internal")) {
            return app.helperError;
        }
        if (card.present) {
            return "On" + (card.iface ? " as " + card.iface : "") + (card.bound ? "." : ", no driver bound.");
        }
        return card.desired ? "On, waiting for the card to appear." : "Off: removed from the PCI bus.";
    }

    component SwitchLabel: PlasmaComponents3.Label {
        id: switchLabel
        property string detail: ""
        property bool waiting: false
        property bool bad: false
        font.bold: true
        font.family: strip.app.monospaceFamily
        color: bad ? Kirigami.Theme.negativeTextColor
             : waiting ? Kirigami.Theme.neutralTextColor
             : Kirigami.Theme.textColor

        MouseArea {
            id: labelHover
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.NoButton
        }
        PlasmaComponents3.ToolTip {
            visible: labelHover.containsMouse && switchLabel.detail !== ""
            text: switchLabel.detail
            delay: 300
        }
    }

    // --- Multipath ------------------------------------------------------------
    SwitchLabel {
        visible: strip.showMultipath
        text: "Multipath"
        detail: strip.multipathDetail()
        waiting: !!strip.mp.desired && !strip.mp.active
        bad: strip.failed("multipath") || !!strip.mp.error
    }
    PlasmaComponents3.BusyIndicator {
        visible: strip.showMultipath && strip.busy("multipath")
        Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
        Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
    }
    PlasmaComponents3.Switch {
        visible: strip.showMultipath && !strip.busy("multipath")
        enabled: strip.app.helperBusy === ""
        checked: !!strip.mp.desired
        onToggled: {
            strip.app.runHelper("multipath", checked ? "enable" : "disable");
            checked = Qt.binding(() => !!strip.mp.desired);
        }
    }

    Item {
        Layout.fillWidth: true
    }

    // --- Internal Wi-Fi (only when install.sh --manage-internal set it up) ----
    SwitchLabel {
        visible: strip.showInternal
        text: strip.confirmInternalOff ? "Only radio: off?" : "Internal Wi-Fi"
        detail: strip.confirmInternalOff
                ? "It's your only connected radio; turning it off disconnects you."
                : strip.internalDetail()
        waiting: strip.confirmInternalOff || (!!strip.card.desired && !strip.card.present)
        bad: strip.failed("internal")
    }
    PlasmaComponents3.BusyIndicator {
        visible: strip.showInternal && strip.busy("internal")
        Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
        Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
    }
    // Confirmation replaces the switch in place.
    PlasmaComponents3.ToolButton {
        visible: strip.showInternal && strip.confirmInternalOff
        icon.name: "dialog-ok-apply"
        display: QQC2.AbstractButton.IconOnly
        text: "Turn off"
        QQC2.ToolTip.visible: hovered
        QQC2.ToolTip.text: text
        onClicked: {
            strip.confirmInternalOff = false;
            strip.app.runHelper("internal", "disable");
        }
    }
    PlasmaComponents3.ToolButton {
        visible: strip.showInternal && strip.confirmInternalOff
        icon.name: "dialog-cancel"
        display: QQC2.AbstractButton.IconOnly
        text: "Cancel"
        QQC2.ToolTip.visible: hovered
        QQC2.ToolTip.text: text
        onClicked: strip.confirmInternalOff = false
    }
    PlasmaComponents3.Switch {
        visible: strip.showInternal && !strip.busy("internal") && !strip.confirmInternalOff
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
