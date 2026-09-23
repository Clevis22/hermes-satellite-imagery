# hermes-satellite-imagery

Get the newest clear satellite image of any place, landmark, or coordinate.
**Free and keyless** — no account, no API key, no paid tier.

Built as a [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill,
but `scripts/get_satellite_image.py` runs standalone.

## Why

Asking "what does <place> look like from space" usually lands you on a paid
imagery API or a browser full of stale tiles. This walks straight to the
authoritative source: Copernicus **Sentinel-2 Level-2A** via the Element 84
Earth Search STAC catalogue on public S3. Raster reads are byte-ranged, so a
10,980 px scene is never fully downloaded — just the crop you asked for.

## Quick start

```bash
python3 scripts/get_satellite_image.py "Emmitsburg, Maryland"
python3 scripts/get_satellite_image.py "Mount Rainier, Washington" --mode latest
python3 scripts/get_satellite_image.py --lat 40.9543 --lon -74.7466 --radius-km 10
python3 scripts/get_satellite_image.py "Crater Lake, Oregon" --format png --max-dim 4096
python3 scripts/get_satellite_image.py "Chernobyl" --date-start 2026-06-01 --date-end 2026-06-30
```

As a Hermes skill, drop the directory into `~/.hermes/skills/utilities/` and it
loads automatically.

## What you get

A JPEG/PNG (≤2048 px by default) written to `~/.hermes/cache/satellite-imagery/`,
plus the metadata that makes it citable: acquisition timestamp, scene ID, MGRS
tile, scene cloud cover, local obscuration, real clipped extent, and
`usable_data_fraction`. Add `--json` for the structured form.

## Options

| Flag | What it does |
|---|---|
| `query` | Place name; use the documented comma form (`"Mount Rainier, Washington"`) |
| `--lat` / `--lon` | Explicit coordinates, skipping geocoding |
| `--radius-km N` | Requested half-width (default 15). The delivered extent is reported, and may be smaller |
| `--mode latest_clear` | Allow up to `--max-cloud` local obscuration (default mode) |
| `--mode latest` | Newest scene regardless of clouds — recency wins over clearness |
| `--max-cloud PCT` | Local obscuration ceiling (default 20) |
| `--max-age-days N` | Search window (default 60; auto-expands to 120, then 365) |
| `--date-start` / `--date-end` | Explicit historical window, honoured as given |
| `--country-code CC` | ISO country filter (e.g. `US`) to disambiguate repeated names |
| `--format jpg\|png` | Output format (default jpg) |
| `--max-dim N` | Longest output edge in px (default 2048) |
| `--out PATH` | Explicit output path |
| `--json` | Structured result instead of prose |
| `--quiet` | Suppress progress output |

## Requirements

Python 3 with `rasterio`, `pillow`, `numpy`, `requests`. `rasterio` ships
aarch64/py3.13 manylinux wheels, so no compiler is needed:

```bash
python3 -m venv .venv && .venv/bin/pip install rasterio pillow numpy requests
.venv/bin/python scripts/get_satellite_image.py "Emmitsburg, Maryland"
```

No credentials. The two endpoints used are public and unauthenticated:

- `geocoding-api.open-meteo.com/v1/search` — geocoding
- `earth-search.aws.element84.com/v1/search` — STAC search; asset hrefs are
  public S3 COGs

## Honest output

**Imagery is never live.** Sentinel-2 revisits every ~5 days and scenes publish
hours later, so results report "acquired on <date>", never "live view".

Coverage and validity are properties of the scene, not the code. Some scenes
carry black orbital-swath wedges; others report ~0% cloud while being ~92%
nodata. Rather than hand back a black rectangle with a flattering "0.0%
obscured", a crop must clear a valid-pixel threshold and the saved image is
re-checked for blankness before it is returned. Where every candidate over a
location is unusable, the script raises instead of returning a black JPEG.

Four failure modes are handled and regression-guarded in `SKILL.md` — see
[SKILL.md](SKILL.md#four-traps-this-script-handles--do-not-regress-them).

## Attribution

Imagery: *"Copernicus Sentinel-2 Level-2A imagery via Element 84 Earth Search."*
Sentinel-2 data is provided by the European Union's Copernicus programme.

## Licence

MIT.
