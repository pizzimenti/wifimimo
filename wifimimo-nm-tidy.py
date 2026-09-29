#!/usr/bin/env python3
"""wifimimo-nm-tidy: collapse per-card copies of Wi-Fi profiles.

Before v1.0, running two radios on one network meant cloning a profile per
card (NetworkManager lets a profile be active on one device at a time).
wifimimo's NM follow makes that unnecessary, and card-bound copies block it.

For every SSID with a card-bound profile, keep one (an unbound one if it
exists, else the most recently used, with its card binding cleared) and
delete the other bound ones. Dry run by default; pass --apply to change
anything.
"""

from __future__ import annotations

import argparse
import sys

from wifimimo_nm import PROFILE_FIELDS, parse_profile, plan_tidy, run_nmcli, split_terse


def list_wifi_profiles() -> list[dict]:
    rc, out = run_nmcli(["-t", "-f", "UUID,TYPE", "connection", "show"])
    if rc != 0:
        sys.exit("nmcli failed; is NetworkManager running?")
    profiles = []
    for line in out.splitlines():
        parts = split_terse(line)
        if len(parts) >= 2 and parts[1] == "802-11-wireless":
            rc, detail = run_nmcli(["-t", "-f", PROFILE_FIELDS, "connection", "show", "uuid", parts[0]])
            if rc == 0:
                profiles.append(parse_profile(detail))
    return profiles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = parser.parse_args(argv)

    plan = plan_tidy(list_wifi_profiles())
    if not plan:
        print("Nothing to tidy: no Wi-Fi profile is tied to one card.")
        return 0
    for group in plan:
        keep = group["keep"]
        note = " (clearing its card binding)" if keep["clear_binding"] else ""
        print(f"{group['ssid']}: keep '{keep['id']}'{note}")
        for dead in group["delete"]:
            print(f"    delete '{dead['id']}' ({dead['uuid']})")
    if not args.apply:
        print("\nDry run. Re-run with --apply to make these changes.")
        return 0

    failed = False
    for group in plan:
        keep = group["keep"]
        if keep["clear_binding"]:
            rc, _ = run_nmcli(["connection", "modify", "uuid", keep["uuid"],
                            "802-11-wireless.mac-address", "", "connection.interface-name", "",
                            "ipv4.route-metric", "-1"])
            failed |= rc != 0
        for dead in group["delete"]:
            rc, _ = run_nmcli(["connection", "delete", "uuid", dead["uuid"]])
            failed |= rc != 0
            print(f"{'deleted' if rc == 0 else 'FAILED to delete'} '{dead['id']}'")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
