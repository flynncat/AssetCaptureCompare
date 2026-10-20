import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Content/Python"))
from acc.common import DEFAULT_PROFILE, contained, digest, validate_profile, write_json
from acc.geometry import fits, frame, projection
from acc.storage import Store
from acc.compare import Comparer, contact_sheet, similarity
from acc.calibration import repeatability


class FramingTests(unittest.TestCase):
    def test_single_frame_default_and_equivalent_browser_numbers(self):
        self.assertEqual(DEFAULT_PROFILE["warmup_frames"], 0)
        reference = copy.deepcopy(DEFAULT_PROFILE)
        browser = dict(reference, fov=35, padding=0, exposure=1, orientation=0)
        self.assertEqual(digest(validate_profile(reference)), digest(validate_profile(browser)))
        with self.assertRaises(ValueError):
            validate_profile(dict(reference, warmup_frames=-1))
        self.assertTrue(DEFAULT_PROFILE["movable"])
        legacy = dict(reference)
        legacy.pop("movable")
        self.assertTrue(validate_profile(legacy)["movable"])
        self.assertNotEqual(digest(reference), digest(validate_profile(dict(reference, movable=False))))
        with self.assertRaises(ValueError):
            validate_profile(dict(reference, movable="false"))

    def test_extreme_shapes_fit_all_views(self):
        for center, extent in (([0, 0, 0], [1, 1, 1]), ([5000, -200, 3], [.001, .001, .001]),
                               ([0, 0, 0], [10000, 1, 2]), ([800, 200, 400], [1, 20, 100000])):
            result = frame(center, extent, DEFAULT_PROFILE)
            self.assertTrue(fits(center, extent, result, DEFAULT_PROFILE))
            for view in DEFAULT_PROFILE["views"]:
                roi = projection(center, extent, result, view, 480)
                self.assertGreaterEqual(min(roi), 24)
                self.assertLessEqual(max(roi), 456)

    def test_growth_invalidates_old_frame(self):
        result = frame([0, 0, 0], [1, 1, 1], DEFAULT_PROFILE)
        self.assertFalse(fits([0, 0, 0], [100, 100, 100], result, DEFAULT_PROFILE))

    def test_tiny_asset_is_visible_without_crossing_near_plane(self):
        framing = frame([0, 0, 0], [.001, .001, .001], DEFAULT_PROFILE)
        self.assertLess(framing["near_cm"], .01)
        self.assertTrue(fits([0, 0, 0], [.001, .001, .001], framing, DEFAULT_PROFILE))
        box = projection([0, 0, 0], [.001, .001, .001], framing, DEFAULT_PROFILE["views"][0], 480)
        self.assertGreater(box[2] - box[0], 300)

    def test_invalid_bounds_and_oversized_sheet(self):
        with self.assertRaises(ValueError):
            frame([math.nan, 0, 0], [1, 1, 1], DEFAULT_PROFILE)
        with self.assertRaises(ValueError):
            validate_profile(dict(DEFAULT_PROFILE, size=600))
        with self.assertRaises(ValueError):
            contained(Path(tempfile.gettempdir()), "../Windows")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.profile = copy.deepcopy(DEFAULT_PROFILE)
        self.profile.update(size=32, columns=1, views=[DEFAULT_PROFILE["views"][0]])
        self.context = {"requested_concurrency": 1}

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, path, changed=False, compatibility="same", framing="same", success=True):
        batch = self.store.new_batch([{"path": path, "name": path, "class": "StaticMesh"}], self.profile, self.context)
        asset = batch["assets"][0]
        directory = self.store.root / ("staging_" + batch["batch_id"])
        (directory / "views").mkdir(parents=True)
        pixels = np.zeros((32, 32, 3), dtype=np.uint8)
        pixels[6:26, 6:26] = [80, 150, 200]
        if changed:
            pixels[12:20, 12:20] = [200, 100, 60]
        for view in self.profile["views"]:
            Image.fromarray(pixels).save(directory / ("views/" + view["id"] + ".png"))
        value = dict(asset, status="success" if success else "failed", compatibility=compatibility, framing_id=framing,
                     framing={"center": [0, 0, 0], "distance": 100, "fov": 35},
                     views=[{"id": v["id"], "file": "views/" + v["id"] + ".png", "roi": [4, 4, 28, 28]}
                            for v in self.profile["views"]])
        contact_sheet(directory, value, self.profile)
        self.store.capture_path(asset["capture_id"]).parent.mkdir(parents=True, exist_ok=True)
        self.store.finish_asset(batch, asset, value, directory)
        batch["status"] = "success" if success else "failed"
        self.store.save_batch(batch)
        return asset["capture_id"]

    def test_latest_is_per_asset_and_excludes_failures(self):
        first = self.capture("/Game/X.X")
        self.capture("/Game/Y.Y")
        self.capture("/Game/X.X", success=False)
        batch = self.store.new_batch([{"path": "/Game/X.X", "name": "X", "class": "StaticMesh"}], self.profile, self.context)
        self.assertEqual(first, batch["assets"][0]["baseline_id"])

    def test_incremental_batch_update_retains_history_and_rebuild_recovers(self):
        first = self.capture("/Game/X.X")
        second = self.capture("/Game/Y.Y")
        batch = self.store.batch(second.split("/")[0])
        with patch.object(self.store, "rebuild", side_effect=AssertionError("Unexpected history rescan")):
            batch["label"] = "Updated"
            self.store.save_batch(batch)
        recovered = Store(self.store.root)
        self.assertEqual(len(recovered.batches), 2)
        self.assertEqual(recovered.latest(self.store.project_id + ":/Game/X.X")["capture_id"], first)
        self.assertEqual(recovered.batch(batch["batch_id"])["label"], "Updated")

    def test_same_image_and_local_difference(self):
        first = self.capture("/Game/X.X")
        same = self.capture("/Game/X.X")
        changed = self.capture("/Game/X.X", changed=True)
        comparer = Comparer(self.store)
        self.assertAlmostEqual(comparer.compare(first, same)["score"], 1.0)
        result = comparer.compare(first, changed)
        self.assertLess(result["score"], .99)
        self.assertEqual(result["worst_view"], "front")

    def test_identical_pixels_skip_gaussian_work_and_keep_difference_images(self):
        first, second = self.capture("/Game/X.X"), self.capture("/Game/X.X")
        with patch("acc.compare.similarity", side_effect=AssertionError("Identical pixels need no Gaussian pass")):
            result = Comparer(self.store).compare(first, second)
        self.assertEqual(result["score"], 1.0)
        for mode in ("diff", "heat"):
            with Image.open(self.store.root / "comparisons" / result["comparison_id"] / ("front_" + mode + ".png")) as image:
                self.assertEqual(np.asarray(image).max(), 0)

    def test_ue_zero_alpha_export_remains_visible(self):
        directory = self.store.root / "alpha_staging"
        (directory / "views").mkdir(parents=True)
        path = directory / "views/front.png"
        Image.new("RGBA", (32, 32), (30, 120, 200, 0)).save(path)
        capture = {"views": [{"file": "views/front.png"}]}
        contact_sheet(directory, capture, self.profile)
        with Image.open(path) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.getpixel((16, 16)), (30, 120, 200))

    def test_cached_comparison_builds_missing_sheets_without_recomputing_ssim(self):
        self.profile.update(columns=2, views=DEFAULT_PROFILE["views"][:3])
        left, right = self.capture("/Game/X.X"), self.capture("/Game/X.X", changed=True)
        comparer = Comparer(self.store)
        comparison = comparer.compare(left, right)
        directory = self.store.root / "comparisons" / comparison["comparison_id"]
        self.assertFalse((directory / "sheet_diff.png").exists())
        with patch("acc.compare.similarity", side_effect=AssertionError("Cached SSIM must be reused")):
            self.assertEqual(comparer.compare(left, right, include_sheets=True), comparison)
        for mode in ("diff", "heat"):
            with Image.open(directory / ("sheet_" + mode + ".png")) as sheet:
                self.assertEqual(sheet.size, (64, 64))
                for index, view in enumerate(comparison["views"]):
                    x, y = (index % 2) * 32, (index // 2) * 32
                    with Image.open(directory / (view["id"] + "_" + mode + ".png")) as image:
                        np.testing.assert_array_equal(np.asarray(sheet.crop((x, y, x + 32, y + 32))), np.asarray(image))
                self.assertEqual(sheet.getpixel((48, 48)), (0, 0, 0))
        (directory / "sheet_heat.png").unlink()
        comparer.compare(left, right, include_sheets=True)
        self.assertTrue((directory / "sheet_heat.png").is_file())

    def test_framing_and_environment_are_not_scored(self):
        first = self.capture("/Game/X.X")
        for value in (self.capture("/Game/X.X", framing="new"), self.capture("/Game/X.X", compatibility="new")):
            result = Comparer(self.store).compare(first, value, include_sheets=True)
            self.assertEqual(result["status"], "incompatible")
            self.assertNotIn("score", result)
            self.assertFalse((self.store.root / "comparisons" / result["comparison_id"] / "sheet_diff.png").exists())

    def test_delete_invalidates_comparison_and_keeps_other_capture(self):
        first = self.capture("/Game/X.X")
        second = self.capture("/Game/X.X")
        result = Comparer(self.store).compare(first, second, include_sheets=True)
        self.store.delete_batch(first.split("/")[0])
        self.assertFalse((self.store.root / "comparisons" / result["comparison_id"]).exists())
        self.assertTrue(self.store.verify(self.store.capture(second)))
        self.assertEqual(first, self.store.capture(second)["baseline_id"])

    def test_rebuild_and_tamper_detection(self):
        capture_id = self.capture("/Game/X.X")
        (self.store.root / "index.json").unlink()
        self.assertEqual(len(self.store.rebuild()), 1)
        path = self.store.capture_path(capture_id) / "views/front.png"
        Image.new("RGB", (32, 32), "red").save(path)
        self.assertFalse(self.store.verify(self.store.capture(capture_id)))
        with self.assertRaises(ValueError):
            Comparer(self.store).compare(capture_id, capture_id)

    def test_small_roi_keeps_other_views_without_a_main_score(self):
        first, second = self.capture("/Game/X.X"), self.capture("/Game/X.X")
        for capture_id in (first, second):
            capture = self.store.capture(capture_id)
            capture["views"].append(dict(capture["views"][0], id="right", roi=[15, 4, 16, 28]))
            write_json(self.store.capture_path(capture_id) / "capture.json", capture)
        result = Comparer(self.store).compare(first, second, include_sheets=True)
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("score", result)
        self.assertEqual(result["invalid_views"], ["right"])
        self.assertEqual(result["views"][0]["score"], 1.0)
        self.assertIsNone(result["views"][1]["score"])
        self.assertTrue((self.store.root / "comparisons" / result["comparison_id"] / "right_heat.png").exists())
        self.assertTrue((self.store.root / "comparisons" / result["comparison_id"] / "sheet_heat.png").exists())

    def test_unscorable_repeatability_requires_exact_verified_images(self):
        first, same = self.capture("/Game/X.X"), self.capture("/Game/X.X")
        changed = self.capture("/Game/X.X", changed=True)
        asset = {"baseline_id": first, "capture_id": same, "comparison": {"status": "incomplete"}}
        self.assertEqual(repeatability(self.store, asset), (True, True))
        asset["capture_id"] = changed
        self.assertEqual(repeatability(self.store, asset), (False, False))

    def test_metadata_write_failure_does_not_publish_an_incomplete_capture(self):
        batch = self.store.new_batch([{"path": "/Game/X.X", "name": "X", "class": "StaticMesh"}], self.profile, self.context)
        asset = batch["assets"][0]
        staging = self.store.root / "pending_capture"
        staging.mkdir()
        capture = dict(asset, status="success", views=[])
        with patch("acc.storage.write_json", side_effect=PermissionError("Output metadata write failed")):
            with self.assertRaises(PermissionError):
                self.store.finish_asset(batch, asset, capture, staging)
        self.assertFalse(self.store.capture_path(asset["capture_id"]).exists())
        self.assertEqual(asset["status"], "queued")
        self.assertTrue(staging.exists())
        self.store.finish_asset(batch, asset, capture, staging)
        self.assertEqual(self.store.capture(asset["capture_id"])["status"], "success")

    def test_atomic_metadata_write_in_deep_windows_output_directory(self):
        base = str(self.store.root)
        folder = self.store.root / ("d" * (215 - len(base) - 1))
        target = folder / "capture.json"
        original_write = Path.write_text

        def legacy_windows_write(path, *args, **kwargs):
            if len(str(path)) >= 260:
                raise FileNotFoundError("Legacy Windows MAX_PATH")
            return original_write(path, *args, **kwargs)

        with patch.object(Path, "write_text", legacy_windows_write):
            write_json(target, {"status": "success"})
        self.assertEqual(json.loads(target.read_text()), {"status": "success"})
        self.assertEqual(list(folder.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
