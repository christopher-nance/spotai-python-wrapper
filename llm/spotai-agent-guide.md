# spotai-python-wrapper — Agent Guide

Drop this file into your project as `CLAUDE.md`, `AGENTS.md`, or attach it to
a Codex/Claude Code session. It is a complete operating manual for writing
correct code against `spotai-python-wrapper` v0.3.0.

Optimise for correctness over brevity. Several API behaviours below are
counter-intuitive and will silently produce broken code if ignored.

---

**Building `site_maps.json` for the first time?** Follow
[`build-site-map-guide.md`](build-site-map-guide.md) instead — it is the
interactive procedure for pulling locations and cameras and confirming
tunnel order with the user.

## 0. Critical rules — violating these produces silent breakage

1. **Never persist a clip URL.** `Clip.url` is a pre-signed GCS URL valid for
   **3600 seconds**, re-signed on every `get_claim` call. Storing it yields
   dead links within the hour. Call `get_claim` at render time.
2. **Never send an `Authorization` header to a clip URL.** They are
   pre-signed; the extra header breaks them. Fetch them bare.
3. **`plate` and `at` are mutually exclusive.** Passing both raises
   `ValueError`. Passing neither raises `ValueError`.
4. **`at` is site-local wall-clock time**, not UTC. `"2026-08-30 10:09"`.
   The library converts using the `SiteMap.timezone`.
5. **`camera_count()` ignores key scope; `cameras()` does not.** A key with no
   Role returns a non-zero count and an empty camera list. This is not an
   error state in the API — detect it by comparing the two.
6. **`events()` requires a device or camera filter.** Spot returns `[]` for an
   unfiltered query even when events exist. This library raises `ValueError`
   rather than reproducing that trap.
7. **`partial` is a normal status, not an error.** Handle it explicitly.
8. **Spot deletes exported footage after 7 days.** The library returns links
   only. Long-term retention is the caller's responsibility.

---

## 1. Install and construct

```
pip install git+https://github.com/christopher-nance/spotai-python-wrapper.git
```

```python
from spotai import SpotAI, SiteMap, Camera

spot = SpotAI(
    api_key: str,                       # required
    site_maps: Sequence[SiteMap] = (),  # required for claim methods
    integration_name: str = "SpotAI Python Wrapper",
    event_type_name: str = "Damage Claim",
    base_url: str = "https://dev-api.spot.ai",
    timeout: int = 30,
    max_retries: int = 4,
)
```

Construct **once** per process (connection reuse + cached integration IDs).
Do not construct per request.

`base_url` note: `dev-api.spot.ai` is correct and serves **production** data.
It is the only server in Spot's OpenAPI definition. `api.spot.ai` returns 404
on `/v1/*`. Do not "fix" this.

---

## 2. Data models

### `Camera`

```python
Camera(
    id: int,
    name: str = "",                    # defaults to "camera-{id}"
    role: str = "tunnel",              # "entry" | "tunnel" | "exit"
    offset_seconds: int | None = None, # None => seeded from transit_seconds
)
```

Invalid `role` raises `ValueError`. Negative `offset_seconds` raises
`ValueError`.

### `SiteMap`

```python
SiteMap(
    location_id: int,
    location_name: str,
    cameras: list[Camera],             # ORDERED entry -> exit
    timezone: str = "America/Chicago", # IANA
    transit_seconds: int = 240,
    clip_seconds: int = 120,
    pad_before_seconds: int = 30,
    pad_after_seconds: int = 60,
    lpr_camera_id: int | None = None,
    key_camera_ids: list[int] = [],    # max 4; auto-selected if empty
)
```

Methods: `ordered_cameras()`, `camera_ids()`, `default_key_cameras()`,
`to_dict()`, `SiteMap.from_dict(d)`, and property `slug`
(`"Example Wash: Wheaton"` → `"WHEATON"`).

Raises `ValueError` on: empty camera list, duplicate camera IDs,
`clip_seconds <= 0`, negative padding, `key_camera_ids` referencing unknown
cameras, or more than 4 `key_camera_ids`.

**Offset seeding.** Any camera left with `offset_seconds=None` is filled in:
`entry` → `0`, `exit` → `transit_seconds`, `tunnel` → evenly spread via
`transit * i / (n + 1)` for the i-th of n tunnel cameras. Explicit values are
never overwritten.

### `Claim` (returned by `collect_damage_claim`)

```python
claim.id          # str  external_id, e.g. "WHEATON:CLAIM-118:ABC1234:2026-08-30"
claim.t0          # datetime, timezone-aware UTC
claim.device_id   # int      <- PERSIST (the first device)
claim.device_ids  # list[int] - every device this claim spans
claim.event_id    # str|None <- PERSIST (may be None; async ingestion)
claim.location    # str
claim.status      # str
claim.share_link  # str|None
claim.reused      # bool - True if an existing claim was returned
claim.cameras     # list[ClaimCamera]
claim.anchor      # "plate" | "estimate"
claim.matched_plate      # what the LPR actually read, or None
claim.match_confidence   # 0.0-1.0, or None
claim.candidates         # list[dict] - the shortlist, for a human to pick from
claim.needs_review       # bool
claim.to_dict()   # JSON-safe
```

### `ClaimResult` (returned by `get_claim`)

```python
result.status      # "pending" | "ready" | "partial" | "failed"
result.share_link  # str|None, 7-day expiry
result.clips       # list[Clip], sorted by offset_seconds
result.problems    # list[dict]: {"camera", "state", "reason"}
result.ready       # bool, == (status == "ready")
result.anchor           # "plate" | "estimate"
result.matched_plate    # what the LPR read, or None
result.match_confidence # 0.0-1.0, or None
result.link_only        # bool - no clips exported (estimated anchor)
```

### `Clip`

```python
clip.camera_id, clip.name, clip.role, clip.offset_seconds
clip.url          # EPHEMERAL - 1 hour
clip.url_expires  # datetime|None - the real deadline
```

---

## 3. Exceptions

All inherit `SpotError`.

| Exception | Cause | Correct handling |
|---|---|---|
| `SpotAuthError` | 401 | Key wrong/expired. Do not retry. |
| `SpotPermissionError` | 403 | Role too narrow. Do not retry. |
| `SpotNotFoundError` | 404, or no events for a device | For `get_claim`, may mean async ingestion lag — retry once after a delay. |
| `SpotAPIError` | other HTTP, network, partial-failure | Inspect message. |
| `SiteMapNotFound` | unknown/ambiguous location | Fix the identifier. |
| `PlateNotFound` | no LPR match | Retry with `fuzzy=True`, another `date`, or fall back to `at`. |
| `NoLprCamera` | site has no LPR and no `at` | Use `at=`. |
| `ClaimExists` | duplicate with `reuse_existing=False` | Fetch the existing claim. |
| `ValueError` | bad arguments | Fix the call. |

429 and 5xx are retried internally with exponential backoff. Do not add your
own retry loop around them.

---

## 4. `collect_damage_claim`

```python
claim = spot.collect_damage_claim(
    location: str | int,          # id, exact name, or UNAMBIGUOUS substring
    customer: str,                # -> device name
    plate: str | None = None,     # XOR with `at`
    at: str | None = None,        # XOR with `plate`; site-local wall clock
    date: str | None = None,      # "YYYY-MM-DD"; with `plate`; default today-at-site
    claim_ref: str | None = None,
    occurrence: str = "first",    # "first" | "last"
    reuse_existing: bool = True,
    clips: str = "auto",          # "auto" | "always" | "never"
    min_confidence: float = 0.78,
    estimate_window_minutes: int = 20,
) -> Claim
```

Returns in seconds. Exports are submitted, **not** awaited.

**Supply `plate`, `at`, or BOTH.** Both is the normal production case: the
plate is the preferred anchor and the timestamp is the fallback when no
confident LPR match exists. (Earlier versions rejected both; they no longer
do.)

### Internal step order (do not reorder if you modify it)

1. Resolve `SiteMap`
2. Resolve `T0` (LPR lookup or `at` parse)
3. Build `external_id`
4. `ensure_integration()` / `ensure_event_type()` (idempotent, cached)
5. Look for an existing device by `external_id` → return it if found
6. **Create the device** — identity before work
7. Submit one footage export per camera (failures recorded, not raised)
8. Create the shared link (failure yields `None`, never raises)
9. Create the event carrying `footage_jobs[]`

Step 6 precedes step 7 deliberately: a failure mid-flight leaves a
recoverable record rather than orphaned export jobs nothing references.

### Idempotency

Identity is `external_id`:
`{SITE_SLUG}:{CLAIM_REF|NOREF}:{PLATE|NOPLATE}:{YYYY-MM-DD}`

Re-submitting identical arguments returns the existing claim with
`reused=True`. Set `reuse_existing=False` to raise `ClaimExists` instead.

Lookup is O(devices with that site tag) — Spot cannot filter devices by
`external_id`.

### Anchors: precise vs estimated

T0 comes from one of two places and they are **not** equally trustworthy.

| Anchor | Source | Precision | Default behaviour |
|---|---|---|---|
| `plate` | LPR match | exact | narrow clips (90-120s per camera) |
| `estimate` | typed time | median 7 min error, up to 15 | **wide scrubbable link**, no clips |

Measured against LPR ground truth, typed times were off by a median of 7
minutes. A 90-second clip centred on an estimate often misses the car, and a
clip of the **wrong car is worse than no clip** because it still looks like
evidence. So `clips="auto"` exports only for a precise anchor.

`Claim.anchor` and `ClaimResult.anchor` record which was used.
`Claim.needs_review` is True when a human should confirm the vehicle.
`ClaimResult.link_only` is True when no clips were exported.

### `match_plate` - ranking, not guessing

```python
candidates = spot.match_plate(location, plate, date=None, limit=5) -> list[PlateCandidate]
```

```python
c.plate, c.score, c.band, c.visits, c.first_seen, c.last_seen
c.auto_acceptable   # score >= 0.78
c.to_dict()
```

**Why ranking beats exact matching.** On 521 live reads, **46% were shorter
than a full plate**, and the shape ladder (`LLDDDDD` -> `LDDDDD` -> `DDDDD`)
shows characters are lost from the **left**. So `plates=["AB12345"]` finds
nothing whenever that car was read as `12345`. `match_plate` scores every read
for the day instead.

| Band | Score | Meaning |
|---|---|---|
| `near-certain` | >= 0.92 | auto-attach |
| `likely` | >= 0.78 | auto-attach, flag for verification |
| `possible` | >= 0.62 | show the shortlist to a person |
| `weak` | < 0.62 | not returned |

Helpers: `similarity(a, b) -> float`, `confidence_band(score) -> str`,
`is_usable(plate) -> bool`, `is_ambiguous(candidates) -> bool`.

**`is_usable` is a guard you must respect.** Real exports contain `N/A`,
`TEST`, `1111`, `CEO`, and pasted notes 46 characters long. `match_plate`
returns `[]` for those rather than fabricating a match; route them to the
timestamp path.

Returning `[]` is also correct when the car was simply never read - the LPR
misses vehicles. Do not treat an empty list as an error.

### Claims spanning more than 4 cameras

`key_camera_ids` is **not** capped at 4. Set it to any length and the collector
chunks it into devices of 4:

```python
site.key_camera_ids = site.all_camera_ids()   # 23 cameras -> 6 devices
```

- Devices are named `{customer} | {date} (n/N)`, truncated to fit 40 chars
- Part 1 keeps the base `external_id`; later parts get `#P2`, `#P3`, ... so
  the idempotency lookup still finds the claim by its base id
- **An event is created on every device.** Spot surfaces footage from the
  cameras of the device an event belongs to, so without repeating the event
  only the first four cameras would ever show
- `chunk_cameras(ids, size=4)` and `part_external_id(base, n)` are exposed for
  callers that need the same grouping

Re-submitting an identical claim reuses all its devices; it does not create a
second set.

### `occurrence`

The LPR holds a car ~37–127s (median ~54s) between `first_seen` and
`last_seen` — approach plus queue time. `"first"` anchors on approach;
`"last"` is nearer actual tunnel entry. If clips start too early, use
`"last"`.

---

## 5. `get_claim`

```python
result = spot.get_claim(device_id: int, event_id: str | None = None) -> ClaimResult
```

Omitting `event_id` uses the newest event on the device. Makes 1 + N API calls
(one event list, one poll per camera) — do not poll faster than every ~10s.

### Status derivation

```
any QUEUED/PROCESSING            -> "pending"   (pending wins over failure)
all SUCCEEDED                    -> "ready"
some SUCCEEDED, some FAILED      -> "partial"
none SUCCEEDED                   -> "failed"
no jobs at all                   -> "failed"
```

---

## 6. Passthrough endpoints

```python
spot.verify_key() -> bool
spot.camera_count() -> int
spot.locations() -> list[dict]
spot.cameras(location_ids: list[int] | None = None) -> list[dict]
spot.camera(camera_id) -> dict
spot.zones(camera_id)
spot.lpr_report(camera_id, start_iso, end_iso, plates=None) -> dict
spot.interest_lists() -> list[dict]
spot.create_footage_job(camera_id, start_iso, end_iso) -> dict
spot.get_footage_job(camera_id, footage_id) -> dict
spot.create_shared_search(camera_ids, start_iso, end_iso, expiry_seconds=604800) -> dict
spot.create_vod_embed(camera_id, start_iso, end_iso, expires_in=604800) -> dict
spot.integrations() -> list[dict]
spot.devices(integration_id, tags=None) -> list[dict]
spot.create_device(integration_id, name, camera_ids, tags=None, external_id=None) -> dict
spot.events(integration_id, device_ids=None, camera_ids=None) -> list[dict]
spot.create_event(integration_id, event_type_id, device_id, timestamp, attributes, duration_ms=240000, buffer_ms=30000) -> None
spot.ensure_integration() -> int
spot.ensure_event_type() -> int
spot.site_map(location) -> SiteMap
```

Camera dict shape: `{id, name, status, location_id, location_name, local_ip,
last_online, mac_address, appliance_id, has_speakers}`. **There is no position
or ordering field** — that is exactly why `SiteMap` exists.

`lpr_report` returns `{timeseries, summary, plates}` where each plate is
`{plate, visits, first_seen, last_seen}`. It is an aggregate report, **not**
an event stream — a plate seen twice yields only first and last.

---

## 7. Hard API limits

| Limit | Value | Enforcement |
|---|---|---|
| Device name | 40 chars | `400`; library truncates, keeps the date |
| Cameras per device | 4 | `400`; a claim wanting more is split across devices |
| Shared link cameras | 16 | `select_share_cameras` keeps both arches, thins tunnel |
| Shared link expiry | 604800s (7d) | library clamps |
| Event duration | 600000ms | library clamps |
| Event buffer | 120000ms | — |
| `external_id` | 255 chars, unique, immutable | library truncates |
| Footage retention | 7 days after export | — |
| Clip URL | 3600s | re-fetch |

---

## 8. Correct patterns

### Web app: submit then view

```python
# once, at startup
spot = SpotAI(api_key=os.environ["SPOTAI_API_KEY"], site_maps=SITE_MAPS)

# on claim submission
claim = spot.collect_damage_claim(
    location=form.site, customer=form.customer,
    plate=form.plate or None, at=None if form.plate else form.time,
    claim_ref=form.claim_number,
)
db.save(claim_id=form.id, device_id=claim.device_id, event_id=claim.event_id)

# on view - ALWAYS re-fetch
def view(claim_id):
    row = db.get(claim_id)
    result = spot.get_claim(row.device_id, row.event_id)
    return render(status=result.status, clips=result.clips,
                  share_link=result.share_link, problems=result.problems)
```

### Handle every status

```python
if result.status == "ready":
    show(result.clips)
elif result.status == "partial":
    show(result.clips)
    warn(f"{len(result.problems)} camera(s) unavailable")
elif result.status == "pending":
    show_spinner()
else:
    show_error(result.problems)
```

### Plate first, timestamp as fallback

```python
from spotai.errors import PlateNotFound, NoLprCamera

try:
    claim = spot.collect_damage_claim(
        location=site, customer=name, plate=plate, date=day, fuzzy=True,
    )
except (PlateNotFound, NoLprCamera):
    claim = spot.collect_damage_claim(
        location=site, customer=name, at=f"{day} {approx_time}",
    )
```

### Site maps from JSON

```python
site_maps = [SiteMap.from_dict(d) for d in json.load(open("site_maps.json"))]
```

---

## 9. Anti-patterns

```python
# WRONG - dead links within the hour
db.save(urls=[c.url for c in result.clips])

# WRONG - breaks the pre-signed URL
requests.get(clip.url, headers={"Authorization": f"Bearer {key}"})

# WRONG - raises ValueError
spot.collect_damage_claim(location="X", customer="Y", plate="A", at="...")

# WRONG - UTC where site-local is expected
spot.collect_damage_claim(location="X", customer="Y", at="2026-08-30T15:09:00Z")

# WRONG - ambiguous, raises SiteMapNotFound if several sites share the prefix
spot.collect_damage_claim(location="Example", ...)

# WRONG - treats a normal outcome as failure
if result.status != "ready":
    raise Exception("claim failed")

# WRONG - per-request construction, no connection reuse or ID caching
def view(id):
    spot = SpotAI(api_key=KEY, site_maps=MAPS)

# WRONG - redundant; 429/5xx already retried internally
for _ in range(5):
    try: return spot.cameras()
    except SpotAPIError: time.sleep(1)
```

---

## 10. Diagnostics

```python
print(spot.verify_key())        # False/raises -> bad key
count = spot.camera_count()     # ignores scope
visible = len(spot.cameras())   # respects scope
if count > 0 and visible == 0:
    # Key authenticates but has NO ROLE attached.
    # Fix: Spot dashboard -> Settings -> API -> key -> "Add new" -> Role.
    ...
```

---

## 11. Module layout

| Module | Responsibility |
|---|---|
| `transport.py` | HTTP: auth, retries, error mapping, cursor pagination |
| `matching.py` | Plate matching: normalisation, scoring, confidence bands |
| `client.py` | `SpotAI` — public surface; thin, delegates |
| `damage_claims.py` | The claim workflow |
| `claims.py` | Models + pure helpers (naming, identity, status, windows) |
| `sitemap.py` | `SiteMap` / `Camera`, offset seeding, site resolution |
| `lpr.py` | Plate → T0, fuzzy variants |
| `timewin.py` | Time and timezone maths |
| `errors.py` | Exception hierarchy |

Adding an endpoint: a three-line method on `SpotAI` delegating to
`self.http.request`. Adding a workflow: a new module beside
`damage_claims.py`. Keep pure logic in `claims.py`/`timewin.py` — it is the
part that is unit-tested without network access.

**If you change the public API, update `docs/GUIDE.md` and this file.**
`tests/test_docs_current.py` fails when a public method is undocumented.

---

## 12. Known behaviours that look like bugs

- **Exports wedge.** One observed at `PROCESSING`/`progress: 0` for 25+
  minutes while a sibling on the same appliance finished in 2m36s. Not a
  library fault. Surfaces as `partial`.
- **`event_id` may be `None` right after creation.** Ingestion returns `202`
  and is asynchronous. `get_claim(device_id)` without `event_id` works.
- **Event attributes are not validated** against the event type schema. Bad
  attributes are accepted silently.
- **Several cameras share `offset_seconds`.** Expected — an inspection arch
  is many cameras at one physical point, all seeing the car at once. Give
  a *mid-tunnel* arch an explicit identical offset; entry and exit arches
  get it automatically from their role.
- **A 23-camera site's share link holds 16.** `select_share_cameras` keeps
  the exit arch first (it shows the damage), then the entry arch, then an
  even spread of tunnel cameras. All clips are still exported.
- **A 400 on device creation** usually means a name over 40 chars or more than
  4 cameras.
