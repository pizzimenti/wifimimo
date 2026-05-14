"""Catch accidental re-introduction of PHY-mode tables in the QML.

The plasmoid is a dumb consumer of `data.display.*`. If any literal "HE" /
"VHT" / "HT" / "EHT" reappears as a string, someone has reimplemented a
table that already lives in `phy_modes.py` — and the two will drift.
"""

from __future__ import annotations

import re
from pathlib import Path

QML = Path(__file__).parent.parent / "plasmoid" / "org.kde.plasma.wifimimo" / "contents" / "ui" / "main.qml"


def test_qml_has_no_phy_mode_literals():
    text = QML.read_text(encoding="utf-8")
    # Strip line + block comments so commentary referring to "HE" doesn't trip.
    text = re.sub(r"//.*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    offenders = re.findall(r'"(EHT|HE|VHT|HT)"', text)
    assert not offenders, (
        f"PHY-mode string literals found in main.qml: {offenders!r}. "
        "Bind to data.display.* instead — phy_modes.py is the source of truth."
    )


def test_qml_does_not_redefine_efficiency_table():
    text = QML.read_text(encoding="utf-8")
    # The EFFICIENCY-like table was the original duplication site.
    assert "efficiencies" not in text and "function computeRates" not in text, (
        "main.qml has reintroduced a local PHY efficiency table; remove it and "
        "consume data.display.tx_rates_mbps / data.display.rx_rates_mbps."
    )
