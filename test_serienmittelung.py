"""Series configs, module APIs, safe input selection and stability rejection."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import aruco_serienmittelung
import aruco_verarbeitung
import pipeline_example


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def frame(self, folder, name, value):
        folder.mkdir(parents=True, exist_ok=True)
        ok, data = cv2.imencode(".png", np.full((40,60,3),value,np.uint8))
        self.assertTrue(ok)
        data.tofile(folder/name)

    def test_direct_api_accepts_partial_config_and_excludes_previous_means(self):
        folder = self.root/"input"
        self.frame(folder,"first.png",10)
        self.frame(folder,"second.png",30)
        self.frame(folder,"old_gemittelt.png",240)
        for name in ("new_gemittelt.png","again_gemittelt.png"):
            report=aruco_serienmittelung.mittel_bildserie(folder,folder/name,
                    config={"MARKERPRUEFUNG":False})
            self.assertEqual(report["status"],"ok")
            self.assertEqual(report["images_total"],2)
            np.testing.assert_array_equal(aruco_serienmittelung.read_image(folder/name),20)
        with self.assertRaises(aruco_serienmittelung.SeriesError):
            aruco_serienmittelung.mittel_bildserie(folder,folder/"new_gemittelt.png",
                    config={"MARKERPRUEFUNG":False})

    def test_config_paths_are_relative_to_config_not_calling_script(self):
        path=self.root/"serien.config.local.json"
        data=dict(REFERENZ_ORDNER="reference",MESS_ORDNER="measurement",AUSGABEORDNER="output",
                  MARKERPRUEFUNG=False)
        path.write_text(json.dumps(data),encoding="utf-8-sig")
        config=aruco_serienmittelung.load_configuration(path)
        self.assertEqual(config["REFERENZ_ORDNER"],str(self.root/"reference"))
        self.assertEqual(config["AUSGABEORDNER"],str(self.root/"output"))
        self.assertIsNone(aruco_serienmittelung.CONFIG["REFERENZ_ORDNER"])
        path.write_text('{"INPUT_TYPO":"input"}',encoding="utf-8")
        with self.assertRaises(aruco_serienmittelung.SeriesError):
            aruco_serienmittelung.load_configuration(path)

    def test_series_wrapper_writes_separate_means_through_json_config(self):
        for folder,values in ((self.root/"ref",(10,30)),(self.root/"mess",(50,70))):
            for i,value in enumerate(values):
                self.frame(folder,f"frame_{i}.png",value)
        path=self.root/"serien.config.local.json"
        path.write_text(json.dumps(dict(REFERENZ_ORDNER="ref",MESS_ORDNER="mess",
            AUSGABEORDNER="results",MARKERPRUEFUNG=False)),encoding="utf-8")
        report=aruco_serienmittelung.serien_auswerten(aruco_serienmittelung.load_configuration(path))
        self.assertEqual(report["status"],"ok")
        np.testing.assert_array_equal(aruco_serienmittelung.read_image(report["reference"]["output_file"]),20)
        np.testing.assert_array_equal(aruco_serienmittelung.read_image(report["measurements"][0]["output_file"]),60)

    def test_marker_motion_is_rejected_without_writing_a_mean(self):
        folder=self.root/"moving"
        folder.mkdir()
        dictionary=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        for index,shift in enumerate((0,8)):
            image=np.full((480,640,3),255,np.uint8)
            for marker_id,x in ((2,80),(3,260)):
                marker=cv2.aruco.generateImageMarker(dictionary,marker_id,80)
                image[140:220,x+shift:x+shift+80]=cv2.cvtColor(marker,cv2.COLOR_GRAY2BGR)
            ok,data=cv2.imencode(".png",image)
            self.assertTrue(ok)
            data.tofile(folder/f"frame_{index}.png")
        config=dict(REFERENZ_IDS=[0,1],MESS_IDS=[2,3],REFERENZ_DICT="DICT_4X4_50",
                    MESS_DICT="DICT_4X4_50",MIN_REFERENZMARKER=2,MIN_MESSMARKER=2)
        output=self.root/"mean.png"
        report=aruco_serienmittelung.mittel_bildserie(folder,output,config=config)
        self.assertEqual(report["status"],"error")
        self.assertEqual(report["images_accepted"],0)
        self.assertFalse(output.exists())

    def test_blank_defaults_fail_with_clear_errors_and_partial_dicts_are_supported(self):
        with self.assertRaises(aruco_serienmittelung.SeriesError):
            aruco_serienmittelung.serien_auswerten()
        with self.assertRaises(aruco_verarbeitung.ProcessingError):
            aruco_verarbeitung.run_pipeline()
        with self.assertRaisesRegex(aruco_verarbeitung.ProcessingError,"MESSBILDER"):
            aruco_verarbeitung.run_pipeline({"REFERENZBILD":"not_used.png"})

    def test_pipeline_example_passes_only_successful_series_outputs(self):
        path=self.root/"config.local.json"
        path.write_text("{}",encoding="utf-8")
        success=dict(status="ok",run_directory="results",processing_config_paths={
            "REFERENZBILD":"reference_gemittelt.png","MESSBILDER":["measurement_gemittelt.png"]})
        with patch.object(aruco_serienmittelung,"load_configuration",return_value={}), \
             patch.object(aruco_serienmittelung,"serien_auswerten",return_value=success), \
             patch.object(aruco_verarbeitung,"run_pipeline",return_value={"ok":True}) as measurement:
            self.assertEqual(pipeline_example.run("series.json",path),{"ok":True})
            self.assertEqual(measurement.call_args.args[0]["MESSBILDER"],["measurement_gemittelt.png"])
        with patch.object(aruco_serienmittelung,"load_configuration",return_value={}), \
             patch.object(aruco_serienmittelung,"serien_auswerten",return_value={**success,"status":"partial_or_error"}), \
             patch.object(aruco_verarbeitung,"run_pipeline") as measurement:
            with self.assertRaises(RuntimeError):
                pipeline_example.run("series.json",path)
            measurement.assert_not_called()


if __name__ == "__main__":
    unittest.main()
