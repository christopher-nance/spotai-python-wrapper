"""End-to-end example: collect a damage claim, then poll for the clips.

Camera IDs and the site name below are placeholders. Replace them with your
own - find them with:

    for cam in spot.cameras(location_ids=[YOUR_LOCATION_ID]):
        print(cam["id"], cam["name"])
"""

import os
import time

from spotai import Camera, SiteMap, SpotAI

MAIN_WASH = SiteMap(
    location_id=1001,
    location_name="Main Wash",
    timezone="America/Chicago",
    transit_seconds=240,          # average entry-to-exit time, in seconds
    clip_seconds=120,
    lpr_camera_id=2001,           # set to None if this site has no LPR camera
    cameras=[
        Camera(id=2001, name="LPR",                role="entry"),
        Camera(id=2002, name="Tunnel Entrance",    role="tunnel"),
        Camera(id=2003, name="Inspection Entrance", role="tunnel"),
        Camera(id=2004, name="SmartStop 1",        role="tunnel"),
        Camera(id=2005, name="SmartStop 2",        role="tunnel"),
        Camera(id=2006, name="Inspection Exit",    role="exit"),
        Camera(id=2007, name="Pole Exit",          role="exit"),
    ],
)

spot = SpotAI(api_key=os.environ["SPOTAI_API_KEY"], site_maps=[MAIN_WASH])

# By plate. For a site with no LPR camera use at="2026-08-30 10:09" instead.
claim = spot.collect_damage_claim(
    location="Main Wash",
    customer="J. Smith",
    plate="ABC1234",
    date="2026-08-30",
    claim_ref="EXAMPLE-1",
)
print("claim:", claim.id)
print("device:", claim.device_id, "event:", claim.event_id)
print("share link:", claim.share_link)

# Exports take a few minutes. In a web app you would do this on page view
# rather than in a loop.
while True:
    result = spot.get_claim(claim.device_id, claim.event_id)
    print(result.status, len(result.clips), "clip(s) ready")
    if result.status != "pending":
        break
    time.sleep(15)

for clip in result.clips:
    # Use these immediately - they expire one hour after this call.
    print(f"  {clip.offset_seconds:>4}s {clip.name:<24} expires {clip.url_expires}")

for problem in result.problems:
    print("  unavailable:", problem["camera"], "-", problem["reason"])
