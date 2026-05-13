#!/usr/bin/env python3
"""One-shot fixture capture.

Run on a live machine to snapshot:
  * the current `iw dev <iface> link` output → tests/fixtures/iw_link_live.txt
  * the pyroute2 `list_dev` + `get_stations` attribute dicts → tests/fixtures/nl80211_live.pkl

Re-run when AP / driver / kernel changes uncover a PHY mode or MLO shape
the suite doesn't yet cover, then promote the output to a named fixture
(iw_link_eht_mlo.txt, etc.) and add a matching parametrised test case.

Usage:  python tests/capture_fixtures.py [iface]
"""

from __future__ import annotations

import pickle
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURES.mkdir(parents=True, exist_ok=True)


def _capture_iw_link(iface: str) -> None:
    result = subprocess.run(
        ["iw", "dev", iface, "link"],
        capture_output=True, text=True, check=False, timeout=5,
    )
    out = FIXTURES / "iw_link_live.txt"
    out.write_text(result.stdout, encoding="utf-8")
    print(f"wrote {out}")


def _capture_nl80211(iface: str) -> None:
    try:
        from pyroute2 import IW
    except ImportError:
        print("pyroute2 unavailable; skipping nl80211 capture", file=sys.stderr)
        return

    iw = IW()
    try:
        list_dev = list(iw.list_dev())
        ifindex = None
        for msg in list_dev:
            for k, v in msg.get("attrs", []):
                if k == "NL80211_ATTR_IFNAME" and v == iface:
                    for k2, v2 in msg.get("attrs", []):
                        if k2 == "NL80211_ATTR_IFINDEX":
                            ifindex = int(v2)
                            break
                    break
            if ifindex is not None:
                break
        stations = list(iw.get_stations(ifindex)) if ifindex else []
    finally:
        iw.close()

    out = FIXTURES / "nl80211_live.pkl"
    payload = {"list_dev": list_dev, "stations": stations, "iface": iface, "ifindex": ifindex}
    out.write_bytes(pickle.dumps(payload))
    print(f"wrote {out} (ifindex={ifindex}, stations={len(stations)})")


def main() -> int:
    iface = sys.argv[1] if len(sys.argv) > 1 else "wlp1s0"
    _capture_iw_link(iface)
    _capture_nl80211(iface)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
