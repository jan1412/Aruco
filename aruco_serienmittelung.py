#!/usr/bin/env python3
"""Separate Referenz- und Messbildserien mitteln, mit optionaler ArUco-Kontrolle.

Installation:
    python -m pip install -r requirements.txt
Start:
    python aruco_serienmittelung.py --config serien.config.local.json

Beispielstruktur (ein unveränderter Zustand je Ordner):
    serien/referenz/             -> Referenzaufnahmen
    serien/messungen/position_1/ -> Messserie 1
    serien/messungen/position_2/ -> Messserie 2
Alternativ darf serien/messungen direkt eine einzelne Bildserie enthalten.
Direkte Bilder und Serien-Unterordner dürfen dort nicht gemischt werden.

Jeder Lauf speichert ein referenz_gemittelt.png, pro Messserie ein eigenes
messung_*_gemittelt.png sowie Bericht und Konfiguration in einem neuen Ordner.
Die PNGs bleiben unbeschriftet und können unverändert ins Rechenskript eingehen.

Kamera, Fokus, Auflösung, Pixelausrichtung und Marker müssen innerhalb einer
Serie unverändert bleiben. Es gibt keine Bildregistrierung, Entzerrung oder
Größenänderung. So bleibt die ursprüngliche Kamerakalibrierung anwendbar,
sofern sie bereits zu den Eingangsbildern passt. Eingaben werden als 8-Bit-BGR
ohne EXIF-Drehung gelesen (gleiches Vorgehen wie im Verarbeitungsskript).

Die Mittelung reduziert zufällige Bildschwankungen. Dauerhafte Unschärfe,
falsche Maße und Kalibrierfehler verschwinden dadurch nicht. Gemittelt werden
die dekodierten Pixelwerte der Dateien, keine linearen RAW-Sensordaten.
Die Markerprüfung beurteilt Bildpunkt-Stabilität, nicht Höhen in Millimetern.
Keine 3D-Posenschätzung und keine Kalibrierdateien erforderlich.
"""

from __future__ import annotations

import copy
import argparse
import json
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


# ========================== KONFIGURATION ==========================
CONFIG = {
    "REFERENZ_ORDNER": None,
    "MESS_ORDNER": None,
    "AUSGABEORDNER": None,
    "AUSGESCHLOSSENE_DATEIMUSTER": ["*_gemittelt*.png"],
    "MIN_BILDER": 2,  # Nach Ausschluss ungeeigneter Aufnahmen.
    "MARKERPRUEFUNG": True,  # False: reine Pixelmittelung.

    "REFERENZ_IDS": [],
    "REFERENZ_DICT": None,
    "MESS_IDS": [],
    "MESS_DICT": None,
    "MIN_REFERENZMARKER": 3,  # Nur in der Referenzserie zwingend.
    "MIN_MESSMARKER": 2,      # Nur in den Messserien zwingend.
    # Zusätzliche Höhenkontrollen ebenfalls auf Stabilität prüfen, z.B.:
    # [{"dictionary": "DICT_4X4_100", "ids": [2, 3]}]. Keine Sollhöhen nötig.
    "ZUSAETZLICHE_MARKER": [],

    "DETEKTOR_PIXEL_PRO_ZELLE": 8,  # Verbesserte Bitdekodierung.
    "DETEKTOR_SCHWELLENFENSTER_MAX": 23,
    # Startwert für den eigenen Aufbau testen/anpassen. Das ist KEIN
    # Reprojektions-RMS: gemessene Ecken werden mit ihrem zeitlichen Median
    # verglichen; der höchste RMS eines beobachteten Markers entscheidet.
    "STABILITAET_MAX_RMS_PX": 1.0,
    "MIN_MARKERBEOBACHTUNGEN": 2,
    # Verhindert, dass aus einer gemischten Serie nur eine kleine zufällig
    # passende Teilmenge als angeblich stabiler Zustand ausgegeben wird.
    "MIN_AKZEPTIERTER_ANTEIL": 0.70,
}
# ======================== ENDE KONFIGURATION ========================

VERSION = "1.1.0"
BASE_DIR = Path(__file__).resolve().parent
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


class SeriesError(ValueError):
    """Ungeeignete Eingaben oder keine ausreichend stabile Bildserie."""


def resolve_path(value):
    p = Path(value).expanduser()
    return (BASE_DIR / p).resolve() if not p.is_absolute() else p.resolve()


def image_paths(directory, excluded_patterns=()):
    directory = Path(directory)
    if not directory.is_dir():
        raise SeriesError(f"Serienordner fehlt: {directory}")
    return sorted(p for p in directory.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
                  and not any(p.match(pattern) for pattern in excluded_patterns))


def read_image(path):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8),
                        cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if image is None:
        raise SeriesError(f"Bild kann nicht gelesen werden: {path}")
    return image


def save_json(path, data):
    def serializable(value):
        if isinstance(value, Path):
            return str(value)
        raise TypeError(f"Nicht serialisierbarer Wert: {type(value).__name__}")
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                    allow_nan=False, default=serializable) + "\n", encoding="utf-8")


def prepare_config(config=None):
    if config is not None and (not isinstance(config, dict) or set(config) - set(CONFIG)):
        raise SeriesError("Config muss ein dict mit bekannten CONFIG-Feldern sein.")
    result = copy.deepcopy(CONFIG)
    if config is not None:
        result.update(copy.deepcopy(config))
    return result


def load_configuration(path):
    """Load JSON overrides; relative folders are anchored to the config file."""
    path = Path(path).expanduser().resolve()
    result = prepare_config(json.loads(path.read_text(encoding="utf-8-sig")))
    for key in ("REFERENZ_ORDNER", "MESS_ORDNER", "AUSGABEORDNER"):
        if result[key] is not None:
            if not isinstance(result[key], str) or not result[key]:
                raise SeriesError(f"{key}: nichtleerer Ordnerpfad erforderlich.")
            folder = Path(result[key]).expanduser()
            result[key] = str(folder if folder.is_absolute() else path.parent / folder)
    validate_config(result)
    return result


def validate_config(config, require_paths=True):
    for key in (("REFERENZ_ORDNER", "MESS_ORDNER", "AUSGABEORDNER") if require_paths else ()):
        if not isinstance(config[key], (str, Path)) or not str(config[key]):
            raise SeriesError(f"{key}: Ordnerpfad erforderlich.")
    if type(config["MARKERPRUEFUNG"]) is not bool:
        raise SeriesError("MARKERPRUEFUNG muss True oder False sein.")
    patterns = config["AUSGESCHLOSSENE_DATEIMUSTER"]
    if (not isinstance(patterns, (list, tuple))
            or any(not isinstance(pattern, str) or not pattern for pattern in patterns)):
        raise SeriesError("AUSGESCHLOSSENE_DATEIMUSTER: Liste nichtleerer Glob-Muster erforderlich.")
    for key in ("MIN_BILDER", "MIN_MARKERBEOBACHTUNGEN", "DETEKTOR_PIXEL_PRO_ZELLE"):
        if type(config[key]) is not int or config[key] < 1:
            raise SeriesError(f"{key}: positive ganze Zahl erforderlich.")
    if config["MIN_BILDER"] < 2:
        raise SeriesError("MIN_BILDER muss mindestens 2 sein.")
    threshold = config["STABILITAET_MAX_RMS_PX"]
    if (isinstance(threshold, bool) or not isinstance(threshold, (float, int))
            or not np.isfinite(threshold) or threshold <= 0):
        raise SeriesError("STABILITAET_MAX_RMS_PX muss endlich und positiv sein.")
    fraction = config["MIN_AKZEPTIERTER_ANTEIL"]
    if (isinstance(fraction, bool) or not isinstance(fraction, (float, int))
            or not np.isfinite(fraction) or not 0 < fraction <= 1):
        raise SeriesError("MIN_AKZEPTIERTER_ANTEIL muss in (0,1] liegen.")
    win = config["DETEKTOR_SCHWELLENFENSTER_MAX"]
    if type(win) is not int or win < 3 or win % 2 != 1:
        raise SeriesError("DETEKTOR_SCHWELLENFENSTER_MAX: ungerade ganze Zahl >= 3.")
    if config["MARKERPRUEFUNG"]:
        for prefix in ("REFERENZ", "MESS"):
            ids = config[prefix + "_IDS"]
            if (not isinstance(ids, (list, tuple)) or not ids
                    or any(type(i) is not int or i < 0 for i in ids)
                    or len(set(ids)) != len(ids)):
                raise SeriesError(f"{prefix}_IDS: eindeutige nichtnegative IDs erforderlich.")
            minimum = config["MIN_" + prefix + "MARKER"]
            if type(minimum) is not int or not 1 <= minimum <= len(ids):
                raise SeriesError(f"MIN_{prefix}MARKER passt nicht zur ID-Liste.")
        if config["MIN_MARKERBEOBACHTUNGEN"] < 2:
            raise SeriesError("MIN_MARKERBEOBACHTUNGEN muss mindestens 2 sein.")


def build_detectors(config):
    definitions = [{"dictionary": config[p + "_DICT"], "ids": config[p + "_IDS"]}
                   for p in ("REFERENZ", "MESS")]
    definitions.extend(config["ZUSAETZLICHE_MARKER"])
    ids_by_dictionary = defaultdict(set)
    for definition in definitions:
        name, ids = definition["dictionary"], definition["ids"]
        if not isinstance(name, str) or not name.startswith("DICT_") or not hasattr(cv2.aruco, name):
            raise SeriesError(f"Ungültiges Dictionary: {name!r}")
        dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))
        if (not isinstance(ids, (list, tuple)) or not ids
                or any(type(i) is not int or not 0 <= i < len(dictionary.bytesList) for i in ids)):
            raise SeriesError(f"Ungültige IDs in {name}")
        ids_by_dictionary[name].update(ids)
    overlap = (set(config["REFERENZ_IDS"]) & set(config["MESS_IDS"]))
    if config["REFERENZ_DICT"] == config["MESS_DICT"] and overlap:
        raise SeriesError("Referenz- und Messmarker dürfen nicht dieselben Dictionary/ID-Paare verwenden.")
    result = {}
    for name, ids in ids_by_dictionary.items():
        parameters = cv2.aruco.DetectorParameters()
        parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        parameters.perspectiveRemovePixelPerCell = config["DETEKTOR_PIXEL_PRO_ZELLE"]
        parameters.adaptiveThreshWinSizeMax = config["DETEKTOR_SCHWELLENFENSTER_MAX"]
        dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))
        result[name] = (cv2.aruco.ArucoDetector(dictionary, parameters), ids)
    return result


def detect_markers(image, detectors):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    observations = {}
    for name, (detector, wanted_ids) in detectors.items():
        corners, ids, _ = detector.detectMarkers(gray)
        if ids is None:
            continue
        for marker_id, corner in zip(ids.ravel(), corners):
            marker_id = int(marker_id)
            if marker_id not in wanted_ids:
                continue
            key = f"{name}:{marker_id}"
            if key in observations:
                raise SeriesError(f"Marker {key} kommt mehrfach im Bild vor.")
            observations[key] = np.asarray(corner, dtype=np.float64).reshape(4, 2)
    values = list(observations.items())
    for index, (key_a, a) in enumerate(values):
        for key_b, b in values[index + 1:]:
            scale = min(np.sqrt(abs(cv2.contourArea(a.astype(np.float32)))),
                        np.sqrt(abs(cv2.contourArea(b.astype(np.float32)))))
            distances = np.linalg.norm(a[:, None] - b[None, :], axis=2)
            if max(distances.min(axis=0).max(), distances.min(axis=1).max()) < max(2., .04 * scale):
                raise SeriesError(f"Mehrdeutige Dictionary-Erkennung: {key_a} / {key_b}")
    return observations


def observation_count(observations, dictionary, ids):
    return sum(f"{dictionary}:{i}" in observations for i in ids)


def mittel_bildserie(input_directory, output_file, kind="messung", config=None,
                     expected_shape=None):
    """Eine statische Serie mitteln; Bericht als dict zurückgeben.

    output_file: eigener PNG-Pfad. expected_shape: optionale gemeinsame
    (Höhe,Breite,Kanäle)-Form, z.B. die Form des Referenzmittelbildes.
    Bei Fehlern wird KEIN Mittelbild geschrieben; Bericht enthält die Ursache.
    """
    config = prepare_config(config)
    validate_config(config, require_paths=False)
    if kind not in ("referenz", "messung"):
        raise SeriesError("kind muss 'referenz' oder 'messung' sein.")
    directory, output_file = resolve_path(input_directory), resolve_path(output_file)
    if output_file.suffix.lower() != ".png":
        raise SeriesError("Mittelbilder werden verlustfrei als PNG gespeichert.")
    if output_file.exists():
        raise SeriesError(f"Ausgabedatei existiert bereits: {output_file}")
    if (directory in output_file.parents
            and not any(output_file.match(pattern) for pattern in config["AUSGESCHLOSSENE_DATEIMUSTER"])):
        raise SeriesError("Ausgabe muss ausserhalb der Eingaben liegen oder durch ein Dateimuster ausgeschlossen sein.")
    report = {"series": directory.name, "kind": kind, "input_directory": str(directory),
              "status": "error", "frames": [], "output_file": None,
              "marker_check": config["MARKERPRUEFUNG"], "warnings": []}
    try:
        paths = image_paths(directory, config["AUSGESCHLOSSENE_DATEIMUSTER"])
        report["excluded_patterns"] = list(config["AUSGESCHLOSSENE_DATEIMUSTER"])
        report["images_total"] = len(paths)
        if len(paths) < config["MIN_BILDER"]:
            raise SeriesError(f"Mindestens {config['MIN_BILDER']} Bilder je Serie erforderlich.")
        detectors = build_detectors(config) if config["MARKERPRUEFUNG"] else {}
        observations_by_frame = []
        # Vergleichsform aus der ersten lesbaren Aufnahme; bei Messserien wird
        # sie von der Referenzserie vorgegeben. Abweichende Auflösungen werden
        # nicht selektiv eingemittelt: die ganze Serie muss korrigiert werden.
        shape = tuple(expected_shape) if expected_shape is not None else None
        shape_errors = []
        for path in paths:
            record = {"file": str(path), "accepted": False, "reasons": []}
            report["frames"].append(record)
            observations = {}
            try:
                image = read_image(path)
                if shape is None:
                    shape = image.shape
                if image.shape != shape:
                    record["reasons"].append(f"Bildform {image.shape} passt nicht zu {shape}.")
                    shape_errors.append(path.name)
                else:
                    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                    record["gray_mean"] = float(gray.mean())
                    # Nur Diagnose: Rauschen kann diesen Wert ebenfalls erhöhen.
                    record["laplacian_variance_diagnostic"] = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                    if detectors:
                        observations = detect_markers(image, detectors)
                        prefix = "REFERENZ" if kind == "referenz" else "MESS"
                        count = observation_count(observations, config[prefix + "_DICT"], config[prefix + "_IDS"])
                        record["required_markers_detected"] = count
                        if count < config["MIN_" + prefix + "MARKER"]:
                            record["reasons"].append(f"Zu wenige {prefix.lower()}marker: {count}.")
                        record["detected_markers"] = sorted(observations)
            except (SeriesError, cv2.error, OSError) as exc:
                record["reasons"].append(str(exc))
            observations_by_frame.append(observations)
        if shape_errors:
            raise SeriesError("Unterschiedliche Bildauflösungen/-formen: " + ", ".join(shape_errors))
        if shape is None:
            raise SeriesError("Keine lesbare Aufnahme.")
        report["image_shape"] = list(shape)
        medians = {}
        if detectors:
            collected = defaultdict(list)
            for record, observations in zip(report["frames"], observations_by_frame):
                if not record["reasons"]:
                    for key, points in observations.items():
                        collected[key].append(points)
            for key, points in collected.items():
                if len(points) >= config["MIN_MARKERBEOBACHTUNGEN"]:
                    medians[key] = np.median(np.stack(points), axis=0)
            report["marker_temporal_medians_px"] = {k: v.tolist() for k, v in medians.items()}
        for record, observations in zip(report["frames"], observations_by_frame):
            if record["reasons"]:
                continue
            if detectors:
                rms_by_marker = {}
                for key, points in observations.items():
                    if key not in medians:
                        record["reasons"].append(f"Marker {key}: zu wenige Beobachtungen für Stabilitätsprüfung.")
                        continue
                    errors = points - medians[key]
                    rms = float(np.sqrt(np.mean(np.sum(errors ** 2, axis=1))))
                    rms_by_marker[key] = rms
                    if rms > config["STABILITAET_MAX_RMS_PX"]:
                        record["reasons"].append(f"Marker {key}: zeitlicher Eck-RMS {rms:.3f} px über Stabilitätsgrenze.")
                record["stability_rms_px_by_marker"] = rms_by_marker
                record["max_stability_rms_px"] = max(rms_by_marker.values(), default=None)
            record["accepted"] = not record["reasons"]
        accepted = [r for r in report["frames"] if r["accepted"]]
        report["images_accepted"] = len(accepted)
        report["images_rejected"] = len(paths) - len(accepted)
        if len(accepted) < config["MIN_BILDER"]:
            raise SeriesError("Zu wenige geeignete Bilder nach Qualitätsprüfung.")
        fraction = len(accepted) / len(paths)
        report["accepted_fraction"] = fraction
        if fraction < config["MIN_AKZEPTIERTER_ANTEIL"]:
            raise SeriesError("Zu großer verworfener Anteil; Serie auf Bewegung oder gemischte Zustände prüfen.")
        mean = np.zeros(shape, dtype=np.float64)
        # Zweiter Durchlauf hält den Speicherbedarf unabhängig von der Zahl
        # der Aufnahmen; keine Liste sämtlicher Originalbilder nötig.
        for index, record in enumerate(accepted, 1):
            frame = read_image(Path(record["file"]))
            if frame.shape != shape:
                raise SeriesError("Ein Eingang wurde während der Verarbeitung verändert.")
            mean += (frame.astype(np.float64) - mean) / index
        averaged = np.clip(np.rint(mean), 0, 255).astype(np.uint8)
        if detectors:
            final_observations = detect_markers(averaged, detectors)
            prefix = "REFERENZ" if kind == "referenz" else "MESS"
            count = observation_count(final_observations, config[prefix + "_DICT"], config[prefix + "_IDS"])
            if count < config["MIN_" + prefix + "MARKER"]:
                raise SeriesError("Im Mittelbild fehlen erforderliche Marker; kein Auswertungsbild geschrieben.")
            report["mean_image_markers_px"] = {k: v.tolist() for k, v in final_observations.items()}
        brightness = [r["gray_mean"] for r in accepted]
        span = max(brightness) - min(brightness)
        report["gray_mean_span"] = span
        if span > .10 * max(float(np.median(brightness)), 1.):
            report["warnings"].append("Deutliche Helligkeitsänderung innerhalb der Serie; Licht/Belichtung kontrollieren.")
        if not detectors:
            report["warnings"].append("Markerprüfung deaktiviert: Bewegung und Fokusänderungen wurden nicht geometrisch geprüft.")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        ok, encoded = cv2.imencode(".png", averaged)
        if not ok:
            raise SeriesError("PNG konnte nicht erzeugt werden.")
        encoded.tofile(output_file)
        report.update(status="ok", output_file=str(output_file),
                      averaging="arithmetic_mean_decoded_8bit_pixels",
                      geometric_transform="none")
    except (SeriesError, cv2.error, OSError) as exc:
        report["error"] = str(exc)
    return report


def discover_measurement_series(root, excluded_patterns=()):
    direct = image_paths(root, excluded_patterns)
    nested = [p for p in sorted(Path(root).iterdir()) if p.is_dir() and image_paths(p, excluded_patterns)]
    if direct and nested:
        raise SeriesError("MESS_ORDNER enthält Bilder und Serien-Unterordner. Eine Struktur wählen.")
    if nested:
        return nested
    if direct:
        return [Path(root)]
    raise SeriesError("Keine Messbildserie gefunden.")


def serien_auswerten(config=None):
    """Referenzserie und getrennte Messserien auswerten; Ergebnisbericht liefern."""
    config = prepare_config(config)
    validate_config(config)
    ref_dir = resolve_path(config["REFERENZ_ORDNER"])
    mess_root = resolve_path(config["MESS_ORDNER"])
    series_dirs = discover_measurement_series(mess_root, config["AUSGESCHLOSSENE_DATEIMUSTER"])
    out_root = resolve_path(config["AUSGABEORDNER"])
    for input_dir in (ref_dir, mess_root):
        if out_root == input_dir or input_dir in out_root.parents:
            raise SeriesError("AUSGABEORDNER muss außerhalb der Eingabeordner liegen.")
    if ref_dir in series_dirs:
        raise SeriesError("Referenz- und Messserie müssen unterschiedliche Ordner verwenden.")
    run_dir = out_root / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    run_dir.mkdir(parents=True, exist_ok=False)
    save_json(run_dir / "konfiguration.json", config)
    reference = mittel_bildserie(ref_dir, run_dir / "referenz_gemittelt.png", "referenz", config)
    measurements = []
    if reference["status"] == "ok":
        for index, directory in enumerate(series_dirs, 1):
            name = re.sub(r"[^\w.-]+", "_", directory.name)[:80]
            result = mittel_bildserie(directory, run_dir / f"messung_{index:03d}_{name}_gemittelt.png",
                                     "messung", config, tuple(reference["image_shape"]))
            measurements.append(result)
    else:
        for directory in series_dirs:
            measurements.append({"series": directory.name, "input_directory": str(directory),
                                 "status": "blocked", "output_file": None,
                                 "error": "Referenzserie nicht auswertbar."})
    report = {"version": VERSION, "created_at": datetime.now().astimezone().isoformat(),
              "run_directory": str(run_dir), "reference": reference, "measurements": measurements,
              "status": "ok" if reference["status"] == "ok" and all(r["status"] == "ok" for r in measurements) else "partial_or_error",
              "processing_config_paths": {
                  "REFERENZBILD": reference.get("output_file"),
                  "MESSBILDER": [r["output_file"] for r in measurements if r["status"] == "ok"],
              },
              "limitations": ["Keine Millimeter-Genauigkeitsprüfung oder 3D-Pose.",
                              "Markerstabilität garantiert keine konstante Kalibrierung.",
                              "Dauerhafte Unschärfe und systematische Fehler bleiben bestehen."]}
    save_json(run_dir / "bericht.json", report)
    lines = [f"ArUco-Serienmittelung {VERSION}", f"Gesamtstatus: {report['status']}", ""]
    for r in [reference] + measurements:
        lines.append(f"{r['series']}: {r['status']} | {r.get('images_accepted', 0)}/{r.get('images_total', 0)} Bilder")
        if r.get("output_file"):
            lines.append("Mittelbild: " + r["output_file"])
        if r.get("error"):
            lines.append("Fehler: " + r["error"])
        for frame in r.get("frames", []):
            if not frame["accepted"]:
                lines.append("  Verworfen: " + Path(frame["file"]).name + " | " + " | ".join(frame["reasons"]))
        lines.extend("Hinweis: " + w for w in r.get("warnings", []))
        lines.append("")
    lines.append("Pfade für das Rechenskript:")
    lines.append(json.dumps(report["processing_config_paths"], ensure_ascii=False, indent=2))
    (run_dir / "bericht.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Statische Referenz- und Messserien mitteln.")
    parser.add_argument("--config", type=Path, help="Lokale JSON-Config mit Input/Output und Markerdefinitionen")
    args = parser.parse_args(argv)
    try:
        report = serien_auswerten(load_configuration(args.config) if args.config else CONFIG)
        print(f"Status: {report['status']} | Ergebnisse: {report['run_directory']}")
        for series in [report["reference"]] + report["measurements"]:
            print(f"{series['series']}: {series['status']} | {series.get('output_file') or series.get('error')}")
        return 0 if report["status"] == "ok" else 2
    except (SeriesError, OSError, UnicodeError, KeyError, json.JSONDecodeError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
