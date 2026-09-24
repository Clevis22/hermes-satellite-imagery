"""Offline regression tests for scene selection and output behavior."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
import rasterio
import requests
from rasterio.transform import from_origin


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "get_satellite_image.py"
spec = importlib.util.spec_from_file_location("get_satellite_image", SCRIPT)
imagery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imagery)


def scene(identifier, acquired, cloud=10):
    return {
        "id": identifier,
        "properties": {"datetime": acquired, "eo:cloud_cover": cloud, "grid:code": "18SUJ"},
        "assets": {"scl": {"href": identifier + "-scl"},
                   "visual": {"href": identifier + "-visual"}},
    }


def landsat_scene(identifier, acquired, cloud=10):
    return {
        "id": identifier,
        "properties": {"datetime": acquired, "eo:cloud_cover": cloud,
                       "platform": "landsat-9"},
        "assets": {band: {"href": "https://sample.blob.core.windows.net/landsat/" + band + ".tif"}
                   for band in ("red", "green", "blue", "qa_pixel")},
    }


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class SearchTests(unittest.TestCase):
    def test_follows_post_pagination_and_orders_all_results(self):
        next_link = {"rel": "next", "href": imagery.EARTH_SEARCH, "method": "POST",
                     "body": {"next": "page-two"}}
        pages = [Response({"features": [scene("older", "2026-01-01T00:00:00Z")],
                           "links": [next_link]}),
                 Response({"features": [scene("newer", "2026-01-02T00:00:00Z")],
                           "links": []})]
        with patch.object(imagery.requests, "post", side_effect=pages) as post:
            result = imagery.search_scenes([0, 0, 1, 1])
        self.assertEqual([item["id"] for item in result], ["newer", "older"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[1].kwargs["json"], next_link["body"])
        self.assertEqual(post.call_args_list[0].kwargs["json"]["sortby"], [
            {"field": "properties.datetime", "direction": "desc"}])

    def test_selection_can_reach_beyond_twentieth_item(self):
        items = [scene(str(i), "2026-01-01T00:00:00Z") for i in range(21)]

        def read(url, *args, **kwargs):
            obscured = 4 if url == "20-scl" else 9
            return np.full((1, 2, 2), obscured, dtype=np.uint8), {"valid_fraction": 1.0}

        with patch.object(imagery, "_read", side_effect=read):
            item, pct, scanned, _ = imagery.select_scene(items, [0, 0, 1, 1], (0.5, 0.5))
        self.assertEqual(item["id"], "20")
        self.assertEqual(pct, 0)
        self.assertEqual(scanned, 21)

    def test_mostly_nodata_scene_is_rejected_even_when_cloud_metadata_is_low(self):
        items = [scene("partial", "2026-01-02T00:00:00Z", cloud=0),
                 scene("usable", "2026-01-01T00:00:00Z", cloud=10)]

        def read(url, *args, **kwargs):
            fraction = 0.08 if url == "partial-scl" else 0.9
            return np.full((1, 2, 2), 4, dtype=np.uint8), {"valid_fraction": fraction}

        with patch.object(imagery, "_read", side_effect=read):
            item, pct, scanned, rejected = imagery.select_scene(
                items, [0, 0, 1, 1], (0.5, 0.5))
        self.assertEqual(item["id"], "usable")
        self.assertEqual(pct, 0)
        self.assertEqual((scanned, rejected), (1, 1))


class GeospatialTests(unittest.TestCase):
    def test_geocoder_prefers_lake_over_similarly_named_dam(self):
        results = [
            {"name": "Crater Lake 511-002 Dam", "feature_code": "DAM",
             "latitude": 35.0, "longitude": -97.0},
            {"name": "Crater Lake", "feature_code": "LK",
             "latitude": 42.9, "longitude": -122.1},
        ]
        with patch.object(imagery.requests, "get", return_value=Response({"results": results})) as get:
            best, _ = imagery.geocode("Crater Lake, Oregon")
        self.assertEqual(best["feature_code"], "LK")
        self.assertEqual(get.call_args.kwargs["params"]["name"], "Crater Lake, Oregon")

    def test_clipped_raster_reports_actual_extent_and_rejects_off_target_point(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tile.tif"
            with rasterio.open(path, "w", driver="GTiff", width=10, height=10,
                               count=3, dtype="uint8", crs="EPSG:4326",
                               transform=from_origin(-77, 40, 0.01, 0.01)) as dataset:
                dataset.write(np.full((3, 10, 10), 100, dtype=np.uint8))
            bbox = [-77.05, 39.95, -76.95, 40.05]
            inside = imagery._read(str(path), bbox, point=(-76.975, 39.975))
            outside = imagery._read(str(path), bbox, point=(-77.02, 40.02))
        self.assertIsNotNone(inside)
        self.assertIsNone(outside)
        self.assertLess(inside[1]["km"][0], 6)
        self.assertLess(inside[1]["km"][1], 6)

    def test_landsat_quality_uses_qa_bits_and_excludes_fill(self):
        qa = np.array([[0, 1], [1 << 3, 1 << 5]], dtype=np.uint16)
        obscured, valid = imagery.landsat_quality(qa)
        self.assertAlmostEqual(obscured, 200 / 3)
        self.assertEqual(valid, 0.75)


class DateAndInputTests(unittest.TestCase):
    def test_date_only_end_covers_whole_day(self):
        parsed = imagery._parse_date_bound("2026-06-30", end=True)
        self.assertEqual(parsed.isoformat(), "2026-06-30T23:59:59.999999+00:00")

    def test_offset_timestamp_converts_to_utc(self):
        parsed = imagery._parse_date_bound("2026-06-30T12:00:00-04:00")
        self.assertEqual(parsed.isoformat(), "2026-06-30T16:00:00+00:00")

    def test_invalid_coordinates_and_reversed_dates_fail_before_search(self):
        for kwargs in ({"lat": 95, "lon": 0}, {"lat": 0},
                       {"lat": 0, "lon": 0, "radius_km": -1},
                       {"lat": 0, "lon": 0, "date_end": "2026-06-30"},
                       {"lat": 0, "lon": 0, "date_start": "2026-07-01",
                        "date_end": "2026-06-30"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                imagery.get_satellite_image(**kwargs)


class OutputTests(unittest.TestCase):
    def test_explicit_format_does_not_depend_on_filename_extension(self):
        pixels = np.full((3, 4, 4), 120, dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "misleading.jpg"
            imagery.save_rgb(pixels, str(path), fmt="png")
            with Image.open(path) as image:
                self.assertEqual(image.format, "PNG")

    def test_blank_retry_uses_successful_scene_date_and_preserves_format(self):
        items = [scene("blank", "2026-06-30T10:00:00Z"),
                 scene("good", "2026-06-29T10:00:00Z")]
        scl = np.full((1, 4, 4), 4, dtype=np.uint8)
        blank = np.zeros((3, 4, 4), dtype=np.uint8)
        good = np.indices((4, 4)).sum(axis=0).astype(np.uint8) * 60
        rgb = np.stack((good, good, good))
        meta = {"valid_fraction": 1.0, "km": (10.0, 10.0)}

        def read(url, *args, **kwargs):
            if url.endswith("-scl"):
                return scl, meta
            return (blank if url == "blank-visual" else rgb), meta

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(imagery, "CACHE_DIR", directory), \
                 patch.object(imagery, "search_scenes", return_value=items), \
                 patch.object(imagery, "search_landsat_scenes", return_value=[]), \
                 patch.object(imagery, "_read", side_effect=read):
                result = imagery.get_satellite_image(lat=40, lon=-75, output_format="png", quiet=True)
            self.assertIn("2026-06-29", result["path"])
            self.assertEqual(result["scene_id"], "good")
            self.assertEqual(os.listdir(directory), [os.path.basename(result["path"])])
            with Image.open(result["path"]) as image:
                self.assertEqual(image.format, "PNG")

    def test_failed_candidates_do_not_replace_existing_output(self):
        item = scene("blank", "2026-06-30T10:00:00Z")
        scl = np.full((1, 4, 4), 4, dtype=np.uint8)
        blank = np.zeros((3, 4, 4), dtype=np.uint8)
        meta = {"valid_fraction": 1.0, "km": (10.0, 10.0)}

        def read(url, *args, **kwargs):
            return (scl if url.endswith("-scl") else blank), meta

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "keep.jpg"
            destination.write_bytes(b"original")
            with patch.object(imagery, "search_scenes", return_value=[item]), \
                 patch.object(imagery, "search_landsat_scenes", return_value=[]), \
                 patch.object(imagery, "_read", side_effect=read):
                with self.assertRaises(RuntimeError):
                    imagery.get_satellite_image(lat=40, lon=-75, out_path=str(destination), quiet=True)
            self.assertEqual(destination.read_bytes(), b"original")
            self.assertEqual(os.listdir(directory), ["keep.jpg"])


class FallbackTests(unittest.TestCase):
    def setUp(self):
        pattern = np.indices((4, 4)).sum(axis=0).astype(np.uint8) * 60
        self.rgb = np.stack((pattern, pattern, pattern))
        self.meta = {"valid_fraction": 1.0, "km": (10.0, 10.0)}

    def test_newer_clear_landsat_wins_and_identifies_its_source(self):
        sentinel = scene("s2", "2026-06-29T10:00:00Z")
        landsat = landsat_scene("l9", "2026-06-30T10:00:00Z")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(imagery, "CACHE_DIR", directory), \
                 patch.object(imagery, "search_scenes", return_value=[sentinel]), \
                 patch.object(imagery, "search_landsat_scenes", return_value=[landsat]), \
                 patch.object(imagery, "_planetary_token", return_value="token"), \
                 patch.object(imagery, "_candidate_quality", return_value=(0.0, 1.0)), \
                 patch.object(imagery, "_candidate_rgb", return_value=(self.rgb, self.meta)):
                result = imagery.get_satellite_image(
                    lat=40, lon=-75, date_start="2026-06-01", date_end="2026-06-30",
                    quiet=True)
            self.assertEqual(result["scene_id"], "l9")
            self.assertEqual(result["platform"], "landsat-9")
            self.assertEqual(result["collection"], imagery.LANDSAT_COLLECTION)
            self.assertEqual(result["resolution_m"], 30)
            self.assertIsNone(result["mgrs_tile"])
            self.assertIn("landsat89", result["path"])

    def test_expands_window_when_initial_sentinel_scenes_are_cloudy(self):
        cloudy = scene("cloudy", "2026-06-30T10:00:00Z")
        clear = scene("clear", "2026-06-01T10:00:00Z")

        def quality(item, *args):
            return (90.0 if item["id"] == "cloudy" else 0.0), 1.0

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(imagery, "CACHE_DIR", directory), \
                 patch.object(imagery, "search_scenes", side_effect=[[cloudy], [cloudy, clear]]) as search, \
                 patch.object(imagery, "search_landsat_scenes", return_value=[]), \
                 patch.object(imagery, "_candidate_quality", side_effect=quality), \
                 patch.object(imagery, "_candidate_rgb", return_value=(self.rgb, self.meta)):
                result = imagery.get_satellite_image(lat=40, lon=-75, quiet=True)
            self.assertEqual(result["scene_id"], "clear")
            self.assertEqual(result["searched_days"], 120)
            self.assertEqual(search.call_count, 2)

    def test_latest_mode_remains_sentinel_only(self):
        sentinel = scene("s2", "2026-06-29T10:00:00Z")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(imagery, "CACHE_DIR", directory), \
                 patch.object(imagery, "search_scenes", return_value=[sentinel]), \
                 patch.object(imagery, "search_landsat_scenes") as landsat_search, \
                 patch.object(imagery, "_candidate_quality", return_value=(90.0, 1.0)), \
                 patch.object(imagery, "_candidate_rgb", return_value=(self.rgb, self.meta)):
                result = imagery.get_satellite_image(lat=40, lon=-75, mode="latest", quiet=True)
            self.assertEqual(result["scene_id"], "s2")
            landsat_search.assert_not_called()

    def test_sentinel_still_works_when_landsat_catalog_is_unavailable(self):
        sentinel = scene("s2", "2026-06-29T10:00:00Z")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(imagery, "CACHE_DIR", directory), \
                 patch.object(imagery, "search_scenes", return_value=[sentinel]), \
                 patch.object(imagery, "search_landsat_scenes",
                              side_effect=requests.ConnectionError("offline")), \
                 patch.object(imagery, "_candidate_quality", return_value=(0.0, 1.0)), \
                 patch.object(imagery, "_candidate_rgb", return_value=(self.rgb, self.meta)):
                result = imagery.get_satellite_image(lat=40, lon=-75, quiet=True)
            self.assertEqual(result["scene_id"], "s2")
            self.assertIn("Landsat", result["provider_warning"])

    def test_continues_after_more_than_three_blank_renders(self):
        items = [scene(str(i), "2026-06-%02dT10:00:00Z" % (30 - i)) for i in range(5)]
        blank = np.zeros((3, 4, 4), dtype=np.uint8)

        def rgb(item, *args):
            return (self.rgb if item["id"] == "4" else blank), self.meta

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(imagery, "CACHE_DIR", directory), \
                 patch.object(imagery, "search_scenes", return_value=items), \
                 patch.object(imagery, "search_landsat_scenes", return_value=[]), \
                 patch.object(imagery, "_candidate_quality", return_value=(0.0, 1.0)), \
                 patch.object(imagery, "_candidate_rgb", side_effect=rgb):
                result = imagery.get_satellite_image(lat=40, lon=-75, quiet=True)
            self.assertEqual(result["scene_id"], "4")


if __name__ == "__main__":
    unittest.main()
