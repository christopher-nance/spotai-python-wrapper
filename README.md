# spotai-python-wrapper

Python wrapper for the [Spot AI](https://www.spot.ai) REST API, plus one thing
the API doesn't give you: turning a damage claim into a packaged,
footage-linked case.

> **Unofficial.** Not affiliated with, endorsed by, or supported by Spot AI.
> Built against the public API documented at
> [developers.spot.ai](https://developers.spot.ai).

**Using an AI coding assistant?** Hand it
[`llm/spotai-agent-guide.md`](llm/spotai-agent-guide.md) — a complete
technical manual written for Claude Code, Codex, and similar tools. Download
it, drop it in your project as `CLAUDE.md` or `AGENTS.md`, and your assistant
will know how to use this library correctly.

---

**Contents**

1. [What this actually does](#1-what-this-actually-does)
2. [Setting up](#2-setting-up)
3. [Your first script](#3-your-first-script)
4. [Site maps: the one thing you configure](#4-site-maps-the-one-thing-you-configure)
5. [Making a damage claim](#5-making-a-damage-claim)
6. [Getting the video back](#6-getting-the-video-back)
7. [Using it in a web app](#7-using-it-in-a-web-app)
8. [When things go wrong](#8-when-things-go-wrong)
9. [Full method reference](#9-full-method-reference)
10. [Things the API does that will surprise you](#10-things-the-api-does-that-will-surprise-you)
11. [FAQ](#11-faq)
12. [Design notes](#12-design-notes)

---

## 1. What this actually does

A customer says their car was scratched in your wash. To prove what happened
you need video of that specific car, from every camera it drove past, at
exactly the right moments.

By hand that's slow. You work out when the car went through, then open each
camera and scrub to the right spot — and because a car takes three or four
minutes to travel the tunnel, every camera needs a *different* time.

This library does it for you. Give it a licence plate (or just a time) and it:

1. Works out exactly when the car entered
2. Works out when each camera along the tunnel saw it
3. Asks Spot AI to cut a clip from each one
4. Creates a case in Spot AI so there's a record
5. Gives you one link showing every camera side by side

Twenty minutes of scrubbing becomes one function call.

### The two ways in

**By licence plate** — if the site has a plate-reading (LPR) camera, it finds
the car itself.

**By time** — if it doesn't, someone types roughly when the car went through.
Everything after that is identical.

That second option matters: most sites don't have LPR cameras, and the tool is
just as useful there.

---

## 2. Setting up

### Install

```bash
pip install git+https://github.com/christopher-nance/spotai-python-wrapper.git
```

Or pin a version in `requirements.txt`:

```
git+https://github.com/christopher-nance/spotai-python-wrapper.git@v0.1.0
```

### Get an API key

1. Log in to the Spot AI dashboard as an organisation admin
2. **Settings → API**
3. **Create New Key**, name it, **Generate Key**
4. **Copy it immediately** — Spot shows it exactly once
5. Click the key, then **"Add new"** above the authorisations table, and give
   it a **Role** (Owner for full access)

> **Step 5 is the one everybody misses.** A key without a Role connects fine
> but can't see anything. The next section checks for this.

### Keep the key out of your code

Create a `.env` file:

```
spotai_api_key=zpka_your_key_here
```

Add `.env` to your `.gitignore` and never commit it. Anyone with that key can
watch your cameras.

---

## 3. Your first script

Start here — this proves your key works before anything else matters.

```python
import os
from spotai import SpotAI

spot = SpotAI(api_key=os.environ["SPOTAI_API_KEY"])

print("Key accepted:", spot.verify_key())
print("Cameras in the org:", spot.camera_count())
print("Cameras I can see:", len(spot.cameras()))

for location in spot.locations():
    print(" ", location["id"], location["name"])
```

**What you want:** the two camera numbers roughly match, and your locations
are listed.

**If `camera_count()` shows a number but `cameras()` shows 0** — that's the
missing Role from step 5. Go add it. Nothing else will work until you do.

### Finding your camera IDs

```python
for camera in spot.cameras(location_ids=[1001]):
    print(camera["id"], camera["name"], camera["status"])
```

Write down the IDs **in the order a car drives past them**. That's the next
step.

---

## 4. Site maps: the one thing you configure

**This is the only fiddly part, so here's why it exists.**

Spot AI knows your cameras exist. It does *not* know what order a car passes
them — there's no "position" field anywhere in the API. So it can't know the
exit camera sees a car three minutes after the entry camera does.

You tell it once, per site. That's a `SiteMap`.

```python
from spotai import SiteMap, Camera

wheaton = SiteMap(
    location_id=1001,
    location_name="Wheaton",
    timezone="America/Chicago",
    transit_seconds=240,          # how long a wash takes, in seconds
    lpr_camera_id=2001,         # the plate-reading camera, or None
    cameras=[
        Camera(id=2001, name="LPR",             role="entry"),
        Camera(id=2002, name="Tunnel Entrance", role="tunnel"),
        Camera(id=2003, name="SmartStop 1",     role="tunnel"),
        Camera(id=2004, name="SmartStop 2",     role="tunnel"),
        Camera(id=2005, name="Exit Inspection", role="exit"),
        Camera(id=2006, name="Pole Exit",       role="exit"),
    ],
)
```

**List the cameras in the order a car drives past them.** That's the trick.

> **Doing this with an AI assistant?** Point it at
> [`llm/build-site-map-guide.md`](llm/build-site-map-guide.md). It walks
> through pulling your locations and cameras, proposing which are tunnel
> cameras and which is the LPR camera, and confirming the order with you
> before writing `site_maps.json`. Much faster than doing it by hand for
> a site with 30+ cameras.

### The three roles

| Role | Meaning |
|---|---|
| `entry` | where the car starts — usually the LPR or entrance camera |
| `tunnel` | anything in the middle |
| `exit` | the end, including exit inspection cameras |

### Timing is worked out for you

Give it `transit_seconds` and it spreads the cameras across that time:

```
transit_seconds = 240  (4 minutes)

LPR                  0 seconds after entry
Tunnel Entrance     60
SmartStop 1        120
SmartStop 2        180
Exit Inspection    240
Pole Exit          240
```

So each camera's clip covers when *that camera* saw the car:

```
T0 = 10:09:00, clip 120s, padding -30/+60

LPR                0s   10:08:30 -> 10:12:00
Tunnel Entrance   60s   10:09:30 -> 10:13:00
SmartStop 1      120s   10:10:30 -> 10:14:00
Exit Inspection  240s   10:12:30 -> 10:16:00   <- correctly excludes T0
```

These are estimates. Once you've timed a real car, set the exact number:

```python
Camera(id=2003, name="SmartStop 1", role="tunnel", offset_seconds=95),
```

Anything you set by hand is left alone; anything you don't is estimated.

### Using them

```python
spot = SpotAI(api_key=..., site_maps=[wheaton, niles, plainfield])
```

Build one per location, hand them all over at once, then refer to sites by
name.

### Keeping site maps in a file

Hard-coding seventeen sites gets ugly. Use JSON:

```python
import json
from spotai import SiteMap

with open("site_maps.json") as f:
    site_maps = [SiteMap.from_dict(d) for d in json.load(f)]
```

To create that file, build one in Python and print it:

```python
print(json.dumps(wheaton.to_dict(), indent=2))
```

---

## 5. Making a damage claim

```python
claim = spot.collect_damage_claim(
    location="Wheaton",
    customer="J. Smith",
    plate="ABC1234",
    claim_ref="CLAIM-118",
)

print(claim.id)         # WHEATON:CLAIM-118:ABC1234:2026-08-30
print(claim.device_id)  # 539     <- save this
print(claim.event_id)   # 0ed1... <- and this
print(claim.status)     # pending
```

**Save `device_id` and `event_id` in your database.** They're how you find the
claim later.

### If the site has no LPR camera

```python
claim = spot.collect_damage_claim(
    location="Niles",
    customer="M. Garcia",
    at="2026-08-30 10:09",     # when the car entered, site local time
)
```

The time is **wall-clock time at that site**. If staff say "about ten past
ten," type `10:09`. Don't convert anything.

### The arguments

| Argument | Required? | What it's for |
|---|---|---|
| `location` | yes | which site — name or ID |
| `customer` | yes | goes in the case name |
| `plate` | one of these | the licence plate to look up |
| `at` | one of these | when the car entered, instead of a plate |
| `date` | no | which day (with `plate`); defaults to today |
| `claim_ref` | no | your own claim number |
| `occurrence` | no | `"first"` or `"last"` — see below |
| `fuzzy` | no | also try similar-looking plates |
| `reuse_existing` | no | defaults to `True` |

Pass **either** `plate` **or** `at`, never both — they'd disagree about when
the car went through.

### It returns immediately

Cutting video takes minutes, so this doesn't wait. It starts everything and
hands back a receipt. You collect the video separately.

### Submitting twice is safe

If someone double-clicks your form you get the same claim back, not two. It's
recognised by plate, date, site, and claim reference.

```python
claim.reused   # True if it already existed
```

### `fuzzy` — for misread plates

Plate readers confuse `0`/`O`, `8`/`B`, `1`/`I`, `5`/`S`, `2`/`Z`. If a plate
isn't found:

```python
claim = spot.collect_damage_claim(..., plate="ABC1234", fuzzy=True)
```

Off by default, because it can match the wrong car.

### `occurrence` — subtle but real

The plate camera watches each car for about a minute as it approaches and
queues, so there are two timestamps: when it *first* saw the car and when it
*last* did.

By default it uses the first. If your clips consistently start too early — you
see the car queueing rather than entering — switch:

```python
claim = spot.collect_damage_claim(..., occurrence="last")
```

Worth testing once per site, then leaving alone.

---

## 6. Getting the video back

```python
result = spot.get_claim(claim.device_id, claim.event_id)

print(result.status)
print(result.share_link)

for clip in result.clips:
    print(clip.name, clip.url)
```

### The four statuses

| Status | What it means | What to do |
|---|---|---|
| `pending` | still cutting video | wait, check again |
| `ready` | every clip is done | show them |
| `partial` | some worked, some didn't | show what you have |
| `failed` | nothing worked | check `result.problems` |

**`partial` is normal.** Spot's export occasionally gets stuck on one camera —
we've seen one sit unfinished for 25 minutes while the others finished in
under three. Fifteen good clips beat a failed claim, so one bad camera never
sinks the case. `result.problems` says which and why.

### ⚠️ Clip links expire in ONE HOUR

The single most important thing here.

**Never save a clip URL to your database.** It'll be dead within the hour and
your evidence page will show broken video.

Call `get_claim` when someone opens the page and use the fresh URLs straight
away:

```python
# WRONG - broken in an hour
db.save(claim_id, [c.url for c in result.clips])

# RIGHT - always fresh
def view_claim(claim_id):
    record = db.get(claim_id)
    result = spot.get_claim(record.device_id, record.event_id)
    return render(clips=result.clips)
```

Every clip carries its real deadline in `clip.url_expires`.

The **share link is different** — it lasts 7 days and is safe to email.

### After 7 days everything is gone

Spot deletes exported video after a week. If you need evidence for longer —
and insurance claims usually run longer — **download the files and store them
yourself within the first week.** This library gives you links, not files.
Saving them is your side of the job.

---

## 7. Using it in a web app

**When a claim is submitted** — call `collect_damage_claim`, save `device_id`
and `event_id` next to your claim record.

**When someone views it** — call `get_claim` and render what's ready. Never
cache the clip URLs.

```python
@app.route("/claims/<claim_id>")
def view_claim(claim_id):
    record = db.get_claim(claim_id)
    result = spot.get_claim(record.spot_device_id, record.spot_event_id)
    return render_template(
        "claim.html",
        status=result.status,
        clips=result.clips,
        share_link=result.share_link,
        problems=result.problems,
    )
```

**Create the client once**, at startup, not per request — it reuses its HTTP
connection and remembers which integration to use.

If form submission must be instant, move `collect_damage_claim` into a
background job. It's usually a few seconds, but it does make several API
calls.

---

## 8. When things go wrong

### `camera_count()` works but `cameras()` is empty

The key has no Role. Dashboard → Settings → API → your key → **"Add new"**
above the authorisations table → Role. By far the most common problem.

### `SpotAuthError: 401`

Key is wrong, expired, or deleted. Keys expire a year after creation.

### `SpotPermissionError: 403`

Key works but its Role doesn't cover that camera or site. Widen the Role.

### `SiteMapNotFound: 'X' matches more than one site`

You wrote something matching two sites — like `"Example"` when every site starts
that way. Use the full name or the location ID. It refuses to guess, because
pulling video from the wrong building is worse than an error.

### `NoLprCamera`

That site has no plate camera. Use `at="..."` instead, or set `lpr_camera_id`
on its `SiteMap`.

### `PlateNotFound`

No matching plate that day. Check the date, try `fuzzy=True`, or fall back to
`at="..."`.

### `ValueError: Pass exactly one of plate= or at=`

You passed both, or neither.

### The clips show the wrong part of the wash

Your camera order or timings are off:

- Cameras listed in the wrong order — fix the order in the `SiteMap`
- `transit_seconds` doesn't match reality — time a real car
- Clips start too early — try `occurrence="last"`

Time one car through with a stopwatch and set `offset_seconds` explicitly. A
five-minute job that fixes it permanently.

### A clip link doesn't work

It expired — they last an hour. Call `get_claim` again.

---

## 9. Full method reference

### Claims

| Method | What it does |
|---|---|
| `collect_damage_claim(...)` | Build a claim case. Returns a `Claim`. |
| `get_claim(device_id, event_id=None)` | Current status, clips, problems. |
| `site_map(location)` | Look up a configured `SiteMap`. |

### Cameras and locations

| Method | What it does |
|---|---|
| `verify_key()` | `True` if the key is accepted. |
| `camera_count()` | Org-wide camera count (ignores key scope). |
| `cameras(location_ids=None)` | Cameras you can see. |
| `camera(camera_id)` | One camera's details. |
| `locations()` | All locations you can see. |
| `zones(camera_id)` | Zones defined on a camera. |

### Plates

| Method | What it does |
|---|---|
| `lpr_report(camera_id, start, end, plates=None)` | Plate reads for a camera and time range. |
| `interest_lists()` | Plate watch-lists configured in Spot. |

### Video

| Method | What it does |
|---|---|
| `create_footage_job(camera_id, start, end)` | Start cutting one clip. |
| `get_footage_job(camera_id, footage_id)` | Check on one clip. |
| `create_shared_search(camera_ids, start, end)` | Public multi-camera link. |
| `create_vod_embed(camera_id, start, end)` | Embeddable single-camera player. |

### Spot Connect (cases)

| Method | What it does |
|---|---|
| `integrations()` | List integrations. |
| `devices(integration_id, tags=None)` | List cases/devices, filterable by tag. |
| `events(integration_id, device_ids=..., camera_ids=...)` | List events. Needs one filter. |

Times are ISO 8601 UTC strings, e.g. `"2026-08-30T15:09:00.000Z"`.

---

## 10. Things the API does that will surprise you

Each confirmed against a live production organisation, and each handled for
you.

| Behaviour | What it means |
|---|---|
| Base URL is `dev-api.spot.ai` | The only server in Spot's spec; `api.spot.ai` 404s on `/v1/*`. Not a sandbox. |
| A key with no Role reads empty | `camera_count()` returns a number while `cameras()` returns `[]`. |
| Clip URLs live **1 hour** | Signed URLs, re-signed each request. Fetch at view time. |
| Clip URLs reject auth headers | They're pre-signed; adding `Authorization` breaks them. |
| Event list needs a filter | Returns `[]` unless filtered by device or camera. |
| Event ingestion is async | Returns `202`; an event may not be queryable for a moment. |
| Attributes aren't validated | Events whose attributes don't match the schema are still accepted. |
| Device name ≤ 40 chars | Enforced with a `400`. Names are truncated, keeping the date. |
| Cameras per device ≤ 4 | Enforced with a `400`. |
| Shared link ≤ 16 cameras, ≤ 7 days | Hard ceilings. A 23-camera site keeps both inspection arches in the link and thins mid-tunnel cameras; every clip is still exported. |
| Exports sometimes wedge | One observed at 0% for 25+ minutes while a sibling finished in 2m36s. |

---

## 11. FAQ

**Do I need to understand the Spot AI API?** No. That's what this is for.

**Can I use it without LPR cameras?** Yes — use `at="2026-08-30 10:09"`. Most
sites work this way.

**How many cameras can one claim have?** As many as you like for the clips.
The share link shows up to 16. The case record inside Spot shows 4 — Spot's
limit, not ours.

**Why only 4 on the case?** Spot allows at most four cameras per integration
device. The library picks the most useful four; the rest are still in the
clips and the share link. Override with `key_camera_ids`.

**Does this store video?** No, it gives you links. Spot deletes video after 7
days, so download anything you need to keep.

**Is it safe to call twice?** Yes — the same details return the same claim.

**How long does a claim take?** The call returns in seconds; video is usually
ready in two to five minutes.

**What Python version?** 3.9 or newer.

**Is this made by Spot AI?** No. Independent wrapper around their public API.

---

## 12. Design notes

**One device per claim.** Spot's model calls a device a *physical
device/object*, so a claim is arguably an Event, not a Device. Devices are
used here because they're the named, listable things under an integration. The
costs: devices grow without bound, and the idempotency lookup scans devices at
a site. If claim volume gets large, moving to one device per tunnel with an
event per claim is the change to make — it's contained in `damage_claims.py`.

**Layout.** `transport.py` is HTTP only. `client.py` is the public surface and
stays thin. `damage_claims.py` holds the workflow. Adding an endpoint is a
three-line method; adding a workflow is a new module.

**Step order in `collect_damage_claim` is deliberate.** The Spot device is
created *before* exports are submitted, so a mid-flight failure leaves a
recoverable record rather than orphaned export jobs nothing points at.

---

## Documentation

| File | Audience |
|---|---|
| This README | People — the complete guide |
| [`llm/spotai-agent-guide.md`](llm/spotai-agent-guide.md) | AI coding assistants — using the library |
| [`llm/build-site-map-guide.md`](llm/build-site-map-guide.md) | AI coding assistants — building your camera directory |
| [`docs/GUIDE.md`](docs/GUIDE.md) | Same guide, standalone copy |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Changing the library |

Both guides are kept in step by `tests/test_docs_current.py`, which fails the
build if a public method goes undocumented.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

No API access or credentials needed. They cover the parts where bugs are
silent: time and timezone maths, offset seeding, name truncation, identity
construction, and status derivation.

## Licence

MIT
