#!/usr/bin/env python3
"""Walk scan tables (run by tools/walk-log): every 2 s, each radio's cfg80211
scan table (dBm, age, channel geometry, BSS load) -> dumps.jsonl.
Unprivileged `iw dev X scan dump` (no scan is triggered).
usage: walk_dump.py OUT_DIR SECONDS"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

OUT = Path(sys.argv[1])
END = time.time() + float(sys.argv[2])


def dump(dev):
    try:
        out = subprocess.run(["iw", "dev", dev, "scan", "dump"], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return []
    bss, cur = [], None
    for raw in out.splitlines():
        line = raw.strip()
        m = re.match(r"BSS ([0-9a-f:]{17})", raw)
        if m:
            cur = {"bssid": m.group(1), "assoc": "associated" in raw}
            bss.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("freq:"):
            cur["freq"] = int(float(line.split()[1]))
        elif line.startswith("signal:"):
            cur["sig"] = float(line.split()[1])
        elif re.match(r"last seen: \d+ ms ago", line):
            cur["age_ms"] = int(line.split()[2])
        elif line.startswith("SSID:"):
            cur["ssid"] = line[5:].strip()
        elif line.startswith("* secondary channel offset:"):
            cur["ht_sec"] = line.split(":", 1)[1].strip()
        elif line.startswith("* channel width:") and "vht_w" not in cur:
            cur["vht_w"] = line.split(":", 1)[1].strip()
        elif line.startswith("* center freq segment 1:"):
            cur["vht_c1"] = int(line.split(":", 1)[1])
        elif line.startswith("* center freq segment 2:"):
            cur["vht_c2"] = int(line.split(":", 1)[1])
        elif line.startswith("* station count:"):
            cur["sta_count"] = int(line.split(":", 1)[1])
        elif line.startswith("* channel utilisation:"):
            cur["util"] = int(line.split(":", 1)[1].split("/")[0])
        elif "Authentication suites:" in line:
            cur["akm"] = line.split(":", 1)[1].strip()
    return bss


with open(OUT / "dumps.jsonl", "a") as f:
    while time.time() < END:
        t0 = time.time()
        devs = sorted(p.name for p in Path("/sys/class/net").iterdir() if (p / "phy80211").exists())
        f.write(json.dumps({"t": round(t0, 3), "dumps": {d: dump(d) for d in devs}}) + "\n")
        f.flush()
        time.sleep(max(0.1, 2.0 - (time.time() - t0)))
