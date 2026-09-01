# Agent Guide: Building the Camera Directory

A procedure for an AI agent (Claude Code, Codex) to build `site_maps.json`
interactively with the user.

**Why this needs a human.** The Spot AI camera object is
`{id, name, status, location_id, location_name, local_ip, last_online,
mac_address, appliance_id, has_speakers}`. There is **no position, sequence,
or role field**. Camera order through the tunnel cannot be derived from the
API at any cost — it must be confirmed by someone who knows the site. Your job
is to do all the fetching and propose a well-reasoned draft, then get it
confirmed. Never silently guess the order.

**Scale.** A single site can have **up to 15 cameras on inspection arches
(including LPR) plus up to 8 in the tunnel — about 23 cameras** for one claim.
Sites also carry 20–30 cameras that are irrelevant here (lobby, vacuums,
equipment rooms). Filtering those out is most of the work.

---

## Step 0 — Prerequisites

```bash
pip install git+https://github.com/christopher-nance/spotai-python-wrapper.git
```

Key in `.env` as `spotai_api_key=zpka_...`. Confirm before anything else:

```python
spot = SpotAI(api_key=key)
assert spot.verify_key()
count, visible = spot.camera_count(), len(spot.cameras())
```

If `count > 0` and `visible == 0`, **stop**. The key has no Role attached.
Tell the user: Spot dashboard → Settings → API → the key → "Add new" above the
authorisations table → Role. Nothing below will work until that is fixed.

---

## Step 1 — Let the user pick a location

```python
for loc in spot.locations():
    print(loc["id"], loc["name"])
```

Present them numbered and ask which site to map. Build **one site map per
tunnel**, not per location — a site with two tunnels needs two entries, each
with its own cameras and its own transit time.

---

## Step 2 — Fetch and classify the cameras

```python
cams = spot.cameras(location_ids=[chosen_id])
```

Sort by name and classify each into one of four buckets **as a proposal**.

### Classification heuristics

These patterns come from real deployments. Treat them as a first draft to be
confirmed, never as an answer.

**LPR candidates** — name contains `LPR`, `ANPR`, `PLATE`, `Drive Up`,
`DriveUp`, `Paystation LPR`.
Most sites have **none**. That is normal and fine; the site then uses
timestamps instead.

**Entry / entrance arch** — `Tunnel Entrance`, `Entrance Loading`,
`Entrance (DS-*)`, `Entrance (PS-*)`, `Inspection Entrance *`,
`* Ent Inspection *`, `T1 Entrance *`, `Loader`, `Queue Line to Tunnel`.

**Tunnel** — `SmartStop #N`, `SS N`, `T1 SS N`, `NPU N`, `TUNNEL NPU N`,
`Mitter Curtain*`, `Wrap*`, `Presoak*`, `Rain/Wax*`, `Tire shine`,
`Flash Dryer`, `BlowerRoom Entrance`, `Tunnel Middle*`, `Mirror Wraps`.

**Exit / exit arch** — `Inspection Exit *`, `Exit inspection *`,
`* Exit Inspection *`, `EXIT D-*`, `EXIT P-*`, `Tunnel Exit`, `T1 Exit`,
`Pole Exit`, `BlowerRoom Exit`, `Tunnel N Exit`.

**Exclude** — anything matching `Lobby`, `Office`, `Break Room`, `Equipment
Room`, `Electrical`, `IT Room`, `Chemicals`, `Hydraulics`, `Refill`,
`Storage`, `Vacuum`, `Vac Lot`, `Vacuums`, `Parking`, `Lot Entrance`,
`Dumpster`, `Back door`, `Backdoor`, `Hallway`, `Stairs`, `Self Wash`,
`Self-Serve`, `Bay N`, `XPT`, `Pay Station`, `PayStation`, `POS`,
`Mission Control`, `Camera Device`, `SmartStop` *(only if it is a self-serve
bay — check with the user)*, `Stacking Lanes`, `Queue` *(unless it is
`Queue Line to Tunnel`)*.

### Reading the position suffixes

Arch cameras encode where they point. This is the key to grouping them:

| Token | Meaning |
|---|---|
| `D` / `DS` | Driver side |
| `P` / `PS` | Passenger side |
| `T` / `H` | Top / high |
| `M` | Middle |
| `B` / `L` | Bottom / low |

So `Inspection Exit D-T` is *exit arch, driver side, top*. **Every camera
sharing an arch sees the car at the same instant**, which matters in Step 4.

### Present the draft

Show the user your proposed grouping as a numbered list, with the excluded
cameras listed separately so they can rescue anything you wrongly dropped:

```
ENTRY / ENTRANCE ARCH
  [1] Inspection Entrance D-T      2011
  [2] Inspection Entrance D-B      2012
  [3] Inspection Entrance P-T      2013
TUNNEL
  [4] SmartStop #1                 2003
  ...
EXIT / EXIT ARCH
  [12] Inspection Exit D-T         2005
  ...
EXCLUDED (say if any of these belong)
  Lobby, Office, Vacuum Lot 2, Pay Stations, ...
```

Ask three things, one at a time:

1. Which of these is the **LPR camera**, if any?
2. Is the **tunnel order** correct? (Do `SmartStop #1…#7` run with traffic?)
3. Should any excluded camera be included, or any included one dropped?

---

## Step 3 — Confirm the LPR camera

Only one camera per site can be `lpr_camera_id`. Do not guess between two
candidates — ask.

Verify the choice actually returns plate data before writing it in:

```python
from spotai.timewin import day_bounds_utc, iso_z
s, e = day_bounds_utc("2026-08-30", "America/Chicago")   # a recent busy day
rep = spot.lpr_report(candidate_id, iso_z(s), iso_z(e))
print(rep["summary"])   # {'unique_plates': 103, 'total_visits': 103, ...}
```

`unique_plates: 0` means the camera exists but LPR is not producing reads.
Tell the user and set `lpr_camera_id=None` — the site will use timestamps.

---

## Step 4 — Assign roles and offsets

This is where a 23-camera site differs from a toy example.

### Roles

Each camera gets `entry`, `tunnel`, or `exit`. The offset seeding uses these:
entry → `0`, exit → `transit_seconds`, tunnel → spread evenly between.

### The arch rule

**Cameras on the same arch must share the same `offset_seconds`.** They are
physically at one point and see the car simultaneously.

The entrance and exit arches get this for free — every `entry` camera is
seeded to `0`, every `exit` camera to `transit_seconds`.

**But a mid-tunnel arch does not.** If a site has an inspection arch partway
down, marking its 5 cameras as `tunnel` spreads them across five different
offsets, which is wrong. Set their `offset_seconds` **explicitly and
identically**:

```python
Camera(id=101, name="Mid Arch D-T", role="tunnel", offset_seconds=120),
Camera(id=102, name="Mid Arch D-B", role="tunnel", offset_seconds=120),
Camera(id=103, name="Mid Arch P-T", role="tunnel", offset_seconds=120),
```

Explicit offsets are never overwritten by seeding. Ask the user whether any
mid-tunnel cameras sit together on an arch.

### Transit time

Ask: *"How long does a car take from entrance to exit, in seconds?"* Typical
is 180–300. Default 240. This is the single number that drives every tunnel
offset, so it is worth getting roughly right; it can be refined later by
timing one car.

---

## Step 5 — Choose the four device cameras

Spot allows **at most 4 cameras** on an integration device. That is a limit on
a *device*, not on a claim: `key_camera_ids` may be any length, and the
collector creates one device per four.

Ask the user which they want:

- **Four cameras, one device** (default) — one tidy named entry per claim
- **All cameras, several devices** — everything surfaces natively in Spot, at
  the cost of N entries per claim. Set
  `key_camera_ids=site.all_camera_ids()`. A 23-camera site produces 6 devices
  named `(1/6)` through `(6/6)`; the user can delete the extras once clips are
  filed elsewhere.

Either way all cameras are exported and the share link is unaffected.

The default picks the first `entry`, first `tunnel`, first `exit`, and the
last camera. For a damage claim, better is usually:

1. The LPR / entrance camera (identifies the car)
2. One entrance arch camera (condition on arrival)
3. One exit arch camera, driver-side top (condition on departure)
4. One more exit arch camera, passenger side

Offer that and let the user override:

```python
key_camera_ids=[2001, 2011, 2005, 2014]
```

---

## Step 6 — Warn about the 16-camera share cap

If the site has **more than 16 clip cameras**, tell the user plainly:

> This site has 23 cameras. All 23 clips are exported and downloadable, but
> Spot's single side-by-side share link holds 16. The library keeps both
> inspection arches in full and thins the middle-of-tunnel cameras, because
> the arches are what show the car's condition. The dropped tunnel cameras are
> still available as individual clips.

Do not present this as an error. It is a Spot limit, handled deliberately by
`select_share_cameras`.

---

## Step 7 — Build, validate, and save

```python
from spotai import SiteMap, Camera

site = SiteMap(
    location_id=1001,
    location_name="Wheaton",              # the name the user will type
    timezone="America/Chicago",
    transit_seconds=240,
    clip_seconds=120,
    lpr_camera_id=2001,
    cameras=[...],                         # ordered entry -> exit
    key_camera_ids=[...],
)
```

Constructing it validates. Expect `ValueError` for: duplicate camera ids,
empty camera list, `clip_seconds <= 0`, negative offsets, more than 4
`key_camera_ids`, or `key_camera_ids` not present in `cameras`. Fix and
re-ask rather than working around it.

Then show the user the resulting timing table and get final confirmation:

```python
for c in site.ordered_cameras():
    print(f"{c.offset_seconds:>4}s  {c.role:<7} {c.name}")
```

Save all sites to one file:

```python
import json
json.dump([s.to_dict() for s in sites], open("site_maps.json", "w"), indent=2)
```

Reload with `SiteMap.from_dict`. **`site_maps.json` should be
`.gitignore`d** — it maps a business's physical camera layout.

---

## Step 8 — Verify against reality

Before declaring the site done, run a dry check on a known car:

```python
claim = spot.collect_damage_claim(
    location="Wheaton", customer="Test", at="2026-08-30 10:09",
    claim_ref="MAPCHECK",
)
result = spot.get_claim(claim.device_id, claim.event_id)
```

Ask the user to open the share link and confirm the car appears **in tunnel
order, moving through the frames**. If it appears at the wrong moment in a
given camera, that camera's offset is wrong. If cameras are out of sequence,
the order is wrong. Fix and re-save.

Then delete the test claim so it does not pollute the case list.

---

## Checklist

- [ ] Key verified, and `camera_count` vs `cameras()` compared
- [ ] One site map per **tunnel**, not per location
- [ ] Irrelevant cameras excluded; exclusions shown to the user
- [ ] LPR camera confirmed, and proven to return plates
- [ ] Tunnel order confirmed by the user, not inferred
- [ ] Same-arch cameras share an identical explicit offset
- [ ] `transit_seconds` asked for, not assumed
- [ ] `key_camera_ids` chosen deliberately
- [ ] 16-camera share cap explained if the site exceeds it
- [ ] `SiteMap` constructs without `ValueError`
- [ ] Timing table reviewed with the user
- [ ] `site_maps.json` written and `.gitignore`d
- [ ] Test claim run, order verified, test claim deleted

---

## Anti-patterns

```python
# WRONG - order cannot be inferred; the API has no position field
cameras = [Camera(id=c["id"], name=c["name"]) for c in spot.cameras()]

# WRONG - includes lobby, vacuums, equipment rooms
cameras = [Camera(id=c["id"]) for c in spot.cameras(location_ids=[1001])]

# WRONG - same arch, three different offsets, so three wrong clip windows
Camera(id=101, name="Mid Arch D-T", role="tunnel"),
Camera(id=102, name="Mid Arch D-B", role="tunnel"),
Camera(id=103, name="Mid Arch P-T", role="tunnel"),

# WRONG - two LPR candidates, picked silently
lpr_camera_id = [c for c in cams if "LPR" in c["name"]][0]

# WRONG - assumes a plate camera exists; most sites have none
site = SiteMap(..., lpr_camera_id=some_guess)
```
