#!/usr/bin/env python3
"""Kamerakalibrierung mit einem lokal definierten ChArUco-Board.

Python >= 3.10, OpenCV >= 4.10. Beispiele und Aufnahmehinweise: README.md.
Es werden originale, nicht bereits entzerrte Bilder erwartet.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ========================= KONFIGURATION =========================
# Pfade und Boarddefinition sind absichtlich nicht vorbelegt.
# Angaben lokal setzen oder die entsprechenden CLI-Optionen verwenden.
# Relative Konfigurationspfade beziehen sich auf den Skriptordner.
INPUT_DIR = None
OUTPUT_DIR = None
BOARD_SQUARES_X = None
BOARD_SQUARES_Y = None
BOARD_SQUARE_MM = None
BOARD_MARKER_MM = None
BOARD_DICTIONARY = None
# Gleiche verbesserte Markerdecodierung wie im Rechenskript:
# feinere Abtastung des perspektivisch entzerrten Bitrasters (Standard: 4).
# Das Originalbild und seine Pixelkoordinaten werden dabei nicht skaliert.
DETEKTOR_PIXEL_PRO_ZELLE = 8
# ================================================================

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit("Abhaengigkeiten fehlen: python -m pip install -r requirements.txt") from exc

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
from calibration_spatial import compute_spatial_quality, spatial_options, write_spatial_outputs

VERSION = "1.2.0"


def calibrated_detection_supported():
    return tuple(int(x) for x in cv2.__version__.split(".")[:2]) >= (4, 10)


@dataclass
class View:
    path: Path
    objects: np.ndarray
    points: np.ndarray
    ids: np.ndarray
    area_fraction: float
    sharpness: float


def make_board(square_mm, marker_mm, squares_x, squares_y, dictionary_name):
    """Laengen intern in Metern; Marker-IDs werden aufsteigend vergeben."""
    if (not dictionary_name.startswith("DICT_")
            or not hasattr(cv2.aruco, dictionary_name)):
        raise RuntimeError(f"Unbekannte Markerbibliothek: {dictionary_name!r}")
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary_name))
    return cv2.aruco.CharucoBoard((squares_x, squares_y), square_mm / 1000,
                                 marker_mm / 1000, dictionary)


def make_detector(board, camera_matrix=None, dist_coeffs=None):
    if not calibrated_detection_supported():
        raise RuntimeError("OpenCV >= 4.10 erforderlich: python -m pip install --upgrade -r requirements.txt")
    if type(DETEKTOR_PIXEL_PRO_ZELLE) is not int or DETEKTOR_PIXEL_PRO_ZELLE < 1:
        raise RuntimeError("DETEKTOR_PIXEL_PRO_ZELLE muss eine positive ganze Zahl sein.")
    params = cv2.aruco.DetectorParameters()
    params.markerBorderBits = 1
    params.perspectiveRemovePixelPerCell = DETEKTOR_PIXEL_PRO_ZELLE
    # ChArUco-Schachbrettecken werden durch detectBoard subpixelgenau verfeinert.
    # Separate ArUco-Eckenverfeinerung kann benachbarte Schachbrettfelder erfassen.
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_NONE
    charuco = cv2.aruco.CharucoParameters()
    # Explizite leere Matrizen statt versionsabhaengiger None-Defaultwerte.
    charuco.cameraMatrix = np.empty((0, 0), dtype=np.float64)
    charuco.distCoeffs = np.empty((0, 0), dtype=np.float64)
    if camera_matrix is not None:
        charuco.cameraMatrix = camera_matrix
        charuco.distCoeffs = dist_coeffs
    return cv2.aruco.CharucoDetector(board, charuco, params)


def read_image(path):
    # fromfile/imdecode unterstuetzen auch Unicode-Dateipfade unter Windows.
    # EXIF-Rotation NICHT anwenden: alle Punkte bleiben im Sensor-Bildsystem.
    flags = cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, flags) if data.size else None
    except (OSError, cv2.error):
        return None


def write_image(path, image):
    ok, encoded = cv2.imencode(path.suffix, image)
    if not ok:
        raise RuntimeError(f"Bild konnte nicht gespeichert werden: {path}")
    encoded.tofile(str(path))


def find_images(folder, recursive=False):
    if not folder.is_dir():
        raise RuntimeError(f"Bildordner fehlt: {folder}")
    entries = folder.rglob("*") if recursive else folder.iterdir()
    return sorted((p for p in entries if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
                  key=lambda p: str(p).lower())


def detect_view(path, image, board, detector, min_corners):
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corners, ids, marker_corners, marker_ids = detector.detectBoard(grey)
    count = 0 if ids is None else len(ids)
    markers = 0 if marker_ids is None else len(marker_ids)
    info = {"file": str(path), "corners": count, "markers": markers}
    if count < min_corners:
        return None, {**info, "status": "rejected", "reason": f"weniger als {min_corners} Ecken"}, (corners, ids, marker_corners, marker_ids)
    objects, points = board.matchImagePoints(corners, ids)
    # Eigene Datenkopien behalten; keine OpenCV-internen Puffer ueber Bilder hinweg.
    objects = np.array(objects, dtype=np.float32, copy=True, order="C").reshape(-1, 3)
    points = np.array(points, dtype=np.float32, copy=True, order="C").reshape(-1, 1, 2)
    if np.linalg.matrix_rank(objects[:, :2] - objects[:, :2].mean(axis=0)) < 2:
        return None, {**info, "status": "rejected", "reason": "kollineare Ecken"}, (corners, ids, marker_corners, marker_ids)
    hull = cv2.convexHull(points)
    area = float(cv2.contourArea(hull)) / (grey.shape[0] * grey.shape[1])
    # Nur Diagnose; keine kameraübergreifend sinnvolle Schaerfe-Schwelle.
    x, y, w, h = cv2.boundingRect(hull)
    sharpness = float(cv2.Laplacian(grey[y:y+h, x:x+w], cv2.CV_64F).var())
    info.update(area_percent=100 * area, sharpness=sharpness)
    if area < 0.005:
        return None, {**info, "status": "rejected", "reason": "Eckenflaeche kleiner als 0.5% des Bildes"}, (corners, ids, marker_corners, marker_ids)
    return View(path, objects, points, ids, area, sharpness), {**info, "status": "detected", "reason": ""}, (corners, ids, marker_corners, marker_ids)


def collect_views(paths, board, detector, min_corners, expected_size=None, debug_dir=None):
    views, rows, hashes = [], [], set()
    corner_count = len(board.getChessboardCorners())
    image_size = expected_size
    for index, path in enumerate(paths):
        image = read_image(path)
        if image is None:
            rows.append({"file": str(path), "status": "rejected", "reason": "Bild nicht lesbar"})
            continue
        size = (image.shape[1], image.shape[0])
        if image_size is None:
            image_size = size
        if size != image_size:
            raise RuntimeError(f"Gemischte Aufloesungen: {path.name} hat {size}, erwartet {image_size}. "
                               "Pro Aufloesung separat kalibrieren; Bilder nicht automatisch skalieren.")
        digest = hashlib.sha256(image.tobytes()).digest()
        if digest in hashes:
            rows.append({"file": str(path), "status": "rejected", "reason": "identische Bildpixel / Duplikat"})
            continue
        hashes.add(digest)
        view, row, detected = detect_view(path, image, board, detector, min_corners)
        rows.append(row)
        if view is not None:
            views.append(view)
        print(f"[{index+1}/{len(paths)}] {path.name}: {row.get('corners', 0)}/{corner_count} Ecken, {row['status']}")
        if debug_dir is not None:
            corners, ids, marker_corners, marker_ids = detected
            if marker_ids is not None:
                cv2.aruco.drawDetectedMarkers(image, marker_corners, marker_ids)
            if ids is not None:
                cv2.aruco.drawDetectedCornersCharuco(image, corners, ids)
            cv2.putText(image, f"{row['status']}: {row.get('corners', 0)}/{corner_count} corners",
                        (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            write_image(debug_dir / f"{index+1:04d}_{path.stem}.png", image)
    return views, rows, image_size


def fit_camera(views, image_size, model="standard"):
    if model not in ("standard", "fix_k3"):
        raise ValueError("Unbekanntes Kalibrierungsmodell.")
    flags = cv2.CALIB_FIX_K3 if model == "fix_k3" else 0
    result = cv2.calibrateCameraExtended(
        [v.objects for v in views], [v.points for v in views], image_size,
        None, None, flags=flags,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-9))
    rms, matrix, distortion, rvecs, tvecs, std_intr, _, per_view = result
    if not all(np.isfinite(a).all() for a in (rms, matrix, distortion, std_intr, per_view)):
        raise RuntimeError("Kalibrierung liefert nicht-endliche Werte. Aufnahmegeometrie pruefen.")
    if matrix[0, 0] <= 0 or matrix[1, 1] <= 0:
        raise RuntimeError("Ungueltige Brennweiten. Mehr geneigte und unterschiedlich positionierte Bilder aufnehmen.")
    return {"rms": float(rms), "matrix": matrix, "distortion": distortion,
            "rvecs": rvecs, "tvecs": tvecs, "std_intr": std_intr.reshape(-1),
            "per_view": per_view.reshape(-1), "model": model, "flags": flags}


def outlier_limit(errors, minimum_limit):
    median = float(np.median(errors))
    mad = float(np.median(np.abs(errors - median)))
    return max(minimum_limit, median + 3 * 1.4826 * mad)


def remove_outliers(views, image_size, fit, min_images, minimum_limit):
    """Optional: maximal 10% / drei Bilder; jede Entfernung dokumentieren."""
    history = []
    budget = min(3, math.floor(len(views) * 0.1), len(views) - min_images)
    for _ in range(budget):
        limit = outlier_limit(fit["per_view"], minimum_limit)
        worst = int(np.argmax(fit["per_view"]))
        error = float(fit["per_view"][worst])
        if error <= limit:
            break
        removed = views[worst]
        history.append({"file": str(removed.path), "rms_px_before_removal": error, "threshold_px": limit})
        print(f"Ausreisser entfernt: {removed.path.name}, RMS {error:.3f} px (Schwelle {limit:.3f} px)")
        views = views[:worst] + views[worst+1:]
        fit = fit_camera(views, image_size, fit.get("model", "standard"))
    return views, fit, history


def quality_summary(views, image_size, fit, minimum_limit):
    width, height = image_size
    points = np.concatenate([v.points.reshape(-1, 2) for v in views])
    cells = np.clip(np.floor(points / [width, height] * [4, 3]).astype(int), [0, 0], [3, 2])
    occupied = len(set(map(tuple, cells)))
    hull_fraction = float(cv2.contourArea(cv2.convexHull(points.astype(np.float32)))) / (width * height)
    tilts, normals = [], []
    for rvec in fit["rvecs"]:
        rotation, _ = cv2.Rodrigues(rvec)
        normal = rotation[:, 2]
        if normal[2] < 0:
            normal = -normal
        normals.append(normal)
        tilts.append(float(np.degrees(np.arccos(np.clip(normal[2], -1, 1)))))
    separation = max(float(np.degrees(np.arccos(np.clip(np.dot(a, b), -1, 1))))
                     for a in normals for b in normals)
    matrix = fit["matrix"]
    deviations = fit["std_intr"]
    warnings = []
    if len(views) < 20:
        warnings.append("Weniger als 20 Bilder; empfohlen sind 25-40 verschiedene Ansichten.")
    if fit["rms"] > 0.5:
        warnings.append("Gesamt-RMS > 0.5 px: Schaerfe, Planheit, Boardmasse und Kameramodus pruefen.")
    if occupied < 10 or hull_fraction < 0.55:
        warnings.append("Bildflaeche unzureichend abgedeckt: Board auch an Raendern und in Ecken aufnehmen.")
    if separation < 20:
        warnings.append("Geringe Vielfalt der Boardnormalen: um beide Achsen neigen, nicht nur in der Ebene drehen.")
    if max(tilts) < 20:
        warnings.append("Kaum geneigte Ansichten: weitere Aufnahmen mit etwa 20-45 Grad Neigung machen.")
    if deviations[0] / matrix[0, 0] > 0.02 or deviations[1] / matrix[1, 1] > 0.02:
        warnings.append("Relative Brennweiten-Standardabweichung > 2%; Geometrie und Anzahl der Bilder verbessern.")
    if not (0 <= matrix[0, 2] <= width and 0 <= matrix[1, 2] <= height):
        warnings.append("Hauptpunkt liegt ausserhalb des Bildes; Kalibrierung ist verdaechtig.")
    if not (0.1 * width < matrix[0, 0] < 10 * width and 0.1 * height < matrix[1, 1] < 10 * height):
        warnings.append("Ungewoehnliche Brennweiten in Pixeln; Kameramodell / Aufnahmen pruefen.")
    limit = outlier_limit(fit["per_view"], minimum_limit)
    flagged = [str(v.path) for v, error in zip(views, fit["per_view"]) if error > limit]
    if flagged:
        warnings.append(f"{len(flagged)} Aufnahme(n) mit auffaelligem Reprojektionsfehler; CSV / Debugbilder ansehen.")
    return {"occupied_grid_cells": occupied, "grid_cells_total": 12,
            "corner_hull_image_fraction": hull_fraction, "max_tilt_deg": max(tilts),
            "normal_separation_deg": separation, "outlier_threshold_px": limit,
            "flagged_images": flagged, "warnings": warnings,
            "note": "Heuristische Diagnosen. Ein kleiner Pixel-RMS belegt keine Hoehengenauigkeit in Millimetern."}


def validate_images(paths, board, fit, size, min_corners, training_paths, spatial=None, cached=None):
    training_hashes = set()
    for path in training_paths:
        image = read_image(path)
        if image is not None:
            training_hashes.add(hashlib.sha256(image.tobytes()).digest())
    if cached is None:
        views, rows, _ = collect_views(paths, board, make_detector(board, fit["matrix"], fit["distortion"]),
                                       min_corners, expected_size=size)
    else:
        views, rows = cached
        rows = [dict(row) for row in rows]
    errors, count, accepted, rvecs, tvecs = [], 0, [], [], []
    for view in views:
        image = read_image(view.path)
        if image is None:
            raise RuntimeError(f"Validierungsbild nicht mehr lesbar: {view.path}")
        if view.path.resolve() in training_paths or hashlib.sha256(image.tobytes()).digest() in training_hashes:
            raise RuntimeError("Validierungsordner enthaelt ein Kalibrierungsbild. Separate neue Aufnahmen verwenden.")
        found, rvec, tvec = cv2.solvePnP(view.objects, view.points, fit["matrix"], fit["distortion"])
        row = next(r for r in rows if r["file"] == str(view.path))
        if not found:
            row.update(status="rejected", reason="solvePnP fehlgeschlagen")
            continue
        predicted, _ = cv2.projectPoints(view.objects, rvec, tvec, fit["matrix"], fit["distortion"])
        squared = np.sum((predicted.reshape(-1, 2) - view.points.reshape(-1, 2)) ** 2, axis=1)
        rms = float(np.sqrt(np.mean(squared)))
        errors.extend(squared.tolist())
        count += 1
        accepted.append(view)
        rvecs.append(rvec)
        tvecs.append(tvec)
        row.update(status="validated", rms_px=rms)
    report = {"images_used": count, "rms_px": float(np.sqrt(np.mean(errors))) if errors else None,
              "views": rows, "note": "Intrinsische Parameter fixiert; pro Bild Pose angepasst. Kein Test der metrischen Hoehengenauigkeit."}
    report["spatial_quality"] = compute_spatial_quality(accepted, size,
        dict(matrix=fit["matrix"], distortion=fit["distortion"], rvecs=rvecs, tvecs=tvecs),
        source="validation", **(spatial or {}))
    return report


def write_csv(path, rows):
    columns = ["file", "status", "reason", "corners", "markers", "area_percent", "sharpness", "rms_px", "tilt_deg"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def save_calibration(output, board, fit, size, views, rows, args, quality, history, validation):
    matrix, distortion = fit["matrix"], fit["distortion"]
    spatial_report = compute_spatial_quality(views, size, fit, **spatial_options(args))
    write_spatial_outputs(output, spatial_report)
    if validation is not None:
        write_spatial_outputs(output, validation["spatial_quality"], "validation_spatial_quality")
    np.savez_compressed(output / "camera_calibration.npz", camera_matrix=matrix,
                        dist_coeffs=distortion, image_size=np.array(size, dtype=np.int32),
                        rms_px=np.array(fit["rms"]))
    data = {"schema_version": 1, "script_version": VERSION, "opencv_version": cv2.__version__,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "camera_label": args.camera_label, "image_size": list(size),
            "image_width": size[0], "image_height": size[1],
            "camera_matrix": matrix.tolist(), "dist_coeffs": distortion.reshape(-1).tolist(),
            "distortion_model": "opencv_pinhole_5", "distortion_order": ["k1", "k2", "p1", "p2", "k3"],
            "board": {"squares_x": args.squares_x, "squares_y": args.squares_y,
                      "square_length_m": args.square_mm / 1000,
                      "marker_length_m": args.marker_mm / 1000, "dictionary": args.dictionary,
                      "marker_ids": board.getIds().reshape(-1).tolist(),
                      "charuco_corners": len(board.getChessboardCorners())},
            "rms_px": fit["rms"], "images_used": len(views), "calibration_flags": fit.get("flags", 0),
            "calibration_model": fit.get("model", "standard"), "spatial_quality": spatial_report,
            "detector_parameters": {"perspectiveRemovePixelPerCell": DETEKTOR_PIXEL_PRO_ZELLE,
                                    "markerBorderBits": 1, "cornerRefinementMethod": "CORNER_REFINE_NONE"},
            "intrinsic_std_deviations": fit["std_intr"].tolist(),
            "intrinsic_std_order": ["fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6", "s1", "s2", "s3", "s4", "tau_x", "tau_y"],
            "quality": quality, "removed_outliers": history, "validation": validation,
            "views": [{"file": str(v.path), "rms_px": float(error), "rvec": r.reshape(-1).tolist(),
                       "tvec_m": t.reshape(-1).tolist()} for v, error, r, t in
                      zip(views, fit["per_view"], fit["rvecs"], fit["tvecs"])],
            "pose_convention": "X_camera = R_board_to_camera @ X_board + t_board_to_camera",
            "input_convention": "original non-undistorted pixels; EXIF rotation ignored"}
    (output / "camera_calibration.json").write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    storage = cv2.FileStorage(str(output / "camera_calibration.yml"), cv2.FILE_STORAGE_WRITE)
    if not storage.isOpened():
        raise RuntimeError("OpenCV-YAML konnte nicht geoeffnet werden; ggf. Ausgabepfad ohne Sonderzeichen verwenden.")
    try:
        storage.write("image_width", size[0])
        storage.write("image_height", size[1])
        storage.write("camera_matrix", matrix)
        storage.write("dist_coeffs", distortion)
        storage.write("rms_px", fit["rms"])
    finally:
        storage.release()
    used = {str(v.path): (float(error), float(tilt)) for v, error, tilt in
            zip(views, fit["per_view"], [np.degrees(np.arccos(np.clip(abs(cv2.Rodrigues(r)[0][2, 2]), 0, 1))) for r in fit["rvecs"]])}
    removed = {h["file"]: h for h in history}
    for row in rows:
        if row["file"] in used:
            row.update(status="used", rms_px=used[row["file"]][0], tilt_deg=used[row["file"]][1])
        elif row["file"] in removed:
            row.update(status="outlier_removed", reason="optionale Ausreisserfilterung", rms_px=removed[row["file"]]["rms_px_before_removal"])
    write_csv(output / "image_report.csv", rows)
    if validation is not None:
        write_csv(output / "validation_report.csv", validation["views"])
    lines = [f"ChArUco-Kalibrierung {VERSION}", f"Kamera: {args.camera_label or '(ohne Bezeichnung)'}",
             f"Aufloesung: {size[0]} x {size[1]}", f"Verwendete Bilder: {len(views)}",
             f"Gesamt-RMS: {fit['rms']:.4f} px", f"Bildzellen: {quality['occupied_grid_cells']}/12",
             f"Groesste Neigung: {quality['max_tilt_deg']:.1f} Grad",
             f"Normalen-Winkelspanne: {quality['normal_separation_deg']:.1f} Grad", "",
             "Hinweise:", *quality["warnings"], "",
             "Kleiner Reprojektionsfehler ist keine Zusicherung einer Hoehengenauigkeit.",
             "Kameramodus, Objektiv, Fokus, Zoom, Crop und Aufloesung muessen zur Messung passen."]
    if validation is not None:
        lines.append(f"Separate Validierung: {validation['images_used']} Bilder; RMS {validation['rms_px']} px")
    lines.extend(["", "Raeumlicher Aufnahmeindikator: spatial_quality.png / .csv / .json",
                  "Zeilen von oben nach unten, Spalten von links nach rechts; Originalpixel."])
    for cell in spatial_report["cells"]:
        if cell["status"] != "observed":
            lines.append(f"  R{cell['row']} C{cell['column']}: " + " ".join(cell["recommendations"]))
    (output / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def capture_images(args, board):
    args.images.mkdir(parents=True, exist_ok=True)
    camera = cv2.VideoCapture(args.camera)
    if not camera.isOpened():
        raise RuntimeError(f"Kamera {args.camera} konnte nicht geoeffnet werden.")
    if args.width:
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    detector = make_detector(board)
    print("Aufnahme: LEERTASTE/S = originales Bild speichern, ESC/Q = beenden und kalibrieren.")
    print("Fokus/Zoom vorher im Kameratreiber einstellen und fixieren. Angeforderte Aufloesung wird geprueft.")
    saved, actual_size = 0, None
    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                raise RuntimeError("Kamera liefert kein Bild.")
            size = (frame.shape[1], frame.shape[0])
            if actual_size is None:
                actual_size = size
                print(f"Tatsaechliche Kamera-Aufloesung: {size[0]} x {size[1]}")
                if args.width and size != (args.width, args.height):
                    raise RuntimeError("Kamera hat die angeforderte Aufloesung nicht uebernommen.")
            elif size != actual_size:
                raise RuntimeError("Kamera hat die Aufloesung waehrend der Aufnahme veraendert.")
            corners, ids, _, _ = detector.detectBoard(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            preview = frame.copy()
            count = 0 if ids is None else len(ids)
            if ids is not None:
                cv2.aruco.drawDetectedCornersCharuco(preview, corners, ids)
            cv2.putText(preview, f"Corners {count}/{len(board.getChessboardCorners())} | saved {saved} | SPACE save | Q finish",
                        (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 255), 2)
            scale = min(1.0, 1280 / preview.shape[1], 800 / preview.shape[0])
            if scale < 1:
                preview = cv2.resize(preview, None, fx=scale, fy=scale)
            cv2.imshow("ChArUco calibration capture", preview)
            key = cv2.waitKey(20) & 0xff
            if key in (27, ord("q")) or cv2.getWindowProperty("ChArUco calibration capture", cv2.WND_PROP_VISIBLE) < 1:
                break
            if key in (32, ord("s")):
                view, _, _ = detect_view(Path("capture"), frame, board, detector, args.min_corners)
                if view is None:
                    print("Nicht gespeichert: Board zu klein / zu wenige geeignete Ecken.")
                    continue
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                write_image(args.images / f"capture_{stamp}.png", frame)
                saved += 1
    finally:
        camera.release()
        cv2.destroyAllWindows()


def configured_path(value):
    """Relative Konfigurationspfade unabhaengig vom Arbeitsordner aufloesen."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(__file__).resolve().parent / path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images", type=Path, default=configured_path(INPUT_DIR) if INPUT_DIR else None,
                        required=not bool(INPUT_DIR), help="Bildordner; alternativ INPUT_DIR lokal setzen")
    parser.add_argument("--output", type=Path, default=configured_path(OUTPUT_DIR) if OUTPUT_DIR else None,
                        required=not bool(OUTPUT_DIR), help="NEUER Ausgabeordner; alternativ OUTPUT_DIR lokal setzen")
    parser.add_argument("--camera-label", default="", help="Optionale lokale Kamerabezeichnung")
    parser.add_argument("--recursive", action="store_true", help="Unterordner der Eingabebilder einlesen")
    parser.add_argument("--squares-x", type=int, default=BOARD_SQUARES_X,
                        required=BOARD_SQUARES_X is None, help="Anzahl Quadrate in X-Richtung")
    parser.add_argument("--squares-y", type=int, default=BOARD_SQUARES_Y,
                        required=BOARD_SQUARES_Y is None, help="Anzahl Quadrate in Y-Richtung")
    parser.add_argument("--square-mm", type=float, default=BOARD_SQUARE_MM,
                        required=BOARD_SQUARE_MM is None, help="Tatsaechliche Quadratseitenlaenge in mm")
    parser.add_argument("--marker-mm", type=float, default=BOARD_MARKER_MM,
                        required=BOARD_MARKER_MM is None, help="Markerseitenlaenge inkl. schwarzem Rand in mm")
    parser.add_argument("--dictionary", default=BOARD_DICTIONARY,
                        required=BOARD_DICTIONARY is None, help="OpenCV-Markerbibliothek des Boards")
    parser.add_argument("--min-corners", type=int, default=12, help="Mindestzahl ChArUco-Ecken pro Bild (Standard: 12)")
    parser.add_argument("--min-images", type=int, default=12, help="Mindestzahl geeigneter Bilder (Standard: 12; empfohlen 25-40)")
    parser.add_argument("--save-debug", action="store_true", help="Bilder mit eingezeichneten Erkennungen speichern")
    parser.add_argument("--single-pass", action="store_true", help="Zweiten Erkennungslauf mit vorlaeufigen Kameraparametern auslassen")
    parser.add_argument("--reject-outliers", action="store_true", help="Optional maximal 10%% / drei Ausreisserbilder entfernen")
    parser.add_argument("--outlier-px", type=float, default=1.0, help="Untere Ausreisserschwelle in Pixeln (Standard: 1.0)")
    parser.add_argument("--validation-images", type=Path, help="Separater Ordner neuer Boardbilder fuer Reprojektionspruefung")
    parser.add_argument("--model", choices=("standard", "fix_k3", "compare"), default="standard",
                        help="5 Parameter, k3=0, oder Auswahl anhand separater Validierung")
    parser.add_argument("--spatial-grid", type=int, nargs=2, default=(6, 4), metavar=("SPALTEN", "ZEILEN"))
    parser.add_argument("--spatial-min-views", type=int, default=3)
    parser.add_argument("--spatial-min-corners", type=int, default=12)
    parser.add_argument("--spatial-min-tilt-span", type=float, default=10.)
    parser.add_argument("--spatial-max-rms-px", type=float, default=1.)
    parser.add_argument("--capture", action="store_true", help="Vorher Bilder direkt von einer Kamera aufnehmen (GUI erforderlich)")
    parser.add_argument("--camera", type=int, default=0, help="Kameraindex fuer --capture")
    parser.add_argument("--width", type=int, help="Gewuenschte Kamera-Aufnahmebreite fuer --capture")
    parser.add_argument("--height", type=int, help="Gewuenschte Kamera-Aufnahmehoehe fuer --capture")
    args = parser.parse_args(argv)
    if args.model == "compare" and args.validation_images is None:
        parser.error("model=compare benoetigt separate validation-images.")
    if (any(not 1 <= n <= 20 for n in args.spatial_grid) or args.spatial_min_views < 1
            or args.spatial_min_corners < 1 or not math.isfinite(args.spatial_min_tilt_span)
            or not 0 <= args.spatial_min_tilt_span <= 180
            or not math.isfinite(args.spatial_max_rms_px) or args.spatial_max_rms_px <= 0):
        parser.error("Ungueltige Grenzen fuer den Raumindikator.")
    if not (0 < args.marker_mm < args.square_mm and math.isfinite(args.square_mm)):
        parser.error("Es muss 0 < marker-mm < square-mm gelten; beide Werte muessen endlich sein.")
    if args.squares_x < 3 or args.squares_y < 3:
        parser.error("squares-x und squares-y muessen mindestens 3 sein.")
    max_corners = (args.squares_x - 1) * (args.squares_y - 1)
    if not 4 <= args.min_corners <= max_corners or args.min_images < 8:
        parser.error(f"min-corners: 4-{max_corners}; min-images: mindestens 8 (Standard 12).")
    if not args.dictionary.startswith("DICT_") or not hasattr(cv2.aruco, args.dictionary):
        parser.error("dictionary muss eine vorhandene OpenCV-Markerbibliothek benennen.")
    if not math.isfinite(args.outlier_px) or args.outlier_px <= 0:
        parser.error("outlier-px muss positiv und endlich sein.")
    if (args.width is None) != (args.height is None) or (args.width is not None and (args.width <= 0 or args.height <= 0)):
        parser.error("width und height muessen gemeinsam angegeben und positiv sein.")
    return args


def main(argv=None):
    args = parse_args(argv)
    if not calibrated_detection_supported() or not hasattr(cv2, "aruco") or not hasattr(cv2.aruco, "CharucoDetector"):
        raise RuntimeError("OpenCV >= 4.10 mit aruco erforderlich: python -m pip install --upgrade -r requirements.txt")
    args.images, args.output = args.images.resolve(), args.output.resolve()
    if args.output.exists():
        raise RuntimeError(f"Ausgabeordner existiert bereits: {args.output}. Bitte neuen --output waehlen.")
    if args.output == args.images or args.output in args.images.parents or args.images in args.output.parents:
        raise RuntimeError("Bild- und Ausgabeordner muessen getrennt und duerfen nicht ineinander liegen.")
    board = make_board(args.square_mm, args.marker_mm, args.squares_x, args.squares_y, args.dictionary)
    if args.capture:
        capture_images(args, board)
    paths = find_images(args.images, args.recursive)
    if not paths:
        raise RuntimeError("Keine Bilder gefunden. PNG/JPG/TIFF/BMP/WebP verwenden; HEIC vorher konsistent konvertieren.")
    args.output.mkdir(parents=True)
    debug_dir = args.output / "detections_initial" if args.save_debug else None
    if debug_dir is not None:
        debug_dir.mkdir()
    print(f"OpenCV {cv2.__version__}; Board {args.squares_x}x{args.squares_y} / "
          f"{args.square_mm:g} / {args.marker_mm:g} mm / {args.dictionary}")
    print(f"Markerdecodierung: {DETEKTOR_PIXEL_PRO_ZELLE} Pixel pro Zelle")
    views, rows, size = collect_views(paths, board, make_detector(board), args.min_corners, debug_dir=debug_dir)
    write_csv(args.output / "initial_detection_report.csv", rows)
    if len(views) < args.min_images:
        raise RuntimeError(f"Nur {len(views)} geeignete Bilder, mindestens {args.min_images} erforderlich. "
                           "initial_detection_report.csv pruefen; weitere geneigte Aufnahmen erstellen.")
    training_model = "fix_k3" if args.model == "fix_k3" else "standard"
    fit = fit_camera(views, size, training_model)
    initial_rms = fit["rms"]
    if not args.single_pass:
        print(f"Vorlaeufiger RMS {initial_rms:.4f} px. Zweite Erkennung mit Kameraparametern ...")
        if args.save_debug:
            debug_dir = args.output / "detections_final"
            debug_dir.mkdir()
        refined, refined_rows, _ = collect_views([v.path for v in views], board,
                                               make_detector(board, fit["matrix"], fit["distortion"]),
                                               args.min_corners, expected_size=size, debug_dir=debug_dir)
        updates = {r["file"]: r for r in refined_rows}
        rows = [updates.get(r["file"], r) for r in rows]
        write_csv(args.output / "image_report.csv", rows)
        if len(refined) < args.min_images:
            raise RuntimeError("Zweite Erkennung hat zu wenige geeignete Bilder. Berichte pruefen und Aufnahmen verbessern.")
        views, fit = refined, fit_camera(refined, size, training_model)
    before_filter_rms = fit["rms"]
    history = []
    if args.reject_outliers:
        views, fit, history = remove_outliers(views, size, fit, args.min_images, args.outlier_px)
    validation = None
    if args.validation_images is not None:
        validation_paths = find_images(args.validation_images.resolve(), args.recursive)
        if not validation_paths:
            raise RuntimeError("Validierungsordner enthaelt keine Bilder.")
        cached_views, cached_rows, _ = collect_views(validation_paths, board,
            make_detector(board, fit["matrix"], fit["distortion"]), args.min_corners, expected_size=size)
        candidates = []
        for model in (("standard", "fix_k3") if args.model == "compare" else (training_model,)):
            candidate = fit if model == training_model else fit_camera(views, size, model)
            checked = validate_images(validation_paths, board, candidate, size, args.min_corners,
                                      set(paths), spatial_options(args), (cached_views, cached_rows))
            if checked["images_used"] == 0:
                raise RuntimeError("Kein geeignetes Validierungsbild gefunden; Ordner / Board pruefen.")
            if args.model == "compare" and checked["images_used"] != len(cached_views):
                raise RuntimeError("Modellvergleich braucht erfolgreiche Posen fuer dieselben Pruefaufnahmen.")
            candidates.append((candidate, checked))
        fit, validation = min(candidates, key=lambda pair: pair[1]["rms_px"])
        if args.model == "compare":
            comparison = dict(selected_model=fit["model"], selection="lowest_validation_rms_px",
                note="Gleiche Trainingsbilder und gleiche Validierungs-Bildecken fuer beide Modelle. "
                     "Die zur Auswahl genutzte Validierung ist kein unabhaengiger Abschlusstest.",
                candidates=[dict(model=candidate["model"], training_rms_px=candidate["rms"],
                                 validation_rms_px=checked["rms_px"], images=checked["images_used"])
                            for candidate, checked in candidates])
            (args.output / "model_comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
            print("Modell anhand separater Aufnahmen ausgewaehlt:", fit["model"])
        if validation["images_used"] == 0:
            raise RuntimeError("Kein geeignetes Validierungsbild gefunden; Ordner / Board pruefen.")
    quality = quality_summary(views, size, fit, args.outlier_px)
    quality.update(initial_pass_rms_px=initial_rms, before_outlier_removal_rms_px=before_filter_rms)
    save_calibration(args.output, board, fit, size, views, rows, args, quality, history, validation)
    print(f"\nFertig: {len(views)} Bilder; RMS {fit['rms']:.4f} px; Ausgabe: {args.output}")
    print("Kameramatrix:\n", fit["matrix"])
    print("Verzerrung [k1, k2, p1, p2, k3]:", fit["distortion"].reshape(-1))
    for warning in quality["warnings"]:
        print("HINWEIS:", warning)
    print("Die Kameraparameter gelten fuer diesen Kameramodus. Hoehen separat an bekannten Stufen validieren.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, cv2.error) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Abgebrochen.", file=sys.stderr)
        sys.exit(130)
