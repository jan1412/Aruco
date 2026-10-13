#!/usr/bin/env python3
"""ArUco-Höhenmessung mit Referenzebene und parallelen Messmarkern.

Installation (nur eine OpenCV-Paketvariante in derselben Python-Umgebung):
    python -m pip install -r requirements.txt
Start:
    python aruco_verarbeitung.py

Alle eigenen Angaben stehen im folgenden CONFIG-Block. Relative Pfade beziehen
sich auf den Ordner dieser Skriptdatei. Pfade sind vor dem Start lokal einzutragen.
Alle Größen und Abstände sind MILLIMETER.

Kamerakalibrierung: zwei numerische NPY-Dateien für K und D, zwei NPZ-Dateien
oder dieselbe gemeinsame NPZ-Datei in beiden Pfadfeldern. NPZ-Schlüssel können
explizit angegeben oder über übliche Namen erkannt werden. Bei reinen NPY-Dateien
muss die originale Kalibrierauflösung im CONFIG-Block stehen (Breite, Höhe).
    np.save("K.npy", K)
    np.save("D.npy", D)
oder:
    np.savez("kalibrierung.npz", camera_matrix=K, dist_coeffs=D,
             image_size=np.array([bildbreite, bildhoehe]))

Geometrie: Grund-x/y liegen in der Referenzebene; Grund-z zeigt aus der bedruckten
Referenzfläche zur Messfläche. Bei einem Referenzwinkel von 0 Grad zeigt die
Richtung von dekodierter Ecke 0 zu Ecke 1 nach Grund-+x und die Richtung von
Ecke 3 zu Ecke 0 nach Grund-+y. Das ist unabhängig von der Bildausrichtung.
Positive Winkel sind Drehungen um Grund-z nach der Rechte-Hand-Regel. Kantenlängen
umfassen das schwarze Quadrat einschließlich seines schwarzen Rahmens, nicht
den zusätzlichen weißen Druckrand. Referenzmarker und Kameraparameter müssen
zwischen den Aufnahmen unverändert bleiben. Bilder werden ohne EXIF-Drehung
eingelesen; sie müssen dieselbe Pixelausrichtung und Auflösung wie die Kalibrierung
besitzen. Verwendet wird das Standard-OpenCV-Kameramodell (kein Fisheye-Modell).

NULL_MODUS="konfiguriert": angegebene Roh-Nullhöhe pro Messmarker abziehen.
NULL_MODUS="referenzbild": dieselben Messmarker müssen im Referenzbild auf
unbeschichtetem Grund aufliegen; ihre geschätzten Rohhöhen werden Nullhöhen.
Explizite NULL_OFFSETS_MM haben in beiden Modi Vorrang. Ohne Nullkorrektur wird
der Abstand der Markerflächen zur definierten Grundebene ausgegeben, nicht
automatisch eine um Papier-/Trägerdicken korrigierte Schichtdicke.

Je Lauf entsteht ein eigener Unterordner mit hoehen.csv, zusammenfassung.txt,
lauf.json, konfiguration.json, lauf.log sowie JSON/TXT und optional markierten
PNG-Bildern für jede Aufnahme. Unkonfigurierte Marker werden mit Bibliothek, ID
und Bildecken gespeichert, aber ohne erfundene metrische Größe. Reprojektions-
fehler sind Pixelwerte und kein Nachweis einer Messunsicherheit in Millimetern.
Bei verworfener Referenzpose bleiben berechnete Höhen ausschließlich als
Diagnosewerte erhalten; die gültige Höhenspalte bleibt leer. Exitcode 2 meldet
mindestens ein unvollständiges oder verworfenes Messbild.
Das Parallelitätsmodell passt x, y, z und eine Drehung in der Grundebene direkt
an die vier Bildecken an. Jeder Messmarker erhält eine eigene lokale Höhe.
"""

from __future__ import annotations

import copy
import csv
import glob
import json
import logging
import math
import re
import sys
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation


# ======================= KONFIGURATION =======================
# Keine lokalen Pfade oder aufbauspezifischen Werte sind vorbelegt.
# Vor dem Start Pfade, Markerdefinitionen und Referenzgeometrie eintragen.
# Alle Laengen in Millimetern; NPZ-Aufloesung wird automatisch gelesen.
# Modi und Felder sind in README.md dokumentiert.
CONFIG = {'REFERENZBILD': None,
 'MESSBILDER': [],
 'AUSGABEORDNER': None,
 'KAMERAMATRIX_DATEI': None,
 'VERZEICHNUNG_DATEI': None,
 'KAMERAMATRIX_NPZ_KEY': 'camera_matrix',
 'VERZEICHNUNG_NPZ_KEY': 'dist_coeffs',
 'KALIBRIER_AUFLOESUNG': None,
 'REFERENZ_IDS': [],
 'MESS_IDS': [],
 'REFERENZ_DICT': None,
 'MESS_DICT': None,
 'REFERENZ_GROESSE_MM': None,
 'MESS_GROESSE_MM': None,
 'REFERENZ_GROESSEN_MM': {},
 'MESS_GROESSEN_MM': {},
 'REFERENZ_DICTS': {},
 'MESS_DICTS': {},
 'REFERENZ_LAYOUT_MODUS': 'positionen',
 'REFERENZ_RASTER_IDS': [],
 'REFERENZ_RASTER_ABSTAND_X_MM': None,
 'REFERENZ_RASTER_ABSTAND_Y_MM': None,
 'REFERENZ_POSITIONEN_MM': {},
 'REFERENZ_ABSTAENDE_MM': [],
 'REFERENZ_URSPRUNG_ID': None,
 'REFERENZ_X_ACHSE_ID': None,
 'REFERENZ_Y_SEITE_ID': None,
 'REFERENZ_Y_SEITE_VORZEICHEN': -1,
 'GEOMETRIE_MAX_ABWEICHUNG_MM': 0.5,
 'REFERENZ_WINKEL_GRAD': {},
 'REFERENZ_EBENENVERSATZ_MM': 0.0,
 'REFERENZPOSE_MODUS': 'pro_bild',
 'MIN_REFERENZMARKER': 3,
 'REF_KONTROLLE_MAX_RMS_PX': 1.5,
 'REF_KONTROLLE_WARN_RMS_PX': 1.0,
 'MESS_MAX_RMS_PX': 2.0,
 'DETEKTOR_PIXEL_PRO_ZELLE': 8,
 'DETEKTOR_SCHWELLENFENSTER_MAX': 23,
 'NULL_MODUS': 'konfiguriert',
 'NULL_OFFSET_MM': 0.0,
 'NULL_OFFSETS_MM': {},
 'ANNOTIERTE_BILDER': True,
 'CSV_DEZIMALKOMMA': True}
# ===================== ENDE KONFIGURATION =====================

VERSION = "2.1.0"
BASE_DIR = Path(__file__).resolve().parent


class ProcessingError(ValueError):
    """Unpassende Eingaben oder nicht auswertbare Aufnahme."""


@dataclass(frozen=True)
class MarkerSpec:
    marker_id: int
    dictionary: str
    size_mm: float
    role: str

    @property
    def key(self):
        return (self.dictionary, self.marker_id)


@dataclass
class Pose:
    rvec: np.ndarray
    tvec: np.ndarray
    rms_px: float
    source: str
    reference_ids: list[int]

    @property
    def R(self):
        return cv2.Rodrigues(self.rvec)[0]

    def as_dict(self):
        R = self.R
        t = self.tvec.reshape(3)
        forward = np.eye(4)
        forward[:3, :3], forward[:3, 3] = R, t
        backward = np.eye(4)
        backward[:3, :3], backward[:3, 3] = R.T, -R.T @ t
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            angles = Rotation.from_matrix(R.T).as_euler("xyz", degrees=True)
        return {
            "source": self.source, "reference_ids": self.reference_ids,
            "reprojection_rms_px": self.rms_px,
            "rvec_ground_to_camera": self.rvec.reshape(3),
            "tvec_ground_to_camera_mm": t,
            "T_ground_to_camera": forward, "T_camera_to_ground": backward,
            "camera_position_ground_mm": -R.T @ t,
            "camera_euler_xyz_extrinsic_deg": angles,
        }


def resolve_path(value):
    path = Path(value).expanduser()
    return (BASE_DIR / path).resolve() if not path.is_absolute() else path.resolve()


def finite_number(value, name, positive=False):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ProcessingError(f"{name}: Zahl erwartet.") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ProcessingError(f"{name}: endliche {'positive ' if positive else ''}Zahl erwartet.")
    return result


def dictionary_object(name):
    if not isinstance(name, str) or not name.startswith("DICT_") or not hasattr(cv2.aruco, name):
        raise ProcessingError(f"Unbekannte OpenCV-Markerbibliothek: {name!r}.")
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))


def build_specs(config):
    specs = []
    for prefix, role in (("REFERENZ", "reference"), ("MESS", "measurement")):
        ids = config[prefix + "_IDS"]
        if not isinstance(ids, (list, tuple)) or not ids:
            raise ProcessingError(f"{prefix}_IDS muss eine nichtleere Liste sein.")
        if any(type(i) is not int or i < 0 for i in ids) or len(set(ids)) != len(ids):
            raise ProcessingError(f"{prefix}_IDS enthält ungültige oder doppelte IDs.")
        sizes, dictionaries = config[prefix + "_GROESSEN_MM"], config[prefix + "_DICTS"]
        if set(sizes) - set(ids) or set(dictionaries) - set(ids):
            raise ProcessingError(f"Overrides für {prefix} enthalten IDs außerhalb der ID-Liste.")
        for marker_id in ids:
            name = dictionaries.get(marker_id, config[prefix + "_DICT"])
            size = finite_number(sizes.get(marker_id, config[prefix + "_GROESSE_MM"]),
                                 f"Größe {prefix} {marker_id}", positive=True)
            dictionary = dictionary_object(name)
            if marker_id >= len(dictionary.bytesList):
                raise ProcessingError(f"ID {marker_id} existiert nicht in {name}.")
            specs.append(MarkerSpec(marker_id, name, size, role))
    if len({s.key for s in specs}) != len(specs):
        raise ProcessingError("Dieselbe Kombination aus Bibliothek und ID hat mehrere Rollen.")
    if config["REFERENZPOSE_MODUS"] not in ("fest", "pro_bild"):
        raise ProcessingError("REFERENZPOSE_MODUS muss 'fest' oder 'pro_bild' sein.")
    if config["NULL_MODUS"] not in ("konfiguriert", "referenzbild"):
        raise ProcessingError("NULL_MODUS muss 'konfiguriert' oder 'referenzbild' sein.")
    minimum = config["MIN_REFERENZMARKER"]
    if type(minimum) is not int or not 1 <= minimum <= len(config["REFERENZ_IDS"]):
        raise ProcessingError("MIN_REFERENZMARKER passt nicht zur Referenz-ID-Liste.")
    for key in ("REF_KONTROLLE_MAX_RMS_PX", "MESS_MAX_RMS_PX"):
        if config[key] is not None:
            config[key] = finite_number(config[key], key, positive=True)
    warning_limit = config.get("REF_KONTROLLE_WARN_RMS_PX")
    if warning_limit is not None:
        config["REF_KONTROLLE_WARN_RMS_PX"] = finite_number(
            warning_limit, "REF_KONTROLLE_WARN_RMS_PX", positive=True)
    finite_number(config["NULL_OFFSET_MM"], "NULL_OFFSET_MM")
    if set(config["NULL_OFFSETS_MM"]) - set(config["MESS_IDS"]):
        raise ProcessingError("NULL_OFFSETS_MM enthält unbekannte Messmarker-IDs.")
    for marker_id, value in config["NULL_OFFSETS_MM"].items():
        finite_number(value, f"NULL_OFFSETS_MM[{marker_id}]")
    return specs


def square_corners(size_mm):
    half = size_mm / 2.0
    return np.array([[-half, half, 0], [half, half, 0],
                     [half, -half, 0], [-half, -half, 0]], dtype=np.float64)


def rotation_z(angle_rad):
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


def layout_from_distances(config):
    ids = config["REFERENZ_IDS"]
    if len(ids) < 3:
        raise ProcessingError("Abstandsmodus benötigt mindestens drei Referenzmarker.")
    pairs = {}
    for entry in config["REFERENZ_ABSTAENDE_MM"]:
        if len(entry) != 3:
            raise ProcessingError("Jeder Paarabstand muss (ID1, ID2, Abstand_mm) enthalten.")
        a, b, distance = entry
        if a not in ids or b not in ids or a == b:
            raise ProcessingError(f"Ungültiges Abstandspaar: {a}, {b}.")
        pair = tuple(sorted((a, b)))
        if pair in pairs:
            raise ProcessingError(f"Doppeltes Abstandspaar: {pair}.")
        pairs[pair] = finite_number(distance, f"Abstand {pair}", positive=True)
    required = {tuple(sorted(pair)) for pair in combinations(ids, 2)}
    if set(pairs) != required:
        raise ProcessingError(f"Alle Paarabstände erforderlich; fehlen: {sorted(required-set(pairs))}.")
    origin, axis, side = (config["REFERENZ_URSPRUNG_ID"], config["REFERENZ_X_ACHSE_ID"],
                          config["REFERENZ_Y_SEITE_ID"])
    side_sign = config.get("REFERENZ_Y_SEITE_VORZEICHEN", 1)
    if type(side_sign) is not int or side_sign not in (-1, 1):
        raise ProcessingError("REFERENZ_Y_SEITE_VORZEICHEN muss +1 oder -1 sein.")
    if len({origin, axis, side}) != 3 or any(i not in ids for i in (origin, axis, side)):
        raise ProcessingError("Drei verschiedene gültige IDs für Ursprung, x-Achse und y-Seite wählen.")
    n = len(ids)
    distances = np.zeros((n, n))
    for (a, b), value in pairs.items():
        i, j = ids.index(a), ids.index(b)
        distances[i, j] = distances[j, i] = value
    centering = np.eye(n) - np.ones((n, n)) / n
    eigenvalues, eigenvectors = np.linalg.eigh(-0.5 * centering @ distances**2 @ centering)
    order = np.argsort(eigenvalues)[::-1][:2]
    if eigenvalues[order[1]] <= 1e-10:
        raise ProcessingError("Paarabstände definieren keine zweidimensionale Referenzgeometrie.")
    points = eigenvectors[:, order] * np.sqrt(np.maximum(eigenvalues[order], 0))
    points -= points[ids.index(origin)].copy()
    ex = points[ids.index(axis)] / np.linalg.norm(points[ids.index(axis)])
    ey = np.array([-ex[1], ex[0]])
    points = points @ np.column_stack((ex, ey))
    if points[ids.index(side), 1] * side_sign < 0:
        points[:, 1] *= -1
    fixed_distance = pairs[tuple(sorted((origin, axis)))]
    variable_ids = [i for i in ids if i not in (origin, axis)]

    def unpack(values):
        positions = {origin: np.array([0.0, 0.0]), axis: np.array([fixed_distance, 0.0])}
        positions.update({i: p for i, p in zip(variable_ids, values.reshape(-1, 2))})
        return positions

    def residual(values):
        p = unpack(values)
        return np.array([np.linalg.norm(p[a] - p[b]) - d for (a, b), d in pairs.items()])

    start = np.concatenate([points[ids.index(i)] for i in variable_ids])
    fit = least_squares(residual, start, x_scale="jac")
    if not fit.success or not np.isfinite(fit.x).all():
        raise ProcessingError("Referenzgeometrie konnte nicht aus Paarabständen bestimmt werden.")
    positions = unpack(fit.x)
    if positions[side][1] * side_sign < 0:
        positions = {i: p * np.array([1, -1]) for i, p in positions.items()}
    if abs(positions[side][1]) < 1e-6:
        raise ProcessingError("Marker für die y-Seite liegt auf der x-Achse.")
    deviation = float(np.max(np.abs(residual(fit.x))))
    limit = finite_number(config["GEOMETRIE_MAX_ABWEICHUNG_MM"],
                          "GEOMETRIE_MAX_ABWEICHUNG_MM", positive=True)
    if deviation > limit:
        raise ProcessingError(f"Paarabstände sind nicht konsistent eben: max. Abweichung {deviation:.3f} mm.")
    return positions, deviation


def build_reference_geometry(config, specs):
    mode = config["REFERENZ_LAYOUT_MODUS"]
    if mode == "raster":
        grid = config["REFERENZ_RASTER_IDS"]
        if (not isinstance(grid, (list, tuple)) or not grid
                or any(not isinstance(row, (list, tuple)) or not row for row in grid)
                or any(len(row) != len(grid[0]) for row in grid)):
            raise ProcessingError("REFERENZ_RASTER_IDS muss ein rechteckiges ID-Raster enthalten.")
        flat = [marker_id for row in grid for marker_id in row]
        if (any(type(i) is not int for i in flat) or len(set(flat)) != len(flat)
                or set(flat) != set(config["REFERENZ_IDS"])):
            raise ProcessingError("REFERENZ_RASTER_IDS muss jede Referenz-ID genau einmal enthalten.")
        dx = finite_number(config["REFERENZ_RASTER_ABSTAND_X_MM"],
                           "REFERENZ_RASTER_ABSTAND_X_MM", positive=True)
        dy = finite_number(config["REFERENZ_RASTER_ABSTAND_Y_MM"],
                           "REFERENZ_RASTER_ABSTAND_Y_MM", positive=True)
        positions = {marker_id: np.array([column * dx, -row_number * dy])
                     for row_number, row in enumerate(grid)
                     for column, marker_id in enumerate(row)}
        origin_id = config["REFERENZ_URSPRUNG_ID"]
        if origin_id not in positions:
            raise ProcessingError("REFERENZ_URSPRUNG_ID fehlt im Raster.")
        origin_point = positions[origin_id].copy()
        positions = {i: p - origin_point for i, p in positions.items()}
        deviation = 0.0
    elif mode == "abstaende":
        positions, deviation = layout_from_distances(config)
    elif mode == "positionen":
        positions, deviation = {}, 0.0
        for marker_id in config["REFERENZ_IDS"]:
            if marker_id not in config["REFERENZ_POSITIONEN_MM"]:
                raise ProcessingError(f"Referenzposition für ID {marker_id} fehlt.")
            point = np.asarray(config["REFERENZ_POSITIONEN_MM"][marker_id], dtype=np.float64)
            if point.shape != (2,) or not np.isfinite(point).all():
                raise ProcessingError(f"Position ID {marker_id}: zwei endliche x/y-Werte erforderlich.")
            positions[marker_id] = point
    else:
        raise ProcessingError("REFERENZ_LAYOUT_MODUS muss 'raster', 'positionen' oder 'abstaende' sein.")
    z = finite_number(config["REFERENZ_EBENENVERSATZ_MM"], "REFERENZ_EBENENVERSATZ_MM")
    geometry, details = {}, []
    for spec in specs:
        if spec.role != "reference":
            continue
        if spec.marker_id not in config["REFERENZ_WINKEL_GRAD"]:
            raise ProcessingError(f"Referenzwinkel für ID {spec.marker_id} fehlt.")
        angle = finite_number(config["REFERENZ_WINKEL_GRAD"][spec.marker_id], "Referenzwinkel")
        center = np.r_[positions[spec.marker_id], z]
        corners = square_corners(spec.size_mm) @ rotation_z(math.radians(angle)).T + center
        geometry[spec.key] = corners
        details.append({"dictionary": spec.dictionary, "id": spec.marker_id,
                        "center_ground_mm": center, "angle_deg": angle,
                        "size_mm": spec.size_mm, "corners_ground_mm": corners})
    return geometry, {"mode": mode, "max_distance_deviation_mm": deviation, "markers": details}


def load_numpy_parameter(path, key, names):
    if not path.is_file():
        raise ProcessingError(f"Kalibrierdatei fehlt: {path}")
    loaded = np.load(path, allow_pickle=False)
    if isinstance(loaded, np.ndarray):
        return np.asarray(loaded, dtype=np.float64), None
    try:
        keys = loaded.files
        if key is None:
            candidates = [name for name in names if name in keys]
            if len(candidates) == 1:
                key = candidates[0]
            elif not candidates and len(keys) == 1:
                key = keys[0]
            else:
                raise ProcessingError(f"NPZ-Schlüssel in {path.name} explizit setzen; vorhanden: {keys}.")
        if key not in keys:
            raise ProcessingError(f"NPZ-Schlüssel {key!r} fehlt in {path.name}; vorhanden: {keys}.")
        size = next((np.asarray(loaded[n]) for n in ("image_size", "imageSize", "resolution")
                     if n in keys), None)
        return np.asarray(loaded[key], dtype=np.float64), size
    finally:
        loaded.close()


def checked_image_size(value):
    a = np.asarray(value)
    if a.shape != (2,) or not np.isfinite(a).all() or np.any(a <= 0) or not np.all(a == a.astype(int)):
        raise ProcessingError("Kalibrierauflösung muss (Breite, Höhe) in ganzen positiven Pixeln sein.")
    return tuple(int(v) for v in a)


def load_calibration(config):
    K, size_k = load_numpy_parameter(resolve_path(config["KAMERAMATRIX_DATEI"]),
        config["KAMERAMATRIX_NPZ_KEY"], ("camera_matrix", "cameraMatrix", "K", "mtx"))
    D, size_d = load_numpy_parameter(resolve_path(config["VERZEICHNUNG_DATEI"]),
        config["VERZEICHNUNG_NPZ_KEY"], ("dist_coeffs", "distCoeffs", "D", "dist", "distortion_coefficients"))
    D = D.reshape(-1, 1)
    if K.shape != (3, 3) or not np.isfinite(K).all() or not np.allclose(K[2], [0, 0, 1]):
        raise ProcessingError("K muss eine endliche 3x3-Kameramatrix mit letzter Zeile [0,0,1] sein.")
    if K[0, 0] <= 0 or K[1, 1] <= 0:
        raise ProcessingError("Die Brennweiten in K müssen positiv sein.")
    if D.size not in (4, 5, 8, 12, 14) or not np.isfinite(D).all():
        raise ProcessingError("D muss 4, 5, 8, 12 oder 14 endliche Standard-Verzeichnungskoeffizienten enthalten.")
    available = [checked_image_size(v) for v in (config["KALIBRIER_AUFLOESUNG"], size_k, size_d)
                 if v is not None]
    if not available:
        raise ProcessingError("Originale Kalibrierauflösung fehlt: KALIBRIER_AUFLOESUNG=(Breite,Höhe) setzen.")
    if len(set(available)) != 1:
        raise ProcessingError(f"Widersprüchliche Kalibrierauflösungen: {available}.")
    return K, D, available[0]


def read_image(path, expected_size):
    if not path.is_file():
        raise ProcessingError(f"Bild fehlt: {path}")
    # fromfile/imdecode unterstützt auch Windows-Pfade mit Umlauten.
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8),
                         cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if image is None:
        raise ProcessingError(f"Bild kann nicht gelesen werden: {path}")
    if (image.shape[1], image.shape[0]) != expected_size:
        raise ProcessingError(f"Bildauflösung {image.shape[1]}x{image.shape[0]} passt nicht zu {expected_size}.")
    return image


def measurement_paths(entries):
    if isinstance(entries, (str, Path)):
        entries = [entries]
    result = []
    for entry in entries:
        raw = str(resolve_path(entry))
        if glob.has_magic(raw):
            matches = sorted(Path(p).resolve() for p in glob.glob(raw, recursive=True) if Path(p).is_file())
            if not matches:
                raise ProcessingError(f"Kein Messbild für Muster: {entry}")
        else:
            path = resolve_path(entry)
            matches = sorted(p for p in path.iterdir() if p.suffix.lower() in
                             (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")) if path.is_dir() else [path]
        result.extend(matches)
    unique = list(dict.fromkeys(result))
    if not unique:
        raise ProcessingError("Keine Messbilder angegeben.")
    return unique


def make_detectors(specs, config=None):
    config = CONFIG if config is None else config
    pixels_per_cell = config.get("DETEKTOR_PIXEL_PRO_ZELLE", 8)
    if type(pixels_per_cell) is not int or pixels_per_cell < 1:
        raise ProcessingError("DETEKTOR_PIXEL_PRO_ZELLE muss eine positive ganze Zahl sein.")
    max_window = config.get("DETEKTOR_SCHWELLENFENSTER_MAX", 23)
    if type(max_window) is not int or max_window < 3:
        raise ProcessingError("DETEKTOR_SCHWELLENFENSTER_MAX muss eine ganze Zahl >= 3 sein.")
    detectors = {}
    for name in sorted({s.dictionary for s in specs}):
        parameters = cv2.aruco.DetectorParameters()
        parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        parameters.perspectiveRemovePixelPerCell = pixels_per_cell
        parameters.adaptiveThreshWinSizeMax = max_window
        detectors[name] = cv2.aruco.ArucoDetector(dictionary_object(name), parameters)
    return detectors


def same_physical_marker(a, b):
    pa, pb = np.asarray(a["corners_px"]), np.asarray(b["corners_px"])
    scale = min(np.sqrt(a["pixel_area"]), np.sqrt(b["pixel_area"]))
    # zyklische Eckreihenfolge kann bei einer anderen Dekodierung verschieden sein.
    corner_distance = max(max(min(np.linalg.norm(p-q) for q in pb) for p in pa),
                          max(min(np.linalg.norm(p-q) for q in pa) for p in pb))
    return corner_distance < max(2.0, scale * 0.04)


def detect_markers(image, detectors, spec_map):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    raw = []
    for name, detector in detectors.items():
        corners, ids, _ = detector.detectMarkers(gray)
        if ids is None:
            continue
        for corner, marker_id in zip(corners, ids.ravel()):
            points = np.asarray(corner, dtype=np.float64).reshape(4, 2)
            raw.append({"dictionary": name, "id": int(marker_id), "corners_px": points,
                        "center_px": points.mean(axis=0),
                        "pixel_area": float(abs(cv2.contourArea(points.astype(np.float32))))})
    groups = []
    for observation in raw:
        target = next((g for g in groups if same_physical_marker(observation, g[0])), None)
        if target is None:
            groups.append([observation])
        else:
            target.append(observation)
    observations = []
    for group in groups:
        configured = [o for o in group if (o["dictionary"], o["id"]) in spec_map]
        if len(configured) > 1:
            names = [f"{o['dictionary']}:{o['id']}" for o in configured]
            raise ProcessingError(f"Mehrdeutige Bibliotheken am selben physischen Marker: {names}.")
        chosen = (configured or group)[0].copy()
        chosen["aliases"] = [{"dictionary": o["dictionary"], "id": o["id"]} for o in group
                              if o is not (configured or group)[0]]
        spec = spec_map.get((chosen["dictionary"], chosen["id"]))
        chosen["role"] = spec.role if spec else "unconfigured"
        chosen["size_mm"] = spec.size_mm if spec else None
        observations.append(chosen)
    known_keys = [(o["dictionary"], o["id"]) for o in observations
                  if (o["dictionary"], o["id"]) in spec_map]
    if len(set(known_keys)) != len(known_keys):
        raise ProcessingError("Ein konfigurierter Marker kommt im Bild mehrfach vor; eindeutige IDs verwenden.")
    return observations


def project(points, rvec, tvec, K, D):
    return cv2.projectPoints(np.asarray(points, dtype=np.float64), rvec, tvec, K, D)[0].reshape(-1, 2)


def reprojection_rms(points, observed, rvec, tvec, K, D):
    errors = project(points, rvec, tvec, K, D) - np.asarray(observed)
    return float(np.sqrt(np.mean(np.sum(errors**2, axis=1))))


def pnp_candidates(points, observed, K, D, flag, refine=False):
    solution = cv2.solvePnPGeneric(np.asarray(points, dtype=np.float64),
                                  np.asarray(observed, dtype=np.float64), K, D, flags=flag)
    candidates = []
    if not solution[0]:
        return candidates
    for rvec, tvec in zip(solution[1], solution[2]):
        if refine:
            rvec, tvec = cv2.solvePnPRefineLM(points, observed, K, D, rvec.copy(), tvec.copy())
        R = cv2.Rodrigues(rvec)[0]
        camera_points = points @ R.T + tvec.reshape(3)
        if not np.isfinite(camera_points).all() or np.any(camera_points[:, 2] <= 0):
            continue
        rms = reprojection_rms(points, observed, rvec, tvec, K, D)
        if math.isfinite(rms):
            candidates.append((rms, rvec, tvec))
    return sorted(candidates, key=lambda candidate: candidate[0])


def reference_correspondences(observations, geometry):
    matched = [o for o in observations if (o["dictionary"], o["id"]) in geometry]
    if not matched:
        return [], None, None
    points = np.concatenate([geometry[(o["dictionary"], o["id"])] for o in matched])
    pixels = np.concatenate([o["corners_px"] for o in matched])
    return [o["id"] for o in matched], points, pixels


def check_limit(value, limit, label):
    if limit is not None and value > limit:
        raise ProcessingError(f"{label}: RMS {value:.3f} px überschreitet {limit:.3f} px.")


def estimate_reference_pose(observations, geometry, K, D, config, source, enforce_quality=True):
    ids, points, pixels = reference_correspondences(observations, geometry)
    if len(ids) < config["MIN_REFERENZMARKER"]:
        raise ProcessingError(f"Referenzpose braucht mindestens {config['MIN_REFERENZMARKER']} Marker; erkannt: {len(ids)}.")
    candidates = pnp_candidates(points, pixels, K, D, cv2.SOLVEPNP_IPPE, refine=True)
    for rms, rvec, tvec in candidates:
        R = cv2.Rodrigues(rvec)[0]
        center_camera = R @ points.mean(axis=0) + tvec.reshape(3)
        if np.dot(R[:, 2], -center_camera) > 0:
            if enforce_quality:
                check_limit(rms, config["REF_KONTROLLE_MAX_RMS_PX"], "Referenzpose")
            return Pose(rvec, tvec, rms, source, ids)
    raise ProcessingError("Keine plausible Referenzpose: Eckzuordnung, Maße und Referenzwinkel prüfen.")


def select_reference_pose(observations, base_pose, geometry, K, D, config, enforce_quality=True):
    ids, points, pixels = reference_correspondences(observations, geometry)
    if config["REFERENZPOSE_MODUS"] == "pro_bild" and len(ids) >= config["MIN_REFERENZMARKER"]:
        pose = estimate_reference_pose(observations, geometry, K, D, config, "measurement_image",
                                       enforce_quality=enforce_quality)
    else:
        pose = base_pose
    control_rms = None
    if points is not None:
        control_rms = reprojection_rms(points, pixels, pose.rvec, pose.tvec, K, D)
        if enforce_quality:
            check_limit(control_rms, config["REF_KONTROLLE_MAX_RMS_PX"], "Referenzkontrolle im Messbild")
    return pose, {"visible_reference_ids": ids, "control_rms_px": control_rms,
                  "control_available": points is not None}


def apply_reference_quality(records, control, config):
    """Berechnete Höhen bei schlechter Referenz nur als Diagnose aufbewahren."""
    rms = control["control_rms_px"]
    if rms is None:
        return None
    try:
        check_limit(rms, config["REF_KONTROLLE_MAX_RMS_PX"], "Referenzkontrolle im Messbild")
    except ProcessingError as exc:
        message = str(exc)
        for record in records:
            if record["role"] != "measurement":
                continue
            if "height_mm" in record:
                record["diagnostic_height_mm"] = record.pop("height_mm")
            previous_error = record.get("error")
            record.update(status="error", error=message +
                          (" " + previous_error if previous_error else ""))
        return message
    return None


def free_marker_pose(observation, spec, K, D, pose):
    local = square_corners(spec.size_mm)
    candidates = pnp_candidates(local, observation["corners_px"], K, D, cv2.SOLVEPNP_IPPE_SQUARE)
    if not candidates:
        raise ProcessingError("Keine plausible Einzelmarkerpose gefunden.")
    rms, rvec, tvec = candidates[0]
    camera_center = tvec.reshape(3)
    # Die zentrale Gegenrichtung: KAMERA -> GRUNDSYSTEM.
    ground_center = pose.R.T @ (camera_center - pose.tvec.reshape(3))
    return {"rvec_marker_to_camera": rvec.reshape(3), "tvec_marker_to_camera_mm": camera_center,
            "center_ground_mm": ground_center, "reprojection_rms_px": rms}, candidates


def fit_parallel_marker(observation, spec, pose, K, D, config, candidates):
    local = square_corners(spec.size_mm)
    R_ground_camera = pose.R
    t_ground_camera = pose.tvec.reshape(3)

    def points_in_ground(parameters):
        return local @ rotation_z(parameters[3]).T + parameters[:3]

    def residual(parameters):
        return (project(points_in_ground(parameters), pose.rvec, pose.tvec, K, D)
                - observation["corners_px"]).ravel()

    fits = []
    for _, rvec, tvec in candidates:
        center = R_ground_camera.T @ (tvec.reshape(3) - t_ground_camera)
        relative_rotation = R_ground_camera.T @ cv2.Rodrigues(rvec)[0]
        psi = math.atan2(relative_rotation[1, 0], relative_rotation[0, 0])
        fit = least_squares(residual, np.r_[center, psi], method="trf", x_scale="jac")
        if not fit.success or not np.isfinite(fit.x).all():
            continue
        camera_points = points_in_ground(fit.x) @ R_ground_camera.T + t_ground_camera
        if np.any(camera_points[:, 2] <= 0):
            continue
        rms = float(np.sqrt(np.mean(np.sum(residual(fit.x).reshape(-1, 2)**2, axis=1))))
        if math.isfinite(rms):
            fits.append((rms, fit.x))
    if not fits:
        raise ProcessingError("Parallelitätsoptimierung fehlgeschlagen.")
    rms, parameters = min(fits, key=lambda value: value[0])
    check_limit(rms, config["MESS_MAX_RMS_PX"], "Messmarker")
    center = parameters[:3]
    psi = parameters[3]
    R_marker_camera = R_ground_camera @ rotation_z(psi)
    rvec_marker = cv2.Rodrigues(R_marker_camera)[0].reshape(3)
    return {"position_ground_mm": center, "height_raw_mm": float(center[2]),
            "rotation_in_ground_plane_deg": (math.degrees(psi) + 180) % 360 - 180,
            "reprojection_rms_px": rms, "rvec_marker_to_camera": rvec_marker,
            "tvec_marker_to_camera_mm": R_ground_camera @ center + t_ground_camera}


def analyze_observations(observations, spec_map, pose, K, D, config):
    records = []
    for observation in observations:
        record = observation.copy()
        spec = spec_map.get((observation["dictionary"], observation["id"]))
        if spec is None:
            record["status"] = "unconfigured"
        else:
            try:
                record["free_pose"], candidates = free_marker_pose(observation, spec, K, D, pose)
                if spec.role == "measurement":
                    record.update(fit_parallel_marker(observation, spec, pose, K, D, config, candidates))
                record["status"] = "ok"
            except (ProcessingError, cv2.error, RuntimeError) as exc:
                record.update(status="error", error=str(exc))
        records.append(record)
    return records


def zero_levels(config, specs, reference_records):
    valid_baselines = {(r["dictionary"], r["id"]): r["height_raw_mm"] for r in reference_records
                       if r["role"] == "measurement" and r["status"] == "ok"}
    levels = {}
    for spec in specs:
        if spec.role != "measurement":
            continue
        if spec.marker_id in config["NULL_OFFSETS_MM"]:
            levels[spec.key] = (float(config["NULL_OFFSETS_MM"][spec.marker_id]), "configured_per_id")
        elif config["NULL_MODUS"] == "konfiguriert":
            levels[spec.key] = (float(config["NULL_OFFSET_MM"]), "configured_default")
        elif spec.key in valid_baselines:
            levels[spec.key] = (valid_baselines[spec.key], "reference_image")
    return levels


def apply_zero_levels(records, levels):
    for record in records:
        if record["role"] != "measurement" or record["status"] != "ok":
            continue
        key = (record["dictionary"], record["id"])
        if key not in levels:
            record.update(status="error", error="Nullhöhe fehlt: Marker im Referenzbild auflegen oder NULL_OFFSETS_MM setzen.")
        else:
            zero, source = levels[key]
            record.update(zero_mm=zero, zero_source=source, height_mm=record["height_raw_mm"] - zero)


def jsonable(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(v) for v in value]
    return value


def save_json(path, content):
    path.write_text(json.dumps(jsonable(content), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def annotate_image(image, report, K, D):
    output = image.copy()
    reference_rms = report.get("reference_control", {}).get("control_rms_px")
    title = f"Status: {report['status']}"
    if reference_rms is not None:
        title += f" | Referenz-RMS: {reference_rms:.2f} px"
    if report.get("warnings"):
        title += " | Qualitaetshinweis: siehe Bericht"
    cv2.putText(output, title, (25, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                (40, 40, 230) if report["status"] == "error" else (0, 140, 255), 2, cv2.LINE_AA)
    pose_data = report.get("reference_pose")
    if pose_data:
        cv2.drawFrameAxes(output, K, D, np.array(pose_data["rvec_ground_to_camera"]),
                          np.array(pose_data["tvec_ground_to_camera_mm"]), 30.0, 2)
    for record in report.get("markers", []):
        points = np.rint(record["corners_px"]).astype(np.int32)
        role, status = record["role"], record.get("status", "unprocessed")
        color = (70, 180, 70) if role == "measurement" and status == "ok" else (230, 150, 40)
        if status == "error":
            color = (40, 40, 230)
        cv2.polylines(output, [points], True, color, 2, cv2.LINE_AA)
        short_name = record["dictionary"].removeprefix("DICT_")
        lines = [f"{short_name}:{record['id']}  {role}"]
        if "height_mm" in record:
            lines.append(f"h={record['height_mm']:.3f} mm  RMS={record['reprojection_rms_px']:.2f}px")
        elif "diagnostic_height_mm" in record:
            lines.append(f"Diagnose h={record['diagnostic_height_mm']:.3f} mm - UNGUELTIG")
        elif status == "error":
            lines.append("FEHLER: siehe TXT/JSON")
        x = max(0, min(int(points[:, 0].min()), output.shape[1] - 20))
        y = max(20, int(points[:, 1].min()) - 8)
        text_width = max(cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0][0] for line in lines)
        x = max(0, min(x, output.shape[1] - text_width - 8))
        y = min(y, output.shape[0] - 8 - 20 * (len(lines) - 1))
        cv2.rectangle(output, (x, y - 16), (x + text_width + 8, y + 20 * (len(lines)-1) + 5), (20, 20, 20), -1)
        for i, line in enumerate(lines):
            cv2.putText(output, line, (x + 3, y + i * 20), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (245, 245, 245), 1, cv2.LINE_AA)
    return output


def write_frame_outputs(directory, prefix, report, image, K, D, config):
    save_json(directory / f"{prefix}.json", report)
    lines = [f"Bild: {report['image']}", f"Status: {report['status']}"]
    if "error" in report:
        lines.append("Fehler: " + report["error"])
    lines.extend("HINWEIS: " + warning for warning in report.get("warnings", []))
    if report.get("reference_pose"):
        lines.append("Referenzquelle: " + report["reference_pose"]["source"])
        for label in ("T_ground_to_camera", "T_camera_to_ground"):
            lines.extend([label + ":", np.array2string(np.asarray(report["reference_pose"][label]), precision=6)])
    lines.append("Referenzkontrolle: " + str(report.get("reference_control")))
    for record in report.get("markers", []):
        lines.extend(["", f"{record['dictionary']} ID {record['id']} | {record['role']} | {record.get('status', 'unprocessed')}",
                      f"Kantenlaenge_mm: {record['size_mm']}", "Bildecken_px:"])
        lines.extend(f"  Ecke {i}: {p[0]:.4f}, {p[1]:.4f}" for i, p in enumerate(record["corners_px"]))
        if record.get("aliases"):
            lines.append("Weitere Dekodierungen derselben Flaeche: " + str(record["aliases"]))
        if "free_pose" in record:
            free = record["free_pose"]
            lines.extend(["Freie Pose Kameraposition_mm: " + str(jsonable(free["tvec_marker_to_camera_mm"])),
                          "Freie Pose Grundposition_mm: " + str(jsonable(free["center_ground_mm"])),
                          f"Freie Pose RMS_px: {free['reprojection_rms_px']:.6f}"])
        if "position_ground_mm" in record:
            lines.extend(["Parallelitaetsmodell Grundposition_mm: " + str(jsonable(record["position_ground_mm"])),
                          f"Rohhoehe_mm: {record['height_raw_mm']:.6f}",
                          f"Parallelitaetsmodell RMS_px: {record['reprojection_rms_px']:.6f}"])
        if "height_mm" in record:
            lines.extend([f"Nullhoehe_mm: {record['zero_mm']:.6f} ({record['zero_source']})",
                          f"Korrigierte_Hoehe_mm: {record['height_mm']:.6f}"])
        if "diagnostic_height_mm" in record:
            lines.append(f"Diagnostische_Hoehe_mm: {record['diagnostic_height_mm']:.6f} (UNGUELTIG)")
        if "error" in record:
            lines.append("Fehler: " + record["error"])
    (directory / f"{prefix}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if config["ANNOTIERTE_BILDER"] and image is not None:
        ok, encoded = cv2.imencode(".png", annotate_image(image, report, K, D))
        if not ok:
            raise ProcessingError("Markiertes Ergebnisbild konnte nicht kodiert werden.")
        encoded.tofile(directory / f"{prefix}_markiert.png")


def measurement_rows(report, specs):
    by_key = {(r["dictionary"], r["id"]): r for r in report.get("markers", [])}
    rows = []
    for spec in specs:
        if spec.role != "measurement":
            continue
        record = by_key.get(spec.key)
        row = {"bild": str(report["image"]), "bibliothek": spec.dictionary, "id": spec.marker_id,
               "groesse_mm": spec.size_mm, "status": "missing", "fehler": "Marker nicht erkannt"}
        if report["status"] == "error":
            row.update(status="error", fehler=report.get("error", "Aufnahme nicht auswertbar"))
        if record:
            if report["status"] != "error":
                row.update(status=record.get("status", "unprocessed"), fehler=record.get("error", ""))
            elif record.get("status") == "error":
                row["fehler"] = record.get("error", row["fehler"])
            if "position_ground_mm" in record:
                row.update(zip(("x_mm", "y_mm", "z_roh_mm"), record["position_ground_mm"]))
                row.update(rms_px=record["reprojection_rms_px"], drehung_grad=record["rotation_in_ground_plane_deg"])
            if "height_mm" in record:
                row.update(hoehe_mm=record["height_mm"], nullhoehe_mm=record["zero_mm"], nullquelle=record["zero_source"])
            if "diagnostic_height_mm" in record:
                row["diagnostische_hoehe_mm"] = record["diagnostic_height_mm"]
        if report.get("reference_pose"):
            row["referenzquelle"] = report["reference_pose"]["source"]
        row["referenz_rms_px"] = report.get("reference_control", {}).get("control_rms_px")
        row["hinweise"] = " | ".join(report.get("warnings", []))
        rows.append(row)
    return rows


def write_csv(path, rows, decimal_comma):
    columns = ["bild", "bibliothek", "id", "groesse_mm", "status", "hoehe_mm", "x_mm", "y_mm",
               "z_roh_mm", "nullhoehe_mm", "nullquelle", "drehung_grad", "rms_px", "referenzquelle",
               "referenz_rms_px", "diagnostische_hoehe_mm", "hinweise", "fehler"]
    def value(v):
        if isinstance(v, (float, np.floating)):
            text = f"{v:.6f}"
            return text.replace(".", ",") if decimal_comma else text
        return v
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: value(v) for key, v in row.items()})


def run_pipeline(config):
    """Auswerten und Ergebnisordner zurückgeben; auch aus anderem Python nutzbar."""
    config = copy.deepcopy(config)
    for key in ("REFERENZBILD", "MESSBILDER", "AUSGABEORDNER",
                "KAMERAMATRIX_DATEI", "VERZEICHNUNG_DATEI"):
        if not config.get(key):
            raise ProcessingError(f"CONFIG[{key!r}] fehlt. Bitte lokale Eingaben konfigurieren.")
    specs = build_specs(config)
    spec_map = {s.key: s for s in specs}
    geometry, geometry_details = build_reference_geometry(config, specs)
    K, D, image_size = load_calibration(config)
    calibration_rms = None
    calibration_path = resolve_path(config["KAMERAMATRIX_DATEI"])
    if calibration_path.suffix.lower() == ".npz":
        with np.load(calibration_path, allow_pickle=False) as calibration_data:
            if "rms_px" in calibration_data:
                calibration_rms = finite_number(calibration_data["rms_px"].item(), "Kalibrierungs-RMS")
    common_warnings = []
    if calibration_rms is not None and calibration_rms > 0.5:
        common_warnings.append(f"Kalibrierungs-RMS {calibration_rms:.3f} px > 0.5 px; "
                               "Höhen sind Schätzwerte, nicht in Millimetern validiert.")
    if (config["NULL_MODUS"] == "konfiguriert" and config["NULL_OFFSET_MM"] == 0
            and not config["NULL_OFFSETS_MM"]):
        common_warnings.append("Ohne gemessene Nullhöhe: Abstand der Markeroberfläche "
                               "zur Referenzfläche einschließlich Papier/Träger.")
    expected_measurements = sum(s.role == "measurement" for s in specs)
    reference_path = resolve_path(config["REFERENZBILD"])
    reference_image = read_image(reference_path, image_size)
    image_paths = measurement_paths(config["MESSBILDER"])
    detectors = make_detectors(specs, config)
    reference_observations = detect_markers(reference_image, detectors, spec_map)
    base_pose = estimate_reference_pose(reference_observations, geometry, K, D, config, "reference_image")

    directory = resolve_path(config["AUSGABEORDNER"]) / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    directory.mkdir(parents=True, exist_ok=False)
    logger = logging.getLogger("aruco_" + directory.name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    file_handler = logging.FileHandler(directory / "lauf.log", encoding="utf-8")
    stream_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    reports, rows = [], []
    try:
        logger.info("Version %s | Ergebnisordner: %s", VERSION, directory)
        logger.info("Referenz: %s | IDs %s | RMS %.4f px | Modus %s", reference_path,
                    base_pose.reference_ids, base_pose.rms_px, config["REFERENZPOSE_MODUS"])
        for warning in common_warnings:
            logger.warning(warning)
        save_json(directory / "konfiguration.json", config)
        reference_records = analyze_observations(reference_observations, spec_map, base_pose, K, D, config)
        levels = zero_levels(config, specs, reference_records)
        apply_zero_levels(reference_records, levels)
        reference_report = {"image": reference_path, "status": "reference", "reference_pose": base_pose.as_dict(),
                            "reference_control": {"visible_reference_ids": base_pose.reference_ids,
                                                   "control_rms_px": base_pose.rms_px, "control_available": True},
                            "markers": reference_records, "warnings": common_warnings.copy()}
        write_frame_outputs(directory, "000_referenz", reference_report, reference_image, K, D, config)
        for spec in specs:
            if spec.role == "measurement" and spec.key not in levels:
                logger.warning("Keine Nullhöhe für %s:%s; diese ID wird nicht korrigiert ausgegeben.", *spec.key)
        for index, path in enumerate(image_paths, 1):
            report = {"image": path, "status": "error", "markers": [], "warnings": common_warnings.copy()}
            image = None
            try:
                image = read_image(path, image_size)
                observations = detect_markers(image, detectors, spec_map)
                report["markers"] = [dict(o, status="unprocessed") for o in observations]
                # Zunächst rechnen; die harte Referenzprüfung entwertet unten
                # sämtliche Messhöhen, bewahrt aber die Diagnosewerte auf.
                pose, control = select_reference_pose(observations, base_pose, geometry, K, D, config,
                                                       enforce_quality=False)
                report.update(reference_pose=pose.as_dict(), reference_control=control)
                records = analyze_observations(observations, spec_map, pose, K, D, config)
                apply_zero_levels(records, levels)
                reference_error = apply_reference_quality(records, control, config)
                warning_limit = config.get("REF_KONTROLLE_WARN_RMS_PX")
                if (warning_limit is not None and control["control_rms_px"] is not None
                        and control["control_rms_px"] > warning_limit):
                    warning = (f"Referenz-RMS {control['control_rms_px']:.3f} px "
                               f"> Warnschwelle {warning_limit:.3f} px.")
                    report["warnings"].append(warning)
                    logger.warning("%s | %s", path.name, warning)
                report["markers"] = records
                seen_measurements = [r for r in records if r["role"] == "measurement"]
                valid = [r for r in seen_measurements if r["status"] == "ok"]
                if reference_error:
                    report.update(status="error", error=reference_error)
                elif not seen_measurements:
                    report.update(status="no_measurements", error="Keine konfigurierten Messmarker erkannt.")
                elif not valid:
                    report.update(status="error", error="Alle erkannten Messmarker sind nicht auswertbar.")
                else:
                    report["status"] = "ok" if len(valid) == expected_measurements else "partial"
                logger.info("%s | %s | %d gültige Höhen | Referenzquelle %s", path.name,
                            report["status"], len(valid), pose.source)
                for record in valid:
                    logger.info("  %s:%s | h=%.6f mm | RMS=%.4f px", record["dictionary"], record["id"],
                                record["height_mm"], record["reprojection_rms_px"])
                for record in records:
                    if record.get("status") == "error":
                        logger.warning("  %s:%s | %s", record["dictionary"], record["id"], record["error"])
            except Exception as exc:
                report.update(status="error", error=str(exc))
                logger.exception("Bild nicht auswertbar: %s", path)
            prefix = f"{index:03d}_" + re.sub(r"[^\w.-]+", "_", path.stem)[:100]
            write_frame_outputs(directory, prefix, report, image, K, D, config)
            reports.append(report)
            rows.extend(measurement_rows(report, specs))
        write_csv(directory / "hoehen.csv", rows, config["CSV_DEZIMALKOMMA"])
        summary = {"version": VERSION, "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "run_directory": directory, "reference_pose_mode": config["REFERENZPOSE_MODUS"],
                   "calibration": {"K": K, "D": D, "image_size": image_size, "rms_px": calibration_rms,
                                   "camera_matrix_file": calibration_path,
                                   "dist_coeffs_file": resolve_path(config["VERZEICHNUNG_DATEI"])},
                   "warnings": common_warnings,
                   "reference_geometry": geometry_details, "reference_pose": base_pose.as_dict(),
                   "zero_levels": [{"dictionary": key[0], "id": key[1], "zero_mm": v[0], "source": v[1]}
                                   for key, v in levels.items()],
                   "images_total": len(reports), "valid_measurements": sum(r["status"] == "ok" for r in rows),
                   "images_error": sum(r["status"] == "error" for r in reports),
                   "images_incomplete": sum(r["status"] != "ok" for r in reports),
                   "reports": reports}
        save_json(directory / "lauf.json", summary)
        text = [f"ArUco-Verarbeitung {VERSION}", f"Messbilder: {len(reports)}",
                f"Gueltige Hoehen: {summary['valid_measurements']}",
                f"Referenzpose: {config['REFERENZPOSE_MODUS']}; Messmarker parallel zur Grundebene.",
                "RMS-Pixelwerte sind keine Messunsicherheit in Millimetern.",
                *common_warnings, ""]
        for row in rows:
            h = f"{row['hoehe_mm']:.6f} mm" if "hoehe_mm" in row else "keine Hoehe"
            if "diagnostische_hoehe_mm" in row:
                h = f"Diagnose {row['diagnostische_hoehe_mm']:.6f} mm (UNGUELTIG)"
            text.append(f"{Path(row['bild']).name} | {row['bibliothek']}:{row['id']} | {row['status']} | {h} | {row['fehler']}")
        (directory / "zusammenfassung.txt").write_text("\n".join(text) + "\n", encoding="utf-8")
        logger.info("Fertig: %d gültige Höhen, %d fehlerhafte Messbilder.",
                    summary["valid_measurements"], summary["images_error"])
        return summary
    finally:
        for handler in (file_handler, stream_handler):
            logger.removeHandler(handler)
            handler.close()


def main():
    try:
        result = run_pipeline(CONFIG)
    except Exception as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    return 0 if result["valid_measurements"] and not result["images_incomplete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
