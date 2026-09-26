# Roaming and radio placement

How wifimimo decides where each radio sits while multipath is on, and why each
rule exists. Every rule below came out of walk tests at a three-building
property on 2026-09-25 (three access points: a workshed AP with 2.4 GHz ch 11 +
5 GHz ch 52/80, a riverhouse AP on 5 GHz ch 36/80 and 2.4 GHz, and a
lavenderhouse Omada EAP720 on 2.4 GHz ch 6 + 5 GHz ch 100/80), with an A9000
(mt7925u), an A8000 (mt7921u) and the built-in mt7925e.

Code: `wifimimo_roam.py` (pure planner), `wifimimo_nm.py` (NetworkManager
follower: I/O, judging, holds, traffic check, fail-safe), `wifimimo-helper`
(root: routing), `wifimimo-daemon.py` (poll loop).

## The model

- The network you pick in the Plasma applet is the **leader**. Every other radio
  is placed on the same network by the planner, one change per poll.
- A **slot** is one access point (BSSID) on one channel span (its primary channel
  widened to its real width, read from the beacon's HT/VHT/HE operation).
- Preference, most important first:
  1. never two radios on one BSSID;
  2. stay connected at all;
  3. **strong slots** (≥ −70 dBm to join, kept down to −75), of which
  4. **tier 1** = a channel no other radio overlaps (this AP's other band or
     another AP); **tier 2** = another AP on an overlapping channel;
  5. then estimated capacity (MCS from SNR × width × free airtime), with
     hysteresis (≥ 20 %, ≥ 40 Mb/s, and ≥ 6 dB stronger for a held radio).
- A radio with no strong slot of its own **scouts**: disconnected, and scanning
  every 6 s while anything moves (30 s when still), so it takes a slot the moment
  one appears. The widget shows it as "Scouting".

## Signal

- Each radio's level is sampled every second and tracked as a trend
  (least-squares slope over 8 s, per BSSID). A held slot is graded by where it's
  heading 6 s out, so a fading radio moves while its link still works.
- Cards hear the air differently (the built-in read ~20 dB below the A9000 on the
  same AP). A reading one radio took is used for another only after a learned
  per-card, per-band offset, less a 3 dB uncertainty penalty.
- Candidates come from each radio's own cfg80211 scan table (`iw dev X scan
  dump`: dBm, age, width, BSS load); joinability from NetworkManager's list.
  Readings older than 10 s are never acted on.

## Rules learned on the walks

| Rule | Evidence |
|---|---|
| One switch at a time: a radio mid-switch is busy until it lands or is judged failed | two overlapping switches left all three radios down for 66 s |
| A move drops the old link first, then joins | switching in place failed "no secrets" 2 of 2 times; joins from disconnected 0 of 8 |
| Nothing below −72 dBm is ever tried | APs with a minimum-signal floor (the EAP720 drops clients it hears below −75) hear us weaker than we hear them: one AP read at −70 heard the A8000 at −84 |
| Traffic check: a gateway ping per radio every 2 s, bound to that radio; 3 misses = dead (2 if the card is stuck at the lowest rate) | a card sat associated at −55 dBm passing nothing for minutes; NM's connectivity check and the neighbour cache both still called it fine |
| The traffic check restarts when a radio changes AP | every AP shares one gateway, so a switched radio kept its pre-switch last reply and read dead 2 s after landing |
| A join not up within 15 s is cancelled | NM waits 45 s for DHCP; each failed join cost ~50 s |
| A signal floor is learned only from joins that never got past authentication | a DHCP timeout isn't a signal problem: see "the AX1800" below |
| Two strikes: a failed AP is avoided 5 min, then retried; a second failure avoids it until you pick the network again | a long first avoid kept radios off an AP after it was fixed |
| A working radio moves for speed only to an AP some radio carried traffic on in the last 30 min; the only working radio never moves for speed | the A9000 left a working AP for an unproven one and lost 56 s |
| NetworkManager's autoconnect is blocked on every radio while following | a flickering radio was reconnected by NM onto the AP the planner had just given another radio |
| A replugged card's autoconnect, or a second profile for the same network, isn't a new network choice | a replugged A9000 autoconnected 3 s after reappearing and pulled every radio onto one AP |

## The AX1800 (not a wifimimo bug)

Riverhouse joins associated fine (even at −24 dBm) but never got an address. A
DHCP capture showed our requests leaving and nothing coming back; the router's
logs showed every request answered (unicast, since the client doesn't set the
broadcast flag). The cause was the AP's switch chip keeping a client's MAC pinned
to its wired uplink after the client had been on another AP, dropping the
replies inside the AP. Fixed on the AP side; afterwards every riverhouse join got
an address in 3–4 s. The 15 s give-up and the rule that DHCP failures teach no
signal floor are what kept this from wedging the planner.

## Tuning a site

`~/.config/wifimimo/roam.json` overrides the thresholds (validated, re-read on
change, see README). The evidence for changing them is the join log,
`~/.local/state/wifimimo/joins.jsonl`, and a walk log (`tools/walk-log`).

## Dead-man switches

See the README table: stop/crash → radios released (`ExecStopPost`); hang →
watchdog; routing re-checked every 20 s by a root timer independent of the
daemon, torn down if the check fails; nothing through for 60 s → radios back to
NetworkManager for 5 min.
