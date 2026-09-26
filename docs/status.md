# Status: feature/v1.0 (2026-09-25)

Branch `feature/v1.0` at **1.7.0**; `main` is still at 0.4.0. Every change on
the branch has its own version and a major / minor / patch heading in
CHANGELOG.md; the merge takes the branch version as-is (tag `v1.7.0` on main).

## Done and verified live

- Multipath (L4 ECMP across radios, rules/tables outside main), NM follow,
  internal-card switch, health flags, signal graph, card names, widget layout.
- Slot planner and early roaming, walk-tested three times across three
  buildings (last walk: every join landed in 2–5 s, 9 of 178 new-connection
  probes failed, longest gap 4 s).
- Traffic check, fast give-up, per-card floors, two-strike avoidance, proven-AP
  gate, NM autoconnect holds, per-site tuning file.
- Dead-man switches: release on stop/crash (verified on a restart), daemon
  watchdog (active), root `wifimimo-deadman.timer` (installed, runs every 20 s,
  quiet when nothing changes), planner fail-safe (unit-tested).

## Not yet done

1. **Reboot check**: multipath comes back, the internal card stays off, the
   dead-man timer and watchdog are active, holds re-asserted within a poll.
2. **One fresh-eyes review** (single reviewer): the roaming planner, the follower's
   judging/holds, and the root helper's `check` path are the risk.
3. **PR and release** via the pr-review-cycle skill, only on Bradley's go.

## Open, not blocking

- **Swaps have never run live** (they need a third radio carrying traffic); they
  have an end-to-end test through the follower.
- **Thresholds were tuned on one property.** Check them at a public network
  (e.g. the library) with `tools/walk-log` and the join log; adjust with
  `roam.json` rather than code.
- The planner fail-safe and the "gateway doesn't answer pings" rule are
  unit-tested only.
- **Riverhouse 2.4 GHz may still drop DHCP replies**: after the AX1800 fix (which
  made every riverhouse 5 GHz join land in 3–4 s), one A8000 join to its 2.4 GHz
  side at 16:30:16 still ended "no address". Worth a check on the AP side; the
  15 s give-up and two-strike avoid contain it meanwhile.
- The router's device registry flags this laptop for holding one address per
  radio; that's the router's policy to decide, not a wifimimo change.
- A radio's routing slot is keyed by permanent MAC; three radios on one subnet
  each get a DHCP lease (by design).

## Testing a walk

```sh
tools/walk-log 3600 my-walk        # records to ./walk-logs/my-walk (1 Hz state,
                                   # scan tables, per-radio gateway pings,
                                   # new-connection probe, NM/kernel/helper logs)
tools/walk-summary walk-logs/my-walk
```

The per-join log (`~/.local/state/wifimimo/joins.jsonl`) records every attempt
with its target signal, outcome and time to land.
