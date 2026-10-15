"""Regressionstests fuer das Live-Addon ohne angeschlossene Kamera."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

# Im Repo das reale Berechnungsmodul importieren. Bei einem isolierten Test
# ohne Projektdatei genuegt ein Stub fuer die reine UI- und Guard-Pruefung.
try:
    import aruco_verarbeitung  # noqa: F401
except ImportError:
    fake = types.ModuleType("aruco_verarbeitung")
    fake.ProcessingError = type("ProcessingError", (ValueError,), {})
    sys.modules["aruco_verarbeitung"] = fake
    import aruco_live as live
    del sys.modules["aruco_verarbeitung"]
else:
    import aruco_live as live


class LiveOverlayTests(unittest.TestCase):
    def setUp(self):
        corner = np.array([[120, 170], [190, 170], [190, 240], [120, 240]], np.float64)
        self.records = [
            {"id": 1, "dictionary": "DICT_5X5_50", "role": "reference", "status": "ok",
             "corners_px": corner},
            {"id": 2, "dictionary": "DICT_5X5_50", "role": "reference", "status": "ok",
             "corners_px": corner + 220},
            {"id": 3, "dictionary": "DICT_5X5_50", "role": "reference", "status": "ok",
             "corners_px": corner + 330},
            {"id": 20, "dictionary": "DICT_5X5_50", "role": "measurement", "status": "ok",
             "height_mm": 12.34, "corners_px": corner + [620, 270]},
            {"id": 21, "dictionary": "DICT_5X5_50", "role": "measurement", "status": "error",
             "error": "RMS exceeded", "corners_px": corner + [850, 270]},
        ]

    def report(self, rms=0.6, status="partial"):
        return {"status": status, "markers": self.records,
                "reference_control": {"control_rms_px": rms}, "warnings": []}

    def quality(self, report):
        return live.quality_summary(report, 4, 2, 0, 3, 1.0, 1.5)

    def test_visibility_and_valid_heights_distinct(self):
        q = self.quality(self.report())
        self.assertEqual((q["reference_visible"], q["reference_total"]), (3, 4))
        self.assertEqual((q["measurement_visible"], q["measurement_valid"]), (2, 1))
        self.assertEqual(q["reference_quality"], "OK")

    def test_reference_quality_thresholds(self):
        self.assertEqual(self.quality(self.report(1.1))["reference_quality"], "WARNUNG")
        self.assertEqual(self.quality(self.report(1.7))["reference_quality"], "UNGUELTIG")

    def test_missing_reference_not_pruefbar(self):
        report = self.report()
        report["markers"] = self.records[2:]
        self.assertEqual(self.quality(report)["reference_quality"], "NICHT PRUEFBAR")

    def test_no_reference(self):
        self.assertEqual(self.quality(self.report(None, "no_reference"))[
            "reference_quality"], "NICHT INITIALISIERT")

    def test_overlay_only_scales_preview(self):
        frame = np.full((1080, 1920, 3), 115, np.uint8)
        source = frame.copy()
        report = self.report()
        displayed = live.draw_overlay(frame, report, self.quality(report), 0.3, 1280)
        self.assertEqual(displayed.shape, (720, 1280, 3))
        np.testing.assert_array_equal(frame, source)

    def test_overlay_old_measurement(self):
        frame = np.full((720, 1280, 3), 90, np.uint8)
        report = self.report()
        shown = live.draw_overlay(frame, report, self.quality(report), 8.0, 1280, 2.0)
        self.assertEqual(shown.shape, frame.shape)
        np.testing.assert_array_equal(frame[680:, :], shown[680:, :])

    def test_relative_paths_and_camera_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.json"
            path.write_text(json.dumps({"KAMERA": {"INDEX": 2},
                                        "BERECHNUNGEN_PRO_SEKUNDE": 3}), encoding="utf-8")
            config = live.load_live_config(path)
            self.assertEqual(config["KAMERA"]["INDEX"], 2)
            self.assertEqual(config["KAMERA"]["BREITE"], 3840)
            self.assertEqual(Path(config["VERARBEITUNGS_CONFIG"]).parent, path.parent)

    def test_unknown_camera_setting_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.json"
            path.write_text(json.dumps({"KAMERA": {"NOT_A_SETTING": 7}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                live.load_live_config(path)


class ReferenceGuardTests(unittest.TestCase):
    def test_no_height_without_minimum_visible_references(self):
        engine = live.LiveAnalyzer.__new__(live.LiveAnalyzer)
        engine.size = (640, 480)
        engine.min_ref = 3
        engine.base_pose = object()
        engine.detectors = object()
        engine.spec_map = {}
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        observations = [
            {"id": 1, "role": "reference", "corners_px": np.ones((4, 2))},
            {"id": 20, "role": "measurement", "corners_px": np.ones((4, 2))},
        ]
        with patch.object(live.calc, "detect_markers", return_value=observations):
            with patch.object(live.calc, "select_reference_pose") as pose_method:
                report = engine.evaluate(frame)
                pose_method.assert_not_called()
        self.assertEqual(report["status"], "error")
        self.assertFalse(any("height_mm" in marker for marker in report["markers"]))
        self.assertIn("Zu wenige", report["error"])


if __name__ == "__main__":
    unittest.main()
