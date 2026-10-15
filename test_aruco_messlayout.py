"""Synthetische Regressionstests fuer starre Messmarker-Raster (ohne Kamera)."""
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

import aruco_verarbeitung as calc
import aruco_live as live


class MeasurementLayoutTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(calc.CONFIG)
        self.config.update(
            REFERENZ_IDS=[0, 1, 2, 3],
            MESS_IDS=[4, 5, 6, 7],
            REFERENZ_DICT="DICT_4X4_50",
            MESS_DICT="DICT_4X4_50",
            REFERENZ_GROESSE_MM=20.,
            MESS_GROESSE_MM=20.,
            REFERENZ_LAYOUT_MODUS="positionen",
            REFERENZ_POSITIONEN_MM={0: [0., 0.], 1: [100., 0.],
                                     2: [0., -100.], 3: [100., -100.]},
            REFERENZ_WINKEL_GRAD={0: 0., 1: 0., 2: 0., 3: 0.},
            MESSMODELL="gemeinsame_ebene",
            MESSGRUPPEN={"oberflaeche": [4, 5, 6, 7]},
            MESS_LAYOUTS={"oberflaeche": {
                "modus": "raster",
                "raster_ids": [[4, 5], [6, 7]],
                "abstand_x_mm": 80.0,
                "abstand_y_mm": 70.0,
                "winkel_grad": {4: 0., 5: 0., 6: 0., 7: 0.},
            }},
        )
        self.K = np.array([[900., 0., 640.], [0., 890., 360.], [0., 0., 1.]])
        self.D = np.array([.03, -.02, .001, -.001, .01])
        R = Rotation.from_euler("xyz", [145., 5., -12.], degrees=True).as_matrix()
        self.pose = calc.Pose(cv2.Rodrigues(R)[0], np.array([[0.], [100.], [900.]]),
                              0., "synthetic", [0, 1, 2, 3])

    def synthesize(self, config=None, height=15., yaw=.3, noise=0.0, subset=None):
        config = config or self.config
        specs = calc.build_specs(config)
        mapping = {s.key: s for s in specs}
        layout = calc.build_measurement_layouts(config, specs)["oberflaeche"]
        observations = []
        for spec in specs:
            if spec.role != "measurement" or (subset is not None and spec.marker_id not in subset):
                continue
            center = layout["positions"][spec.marker_id]
            local = calc.square_corners(spec.size_mm)
            angle = layout["angles_rad"][spec.marker_id] + yaw
            points = (local @ calc.rotation_z(angle).T
                      + calc.rotation_z(yaw) @ np.r_[center, 0.]
                      + np.array([65., -90., height]))
            corners = cv2.projectPoints(points, self.pose.rvec, self.pose.tvec, self.K, self.D)[0].reshape(4, 2)
            if noise:
                corners += np.random.default_rng(spec.marker_id + 10).normal(0, noise, corners.shape)
            observations.append({"dictionary": spec.dictionary, "id": spec.marker_id,
                                 "role": spec.role, "size_mm": spec.size_mm,
                                 "corners_px": corners})
        return config, mapping, observations

    def process(self, config=None, height=15., yaw=.3, noise=0.0, subset=None):
        config, mapping, observations = self.synthesize(config, height, yaw, noise, subset)
        records = calc.analyze_observations(observations, mapping, self.pose, self.K, self.D, config)
        levels = {key: (0., "test") for key in mapping
                  if mapping[key].role == "measurement"}
        calc.apply_zero_levels(records, levels)
        planes = calc.fit_measurement_planes(records, mapping, self.pose, self.K, self.D, config)
        return records, planes

    def test_rigid_grid_recovers_height_and_position(self):
        records, planes = self.process()
        self.assertEqual(planes[0]["status"], "ok")
        self.assertTrue(planes[0]["rigid_layout"])
        self.assertEqual(planes[0]["layout_mode"], "raster")
        self.assertAlmostEqual(planes[0]["height_raw_mm"], 15., places=4)
        self.assertAlmostEqual(planes[0]["angle_deg"], math.degrees(.3), places=4)
        for rec in records:
            self.assertEqual(rec["status"], "ok")
            self.assertAlmostEqual(rec["height_mm"], 15., places=4)
            self.assertLess(rec["reprojection_rms_px"], 1e-5)

    def test_with_two_visible_markers_can_fit_layout(self):
        records, planes = self.process(subset={4, 7})
        self.assertEqual(planes[0]["status"], "ok")
        self.assertEqual(len(records), 2)
        self.assertAlmostEqual(planes[0]["height_raw_mm"], 15., places=4)

    def test_markers_do_not_need_world_fixed_xy(self):
        first, planes1 = self.process(height=7., yaw=.15)
        second, planes2 = self.process(height=24., yaw=.65)
        self.assertEqual(planes1[0]["status"], "ok")
        self.assertEqual(planes2[0]["status"], "ok")
        self.assertAlmostEqual(first[0]["height_mm"], 7., places=4)
        self.assertAlmostEqual(second[0]["height_mm"], 24., places=4)

    def test_known_positions_mode_and_marker_angles(self):
        cfg = copy.deepcopy(self.config)
        cfg["MESS_LAYOUTS"]["oberflaeche"] = {
            "modus": "positionen",
            "positionen_mm": {4: [0., 0.], 5: [80., 0.],
                               6: [0., -70.], 7: [80., -70.]},
            "winkel_grad": {5: 90., 7: 180.}
        }
        records, planes = self.process(cfg, height=9.)
        self.assertEqual(planes[0]["status"], "ok")
        self.assertAlmostEqual(planes[0]["height_raw_mm"], 9., places=4)
        self.assertEqual(len(records), 4)

    def test_pair_distance_mode_two_markers(self):
        cfg = copy.deepcopy(self.config)
        cfg["MESSGRUPPEN"] = {"oberflaeche": [4, 5]}
        cfg["MESS_LAYOUTS"] = {"oberflaeche": {"modus": "abstaende",
                               "abstaende_mm": [[4, 5, 80.]]}}
        records, planes = self.process(cfg, height=21.)
        self.assertEqual(planes[0]["status"], "ok")
        self.assertAlmostEqual(planes[0]["height_raw_mm"], 21., places=4)

    def test_bad_known_distance_must_reject(self):
        cfg = copy.deepcopy(self.config)
        cfg["MESS_LAYOUTS"]["oberflaeche"]["abstand_x_mm"] = 90.
        records, planes = self.process(cfg)
        self.assertEqual(planes[0]["status"], "error")
        self.assertFalse(any("height_mm" in rec for rec in records))

    def test_no_layout_backward_compatible(self):
        cfg = copy.deepcopy(self.config)
        cfg["MESS_LAYOUTS"] = {}
        specs = calc.build_specs(cfg)
        self.assertEqual(calc.build_measurement_layouts(cfg, specs), {})
        self.assertEqual(cfg["MESSMODELL"], "gemeinsame_ebene")

    def test_invalid_layout_raises(self):
        cfg = copy.deepcopy(self.config)
        cfg["MESS_LAYOUTS"]["oberflaeche"]["raster_ids"] = [[4, 4], [6, 7]]
        with self.assertRaises(calc.ProcessingError):
            calc.build_specs(cfg)
        cfg = copy.deepcopy(self.config)
        cfg["MESSMODELL"] = "einzelmarker"
        with self.assertRaises(calc.ProcessingError):
            calc.build_specs(cfg)

    def test_json_converts_nested_id_keys(self):
        cfg = {"MESS_LAYOUTS": {"oberflaeche": {
            "modus": "positionen",
            "positionen_mm": {"4": [0., 0.], "5": [80., 0.],
                               "6": [0., -70.], "7": [80., -70.]},
            "winkel_grad": {"4": 0., "5": 0.}}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            loaded = calc.load_configuration(path)
        self.assertIn(4, loaded["MESS_LAYOUTS"]["oberflaeche"]["positionen_mm"])
        self.assertIn(5, loaded["MESS_LAYOUTS"]["oberflaeche"]["winkel_grad"])


class FixedReferenceWithoutLiveMarkers(unittest.TestCase):
    def test_fixed_reference_disables_live_visibility_guard(self):
        engine = live.LiveAnalyzer.__new__(live.LiveAnalyzer)
        engine.size = (640, 480)
        engine.base_pose = calc.Pose(np.zeros((3, 1)), np.array([[0.], [0.], [1000.]]),
                                     0., "reference_image", [0, 1, 2])
        engine.min_ref = 3
        engine.require_reference_control = False
        engine.pc = {"REFERENZPOSE_MODUS": "fest", "REF_KONTROLLE_WARN_RMS_PX": 1.,
                     "HOEHENREFERENZEN_MM": {}}
        engine.detectors = object()
        engine.spec_map = {}
        engine.geometry = {}
        engine.K = np.eye(3)
        engine.D = np.zeros(5)
        engine.null_levels = {}
        engine.mess_count = 1
        engine.ctrl_count = 0
        sample = {"dictionary": "DICT_4X4_50", "id": 9, "role": "measurement",
                  "status": "ok", "height_mm": 11., "corners_px": np.zeros((4, 2))}
        with patch.object(live.calc, "detect_markers", return_value=[sample]):
            with patch.object(live.calc, "select_reference_pose") as select:
                with patch.object(live.calc, "analyze_observations", return_value=[dict(sample)]):
                    with patch.object(live.calc, "apply_zero_levels"):
                        with patch.object(live.calc, "fit_measurement_planes", return_value=[]):
                            with patch.object(live.calc, "initialize_height_controls"):
                                with patch.object(live.calc, "apply_height_controls",
                                                  return_value={"status": "off"}):
                                    report = engine.evaluate(np.zeros((480, 640, 3), dtype=np.uint8))
                select.assert_not_called()
        self.assertEqual(report["status"], "ok")
        self.assertIsNone(report["reference_control"]["control_rms_px"])
        self.assertTrue(any("Kamerabewegung" in w for w in report["warnings"]))
        quality = live.quality_summary(report, 3, 1, 0, 3, 1., 1.5,
                                       reference_control_active=False, fixed_reference=True)
        self.assertEqual(quality["reference_quality"], "FEST / UNGEPRUEFT")


if __name__ == "__main__":
    unittest.main()
