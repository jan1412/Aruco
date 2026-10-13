"""Known synthetic geometry, failure gates and spatial diagnostic semantics."""
import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import aruco_verarbeitung as a
import calibrate_charuco as c
from calibration_spatial import compute_spatial_quality, write_spatial_outputs
import test_aruco_verarbeitung as fixtures


class MeasurementOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.HeightMeasurementTests()
        self.f.setUp()
        self.config = copy.deepcopy(self.f.config)

    def records(self, heights, config=None, noise=0., seed=1):
        config = config or self.config
        specs = a.build_specs(config)
        observations = []
        rng = np.random.default_rng(seed)
        for index, spec in enumerate(s for s in specs if s.role != "reference"):
            center = np.array([35. + index * 35., -45. - index * 20., heights[spec.marker_id]])
            points = a.square_corners(spec.size_mm) @ a.rotation_z(.3 + index * .2).T + center
            observation = self.f.projected_observation(spec, points, self.f.pose)
            observation["corners_px"] += rng.normal(0, noise, (4, 2))
            observations.append(observation)
        spec_map = {s.key: s for s in specs}
        records = a.analyze_observations(observations, spec_map, self.f.pose, self.f.K, self.f.D, config)
        a.initialize_height_controls(records, config)
        a.apply_zero_levels(records, a.zero_levels(config, specs, []))
        return records, specs, spec_map, observations

    def test_common_plane_recovers_known_height_and_reduces_noise(self):
        self.config.update(MESSMODELL="gemeinsame_ebene", MESSGRUPPEN={"surface": [4, 5]})
        individual_errors, joint_errors = [], []
        for seed in range(20):
            records, _, spec_map, _ = self.records({4:8., 5:8.}, noise=.05, seed=seed)
            individual_errors.extend(r["height_mm"] - 8. for r in records)
            planes = a.fit_measurement_planes(records, spec_map, self.f.pose, self.f.K, self.f.D, self.config)
            self.assertEqual(planes[0]["status"], "ok")
            a.finalize_plane_reports(planes, records)
            self.assertAlmostEqual(records[0]["height_raw_mm"], records[1]["height_raw_mm"], places=10)
            joint_errors.append(planes[0]["height_mm"] - 8.)
        self.assertLess(np.sqrt(np.mean(np.square(joint_errors))), np.sqrt(np.mean(np.square(individual_errors))))

    def test_incompatible_plane_and_missing_member_are_rejected(self):
        self.config.update(MESSMODELL="gemeinsame_ebene", MESSGRUPPEN={"surface": [4, 5]},
                           MESSEBENE_MAX_EINZELHOEHENSPANNE_MM=1.)
        records, _, spec_map, _ = self.records({4:3., 5:13.})
        for selected in (records, records[:1]):
            selected = copy.deepcopy(selected)
            planes = a.fit_measurement_planes(selected, spec_map, self.f.pose, self.f.K, self.f.D, self.config)
            self.assertEqual(planes[0]["status"], "error")
            self.assertTrue(all("height_mm" not in r for r in selected))

    def test_nonplanar_reference_geometry_recovers_pose(self):
        self.config["REFERENZ_HOEHEN_MM"] = {1:12., 3:25.}
        specs = a.build_specs(self.config)
        geometry, _ = a.build_reference_geometry(self.config, specs)
        observations = [self.f.projected_observation(s, geometry[s.key], self.f.pose)
                        for s in specs if s.role == "reference"]
        pose = a.estimate_reference_pose(observations, geometry, self.f.K, self.f.D, self.config, "synthetic")
        np.testing.assert_allclose(pose.R, self.f.pose.R, atol=1e-6)
        np.testing.assert_allclose(pose.tvec, self.f.pose.tvec, atol=1e-5)
        self.assertLess(pose.rms_px, 1e-5)

    def test_offset_correction_and_independent_control(self):
        self.config.update(MESS_IDS=[4, 5, 6], HOEHENREFERENZEN_MM={5:10., 6:15.},
            KONTROLLKORREKTUR_MODUS="offset", KONTROLLKORREKTUR_IDS=[5],
            HOEHENREFERENZ_MAX_FEHLER_MM=.2)
        records, _, _, _ = self.records({4:8.4, 5:10.4, 6:15.4})
        correction = a.apply_height_controls(records, self.config)
        self.assertEqual(correction["status"], "ok")
        self.assertAlmostEqual(correction["offset_mm"], .4, places=5)
        self.assertAlmostEqual(records[0]["height_mm"], 8., places=5)
        self.assertTrue(records[1]["used_for_control_correction"])
        self.assertFalse(records[2]["used_for_control_correction"])
        self.assertAlmostEqual(records[2]["height_error_mm"], 0., places=5)
        records[2]["height_mm"] += 1.
        correction = a.apply_height_controls(records, {**self.config, "KONTROLLKORREKTUR_MODUS":"aus"})
        self.assertEqual(correction["status"], "error")
        self.assertNotIn("height_mm", records[0])

    def test_missing_or_inconsistent_controls_block_correction(self):
        self.config.update(HOEHENREFERENZEN_MM={5:10.}, KONTROLLKORREKTUR_MODUS="offset",
                           KONTROLLKORREKTUR_IDS=[5])
        records, _, _, _ = self.records({4:8., 5:10.})
        correction = a.apply_height_controls(records[:1], self.config)
        self.assertEqual(correction["status"], "error")
        self.assertNotIn("height_mm", records[0])
        self.config.update(MESS_IDS=[4,5,6], HOEHENREFERENZEN_MM={5:10.,6:15.},
                           KONTROLLKORREKTUR_IDS=[5,6], KONTROLLKORREKTUR_MIN_MARKER=2,
                           KONTROLLKORREKTUR_MAX_SPANNE_MM=.2)
        records, _, _, _ = self.records({4:8.4,5:10.4,6:16.})
        self.assertEqual(a.apply_height_controls(records,self.config)["status"],"error")
        self.assertNotIn("height_mm",records[0])

    def test_robust_loss_keeps_the_normal_rms_rejection(self):
        self.config.update(MESS_OPTIMIERUNG_LOSS="soft_l1", MESS_MAX_RMS_PX=.5)
        _, _, spec_map, observations = self.records({4:8.,5:8.})
        observations[0]["corners_px"][0] += np.array([5.,-4.])
        records=a.analyze_observations(observations,spec_map,self.f.pose,self.f.K,self.f.D,self.config)
        self.assertEqual(records[0]["status"],"error")
        self.assertNotIn("height_mm",records[0])

    def test_local_json_normalizes_id_keys_and_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            root=Path(folder)
            path=root/"config.local.json"
            path.write_text(json.dumps(dict(REFERENZ_HOEHEN_MM={"1":12.},
                MESSBILDER=["input/*.png"], REFERENZBILD="reference.png")),encoding="utf-8")
            config=a.load_configuration(path)
            self.assertEqual(config["REFERENZ_HOEHEN_MM"],{1:12.})
            self.assertEqual(config["REFERENZBILD"],str(root/"reference.png"))
            self.assertEqual(config["MESSBILDER"],[str(root/"input"/"*.png")])
            path.write_text('{"MESSMODELL_TYPO": "gemeinsame_ebene"}',encoding="utf-8")
            with self.assertRaises(a.ProcessingError):
                a.load_configuration(path)

    def test_bad_reference_invalidates_plane_and_controls(self):
        self.config.update(MESS_IDS=[4, 5, 6], HOEHENREFERENZEN_MM={6:10.},
                           MESSMODELL="gemeinsame_ebene", MESSGRUPPEN={"surface":[4, 5]})
        records, _, spec_map, _ = self.records({4:8., 5:8., 6:10.})
        planes = a.fit_measurement_planes(records, spec_map, self.f.pose, self.f.K, self.f.D, self.config)
        self.assertIsNotNone(a.apply_reference_quality(records, {"control_rms_px":9.}, self.config))
        a.finalize_plane_reports(planes, records)
        self.assertNotIn("height_mm", planes[0])
        self.assertNotIn("height_raw_mm", planes[0])
        self.assertTrue(all("height_mm" not in r for r in records))

    def test_pipeline_exports_plane_and_separate_control_rows(self):
        self.config.update(MESS_IDS=[4, 5, 6], HOEHENREFERENZEN_MM={6:10.},
                           MESSMODELL="gemeinsame_ebene", MESSGRUPPEN={"surface":[4, 5]},
                           ANNOTIERTE_BILDER=False)
        _, _, _, observations = self.records({4:8., 5:8., 6:10.})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            root = Path(folder)
            image = np.full((720, 1280, 3), 255, np.uint8)
            cv2.imwrite(str(root / "reference.png"), image)
            cv2.imwrite(str(root / "measurement.png"), image)
            np.savez(root / "camera.npz", camera_matrix=self.f.K, dist_coeffs=self.f.D, image_size=[1280,720])
            self.config.update(REFERENZBILD=str(root/"reference.png"), MESSBILDER=[str(root/"measurement.png")],
                AUSGABEORDNER=str(root/"output"), KAMERAMATRIX_DATEI=str(root/"camera.npz"),
                VERZEICHNUNG_DATEI=str(root/"camera.npz"))
            references = self.f.reference_observations(self.f.pose)
            with patch.object(a, "detect_markers", side_effect=[references, references+observations]):
                result = a.run_pipeline(self.config)
            self.assertEqual(result["valid_measurements"], 2)
            self.assertEqual(result["images_incomplete"], 0)
            with (result["run_directory"]/"hoehen.csv").open(encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle, delimiter=";"))
            self.assertEqual([r["rolle"] for r in rows], ["measurement", "measurement", "height_control"])
            self.assertTrue((result["run_directory"]/"messebene.csv").is_file())


class SpatialIndicatorTests(unittest.TestCase):
    def fixture(self, residual=(0.,0.), tilted=True, outside=False):
        K = np.array([[100.,0.,100.],[0.,100.,50.],[0.,0.,1.]])
        objects = np.array([[-.75,-.1,0.],[-.65,-.1,0.],[-.75,.1,0.],[-.65,.1,0.]], np.float32)
        if outside:
            objects = np.vstack([objects, [2.,0.,0.]])
        views, rvecs, tvecs = [], [], []
        for index, angle in enumerate((-.2,0.,.2) if tilted else (0.,0.,0.)):
            rvec, tvec = np.array([angle,0.,0.]), np.array([0.,0.,1.])
            pixels = cv2.projectPoints(objects, rvec, tvec, K, np.zeros(5))[0] + np.asarray(residual)
            views.append(c.View(Path(f"synthetic_{index}.png"), objects, pixels,
                        np.arange(len(objects)), .1, 100.))
            rvecs.append(rvec)
            tvecs.append(tvec)
        return views, dict(matrix=K, distortion=np.zeros(5), rvecs=rvecs, tvecs=tvecs)

    def test_missing_regions_remain_unknown_and_outside_points_are_not_clamped(self):
        views, fit = self.fixture(outside=True)
        report = compute_spatial_quality(views, (200,100), fit, grid=(2,1))
        self.assertEqual(report["cells"][0]["status"], "observed")
        self.assertEqual(report["cells"][1]["status"], "unobserved")
        self.assertIsNone(report["cells"][1]["rms_px"])
        self.assertEqual(report["outside_image_corners"], 3)

    def test_local_error_and_pose_diversity_are_distinguished(self):
        views, fit = self.fixture(residual=(3.,4.))
        report = compute_spatial_quality(views, (200,100), fit, grid=(2,1))
        self.assertAlmostEqual(report["cells"][0]["rms_px"], 5.)
        self.assertEqual(report["cells"][0]["status"], "high_error")
        views, fit = self.fixture(tilted=False)
        report = compute_spatial_quality(views, (200,100), fit, grid=(2,1))
        self.assertEqual(report["cells"][0]["status"], "low_tilt_span")

    def test_export_retains_null_and_validation_source(self):
        views, fit = self.fixture()
        report = compute_spatial_quality(views, (200,100), fit, grid=(2,1), source="validation")
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            write_spatial_outputs(folder, report)
            data = json.loads((Path(folder)/"spatial_quality.json").read_text(encoding="utf-8"))
            self.assertEqual(data["source"], "validation")
            self.assertIsNone(data["cells"][1]["rms_px"])
            image = cv2.imread(str(Path(folder)/"spatial_quality.png"))
            self.assertEqual(image.shape, (850,1920,3))


if __name__ == "__main__":
    unittest.main()
