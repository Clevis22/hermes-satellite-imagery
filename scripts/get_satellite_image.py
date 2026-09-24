#!/usr/bin/env python3
"""Fetch the newest clear Sentinel-2 or Landsat 8/9 image for a place or coordinate.

Free, keyless stack:
  - geocoding:  Open-Meteo Geocoding API
  - imagery:    Copernicus Sentinel-2 Level-2A via Element 84 Earth Search (STAC v1)
  - fallback:   USGS Landsat Collection 2 Level-2 via Microsoft Planetary Computer

Writes a JPEG/PNG into ~/.hermes/cache/satellite-imagery/ and prints the
acquisition metadata. Never claims imagery is live.
"""
import argparse
import json
import math
import os
import sys
import tempfile
import time
import unicodedata
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from urllib.parse import urlsplit

import numpy as np
import requests
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
PLANETARY_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PLANETARY_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2"
GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
COLLECTION = "sentinel-2-c1-l2a"
LANDSAT_COLLECTION = "landsat-c2-l2"
OBSCURED_SCL = [3, 8, 9, 10, 11]
OBSCURED_LANDSAT_BITS = sum(1 << bit for bit in (1, 2, 3, 4, 5))
CACHE_DIR = os.path.expanduser("~/.hermes/cache/satellite-imagery")
ATTRIBUTION = "Copernicus Sentinel-2 Level-2A imagery via Element 84 Earth Search"
LANDSAT_ATTRIBUTION = "USGS Landsat Collection 2 Level-2 imagery via Microsoft Planetary Computer"

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


def _search_catalog(endpoint, collection, bbox, days=60, limit=100, start=None, end=None):
    if start is None:
        start = datetime.now(timezone.utc) - timedelta(days=days)
    if end is None:
        end = datetime.now(timezone.utc)
    payload = {
        "collections": [collection],
        "bbox": bbox,
        "datetime": "%s/%s" % (start.isoformat(), end.isoformat()),
        "limit": limit,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    feats = []
    next_link = {"href": endpoint, "method": "POST", "body": payload}
    seen_pages = set()
    while next_link:
        method = next_link.get("method", "GET").upper()
        href = next_link["href"]
        body = next_link.get("body")
        page_key = (method, href, json.dumps(body, sort_keys=True))
        if page_key in seen_pages:
            raise RuntimeError("STAC search returned a repeated next page")
        seen_pages.add(page_key)
        if method == "POST":
            r = requests.post(href, json=body, timeout=45)
        elif method == "GET":
            r = requests.get(href, timeout=45)
        else:
            raise RuntimeError("Unsupported STAC pagination method: %s" % method)
        r.raise_for_status()
        page = r.json()
        feats.extend(page.get("features", []))
        next_link = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
    feats.sort(key=lambda x: x.get("properties", {}).get("datetime", ""), reverse=True)
    return feats


def search_scenes(bbox, days=60, limit=100, start=None, end=None):
    """Search Sentinel-2 scenes, newest first."""
    return _search_catalog(EARTH_SEARCH, COLLECTION, bbox, days, limit, start, end)


def search_landsat_scenes(bbox, days=60, limit=100, start=None, end=None):
    """Search only Landsat 8/9 scenes with RGB and pixel-quality assets."""
    scenes = _search_catalog(PLANETARY_SEARCH, LANDSAT_COLLECTION, bbox,
                             days, limit, start, end)
    needed = {"red", "green", "blue", "qa_pixel"}
    return [item for item in scenes
            if item.get("properties", {}).get("platform") in ("landsat-8", "landsat-9")
            and needed.issubset(item.get("assets", {}))]


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
                col, row = ~src.transform @ (px[0], py[0])
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


def landsat_quality(qa_arr):
    """Return local obscuration and valid coverage from Landsat 8/9 QA_PIXEL."""
    qa = np.asarray(qa_arr)
    if qa.ndim == 3:
        qa = qa[0]
    valid = (qa & 1) == 0  # Bit 0 marks fill, not a cloud-free pixel.
    valid_count = int(valid.sum())
    if not valid_count:
        return None, 0.0
    obscured = ((qa & OBSCURED_LANDSAT_BITS) != 0) & valid
    return float(obscured.sum()) / valid_count * 100.0, float(valid.mean())


def _planetary_token():
    response = requests.get(PLANETARY_TOKEN, timeout=25)
    response.raise_for_status()
    token = response.json().get("token")
    if not token:
        raise RuntimeError("Planetary Computer did not return an access token")
    return token


def _signed_landsat_asset(asset, token):
    href = asset["href"]
    parsed = urlsplit(href)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".blob.core.windows.net"):
        raise RuntimeError("Unexpected Landsat asset host")
    return href + ("&" if parsed.query else "?") + token


def _landsat_rgb(item, bbox, point, max_dim, token):
    arrays = []
    meta = None
    for band in ("red", "green", "blue"):
        url = _signed_landsat_asset(item["assets"][band], token)
        got = _read(url, bbox, bands=[1], max_dim=max_dim, point=point)
        if got is None:
            return None
        arr, band_meta = got
        if arrays and arr.shape != arrays[0].shape:
            return None
        arrays.append(arr)
        if meta is None:
            meta = band_meta
        else:
            meta["valid_fraction"] = min(meta["valid_fraction"], band_meta["valid_fraction"])
    return np.concatenate(arrays, axis=0), meta


def select_scene(feats, bbox, point, mode="latest_clear", max_cloud=20.0, candidates=None,
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
    for it in feats if candidates is None else feats[:candidates]:
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
    image_format = "PNG" if fmt.lower() == "png" else "JPEG"
    Image.fromarray(rgb, mode="RGB").save(output_path, format=image_format, **kwargs)
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


def _parse_date_bound(value, end=False):
    """Parse an ISO date or timestamp as a UTC search bound."""
    if len(value) == 10:
        day = date.fromisoformat(value)
        return datetime.combine(day, datetime_time.max if end else datetime_time.min, timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_inputs(lat, lon, radius_km, max_cloud, max_age_days, max_dim,
                     date_start, date_end):
    if (lat is None) != (lon is None):
        raise ValueError("Provide both --lat and --lon")
    if lat is not None and (not math.isfinite(lat) or not -90 <= lat <= 90):
        raise ValueError("Latitude must be between -90 and 90")
    if lon is not None and (not math.isfinite(lon) or not -180 <= lon <= 180):
        raise ValueError("Longitude must be between -180 and 180")
    if not math.isfinite(radius_km) or radius_km <= 0:
        raise ValueError("--radius-km must be positive")
    if not math.isfinite(max_cloud) or not 0 <= max_cloud <= 100:
        raise ValueError("--max-cloud must be between 0 and 100")
    if max_age_days <= 0:
        raise ValueError("--max-age-days must be positive")
    if max_dim <= 0:
        raise ValueError("--max-dim must be positive")
    if date_end and not date_start:
        raise ValueError("--date-end requires --date-start")
    if date_start:
        start = _parse_date_bound(date_start)
        end = _parse_date_bound(date_end, end=True) if date_end else datetime.now(timezone.utc)
        if end < start:
            raise ValueError("--date-end must not precede --date-start")
        return start, end
    return None, None


def _candidate_quality(item, source, bbox, point, token=None):
    """Return (local obscuration, valid fraction), or None for an unusable crop."""
    assets = item.get("assets", {})
    if source == "sentinel2":
        if "visual" not in assets:
            return None
        if "scl" not in assets:
            return None, 1.0  # Only --mode latest can use a scene without SCL.
        probe = _read(assets["scl"]["href"], bbox, bands=[1], max_dim=512,
                      resampling=Resampling.nearest, point=point)
        if probe is None:
            return None
        arr, meta = probe
        return local_obscured_percent(arr), meta["valid_fraction"]
    qa_url = _signed_landsat_asset(assets["qa_pixel"], token)
    probe = _read(qa_url, bbox, bands=[1], max_dim=512,
                  resampling=Resampling.nearest, point=point)
    if probe is None:
        return None
    return landsat_quality(probe[0])


def _candidate_rgb(item, source, bbox, point, max_dim, token=None):
    if source == "sentinel2":
        return _read(item["assets"]["visual"]["href"], bbox,
                     max_dim=max_dim, point=point)
    return _landsat_rgb(item, bbox, point, max_dim, token)


def _save_candidate(arr, meta, destination, output_format, min_valid=0.5):
    """Publish only a verified image; leave an existing destination untouched on failure."""
    if meta["valid_fraction"] < min_valid:
        return None
    directory = os.path.dirname(os.path.abspath(destination))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, suffix="." + output_format.lower())
    os.close(fd)
    try:
        save_rgb(arr, temporary, fmt=output_format)
        dimensions = verify_image(temporary)
        if _image_is_blank(temporary):
            return None
        os.replace(temporary, destination)
        return dimensions
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _output_destination(out_path, query, lat, lon, acquired, source, output_format):
    if out_path is not None:
        return out_path
    safe = "".join(c if (c.isalnum() or c in "-_") else "-"
                   for c in (query or "").strip().lower()).strip("-")
    stem = safe or ("%.4f_%.4f" % (lat, lon))
    sensor = "sentinel2" if source == "sentinel2" else "landsat89"
    return os.path.join(CACHE_DIR, "%s_%s_%s.%s" % (
        stem, acquired[:10], sensor, output_format.lower()))


# --------------------------------------------------------------------------- #
def get_satellite_image(query=None, lat=None, lon=None, radius_km=15, mode="latest_clear",
                        max_cloud=20.0, max_age_days=60, output_format="jpg",
                        max_dim=2048, out_path=None, country_code=None, quiet=False,
                        date_start=None, date_end=None):
    t0 = time.time()
    start, end = _validate_inputs(lat, lon, radius_km, max_cloud, max_age_days,
                                  max_dim, date_start, date_end)
    if lat is None or lon is None:
        if not query:
            raise RuntimeError("Provide a place query or lat/lon")
        best, ambiguous = geocode(query, country_code=country_code)
        lat, lon = best["latitude"], best["longitude"]
        _validate_inputs(lat, lon, radius_km, max_cloud, max_age_days, max_dim,
                         date_start, date_end)
        place = describe_place(best)
        feature = "%s (%s)" % (best.get("name"), best.get("feature_code"))
        note = None
        if ambiguous:
            note = "matched %s; also considered %s" % (place, describe_place(ambiguous))
    else:
        place, feature, note = "%.4f, %.4f" % (lat, lon), None, None

    bbox = bbox_from_center(lat, lon, radius_km)
    point = (lon, lat)
    days = max_age_days
    windows = (days,) if date_start else (days,) + tuple(d for d in (120, 365) if d > days)
    seen = set()
    cloudy_candidates = []
    selected = None
    pc_token = None
    pc_unavailable = False
    pc_warning = False
    s2_unavailable = False
    s2_warning = False
    any_scenes = False
    scanned = rejected = 0

    def render(item, source, local_pct):
        got = _candidate_rgb(item, source, bbox, point, max_dim, pc_token)
        if got is None:
            return None
        arr, meta = got
        destination = _output_destination(out_path, query, lat, lon,
                                          item["properties"]["datetime"], source, output_format)
        dimensions = _save_candidate(arr, meta, destination, output_format)
        if dimensions is None:
            return None
        return item, source, local_pct, meta, destination, dimensions

    for attempt_days in windows:
        days = attempt_days
        search_options = {"limit": 100, "start": start, "end": end} if date_start else {
            "days": attempt_days, "limit": 100}
        entries = []
        if not s2_unavailable:
            try:
                entries = [("sentinel2", item) for item in search_scenes(bbox, **search_options)]
            except requests.RequestException:
                s2_unavailable = s2_warning = True
        # latest retains its original Sentinel-2-only meaning. In latest_clear,
        # a newer clear Landsat scene can win despite its lower spatial detail.
        if mode == "latest_clear" and not pc_unavailable:
            try:
                entries.extend(("landsat", item) for item in search_landsat_scenes(
                    bbox, **search_options))
            except requests.RequestException:
                pc_unavailable = pc_warning = True
        any_scenes = any_scenes or bool(entries)
        entries.sort(key=lambda pair: (
            datetime.fromisoformat(pair[1]["properties"]["datetime"].replace("Z", "+00:00")),
            pair[0] == "sentinel2"), reverse=True)
        for source, item in entries:
            identity = (source, item["id"])
            if identity in seen:
                continue
            seen.add(identity)
            if source == "landsat" and pc_unavailable:
                continue
            if source == "landsat" and pc_token is None:
                try:
                    pc_token = _planetary_token()
                except requests.RequestException:
                    pc_unavailable = pc_warning = True
                    continue
            try:
                quality = _candidate_quality(item, source, bbox, point, pc_token)
            except (requests.RequestException, rasterio.errors.RasterioError, OSError):
                rejected += 1
                if source == "landsat":
                    pc_warning = True
                else:
                    s2_warning = True
                continue
            if quality is None or quality[1] < 0.5 or (quality[0] is None and mode != "latest"):
                rejected += 1
                continue
            local_pct = quality[0]
            scanned += 1
            if mode != "latest" and local_pct > max_cloud:
                cloudy_candidates.append((source, item, local_pct))
                continue
            try:
                selected = render(item, source, local_pct)
            except (requests.RequestException, rasterio.errors.RasterioError, OSError):
                if source == "landsat":
                    pc_warning = True
                else:
                    s2_warning = True
            if selected:
                break
            rejected += 1
        if selected:
            break

    if selected is None and cloudy_candidates:
        cloudy_candidates.sort(key=lambda entry: (
            entry[2],
            -datetime.fromisoformat(entry[1]["properties"]["datetime"].replace("Z", "+00:00")).timestamp()))
        for source, item, local_pct in cloudy_candidates:
            try:
                selected = render(item, source, local_pct)
            except (requests.RequestException, rasterio.errors.RasterioError, OSError):
                if source == "landsat":
                    pc_warning = True
                else:
                    s2_warning = True
            if selected:
                break
            rejected += 1
    if selected is None:
        if s2_unavailable and pc_unavailable:
            raise RuntimeError("Both imagery catalogs are unavailable.")
        if pc_unavailable:
            raise RuntimeError("Landsat fallback is unavailable and no usable Sentinel-2 scene was found.")
        if s2_unavailable and mode == "latest":
            raise RuntimeError("Sentinel-2 search is unavailable.")
        if not any_scenes:
            raise RuntimeError("No Sentinel-2 or Landsat 8/9 scenes found in the searched interval.")
        raise RuntimeError("No usable Sentinel-2 or Landsat 8/9 scene over this location.")

    item, source, local_pct, meta, selected_path, (w, h) = selected
    props = item["properties"]
    acquired = props["datetime"]
    attribution = ATTRIBUTION if source == "sentinel2" else LANDSAT_ATTRIBUTION

    result = {
        "path": selected_path,
        "source": attribution,
        "place": place,
        "latitude": lat,
        "longitude": lon,
        "acquired": acquired,
        "scene_id": item["id"],
        "mgrs_tile": props.get("grid:code") if source == "sentinel2" else None,
        "platform": props.get("platform"),
        "collection": COLLECTION if source == "sentinel2" else LANDSAT_COLLECTION,
        "resolution_m": 10 if source == "sentinel2" else 30,
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
    provider_warnings = []
    if s2_warning:
        provider_warnings.append("Some Sentinel-2 candidates were unavailable")
    if pc_warning:
        provider_warnings.append("Some Landsat candidates were unavailable")
    if provider_warnings:
        result["provider_warning"] = "; ".join(provider_warnings) + "."

    if not quiet:
        ex = meta["km"]
        label = "Sentinel-2" if source == "sentinel2" else "Landsat 8/9"
        quality_label = "clear" if local_pct is not None and local_pct <= max_cloud else "available"
        print("Newest %s %s scene for %s" % (quality_label, label, place))
        print("  Acquired     : %s UTC" % acquired.replace("T", " ").replace(".000Z", "Z"))
        print("  Area shown   : %.1f x %.1f km (requested radius %s km)" % (ex[0], ex[1], radius_km))
        print("  Obscuration  : %s over the target (scene-wide %.1f%%)" % (
            "unknown" if local_pct is None else "%.1f%%" % local_pct,
            float(props.get("eo:cloud_cover", -1))))
        print("  Data present : %.0f%% of the requested area" % (100 * meta["valid_fraction"]))
        if rejected:
            print("  Rejected     : %d candidate(s) unusable (off-target tile or mostly nodata)" % rejected)
        if source == "sentinel2":
            print("  Tile         : %s" % props.get("grid:code"))
        print("  Resolution   : %d m" % result["resolution_m"])
        print("  Image        : %s (%dx%d)" % (selected_path, w, h))
        print("  Source       : %s" % attribution)
        if result.get("warning"):
            print("  Note         : %s" % result["warning"])
        if result.get("provider_warning"):
            print("  Note         : %s" % result["provider_warning"])
        if note:
            print("  Geocode      : %s" % note)
    return result


def main():
    p = argparse.ArgumentParser(description="Newest clear Sentinel-2 or Landsat 8/9 image for a place.")
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
