#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live-Hoehenmessung via OpenCV (Insta360 Link 2 Pro), Addon fuer Aruco 2.2.1.

OpenCV-Frames werden in ORIGINALaufloesung ausgewertet und als PNG gesichert.
Referenz- und Messmodell stammen unveraendert aus aruco_verarbeitung.py.

    python aruco_live.py --config live.config.local.json
    python aruco_live.py --probe 5 --backend DSHOW
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import math
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

try:
    import aruco_verarbeitung as calc
except ImportError as exc:
    raise SystemExit("aruco_verarbeitung.py fehlt: Addon im selben Ordner starten.") from exc

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent

CONFIG = {
    "VERARBEITUNGS_CONFIG": "config.local.json",  # dieselbe Marker-/Kalibrier-Config wie bisher
    "AUSGABEORDNER": "live_aufnahmen",
    "REFERENZ_START": "live",             # live = mit R erfassen, bild = REFERENZBILD laden
    "BERECHNUNGEN_PRO_SEKUNDE": 2.0,    # Aktualisierung der Hoehen, nicht Kamera-FPS
    "ANZEIGE_MAX_BREITE_PX": 1600,      # lediglich die Vorschau skalieren
    "MAX_ANZEIGEALTER_S": 2.0,          # alte Analyse nicht weiter als gueltig markieren
    "MIN_SICHTBARE_REFERENZEN": None,  # None = MIN_REFERENZMARKER aus Berechnung
    "CONTINUOUS_CSV": False,
    "KAMERA": {
        "INDEX": 0,
        "BACKEND": "DSHOW",             # Windows: DSHOW / MSMF / ANY; Linux: V4L2
        "BREITE": 3840,
        "HOEHE": 2160,
        "FPS": 30,
        "FOURCC": "MJPG",
        "PUFFERGROESSE": 1,
        # None: nicht ueber OpenCV veraendern. Manuelle Einstellungen sind
        # treiberabhaengig und muessen ueberprueft werden.
        "AUTO_FOKUS": None,
        "FOKUS": None,
        "AUTO_BELICHTUNG": None,         # CAP_PROP_AUTO_EXPOSURE; DSHOW oft 0.25/0.75
        "BELICHTUNG": None,
        "AUTO_WEISSABGLEICH": None,
        "WEISSABGLEICH_K": None,
    },
}


def resolve_path(value, base):
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def load_live_config(filename=None):
    config = copy.deepcopy(CONFIG)
    base = ROOT
    if filename is not None:
        file = Path(filename).expanduser().resolve()
        base = file.parent
        changes = json.loads(file.read_text(encoding="utf-8-sig"))
        if not isinstance(changes, dict) or set(changes) - set(CONFIG):
            raise ValueError("Unbekannte oder ungueltige Schluessel in Live-Config.")
        for key, value in changes.items():
            if key == "KAMERA":
                if not isinstance(value, dict) or set(value) - set(CONFIG["KAMERA"]):
                    raise ValueError("Unbekannte oder ungueltige KAMERA-Config-Schluessel.")
                config["KAMERA"].update(value)
            else:
                config[key] = value
    for key in ("VERARBEITUNGS_CONFIG", "AUSGABEORDNER"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"{key} braucht einen Verzeichnispfad/Dateipfad.")
        config[key] = str(resolve_path(config[key], base))
    if config["REFERENZ_START"] not in ("live", "bild"):
        raise ValueError("REFERENZ_START: nur 'live' oder 'bild'.")
    if not 0 < float(config["BERECHNUNGEN_PRO_SEKUNDE"]) <= 60:
        raise ValueError("BERECHNUNGEN_PRO_SEKUNDE: muss in (0, 60] sein.")
    if int(config["ANZEIGE_MAX_BREITE_PX"]) < 640:
        raise ValueError("ANZEIGE_MAX_BREITE_PX: mindestens 640.")
    if float(config["MAX_ANZEIGEALTER_S"]) <= 0:
        raise ValueError("MAX_ANZEIGEALTER_S: muss positiv sein.")
    return config


def get_backend(name):
    backends = {"ANY": cv2.CAP_ANY, "DSHOW": cv2.CAP_DSHOW,
                "MSMF": cv2.CAP_MSMF, "V4L2": getattr(cv2, "CAP_V4L2", cv2.CAP_ANY)}
    try:
        return backends[str(name).upper()]
    except KeyError as exc:
        raise ValueError(f"Unbekanntes Kamera-Backend: {name}") from exc


def set_camera_prop(cap, name, value):
    prop = getattr(cv2, name, None)
    if prop is None:
        print(f"Kamera | {name}: Property in OpenCV nicht verfuegbar")
        return
    try:
        ok = cap.set(prop, float(value))
        actual = cap.get(prop)
        print(f"Kamera | {name}: angefordert {value}, gemeldet {actual:g}, akzeptiert={ok}")
        if not ok:
            print(f"WARNUNG | {name}: Kameratreiber hat Einstellung nicht bestaetigt.")
    except (cv2.error, ValueError, TypeError) as exc:
        print(f"WARNUNG | {name}: {exc}")


def open_camera(config):
    cam = cv2.VideoCapture(int(config["INDEX"]), get_backend(config["BACKEND"]))
    if not cam.isOpened():
        raise RuntimeError(f"Kameraindex {config['INDEX']} ({config['BACKEND']}) nicht erreichbar.")
    try:
        fourcc = config.get("FOURCC")
        if fourcc is not None:
            if not isinstance(fourcc, str) or len(fourcc) != 4:
                raise ValueError("KAMERA.FOURCC braucht genau vier Zeichen.")
            set_camera_prop(cam, "CAP_PROP_FOURCC", cv2.VideoWriter_fourcc(*fourcc))
        for cfg, prop in (("BREITE", "CAP_PROP_FRAME_WIDTH"),
                          ("HOEHE", "CAP_PROP_FRAME_HEIGHT"),
                          ("FPS", "CAP_PROP_FPS"),
                          ("PUFFERGROESSE", "CAP_PROP_BUFFERSIZE"),
                          ("AUTO_FOKUS", "CAP_PROP_AUTOFOCUS"),
                          ("FOKUS", "CAP_PROP_FOCUS"),
                          ("AUTO_BELICHTUNG", "CAP_PROP_AUTO_EXPOSURE"),
                          ("BELICHTUNG", "CAP_PROP_EXPOSURE"),
                          ("AUTO_WEISSABGLEICH", "CAP_PROP_AUTO_WB"),
                          ("WEISSABGLEICH_K", "CAP_PROP_WB_TEMPERATURE")):
            value = config.get(cfg)
            if value is not None:
                set_camera_prop(cam, prop, value)
        return cam
    except Exception:
        cam.release()
        raise


def save_png(file, image):
    success, encoded = cv2.imencode(".png", image)
    if not success:
        raise RuntimeError(f"PNG konnte nicht kodiert werden: {file}")
    encoded.tofile(file)  # imdecode/imencode unterstuetzt Unicode-Pfade auf Windows


def get_visible(observations):
    """Exakte Erkennung, nicht mit gueltiger Hoehenbestimmung verwechseln."""
    return {
        role: [o for o in observations if o.get("role") == role]
        for role in ("reference", "measurement", "height_control", "unconfigured")
    }


def quality_summary(report, expected_ref, expected_mess, expected_controls, minimum, warn, limit):
    records = report.get("markers", [])
    visible = get_visible(records)
    n_ref = len(visible["reference"])
    n_mess = len(visible["measurement"])
    n_controls = len(visible["height_control"])
    valid = sum(r.get("role") == "measurement" and r.get("status") == "ok"
                and "height_mm" in r for r in records)
    rms = report.get("reference_control", {}).get("control_rms_px")
    # "OK" ist ausschliesslich die konfigurierte Pixel-RMS-Pruefung,
    # kein Nachweis einer Messgenauigkeit in mm.
    if report.get("status") == "no_reference":
        quality = "NICHT INITIALISIERT"
    elif n_ref < minimum:
        quality = "NICHT PRUEFBAR"
    elif rms is None or not math.isfinite(rms):
        quality = "NICHT PRUEFBAR"
    elif limit is not None and rms > limit:
        quality = "UNGUELTIG"
    elif warn is not None and rms > warn:
        quality = "WARNUNG"
    elif limit is None:
        quality = "OHNE MAX-GRENZE"
    else:
        quality = "OK"
    return {
        "reference_visible": n_ref, "reference_total": expected_ref,
        "reference_minimum": minimum,
        "measurement_visible": n_mess, "measurement_total": expected_mess,
        "control_visible": n_controls, "control_total": expected_controls,
        "measurement_valid": valid, "reference_rms_px": rms,
        "reference_quality": quality, "warning_rms_px": warn, "maximum_rms_px": limit,
    }


class LiveAnalyzer:
    def __init__(self, config):
        self.config = config
        self.pc = calc.load_configuration(config["VERARBEITUNGS_CONFIG"])
        self.specs = calc.build_specs(self.pc)
        self.spec_map = {s.key: s for s in self.specs}
        self.geometry, _ = calc.build_reference_geometry(self.pc, self.specs)
        self.K, self.D, self.size = calc.load_calibration(self.pc)
        self.detectors = calc.make_detectors(self.specs, self.pc)
        self.base_pose = None
        self.null_levels = {}
        self.reference_file = None
        self.ref_count = sum(s.role == "reference" for s in self.specs)
        self.mess_count = sum(s.role == "measurement" for s in self.specs)
        self.ctrl_count = sum(s.role == "height_control" for s in self.specs)
        minimum = config["MIN_SICHTBARE_REFERENZEN"]
        self.min_ref = (self.pc["MIN_REFERENZMARKER"] if minimum is None else int(minimum))
        if not self.pc["MIN_REFERENZMARKER"] <= self.min_ref <= self.ref_count:
            raise ValueError("MIN_SICHTBARE_REFERENZEN muss zwischen MIN_REFERENZMARKER und Referenzanzahl liegen.")
        self.out = Path(config["AUSGABEORDNER"]) / (
            datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        )
        self.out.mkdir(parents=True, exist_ok=False)
        calc.save_json(self.out / "live_config.json", config)
        calc.save_json(self.out / "verarbeitungs_config.json", self.pc)
        print(f"Messlauf: {self.out}")

    def assert_size(self, frame):
        shape = (frame.shape[1], frame.shape[0])
        if shape != self.size:
            raise calc.ProcessingError(
                f"Liveaufloesung {shape[0]}x{shape[1]} != Kalibrierung "
                f"{self.size[0]}x{self.size[1]}; Frames werden NICHT umskaliert."
            )

    def set_reference(self, frame, source, save=False):
        self.assert_size(frame)
        obs = calc.detect_markers(frame, self.detectors, self.spec_map)
        if len(get_visible(obs)["reference"]) < self.min_ref:
            raise calc.ProcessingError(
                f"Referenzaufnahme: mindestens {self.min_ref} sichtbare "
                "Referenzmarker erforderlich."
            )
        pose = calc.estimate_reference_pose(obs, self.geometry, self.K, self.D,
                                            self.pc, "reference_image", enforce_quality=True)
        records = calc.analyze_observations(obs, self.spec_map, pose, self.K, self.D, self.pc)
        calc.initialize_height_controls(records, self.pc)
        levels = calc.zero_levels(self.pc, self.specs, records)
        new_name = self.reference_file
        if save:
            path = self.out / ("referenz_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".png")
            save_png(path, frame)
            new_name = str(path)
        else:
            new_name = str(source)
        self.base_pose, self.null_levels, self.reference_file = pose, levels, new_name
        print(f"Referenz gespeichert: {self.reference_file} | Referenz-RMS "
              f"{pose.rms_px:.3f} px | {len(pose.reference_ids)} Referenzmarker | "
              f"{len(levels)} Nullhoehen")
        if self.pc["NULL_MODUS"] == "referenzbild" and len(levels) < self.mess_count:
            print("WARNUNG: Nicht alle Messmarker haben eine Nullhoehe aus dem Referenzbild!")

    def summary(self, report):
        summary = quality_summary(report, self.ref_count, self.mess_count,
                                  self.ctrl_count, self.min_ref,
                                  self.pc.get("REF_KONTROLLE_WARN_RMS_PX"),
                                  self.pc.get("REF_KONTROLLE_MAX_RMS_PX"))
        seen_refs = {r["id"] for r in report.get("markers", [])
                     if r.get("role") == "reference"}
        seen_mess = {r["id"] for r in report.get("markers", [])
                     if r.get("role") == "measurement"}
        summary["missing_reference_ids"] = [i for i in self.pc["REFERENZ_IDS"]
                                            if i not in seen_refs]
        summary["missing_measurement_ids"] = [spec.marker_id for spec in self.specs
                                              if spec.role == "measurement" and spec.marker_id not in seen_mess]
        return summary

    def evaluate(self, frame):
        self.assert_size(frame)
        obs = calc.detect_markers(frame, self.detectors, self.spec_map)
        report = {"image": "live", "status": "no_reference", "markers": obs,
                  "warnings": [], "captured_at": datetime.now().astimezone().isoformat()}
        if self.base_pose is None:
            report["error"] = "Noch keine Referenz: Taste R druecken."
            return report
        visible_refs = get_visible(obs)["reference"]
        if len(visible_refs) < self.min_ref:
            report["status"] = "error"
            report["error"] = (f"Zu wenige sichtbare Referenzmarker: "
                               f"{len(visible_refs)}/{self.min_ref}. Hoehen gesperrt.")
            return report
        try:
            pose, control = calc.select_reference_pose(
                obs, self.base_pose, self.geometry, self.K, self.D,
                self.pc, enforce_quality=False
            )
            report["reference_pose"] = pose.as_dict()
            report["reference_control"] = control
            records = calc.analyze_observations(obs, self.spec_map, pose, self.K, self.D, self.pc)
            calc.initialize_height_controls(records, self.pc)
            calc.apply_zero_levels(records, self.null_levels)
            planes = calc.fit_measurement_planes(records, self.spec_map, pose,
                                                  self.K, self.D, self.pc)
            reference_error = calc.apply_reference_quality(records, control, self.pc)
            height_controls = (calc.apply_height_controls(records, self.pc)
                               if reference_error is None
                               else {"status": "blocked", "error": reference_error})
            calc.finalize_plane_reports(planes, records)
            report.update(markers=records, measurement_planes=planes,
                          height_control=height_controls)
            rms = control.get("control_rms_px")
            warn = self.pc.get("REF_KONTROLLE_WARN_RMS_PX")
            if rms is not None and warn is not None and rms > warn:
                report["warnings"].append(
                    f"Referenz-RMS {rms:.3f} px ueber Warnschwelle {warn:.3f} px."
                )
            measured = [r for r in records if r["role"] == "measurement"]
            valid = [r for r in measured if r.get("status") == "ok" and "height_mm" in r]
            control_valid = sum(r["role"] == "height_control" and r["status"] == "ok"
                                for r in records)
            if reference_error:
                report.update(status="error", error=reference_error)
            elif height_controls["status"] == "error":
                report.update(status="error", error=height_controls["error"])
            elif not measured:
                report.update(status="no_measurements", error="Keine Messmarker sichtbar.")
            elif not valid:
                report.update(status="error", error="Kein gueltiger Messmarker.")
            else:
                complete = len(valid) == self.mess_count and control_valid == self.ctrl_count
                report["status"] = "ok" if complete else "partial"
        except (calc.ProcessingError, cv2.error, RuntimeError, ValueError) as exc:
            # Keine geschaetzte Hoehe aus einem Pose-Fehler freigeben.
            report.update(status="error", error=str(exc), markers=obs)
        return report

    def save_measurement(self, frame, report):
        """Foto und mathematische Ergebnisse stammen aus exakt demselben Frame."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"messung_{timestamp}"
        photo = f"{name}.png"
        save_png(self.out / photo, frame)
        result = dict(report, image=photo, reference_image=self.reference_file)
        calc.write_frame_outputs(self.out, name, result, frame, self.K, self.D, self.pc)
        calc.write_csv(self.out / f"{name}.csv", calc.measurement_rows(result, self.specs),
                       self.pc["CSV_DEZIMALKOMMA"])
        print(f"Messfoto {photo}: Status {result['status']} | Ordner {self.out}")

    def append_live_csv(self, report):
        if not self.config["CONTINUOUS_CSV"]:
            return
        rows = calc.measurement_rows(report, self.specs)
        if not rows:
            return
        path = self.out / "live_werte.csv"
        new = not path.is_file()
        fields = ["zeit"] + list(rows[0].keys())
        with path.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
            if new:
                writer.writeheader()
            timestamp = datetime.now().astimezone().isoformat()
            for row in rows:
                writer.writerow({"zeit": timestamp, **row})


def status_color(quality, status):
    if status in ("error", "no_reference") or quality in ("UNGUELTIG", "NICHT PRUEFBAR"):
        return (55, 70, 230)  # BGR rot
    if quality == "WARNUNG" or status in ("partial", "no_measurements"):
        return (40, 190, 245)  # gelb
    return (70, 205, 85)  # gruen


def draw_overlay(frame, report, summary, age, max_width, max_age=2.0):
    """Skaliert NUR das Vorschaubild; Markerkoordinaten werden mit skaliert."""
    scale = min(1.0, int(max_width) / frame.shape[1])
    if scale < 1:
        image = cv2.resize(frame, (round(frame.shape[1] * scale),
                                   round(frame.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    else:
        image = frame.copy()
    height, width = image.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    stale = age > max_age
    main_color = ((120, 120, 200) if stale else
                  status_color(summary["reference_quality"], report["status"]))
    reference_text = (f"Referenz: {summary['reference_visible']}/{summary['reference_total']} "
                      f"(min. {summary['reference_minimum']}) | {summary['reference_quality']}")
    rms = summary["reference_rms_px"]
    rms_text = "---" if rms is None else f"{rms:.3f} px"
    warn, limit = summary.get("warning_rms_px"), summary.get("maximum_rms_px")
    threshold = f" | Warn: {warn:g}" if warn is not None else ""
    threshold += f" / Max: {limit:g} px" if limit is not None else ""
    lines = [
        f"ArUco LIVE | {('VERALTET' if stale else report['status']).upper()} | Auswertung vor {age:.1f}s",
        reference_text,
        f"Referenz-RMS: {rms_text}{threshold} (keine mm-Genauigkeit)",
        (f"Messmarker sichtbar: {summary['measurement_visible']}/{summary['measurement_total']} | "
         f"Hoehen gueltig: {summary['measurement_valid']}/{summary['measurement_total']}"),
    ]
    if summary["control_total"]:
        lines.append(f"Hoehenkontrollen: {summary['control_visible']}/{summary['control_total']}")
    for title, key in (("Fehlende REF-IDs", "missing_reference_ids"),
                       ("Fehlende MESS-IDs", "missing_measurement_ids")):
        missing = summary.get(key, [])
        if missing:
            label = ",".join(map(str, missing[:12]))
            lines.append(f"{title}: {label}" + (" ..." if len(missing) > 12 else ""))
    if stale:
        lines.append("ANZEIGE VERALTET: Hoehen bis zur naechsten Auswertung nicht bestaetigt!")
    lines.append("R = Referenz | S = Messfoto+Auswertung | F = Foto | Q/ESC = Ende")
    if report.get("error"):
        lines.append("FEHLER: " + str(report["error"])[:100])
    for warning in report.get("warnings", [])[:2]:
        lines.append("WARNUNG: " + str(warning)[:100])
    # Schmale, gut lesbare Statusleiste im Bild. Dynamisch kleine Schrift.
    fontsize = max(0.40, min(0.75, width / 2000))
    step = max(19, round(fontsize * 36))
    pad = 12
    panel_h = min(height, (len(lines) + 1) * step + 2 * pad)
    panel_w = min(width, max(500, round(width * 0.87)))
    overlay = image.copy()
    cv2.rectangle(overlay, (0, 0), (panel_w, panel_h), (20, 25, 25), -1)
    cv2.addWeighted(overlay, 0.82, image, 0.18, 0, dst=image)
    for index, label in enumerate(lines):
        y = pad + step * (index + 1)
        if y > height - 4:
            break
        color = main_color if index in (0, 1) else (240, 240, 240)
        cv2.putText(image, label, (pad, y), font, fontsize, color, 1, cv2.LINE_AA)
    # Rahmen und lokale Hoehen direkt auf den jeweiligen Marker projizieren.
    # Die Koordinaten stammen aus dem zuletzt ausgewerteten Frame. Bei Bewegung
    # sind sie veraltet; das Messalter steht deshalb explizit im Kopf.
    for record in report.get("markers", []):
        if record.get("role") == "unconfigured":
            continue
        points = np.asarray(record.get("corners_px", []), dtype=np.float64)
        if points.shape != (4, 2):
            continue
        poly = np.rint(points * scale).astype(np.int32)
        role = record.get("role")
        good = (not stale and record.get("status") == "ok" and "height_mm" in record)
        if role == "reference":
            color = (255, 160, 30)   # blau = Referenz
        elif role == "height_control":
            color = (230, 180, 40)   # cyan = Kontrollhoehe
        else:
            color = (80, 215, 85) if good else (65, 70, 230)
        cv2.polylines(image, [poly], True, color, max(1, int(round(2 * max(scale, .5)))), cv2.LINE_AA)
        label = f"{record['id']} REF" if role == "reference" else str(record["id"])
        if good and role == "measurement":
            label += f": {record['height_mm']:.3f} mm"
        elif role == "measurement":
            label += ": --- (ungueltig)"
        elif role == "height_control" and good:
            label += f": {record['height_mm']:.3f} mm [Kontrolle]"
        x = max(3, min(width - 5, int(np.min(poly[:, 0]))))
        y = max(panel_h + 16 if int(np.min(poly[:, 1])) < panel_h else 16,
                int(np.min(poly[:, 1])) - 6)
        y = min(y, height - 5)
        cv2.putText(image, label, (x, y), font, max(0.38, fontsize * 0.85), color,
                    1, cv2.LINE_AA)
    return image


def probe(max_index, backend):
    for i in range(max_index + 1):
        camera = cv2.VideoCapture(i, get_backend(backend))
        try:
            if not camera.isOpened():
                continue
            ok, frame = camera.read()
            if ok:
                print(f"Kameraindex {i}: {frame.shape[1]}x{frame.shape[0]} | "
                      f"Backend: {camera.getBackendName()} (Modell aus OpenCV nicht erkennbar)")
        finally:
            camera.release()


def run(config):
    engine = LiveAnalyzer(config)
    cap = open_camera(config["KAMERA"])
    try:
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError("Kamera geoeffnet, aber kein Videobild empfangen.")
        engine.assert_size(frame)
        print(f"Stream: {frame.shape[1]}x{frame.shape[0]}, "
              f"Treiber-FPS: {cap.get(cv2.CAP_PROP_FPS):.1f}")
        if config["REFERENZ_START"] == "bild":
            ref = engine.pc.get("REFERENZBILD")
            if not ref:
                raise ValueError("REFERENZ_START='bild': REFERENZBILD in config.local.json fehlt.")
            image = calc.read_image(calc.resolve_path(ref), engine.size)
            engine.set_reference(image, str(ref))
        else:
            print("Referenzaufnahme mit R ausloesen. Fuer NULL_MODUS='referenzbild' "
                  "muessen die Messmarker dabei auf der Nullebene liegen.")
        window = "ArUco Live | Insta360 Link 2 Pro"
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        period = 1 / float(config["BERECHNUNGEN_PRO_SEKUNDE"])
        last_attempt = -math.inf
        last_report = None
        last_time = None
        while True:
            now = time.monotonic()
            if now - last_attempt >= period:
                last_attempt = now
                try:
                    last_report = engine.evaluate(frame)
                    engine.append_live_csv(last_report)
                except (calc.ProcessingError, cv2.error, RuntimeError, ValueError) as exc:
                    logging.warning("Live-Auswertung fehlgeschlagen: %s", exc)
                    last_report = {"image": "live", "status": "error", "markers": [],
                                   "error": str(exc), "warnings": []}
                last_time = time.monotonic()
            summary = engine.summary(last_report)
            age = time.monotonic() - last_time if last_time is not None else 0.0
            preview = draw_overlay(frame, last_report, summary, age,
                                   config["ANZEIGE_MAX_BREITE_PX"],
                                   config["MAX_ANZEIGEALTER_S"])
            cv2.imshow(window, preview)
            key = cv2.waitKey(1) & 0xff
            if key in (27, ord("q"), ord("Q")):
                break
            if key in (ord("r"), ord("R")):
                try:
                    engine.set_reference(frame, "live_capture", save=True)
                    last_attempt = -math.inf
                except (calc.ProcessingError, cv2.error, ValueError) as exc:
                    print(f"REFERENZ NICHT UEBERNOMMEN: {exc}")
            elif key in (ord("f"), ord("F")):
                photo = engine.out / ("foto_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".png")
                save_png(photo, frame)
                print(f"Foto ohne Auswertung: {photo}")
            elif key in (ord("s"), ord("S")):
                if engine.base_pose is None:
                    print("Keine Messung moeglich: zuerst Referenz mit R aufnehmen.")
                else:
                    # Kein alter Messwert: exakt das gespeicherte Foto auswerten.
                    # Fehler beim Dekodieren duerfen die Live-Kamera nicht beenden.
                    try:
                        fresh = engine.evaluate(frame)
                    except (calc.ProcessingError, cv2.error, RuntimeError, ValueError) as exc:
                        fresh = {"image": "live", "status": "error", "markers": [],
                                 "error": str(exc), "warnings": []}
                        print(f"MESSFOTO NICHT AUSWERTBAR: {exc}")
                    engine.save_measurement(frame, fresh)
                    last_report, last_time = fresh, time.monotonic()
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError("Kamerastream abgerissen.")
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="ArUco Live-Hoehenmessung (Insta360 Link 2 Pro)")
    parser.add_argument("--config", type=Path, help="JSON-Konfig fuer Live-Modus")
    parser.add_argument("--probe", type=int, metavar="N", help="Kamera-Indizes 0 bis N suchen")
    parser.add_argument("--backend", default="DSHOW", help="Backend fuer --probe (DSHOW/MSMF/ANY/V4L2)")
    args = parser.parse_args(argv)
    try:
        if args.probe is not None:
            if args.probe < 0:
                raise ValueError("--probe erwartet N>=0")
            probe(args.probe, args.backend)
            return 0
        return run(load_live_config(args.config))
    except (OSError, ValueError, KeyError, RuntimeError, calc.ProcessingError,
            json.JSONDecodeError, cv2.error) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())