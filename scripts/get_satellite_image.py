#!/usr/bin/env python3
"""Fetch the newest (clear) Sentinel-2 L2A satellite image for a place or coordinate.

Free, keyless stack:
  - geocoding:  Open-Meteo Geocoding API
  - imagery:    Copernicus Sentinel-2 Level-2A via Element 84 Earth Search (STAC v1)

Writes a JPEG/PNG into ~/.hermes/cache/satellite-imagery/ and prints the
acquisition metadata. Never claims imagery is live.
"""
import argparse
import json
import math
import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
COLLECTION = "sentinel-2-c1-l2a"
OBSCURED_SCL = [3, 8, 9, 10, 11]
CACHE_DIR = os.path.expanduser("~/.hermes/cache/satellite-imagery")
ATTRIBUTION = "Copernicus Sentinel-2 Level-2A imagery via Element 84 Earth Search"

# Place-like feature classes: a request for "Crater Lake" should prefer the lake
# or its national park over "Crater Lake 511-002 Dam" 200 km away.
STRONG_PREFIXES = ("PPL", "ADM")
STRONG_CODES = {
    "PRK", "PK", "RES", "MT", "MTS", "RST", "LK", "LKS", "SPNG", "STM", "STMI",
    "ISL", "ISLS", "PLN", "VLY", "FRST", "GLCR", "BAY", "COVE", "CAPE", "SPT",
    "BCH", "DES", "SWMP", "AREA", "RGN", "PRT", "HBR", "CHN", "CNL", "RGN",
}
# Head-noun intent: which feature class the user's wording implies.
INTENT_RULES = (
    (("mount", "mt.", "mt", "mountain"), {"MT", "MTS", "RST", "PK", "HLL"}),
    (("lake", "reservoir", "pond"), {"LK", "LKS", "RES", "RESV", "SPNG", "BAY", "STM"}),
    (("park", "national park"), {"PRK", "PK"}),
    (("river", "creek", "stream"), {"STM", "STMI", "STMQ", "CNL"}),
    (("island",), {"ISL", "ISLS"}),
    (("glacier",), {"GLCR"}),
    (("valley",), {"VLY"}),
    (("forest",), {"FRST"}),
    (("bay", "harbor"), {"BAY", "HBR", "COVE"}),
    (("beach",), {"BCH", "SPT"}),
)


# --------------------------------------------------------------------------- #
# geocoding
# --------------------------------------------------------------------------- #
def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return " ".join(s.casefold().split())


def _feature_is_strong(code):
    return bool(code) and (code.startswith(STRONG_PREFIXES) or code in STRONG_CODES)


def _intent_bonus(code, query):
    q = _norm(query)
    for keys, codes in INTENT_RULES:
        for k in keys:
            if k == q or q.startswith(k + " ") or q.endswith(" " + k) or (" " + k + " ") in (" " + q + " "):
                if code in codes:
                    return 0.5
    return 0.0


def score_candidate(r, query):
    """Rank a geocoding hit. Higher is better, on a roughly 0-2 scale."""
    rn = _norm(r.get("name"))
    q = _norm(query)
    if rn == q:
        s = 1.0
    elif rn.startswith(q):
        s = 0.8
    elif q and q in rn:
        s = 0.5
    else:
        s = 0.0
    code = r.get("feature_code", "")
    if _feature_is_strong(code):
        s += 0.30
    elif code.startswith(("PPL",)):
        s += 0.25
    s += _intent_bonus(code, query)
    pop = r.get("population") or 0
    s += math.log10(pop + 1) * 0.01
    return s


def geocode(query, country_code=None, count=10):
    """Resolve a place name. Accepts 'Name, Region' qualifiers (documented form)."""
    params = {"name": query, "count": count, "language": "en", "format": "json"}
    if country_code:
        params["countryCode"] = country_code.upper()
    r = requests.get(GEOCODING, params=params, timeout=25)
    r.raise_for_status()
    results = r.json().get("results", [])

    # If the documented ', qualifier' form found nothing, retry with just the head
    # noun and rank client-side (state abbreviations are inconsistently accepted).
    if not results and "," in query:
        head = query.split(",")[0].strip()
        params["name"] = head
        r = requests.get(GEOCODING, params=params, timeout=25)
        r.raise_for_status()
        results = r.json().get("results", [])
    if not results:
        raise RuntimeError("Could not resolve place %r. Try 'Name, State' or coordinates." % query)

    ranked = sorted(results, key=lambda x: score_candidate(x, query.split(",")[0]), reverse=True)
    best = ranked[0]
    ambiguous = None
    if len(ranked) > 1:
        s0, s1 = score_candidate(ranked[0], query.split(",")[0]), score_candidate(ranked[1], query.split(",")[0])
        if abs(s0 - s1) < 0.06:
            ambiguous = ranked[1]
    return best, ambiguous


def describe_place(r):
    bits = [r.get("name")]
    if r.get("admin1") and r["admin1"] != r.get("name"):
        bits.append(r["admin1"])
    if r.get("country"):
        bits.append(r["country"])
    return ", ".join(bits)


# --------------------------------------------------------------------------- #
# geometry + STAC search
# --------------------------------------------------------------------------- #
def bbox_from_center(lat, lon, radius_km=15):
    lat_delta = radius_km / 110.574
    lon_scale = max(math.cos(math.radians(lat)), 0.01)
    lon_delta = radius_km / (111.320 * lon_scale)
    return [lon - lon_delta, lat - lat_delta, lon + lon_delta, lat + lat_delta]


def search_scenes(bbox, days=60, limit=100, start=None, end=None):
    if start is None:
        start = datetime.now(timezone.utc) - timedelta(days=days)
    if end is None:
        end = datetime.now(timezone.utc)
    payload = {
        "collections": [COLLECTION],
        "bbox": bbox,
        "datetime": "%s/%s" % (start.isoformat(), end.isoformat()),
        "limit": limit,
    }
    r = requests.post(EARTH_SEARCH, json=payload, timeout=45)
    r.raise_for_status()
    feats = r.json().get("features", [])
    feats.sort(key=lambda x: x.get("properties", {}).get("datetime", ""), reverse=True)
    return feats


def _read(url, bbox_wgs84, bands=None, max_dim=2048, resampling=Resampling.bilinear, point=None):
    """Remote COG range-read of the clipped window.

    Returns (array, meta), or None when the window is empty or does not actually
    contain `point`. Range reads only: never downloads a full 10,980 px tile.
    """
    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
    ):
        with rasterio.open(url) as src:
            left, bottom, right, top = transform_bounds(
                "EPSG:4326", src.crs, *bbox_wgs84, densify_pts=21
            )
            win = from_bounds(left, bottom, right, top, transform=src.transform)
            full = Window(0, 0, src.width, src.height)
            try:
                win = win.intersection(full)
            except Exception:
                return None
            if win.width < 1 or win.height < 1:
                return None

            # The fix: a bbox straddling a UTM-boundary tile pair still returns an
            # item whose clipped window covers only the far side of the request.
            # Without this check the target silently falls outside the crop.
            if point is not None:
                px, py = warp_transform("EPSG:4326", src.crs, [point[0]], [point[1]])
                col, row = ~src.transform * (px[0], py[0])
                pad = 2
                if not (win.col_off + pad <= col < win.col_off + win.width - pad
                        and win.row_off + pad <= row < win.row_off + win.height - pad):
                    return None

            out_w = max(1, int(round(win.width)))
            out_h = max(1, int(round(win.height)))
            scale = min(1.0, max_dim / max(out_w, out_h))
            out_w = max(1, int(out_w * scale))
            out_h = max(1, int(out_h * scale))
            indexes = bands or list(range(1, min(src.count, 3) + 1))
            arr = src.read(
                indexes, window=win, out_shape=(len(indexes), out_h, out_w), resampling=resampling
            )
            # Nodata is 0 across Sentinel-2 L2A products, but partial-footprint
            # publishes (Sentinel-2C scenes over some regions) are >90% nodata
            # while reporting ~0% cloud. Track coverage so a near-empty crop is
            # never mistaken for a clear one.
            a = np.asarray(arr)
            valid_mask = (a != 0) if a.ndim == 2 or a.shape[0] == 1 else (a != 0).all(axis=0)
            valid_fraction = float(valid_mask.mean())
            wb = rasterio.windows.bounds(win, src.transform)
            wgs = transform_bounds(src.crs, "EPSG:4326", *wb)
            meta = {
                "pix": (out_w, out_h),
                "extent_wgs84": [round(v, 6) for v in wgs],
                "km": (
                    round((wgs[2] - wgs[0]) * 111.32 * math.cos(math.radians(point[1] if point else 0)), 1),
                    round((wgs[3] - wgs[1]) * 110.574, 1),
                ),
                "epsg": src.crs.to_epsg(),
                "src_px": (int(win.width), int(win.height)),
                "valid_fraction": valid_fraction,
            }
            return arr, meta


def local_obscured_percent(scl_arr):
    a = np.asarray(scl_arr)
    if a.ndim == 3:
        a = a[0]
    valid = a != 0
    valid_count = int(valid.sum())
    if valid_count == 0:
        return None
    obscured = np.isin(a, OBSCURED_SCL) & valid
    return float(obscured.sum()) / valid_count * 100.0


def select_scene(feats, bbox, point, mode="latest_clear", max_cloud=20.0, candidates=20,
                 min_valid=0.5, avoid=None):
    """Pick a scene whose target crop is genuinely usable.

    Rejects candidates that fail the target-containment check, that have no
    visual asset, or whose crop is mostly nodata (partial-footprint publishes
    report near-zero cloud while containing almost no data). Returns
    (item, local_pct, scanned, rejected).
    """
    scanned, rejected = 0, 0
    best = None
    avoid = avoid or set()
    for it in feats[:candidates]:
        if it["id"] in avoid:
            continue
        assets = it.get("assets", {})
        if "visual" not in assets:
            continue
        if "scl" in assets:
            probe = _read(assets["scl"]["href"], bbox, bands=[1], max_dim=512,
                          resampling=Resampling.nearest, point=point)
            if probe is None:
                rejected += 1
                continue
            _, pmeta = probe
            if pmeta["valid_fraction"] < min_valid:
                rejected += 1
                continue
            scanned += 1
            pct = local_obscured_percent(probe[0])
            if mode == "latest":
                return it, pct, scanned, rejected
            if pct is None:
                continue
            if best is None or pct < best[1]:
                best = (it, pct)
            if pct <= max_cloud:
                return it, pct, scanned, rejected
        else:
            if mode == "latest":
                return it, None, scanned, rejected
    return (best[0] if best else None), (best[1] if best else None), scanned, rejected


def save_rgb(arr, output_path, fmt="jpg", quality=92):
    rgb = np.moveaxis(arr[:3], 0, -1)
    if rgb.dtype != np.uint8:
        rgb = rgb.astype(np.float32)
        finite = np.isfinite(rgb)
        if finite.any():
            lo, hi = np.percentile(rgb[finite], 2), np.percentile(rgb[finite], 98)
            if hi > lo:
                rgb = (rgb - lo) / (hi - lo) * 255.0
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    elif rgb.shape[-1] != 3:
        raise RuntimeError("Expected 3-band RGB, got %d bands" % rgb.shape[-1])
    kwargs = {"quality": quality, "optimize": True} if fmt.lower() in ("jpg", "jpeg") else {"optimize": True}
    Image.fromarray(rgb, mode="RGB").save(output_path, **kwargs)
    return rgb


def verify_image(path):
    with Image.open(path) as im:
        im.verify()
    with Image.open(path) as im:
        w, h = im.width, im.height
        if w <= 0 or h <= 0:
            raise RuntimeError("Degenerate image dimensions")
        return w, h


def _image_is_blank(path):
    """True when the rendered image is effectively black (no usable imagery)."""
    with Image.open(path) as im:
        a = np.asarray(im.convert("RGB").resize((128, 128)))
    return float(a.mean()) < 12.0 and float(a.std()) < 12.0


# --------------------------------------------------------------------------- #
def get_satellite_image(query=None, lat=None, lon=None, radius_km=15, mode="latest_clear",
                        max_cloud=20.0, max_age_days=60, output_format="jpg",
                        max_dim=2048, out_path=None, country_code=None, quiet=False,
                        date_start=None, date_end=None):
    t0 = time.time()
    if lat is None or lon is None:
        if not query:
            raise RuntimeError("Provide a place query or lat/lon")
        best, ambiguous = geocode(query, country_code=country_code)
        lat, lon = best["latitude"], best["longitude"]
        place = describe_place(best)
        feature = "%s (%s)" % (best.get("name"), best.get("feature_code"))
        note = None
        if ambiguous:
            note = "matched %s; also considered %s" % (place, describe_place(ambiguous))
    else:
        place, feature, note = "%.4f, %.4f" % (lat, lon), None, None

    bbox = bbox_from_center(lat, lon, radius_km)
    days = max_age_days
    feats = []
    for attempt_days in (days, 120, 365) if not date_start else (days,):
        if date_start:
            start = datetime.fromisoformat(date_start).replace(tzinfo=timezone.utc)
            end = (datetime.fromisoformat(date_end).replace(tzinfo=timezone.utc)
                   if date_end else datetime.now(timezone.utc))
            feats = search_scenes(bbox, limit=100, start=start, end=end)
        else:
            feats = search_scenes(bbox, days=attempt_days, limit=100)
        if feats:
            days = attempt_days
            break
    if not feats:
        raise RuntimeError("No Sentinel-2 scenes found in the searched interval.")

    # Select + fetch, retrying the next-best candidate if the rendered image
    # turns out blank even though the scene metadata looked clear.
    avoid = set()
    item = local_pct = meta = rgb = None
    scanned = rejected = 0
    render_attempts = 0
    while render_attempts < 3:
        item, local_pct, scanned, rejected = select_scene(
            feats, bbox, (lon, lat), mode=mode, max_cloud=max_cloud, avoid=avoid
        )
        if item is None:
            break
        got = _read(item["assets"]["visual"]["href"], bbox, max_dim=max_dim, point=(lon, lat))
        if got is None:
            avoid.add(item["id"])
            render_attempts += 1
            continue
        arr, meta = got
        props = item["properties"]
        acquired = props["datetime"]
        if out_path is None:
            safe = "".join(c if (c.isalnum() or c in "-_") else "-"
                           for c in (query or "").strip().lower()).strip("-")
            stem = safe or ("%.4f_%.4f" % (lat, lon))
            os.makedirs(CACHE_DIR, exist_ok=True)
            out_path = os.path.join(CACHE_DIR, "%s_%s_sentinel2.%s" % (stem, acquired[:10], output_format.lower()))
        else:
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        rgb = save_rgb(arr, out_path, fmt=output_format)
        if _image_is_blank(out_path):
            # Near-black output: unusable. Try the next candidate.
            avoid.add(item["id"])
            render_attempts += 1
            continue
        break
    if item is None or meta is None:
        raise RuntimeError("No usable Sentinel-2 scene with a visual asset over this location.")
    if _image_is_blank(out_path):
        raise RuntimeError("Every candidate scene rendered as blank imagery for this location.")

    w, h = verify_image(out_path)

    result = {
        "path": out_path,
        "source": ATTRIBUTION,
        "place": place,
        "latitude": lat,
        "longitude": lon,
        "acquired": acquired,
        "scene_id": item["id"],
        "mgrs_tile": props.get("grid:code"),
        "scene_cloud_percent": round(float(props.get("eo:cloud_cover", -1)), 2),
        "local_obscured_percent": None if local_pct is None else round(local_pct, 1),
        "radius_km": radius_km,
        "mode": mode,
        "actual_extent_km": meta["km"],
        "image_px": [w, h],
        "usable_data_fraction": round(meta["valid_fraction"], 3),
        "searched_days": days,
        "candidates_scanned": scanned,
        "candidates_rejected": rejected,
        "elapsed_s": round(time.time() - t0, 1),
    }
    if feature:
        result["feature"] = feature
    if note:
        result["geocode_note"] = note
    if local_pct is not None and local_pct > max_cloud:
        result["warning"] = ("No scene met the %.0f%% local cloud threshold in the search window; "
                             "returning the clearest available scene." % max_cloud)
    if mode == "latest" and local_pct is not None and local_pct > max_cloud:
        result["warning"] = "Newest scene returned in latest mode; clouds obscure the target (%.0f%%)." % local_pct

    if not quiet:
        ex = meta["km"]
        print("Newest %s Sentinel-2 scene for %s" % (
            "clear" if mode == "latest_clear" else "available", place))
        print("  Acquired     : %s UTC" % acquired.replace("T", " ").replace(".000Z", "Z"))
        print("  Area shown   : %.1f x %.1f km (requested radius %s km)" % (ex[0], ex[1], radius_km))
        print("  Obscuration  : %s over the target (scene-wide %.1f%%)" % (
            "unknown" if local_pct is None else "%.1f%%" % local_pct,
            float(props.get("eo:cloud_cover", -1))))
        print("  Data present : %.0f%% of the requested area" % (100 * meta["valid_fraction"]))
        if rejected:
            print("  Rejected     : %d candidate(s) unusable (off-target tile or mostly nodata)" % rejected)
        print("  Tile         : %s" % props.get("grid:code"))
        print("  Image        : %s (%dx%d)" % (out_path, w, h))
        print("  Source       : %s" % ATTRIBUTION)
        if result.get("warning"):
            print("  Note         : %s" % result["warning"])
        if note:
            print("  Geocode      : %s" % note)
    return result


def main():
    p = argparse.ArgumentParser(description="Newest clear Sentinel-2 image for a place.")
    p.add_argument("query", nargs="?", help="Place name, e.g. 'Emmitsburg' or 'Mount Rainier, Washington'")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--radius-km", type=float, default=15)
    p.add_argument("--mode", choices=["latest_clear", "latest"], default="latest_clear")
    p.add_argument("--max-cloud", type=float, default=20.0)
    p.add_argument("--max-age-days", type=int, default=60)
    p.add_argument("--format", default="jpg", choices=["jpg", "jpeg", "png"])
    p.add_argument("--max-dim", type=int, default=2048)
    p.add_argument("--country-code", help="ISO country code filter, e.g. US")
    p.add_argument("--date-start", help="ISO date lower bound (explicit historical range)")
    p.add_argument("--date-end", help="ISO date upper bound")
    p.add_argument("--out", help="Output file path")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of prose")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args()
    try:
        res = get_satellite_image(
            query=a.query, lat=a.lat, lon=a.lon, radius_km=a.radius_km, mode=a.mode,
            max_cloud=a.max_cloud, max_age_days=a.max_age_days, output_format=a.format,
            max_dim=a.max_dim, out_path=a.out, country_code=a.country_code, quiet=a.json or a.quiet,
            date_start=a.date_start, date_end=a.date_end,
        )
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
