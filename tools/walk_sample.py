#!/usr/bin/env python3
"""Walk sampler (run by tools/walk-log): 1 Hz radio / route / follower state,
2 s NM AP lists, and a 1 Hz new-connection probe. Triggers no scans (the
follower does). Keeps the daemon in 1 s polling while it runs. Local files
only. usage: walk_sample.py OUT_DIR [SECONDS]"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

OUT = Path(sys.argv[1])
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 3600.0
OUT.mkdir(parents=True, exist_ok=True)
RUN = Path(f"/run/user/{os.getuid()}")
STATE = RUN / "wifimimo-state"
UI = RUN / "wifimimo-ui-active"
END = time.time() + DUR
lock = threading.Lock()


def run(args, timeout=3.0):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return 127, ""


def radios():
    return sorted(p.name for p in Path("/sys/class/net").iterdir() if (p / "phy80211").exists())


INT_KEYS = ("tx retries", "tx failed", "beacon loss", "connected time", "inactive time",
            "rx bytes", "tx bytes", "rx packets", "tx packets", "rx drop misc")
DBM_KEYS = ("signal", "signal avg", "beacon signal avg", "last ack signal")


def station(dev):
    _, out = run(["iw", "dev", dev, "station", "dump"])
    st = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Station "):
            st["bssid"] = line.split()[1]
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        name = key.replace(" ", "_")
        if key in DBM_KEYS:
            m = re.match(r"(-?\d+)", val)
            st[name] = int(m.group(1)) if m else None
        elif key in ("tx bitrate", "rx bitrate"):
            m = re.match(r"([\d.]+)", val)
            st[name] = float(m.group(1)) if m else None
            st[name + "_raw"] = val
        elif key in INT_KEYS:
            m = re.match(r"(\d+)", val)
            st[name] = int(m.group(1)) if m else None
    return st


def info(dev):
    _, out = run(["iw", "dev", dev, "info"])
    m = re.search(r"channel (\d+) \((\d+) MHz\), width: (\d+) MHz(?:, center1: (\d+) MHz)?", out)
    if not m:
        return {}
    return {"chan": int(m.group(1)), "freq": int(m.group(2)), "width": int(m.group(3)),
            "center1": int(m.group(4) or 0)}


def nm_devices():
    _, out = run(["nmcli", "-t", "-f",
                  "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.REASON,GENERAL.CONNECTION,"
                  "GENERAL.IP4-CONNECTIVITY", "device", "show"])
    devs, cur = {}, None
    for line in out.splitlines():
        key, _, val = line.partition(":")
        if key == "GENERAL.DEVICE":
            cur = devs.setdefault(val, {})
        elif cur is not None and key:
            cur[key.split(".", 1)[1].lower()] = val
    return {k: v for k, v in devs.items() if v.get("type") == "wifi"}


def ipv4():
    _, out = run(["ip", "-j", "-4", "addr", "show"])
    try:
        return {i["ifname"]: [a["local"] for a in i.get("addr_info", [])] for i in json.loads(out or "[]")}
    except ValueError:
        return {}


def ecmp():
    _, out = run(["ip", "-j", "route", "show", "table", "100"])
    try:
        routes = json.loads(out or "[]")
    except ValueError:
        return None
    devs = []
    for r in routes:
        if r.get("dst") != "default":
            continue
        if r.get("nexthops"):
            devs += [n.get("dev") for n in r["nexthops"]]
        elif r.get("dev"):
            devs.append(r["dev"])
    return devs


def daemon():
    try:
        d = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}
    return {"nm": d.get("nm"), "multipath": d.get("multipath"), "sampled_at": d.get("sampled_at")}


def sampler():
    with open(OUT / "samples.jsonl", "a") as f:
        while time.time() < END:
            t0 = time.time()
            try:
                UI.touch()
            except OSError:
                pass
            rec = {"t": round(t0, 3), "radios": {}, "nm": nm_devices(), "ip": ipv4(),
                   "ecmp": ecmp(), "daemon": daemon()}
            for dev in radios():
                try:
                    oper = Path(f"/sys/class/net/{dev}/operstate").read_text().strip()
                except OSError:
                    oper = "gone"
                rec["radios"][dev] = {"oper": oper, **info(dev), "sta": station(dev)}
            with lock:
                f.write(json.dumps(rec) + "\n")
                f.flush()
            time.sleep(max(0.05, 1.0 - (time.time() - t0)))


def aplists():
    with open(OUT / "aps.jsonl", "a") as f:
        while time.time() < END:
            t0 = time.time()
            _, out = run(["nmcli", "-t", "-f", "SSID,BSSID,CHAN,FREQ,BANDWIDTH,SIGNAL,DEVICE,IN-USE",
                          "device", "wifi", "list", "--rescan", "no"], timeout=5)
            rows = []
            for line in out.splitlines():
                parts, cur, i = [], [], 0
                while i < len(line):
                    if line[i] == "\\" and i + 1 < len(line):
                        cur.append(line[i + 1]); i += 2; continue
                    if line[i] == ":":
                        parts.append("".join(cur)); cur = []
                    else:
                        cur.append(line[i])
                    i += 1
                parts.append("".join(cur))
                if len(parts) >= 8:
                    rows.append(parts[:8])
            f.write(json.dumps({"t": round(t0, 3), "aps": rows}) + "\n")
            f.flush()
            time.sleep(max(0.1, 2.0 - (time.time() - t0)))


def probe():
    with open(OUT / "http.csv", "a") as f:
        f.write("t,code,connect_s,total_s,local_ip\n")
        while time.time() < END:
            t0 = time.time()
            # --noproxy: a proxy's answer says nothing about the default route
            _, out = run(["curl", "-s", "--noproxy", "*", "-o", "/dev/null", "-w",
                          "%{http_code},%{time_connect},%{time_total},%{local_ip}",
                          "--max-time", "2", "http://connectivity-check.ubuntu.com/"], timeout=4)
            f.write(f"{t0:.3f},{out.strip() or '000,,,'}\n")
            f.flush()
            time.sleep(max(0.05, 1.0 - (time.time() - t0)))


threads = [threading.Thread(target=fn, daemon=True) for fn in (sampler, aplists, probe)]
for th in threads:
    th.start()
while time.time() < END and all(th.is_alive() for th in threads):
    time.sleep(1)
