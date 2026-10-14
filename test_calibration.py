"""Synthetic checks for configurable ChArUco boards and calibration export."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import calibrate_charuco


class CalibrationTests(unittest.TestCase):
    def args(self):
        return calibrate_charuco.parse_args(["--images", "input", "--output", "output",
            "--squares-x", "4", "--squares-y", "6", "--square-mm", "25",
            "--marker-mm", "15", "--dictionary", "DICT_6X6_50", "--min-corners", "8"])

    def test_board_uses_explicit_geometry(self):
        args = self.args()
        board = calibrate_charuco.make_board(args.square_mm, args.marker_mm, args.squares_x,
                             args.squares_y, args.dictionary)
        self.assertEqual(tuple(board.getChessboardSize()), (4, 6))
        self.assertEqual(len(board.getChessboardCorners()), 15)
        self.assertEqual(board.getDictionary().markerSize, 6)

    def test_synthetic_calibration_and_export(self):
        args = self.args()
        board = calibrate_charuco.make_board(args.square_mm, args.marker_mm, args.squares_x,
                             args.squares_y, args.dictionary)
        objects = board.getChessboardCorners()
        K = np.array([[900., 0., 640.], [0., 890., 360.], [0., 0., 1.]])
        D = np.array([0.03, -0.02, 0.001, -0.001, 0.01])
        rng = np.random.default_rng(7)
        views = []
        for index in range(12):
            rvec = rng.uniform(-0.6, 0.6, 3)
            tvec = np.array([rng.uniform(-0.15, 0.1), rng.uniform(-0.15, 0.1), rng.uniform(0.4, 0.8)])
            points = cv2.projectPoints(objects, rvec, tvec, K, D)[0].astype(np.float32)
            views.append(calibrate_charuco.View(Path(f"synthetic_{index}.png"), objects.copy(), points,
                                np.arange(len(objects), dtype=np.int32).reshape(-1, 1), 0.1, 100.))
        fit = calibrate_charuco.fit_camera(views, (1280, 720))
        self.assertLess(fit["rms"], 1e-3)
        np.testing.assert_allclose(fit["matrix"], K, rtol=1e-4, atol=0.02)
        quality = calibrate_charuco.quality_summary(views, (1280, 720), fit, 1.)
        with tempfile.TemporaryDirectory(prefix="calibration_test_", dir=Path(__file__).resolve().parent) as folder:
            output = Path(folder)
            calibrate_charuco.save_calibration(output, board, fit, (1280, 720), views,
                               [{"file": str(v.path)} for v in views], args, quality, [], None)
            metadata = json.loads((output / "camera_calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["board"]["squares_x"], 4)
            self.assertEqual(metadata["board"]["squares_y"], 6)
            self.assertEqual(metadata["board"]["dictionary"], "DICT_6X6_50")
            self.assertEqual(metadata["board"]["charuco_corners"], 15)
            with np.load(output / "camera_calibration.npz", allow_pickle=False) as data:
                np.testing.assert_array_equal(data["image_size"], [1280, 720])
            self.assertTrue((output / "spatial_quality.png").is_file())

    def test_model_comparison_uses_shared_holdout_observations(self):
        args = self.args()
        board = calibrate_charuco.make_board(args.square_mm, args.marker_mm, args.squares_x,
                             args.squares_y, args.dictionary)
        objects = board.getChessboardCorners()
        K = np.array([[900.,0.,640.],[0.,890.,360.],[0.,0.,1.]])
        D = np.array([.03,-.02,.001,-.001,.1])
        rng = np.random.default_rng(11)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            root = Path(folder)
            training, holdout = root/"training", root/"holdout"
            training.mkdir()
            holdout.mkdir()
            views = []
            for index in range(17):
                path = (training if index < 12 else holdout)/f"synthetic_{index}.png"
                calibrate_charuco.write_image(path, np.full((720,1280,3), index, np.uint8))
                rvec = rng.uniform(-.6,.6,3)
                tvec = np.array([rng.uniform(-.15,.1),rng.uniform(-.15,.1),rng.uniform(.4,.8)])
                pixels = cv2.projectPoints(objects, rvec, tvec, K, D)[0].astype(np.float32)
                views.append(calibrate_charuco.View(path, objects, pixels, np.arange(len(objects)), .1,100.))
            rows = [{"file":str(v.path)} for v in views]
            argv = ["--images",str(training),"--output",str(root/"output"),
                    "--squares-x","4","--squares-y","6","--square-mm","25",
                    "--marker-mm","15","--dictionary","DICT_6X6_50","--min-corners","8",
                    "--single-pass","--model","compare","--validation-images",str(holdout)]
            with patch.object(calibrate_charuco,"collect_views",side_effect=[(views[:12],rows[:12],(1280,720)),
                                                              (views[12:],rows[12:],(1280,720))]):
                self.assertEqual(calibrate_charuco.main(argv),0)
            comparison=json.loads((root/"output"/"model_comparison.json").read_text(encoding="utf-8"))
            self.assertEqual(comparison["selected_model"],"standard")
            self.assertEqual([candidate["images"] for candidate in comparison["candidates"]],[5,5])
            metadata=json.loads((root/"output"/"camera_calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["validation"]["spatial_quality"]["source"],"validation")
            self.assertTrue((root/"output"/"validation_spatial_quality.png").is_file())


if __name__ == "__main__":
    unittest.main()
