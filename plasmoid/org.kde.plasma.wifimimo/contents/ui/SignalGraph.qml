pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

// Last 60 s of signal (dBm) for every radio on one FIXED axis (-90..-30),
// so "good" always sits in the same place and a glance tells the story.
// Points are placed by wall-clock time, not index: the daemon samples at
// 5 s while the popup is closed and 1 s while it's open, and gaps (radio
// down, unplugged) break the line instead of being bridged.
Item {
    id: graph

    required property var app

    readonly property real yTop: -30
    readonly property real yBottom: -90
    readonly property real windowS: 60
    readonly property var gridLines: [-30, -50, -65, -75, -90]
    readonly property real nowTs: app.doc && app.doc.sampled_at ? app.doc.sampled_at : Date.now() / 1000
    readonly property var series: app.radios
    readonly property real smallFont: Math.max(9, Kirigami.Theme.defaultFont.pixelSize - 2)

    readonly property real leftPad: Kirigami.Units.gridUnit * 2
    readonly property real rightPad: Kirigami.Units.gridUnit * 5.5
    readonly property real bottomPad: Kirigami.Units.gridUnit * 0.9
    readonly property real plotW: Math.max(1, width - leftPad - rightPad)
    readonly property real plotH: Math.max(1, height - bottomPad)

    implicitHeight: Kirigami.Units.gridUnit * 5

    function xFor(ts) {
        return (ts - (nowTs - windowS)) / windowS * plotW;
    }

    function yFor(dbm) {
        const c = Math.max(yBottom, Math.min(yTop, dbm));
        return (yTop - c) / (yTop - yBottom) * plotH;
    }

    // Split a [[ts, dbm], ...] history into polylines, breaking wherever the
    // spacing jumps well past the normal cadence (a gap, not slow polling).
    function segments(points) {
        if (!points || points.length === 0) {
            return [];
        }
        const gaps = [];
        for (let i = 1; i < points.length; ++i) {
            gaps.push(points[i][0] - points[i - 1][0]);
        }
        const sorted = gaps.slice().sort((a, b) => a - b);
        const median = sorted.length ? sorted[Math.floor(sorted.length / 2)] : 1;
        const breakAt = Math.max(6, 2.5 * median);
        const out = [];
        let cur = [];
        for (let i = 0; i < points.length; ++i) {
            if (i > 0 && points[i][0] - points[i - 1][0] > breakAt) {
                out.push(cur);
                cur = [];
            }
            cur.push(Qt.point(xFor(points[i][0]), yFor(points[i][1])));
        }
        out.push(cur);
        // A single sample renders as a short dash so it's still visible.
        return out.map(seg => seg.length === 1
            ? [seg[0], Qt.point(seg[0].x + 3, seg[0].y)] : seg);
    }

    // End labels ("A9000 -56"), nudged apart so they never overlap.
    readonly property var labelSlots: {
        const rows = [];
        for (let i = 0; i < series.length; ++i) {
            const s = series[i];
            if (s.history && s.history.length > 0 && s.connected) {
                const last = s.history[s.history.length - 1][1];
                rows.push({ index: i, y: yFor(last), dbm: last });
            }
        }
        rows.sort((a, b) => a.y - b.y);
        const gap = smallFont + 3;
        for (let i = 1; i < rows.length; ++i) {
            rows[i].y = Math.max(rows[i].y, rows[i - 1].y + gap);
        }
        const map = {};
        for (const r of rows) {
            map[r.index] = r;
        }
        return map;
    }

    // --- background: tier bands and gridlines -------------------------------
    Item {
        id: plotBg
        x: graph.leftPad
        width: graph.plotW
        height: graph.plotH

        Rectangle {
            y: 0
            width: parent.width
            height: graph.yFor(-65)
            color: Kirigami.Theme.positiveTextColor
            opacity: 0.07
        }
        Rectangle {
            y: graph.yFor(-65)
            width: parent.width
            height: graph.yFor(-75) - graph.yFor(-65)
            color: Kirigami.Theme.neutralTextColor
            opacity: 0.07
        }
        Rectangle {
            y: graph.yFor(-75)
            width: parent.width
            height: graph.plotH - graph.yFor(-75)
            color: Kirigami.Theme.negativeTextColor
            opacity: 0.07
        }

        Repeater {
            model: graph.gridLines
            delegate: Rectangle {
                required property var modelData
                y: Math.round(graph.yFor(modelData))
                width: plotBg.width
                height: 1
                color: Kirigami.Theme.textColor
                opacity: 0.15
            }
        }
    }

    Repeater {
        model: graph.gridLines
        delegate: PlasmaComponents3.Label {
            required property var modelData
            x: 0
            width: graph.leftPad - 4
            y: Math.round(graph.yFor(modelData) - height / 2)
            horizontalAlignment: Text.AlignRight
            text: modelData
            color: Kirigami.Theme.disabledTextColor
            font.family: graph.app.monospaceFamily
            font.pixelSize: graph.smallFont
        }
    }

    Repeater {
        model: [{ label: "-60s", at: 0 }, { label: "-30s", at: 0.5 }, { label: "now", at: 1 }]
        delegate: PlasmaComponents3.Label {
            required property var modelData
            y: graph.plotH + 1
            x: graph.leftPad + graph.plotW * modelData.at
               - (modelData.at === 0 ? 0 : modelData.at === 1 ? width : width / 2)
            text: modelData.label
            color: Kirigami.Theme.disabledTextColor
            font.family: graph.app.monospaceFamily
            font.pixelSize: graph.smallFont
        }
    }

    // --- the lines ----------------------------------------------------------
    Item {
        id: plot
        x: graph.leftPad
        width: graph.plotW
        height: graph.plotH
        clip: true

        Repeater {
            model: graph.series
            delegate: Shape {
                id: line
                required property var modelData
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                // Selected radio on top and heavier; the rest recede a little.
                z: modelData.selected ? 2 : 1
                opacity: modelData.selected || graph.app.selectedIface === "" ? 1.0 : 0.7

                ShapePath {
                    strokeColor: line.modelData.color
                    strokeWidth: line.modelData.selected ? 2.5 : 1.5
                    fillColor: "transparent"
                    capStyle: ShapePath.RoundCap
                    joinStyle: ShapePath.RoundJoin
                    PathMultiline {
                        paths: graph.segments(line.modelData.history)
                    }
                }
            }
        }
    }

    Repeater {
        model: graph.series
        delegate: Row {
            id: endLabel
            required property var modelData
            required property int index
            readonly property var slot: graph.labelSlots[index]
            visible: slot !== undefined
            x: graph.leftPad + graph.plotW + 5
            y: visible ? Math.round(slot.y - height / 2) : 0
            spacing: 3

            Rectangle {
                width: 7
                height: 7
                radius: 3.5
                anchors.verticalCenter: parent.verticalCenter
                color: endLabel.modelData.color
            }
            PlasmaComponents3.Label {
                text: endLabel.modelData.name + " " + (endLabel.slot ? endLabel.slot.dbm : "")
                color: Kirigami.Theme.textColor
                font.family: graph.app.monospaceFamily
                font.pixelSize: graph.smallFont
                font.bold: endLabel.modelData.selected
            }
        }
    }

    PlasmaComponents3.Label {
        anchors.centerIn: plot
        visible: graph.series.length === 0
        text: "No wifi radios"
        color: Kirigami.Theme.disabledTextColor
        font.family: graph.app.monospaceFamily
    }
}
