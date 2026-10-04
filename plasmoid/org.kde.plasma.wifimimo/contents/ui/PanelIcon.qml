import QtQuick
import QtQuick.Shapes

import org.kde.kirigami as Kirigami

// The panel glyph (a hotspot mast), drawn rather than loaded from an SVG so
// its colour follows the panel it sits on: an SVG file carries one fixed fill
// (the old white one all but vanished on a light panel), and Kirigami.Icon
// doesn't recolour a file. The tier picks the colour:
//   disabled  no link / wifi off / stale   panel text colour, dimmed
//   alert     max(tx,rx) NSS < 2           the theme's negative colour
//   good      2x2, multi-link MLO          gold
//   wifi6e    2x2, 6 GHz, single link      blue
//   normal    2x2, everything else         panel text colour
Item {
    id: icon

    property string tier: "normal"

    // Gold and blue have no theme role, so each has a shade per panel
    // lightness, both at least 3:1 against Breeze / Breath light and dark
    // panels (the old #f9a825 gold was 1.5:1 on a light one).
    readonly property bool darkPanel: {
        const c = Kirigami.Theme.backgroundColor;
        return (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) < 0.5;
    }
    readonly property color glyphColor: {
        switch (tier) {
        case "alert":
            return Kirigami.Theme.negativeTextColor;
        case "good":
            return darkPanel ? "#f9a825" : "#9a6c00";
        case "wifi6e":
            return darkPanel ? "#42a5f5" : "#1565c0";
        default:
            return Kirigami.Theme.textColor;
        }
    }

    implicitWidth: Kirigami.Units.iconSizes.smallMedium
    implicitHeight: Kirigami.Units.iconSizes.smallMedium
    opacity: tier === "disabled" ? 0.45 : 1.0

    // Drawn on the original SVG's 22x22 grid and scaled to fit. Three
    // separate paths, as in the SVG: their even-odd holes (the arcs' gap,
    // the ring's centre) mustn't cancel where the mast overlaps the ring.
    Shape {
        width: 22
        height: 22
        anchors.centerIn: parent
        scale: Math.min(icon.width, icon.height) / 22
        preferredRendererType: Shape.CurveRenderer

        ShapePath {
            fillColor: icon.glyphColor
            strokeColor: "transparent"
            fillRule: ShapePath.OddEvenFill
            PathSvg {
                path: "M11 3c-4.433594 0-8 3.566406-8 8 0 1.960938.699219 3.75 1.863281 5.140625l.710938-.710937c-.984375-1.207032-1.574219-2.746094-1.574219-4.429688 0-3.878906 3.121094-7 7-7s7 3.121094 7 7c0 1.683594-.589844 3.222656-1.574219 4.429688l.714844.710937c1.160156-1.390625 1.859375-3.179687 1.859375-5.140625 0-4.433594-3.566406-8-8-8zm0 3c-2.769531 0-5 2.230469-5 5 0 1.128906.382812 2.15625 1.007812 2.992188l.710938-.707032c-.453125-.648437-.71875-1.433594-.71875-2.285156 0-2.214844 1.785156-4 4-4s4 1.785156 4 4c0 .851562-.265625 1.636719-.71875 2.285156l.710938.707032c.625-.835938 1.007812-1.863282 1.007812-2.992188 0-2.769531-2.230469-5-5-5z"
            }
        }
        ShapePath {
            fillColor: icon.glyphColor
            strokeColor: "transparent"
            fillRule: ShapePath.OddEvenFill
            PathSvg {
                path: "M11 9c-1.109375 0-2 .890625-2 2s.890625 2 2 2 2-.890625 2-2-.890625-2-2-2zm0 1c.554688 0 1 .445312 1 1s-.445312 1-1 1-1-.445312-1-1 .445312-1 1-1z"
            }
        }
        ShapePath {
            fillColor: icon.glyphColor
            strokeColor: "transparent"
            PathSvg {
                path: "M10 12h2v7h-2z"
            }
        }
    }
}
