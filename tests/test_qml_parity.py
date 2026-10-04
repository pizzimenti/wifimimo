"""Catch accidental re-introduction of PHY-mode tables in the QML.

The plasmoid is a dumb consumer of `data.display.*`. If any literal "HE" /
"VHT" / "HT" / "EHT" reappears as a string, someone has reimplemented a
table that already lives in `phy_modes.py` — and the two will drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import wifimimo_core

UI_DIR = Path(__file__).parent.parent / "plasmoid" / "org.kde.plasma.wifimimo" / "contents" / "ui"
QML = UI_DIR / "main.qml"
ALL_QML = sorted(UI_DIR.glob("*.qml"))


def _code(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    # Strip line + block comments so commentary referring to "HE" doesn't trip.
    text = re.sub(r"//.*", "", text)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def test_qml_has_no_phy_mode_literals():
    for path in ALL_QML:
        offenders = re.findall(r'"(EHT|HE|VHT|HT)"', _code(path))
        assert not offenders, (
            f"PHY-mode string literals found in {path.name}: {offenders!r}. "
            "Bind to data.display.* instead — phy_modes.py is the source of truth."
        )


def test_qml_does_not_redefine_efficiency_table():
    for path in ALL_QML:
        text = path.read_text(encoding="utf-8")
        # The EFFICIENCY-like table was the original duplication site.
        assert "efficiencies" not in text and "function computeRates" not in text, (
            f"{path.name} has reintroduced a local PHY efficiency table; remove it and "
            "consume data.display.tx_rates_mbps / data.display.rx_rates_mbps."
        )


def test_qml_schema_version_matches_core():
    found = re.findall(r"schema_version:\s*(\d+)", QML.read_text(encoding="utf-8"))
    assert found == [str(wifimimo_core.SCHEMA_VERSION)]


def test_qml_graph_axis_is_fixed():
    # The signal graph must never autoscale: a moving axis hides whether a
    # link has been good at a glance.
    text = (UI_DIR / "SignalGraph.qml").read_text(encoding="utf-8")
    assert "readonly property real yTop: -30" in text
    assert "readonly property real yBottom: -90" in text
