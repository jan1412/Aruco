"""Geometrie und Qualitätsprüfung: .venv/Scripts/python.exe -m unittest -v."""
import copy
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

import aruco_verarbeitung as a


class HeightMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(a.CONFIG)
        # Rein synthetischer Aufbau; keine echten Messdaten oder Kameraparameter.
        self.config.update(REFERENZ_IDS=[0, 1, 2, 3], MESS_IDS=[4, 5],
            REFERENZ_DICT="DICT_4X4_50", MESS_DICT="DICT_4X4_50",
            REFERENZ_GROESSE_MM=20., MESS_GROESSE_MM=20.,
            REFERENZ_LAYOUT_MODUS="positionen",
            REFERENZ_POSITIONEN_MM={0:(0.,0.), 1:(100.,0.), 2:(0.,-100.), 3:(100.,-100.)},
            REFERENZ_WINKEL_GRAD={0:0., 1:0., 2:0., 3:0.})
        self.specs = a.build_specs(self.config)
        self.geometry, _ = a.build_reference_geometry(self.config, self.specs)
        self.K = np.array([[900., 0., 640.], [0., 890., 360.], [0., 0., 1.]])
        self.D = np.array([0.03, -0.02, 0.001, -0.001, 0.01])
        R = Rotation.from_euler("xyz", [145., 5., -12.], degrees=True).as_matrix()
        self.pose = a.Pose(cv2.Rodrigues(R)[0], np.array([[0.], [100.], [900.]]),
                           0., "synthetic", [0, 1, 2, 3])

    def projected_observation(self, spec, points, pose):
        pixels = cv2.projectPoints(points, pose.rvec, pose.tvec, self.K, self.D)[0].reshape(4, 2)
        return {"dictionary": spec.dictionary, "id": spec.marker_id,
                "role": spec.role, "size_mm": spec.size_mm, "corners_px": pixels}

    def reference_observations(self, pose):
        return [self.projected_observation(s, self.geometry[s.key], pose)
                for s in self.specs if s.role == "reference"]

    def test_known_heights_and_in_plane_rotation(self):
        estimated = a.estimate_reference_pose(self.reference_observations(self.pose), self.geometry,
                                              self.K, self.D, self.config, "synthetic")
        spec = next(s for s in self.specs if s.role == "measurement")
        for height in (0., 6.5, 20.):
            with self.subTest(height=height):
                center = np.array([90., -120., height])
                points = a.square_corners(spec.size_mm) @ a.rotation_z(0.47).T + center
                observation = self.projected_observation(spec, points, self.pose)
                record = a.analyze_observations([observation], {spec.key: spec}, estimated,
                                               self.K, self.D, self.config)[0]
                self.assertEqual(record["status"], "ok")
                np.testing.assert_allclose(record["position_ground_mm"], center, atol=1e-5)
                self.assertLess(record["reprojection_rms_px"], 1e-5)

    def test_per_image_pose_recovers_camera_movement(self):
        moved = a.Pose(self.pose.rvec.copy(), self.pose.tvec + np.array([[12.], [3.], [0.]]),
                       0., "moved", [0, 1, 2, 3])
        observations = self.reference_observations(moved)
        recovered, control = a.select_reference_pose(observations, self.pose, self.geometry,
                                                     self.K, self.D, self.config)
        self.assertEqual(recovered.source, "measurement_image")
        self.assertLess(control["control_rms_px"], 1e-5)
        np.testing.assert_allclose(recovered.tvec, moved.tvec, atol=1e-5)
        self.config["REFERENZPOSE_MODUS"] = "fest"
        with self.assertRaises(a.ProcessingError):
            a.select_reference_pose(observations, self.pose, self.geometry,
                                    self.K, self.D, self.config)

    def test_bad_reference_preserves_only_diagnostic_height(self):
        record = {"dictionary": "DICT_4X4_50", "id": 4, "role": "measurement",
                  "size_mm": 20., "status": "ok", "height_mm": 6.5, "height_raw_mm": 6.7}
        control = {"control_rms_px": 6.31}
        error = a.apply_reference_quality([record], control, self.config)
        self.assertIsNotNone(error)
        self.assertEqual(record["status"], "error")
        self.assertNotIn("height_mm", record)
        self.assertEqual(record["diagnostic_height_mm"], 6.5)
        report = {"image": "synthetic.jpg", "status": "error", "error": error,
                  "markers": [record], "reference_control": control}
        row = a.measurement_rows(report, self.specs)[0]
        self.assertEqual(row["status"], "error")
        self.assertNotIn("hoehe_mm", row)
        self.assertEqual(row["diagnostische_hoehe_mm"], 6.5)

    def test_calibration_resolution_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="aruco_test_", dir=a.BASE_DIR) as folder:
            path = Path(folder) / "camera.npz"
            np.savez(path, camera_matrix=self.K, dist_coeffs=self.D, image_size=[1280, 720])
            self.config["KAMERAMATRIX_DATEI"] = str(path)
            self.config["VERZEICHNUNG_DATEI"] = str(path)
            self.config["KALIBRIER_AUFLOESUNG"] = (640, 360)
            with self.assertRaisesRegex(a.ProcessingError, "Widersprüchliche Kalibrierauflösungen"):
                a.load_calibration(self.config)


if __name__ == "__main__":
    unittest.main()
