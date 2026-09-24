---
name: satellite-imagery
description: "Use when asked for satellite imagery of a place, landmark, or coordinates. Newest clear Sentinel-2 or Landsat 8/9 scene, free and keyless."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [satellite, imagery, sentinel-2, landsat, stac, earth-search, geospatial, cog, remote-sensing]
    related_skills: [maps, grounded-citations]
---

# Satellite imagery (Sentinel-2 and Landsat 8/9)

Trigger on "satellite image of X", "newest/latest clear picture of X", "show me
<place> from space", or an explicit lat/lon. Free and keyless: Open-Meteo
geocoding + Copernicus Sentinel-2 Level-2A via Element 84 Earth Search, with
Landsat 8/9 through Microsoft Planetary Computer when it offers a newer clear
image. No account, no API key, no paid tier. Planetary Computer supplies an
anonymous short-lived asset token automatically.

**Never imply this is live.** Say "acquired on <date>", not "live view".

## Run it

```bash
~/.hermes/skills/utilities/satellite-imagery/.venv/bin/python \
  ~/.hermes/skills/utilities/satellite-imagery/scripts/get_satellite_image.py "Emmitsburg, Maryland"
```

Add `--json` for a structured result, `--mode latest` to allow clouds,
`--radius-km`, `--max-cloud`, `--format png`, `--date-start/--date-end` for an
explicit historical window, `--out` for a specific path. Then send the file
from `path`.

## Report format — keep it to the facts

The reply is a caption, not a briefing. Kieran asked for this explicitly: give
the acquisition date, the image stats, and how it was captured. Nothing else.

Template:

```
<Place> — <lat>, <lon>
Acquired: <date, time UTC> (<platform>, <resolution_m> m; tile <mgrs> if present) — <n> days old
Cloud: <local_obscured_percent>% local, <usable_data_fraction> valid data
Extent: <actual_extent_km[0]> x <actual_extent_km[1]> km
```

Do NOT narrate process or traps. Forbidden in a reply:
- "known geocoder trap", "the same-named place in <other state> outranks this
  one", any mention of the name-collision/disambiguation problem
- trap numbers, `candidates_scanned`, rejection counts
- "verified it isn't a black rectangle", blank-check or vision-zoom narration
- recapping which flags were passed (`--country-code`, `--mode`, ...)

If a trap silently changed the answer, state only the outcome: `pinned to the
NJ feature (40.9543, -74.7466)`. One clause, no mechanism explained.

Keep the `source` attribution line for the actual selected provider — required,
not narration. Do not label a Landsat scene as Sentinel-2.

Dependencies live in the skill's own venv. Check before reinstalling:

```bash
SK=~/.hermes/skills/utilities/satellite-imagery
[ -x $SK/.venv/bin/python ] || python3 -m venv $SK/.venv
$SK/.venv/bin/python -c "import rasterio, PIL, numpy, requests" 2>/dev/null || \
  $SK/.venv/bin/pip install -r $SK/requirements.txt
```

`rasterio` ships aarch64/py3.13 manylinux wheels: ~2 min install, 227 MB venv,
no compiler. Outputs land in `~/.hermes/cache/satellite-imagery/`.

## Defaults

| Setting | Value |
|---|---|
| Source | Newest qualifying Sentinel-2 L2A (`sentinel-2-c1-l2a`) or Landsat 8/9 (`landsat-c2-l2`) |
| Mode | `latest_clear` (≤20% local obscuration) |
| Search window | 60 days, auto-expanding to 120 then 365 |
| Radius | 15 km |
| Output | JPEG, ≤2048 px |

Literal recency wins over clearness: "most recent" → `--mode latest`, which
retains its Sentinel-2-only meaning. `latest_clear` compares qualifying scenes
from both sources by acquisition time and favors Sentinel-2 on a tie. If no
scene qualifies, widen the search to 120 and 365 days, then return the clearest
available crop with a warning. Explicit date ranges are honored as given.

## Four traps this script handles — do not regress them

1. **`results[0]` geocoding returns the wrong place.** Open-Meteo puts *Mount
   Rainier, Maryland* (pop 8,475) ahead of the Washington volcano, *Crater Lake
   Dam, Oklahoma* ahead of Crater Lake National Park, *Cranberry Lake, NY* ahead
   of NJ. Send qualifiers in the **documented comma form** (`"Mount Rainier,
   Washington"`); bare `"Mount Rainier Washington"` matches nothing. Then rank
   client-side: exact/prefix name match, place-like `feature_code` intent
   (`MT`/`LK`/`PRK`), population tiebreak. Comma-qualifiers alone do **not** fix
   feature type — `"Crater Lake, Oregon"` still returns the dam first.
2. **Tiles sharing a timestamp — catalog order is not correctness.** Several
   MGRS tiles publish at one instant, and a bbox near a UTM-zone boundary
   matches tiles that clip to ground *beside* the target (Emmitsburg: 7 of 20
   candidates were 17SQD/17TQE, whose window put the target at row −138 —
   outside the crop). After clipping, assert the projected target pixel lies
   inside the window; otherwise reject and fall through.
3. **Partial-footprint publishes look perfectly clear.** Sentinel-2C scenes
   over some regions carry **~92% nodata** while reporting `eo:cloud_cover` ≈
   0. Dividing by the tiny valid remainder yields "0.0% obscured" for a black
   rectangle (Crater Lake: 92.4% nodata, crop 92.5% all-zero). Require ≥50%
   valid pixels in the crop and re-check the saved image for blankness, retrying
   the next candidate. Report `usable_data_fraction`.
4. **The requested radius is not the delivered area.** A "15 km radius" cannot
   always be delivered from one tile: measured 30.6 × 30.6 km, but also 25.1 ×
   18.5 km and 11.0 × 14.6 km. Report the real extent from the clipped window's
   bounds, never `radius_km` as if it were the area shown.

`visual` min/max/mean brightness is a poor blank check — dense September forest
legitimately averages 36/255. Test mean **and** std together.

## Verified working paths

Coverage and validity are inherent properties of the scene, not the code, so
some scenes carry black orbital-swath wedges (Mount Rainier: 67% data present,
the rest a diagonal nodata edge). That is honest output; say so rather than
hiding it. Where every candidate over a location is unusable, the script raises
instead of returning a black JPEG.

## Attribution

Use the `source` returned by the script. Sentinel-2: "Copernicus Sentinel-2
Level-2A imagery via Element 84 Earth Search." Landsat: "USGS Landsat
Collection 2 Level-2 imagery via Microsoft Planetary Computer." Endpoint URLs
are in README.md. Asset reads are byte-ranged; the full scene is not downloaded.
