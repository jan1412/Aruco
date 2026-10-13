"""Spatial coverage and reprojection diagnostics in original sensor pixels."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import cv2
import numpy as np


STATUS = {
    "unobserved": ("Keine Daten", (95, 95, 95)),
    "few_samples": ("Mehr Aufnahmen", (40, 150, 230)),
    "low_tilt_span": ("Anders neigen", (60, 205, 225)),
    "high_error": ("Fehler pruefen", (70, 70, 215)),
    "observed": ("Beobachtet", (100, 160, 65)),
}


def spatial_options(args):
    return dict(grid=tuple(args.spatial_grid), min_views=args.spatial_min_views,
                min_corners=args.spatial_min_corners,
                min_normal_span_deg=args.spatial_min_tilt_span,
                max_rms_px=args.spatial_max_rms_px)


def compute_spatial_quality(views, image_size, fit, *, grid=(6, 4), min_views=3,
                            min_corners=12, min_normal_span_deg=10., max_rms_px=1.,
                            source="training"):
    width, height = image_size
    cols, rows = grid
    if (width <= 0 or height <= 0 or not 1 <= cols <= 20 or not 1 <= rows <= 20
            or min_views < 1 or min_corners < 1
            or not math.isfinite(max_rms_px) or max_rms_px <= 0
            or not math.isfinite(min_normal_span_deg) or not 0 <= min_normal_span_deg <= 180):
        raise ValueError("Ungueltige Grenzen fuer den Raumindikator.")
    if len(views) != len(fit["rvecs"]) or len(views) != len(fit["tvecs"]):
        raise ValueError("Ansichten und Kalibrierungsposen passen nicht zusammen.")
    samples = [[] for _ in range(cols * rows)]
    image_normals = [{} for _ in samples]
    outside = 0
    for index, (view, rvec, tvec) in enumerate(zip(views, fit["rvecs"], fit["tvecs"])):
        observed = np.asarray(view.points).reshape(-1, 2)
        predicted = cv2.projectPoints(view.objects, rvec, tvec, fit["matrix"],
                                      fit["distortion"])[0].reshape(-1, 2)
        if not np.isfinite(observed).all() or not np.isfinite(predicted).all():
            raise ValueError("Nicht-endliche Bildecken im Raumindikator.")
        normal = cv2.Rodrigues(rvec)[0][:, 2]
        if normal[2] < 0:
            normal = -normal
        for point, error in zip(observed, observed - predicted):
            x, y = point
            if not (0 <= x < width and 0 <= y < height):
                outside += 1
                continue
            cell = int(y * rows / height) * cols + int(x * cols / width)
            samples[cell].append(error)
            image_normals[cell][index] = normal
    cells = []
    for index, errors in enumerate(samples):
        row, col = divmod(index, cols)
        normals = list(image_normals[index].values())
        span = max((float(np.degrees(np.arccos(np.clip(np.dot(a, b), -1, 1))))
                    for a in normals for b in normals), default=0.)
        count, images = len(errors), len(normals)
        error_array = np.asarray(errors).reshape(-1, 2)
        rms = float(np.sqrt(np.mean(np.sum(error_array ** 2, axis=1)))) if count else None
        mean = error_array.mean(axis=0).tolist() if count else [None, None]
        reasons = []
        if not count:
            status = "unobserved"
            reasons.append("Schachbrettecken in diesen Bildbereich bringen.")
        else:
            if images < min_views or count < min_corners:
                reasons.append("Weitere scharfe Aufnahmen in diesem Bereich machen.")
            if span < min_normal_span_deg:
                reasons.append("Board hier um unterschiedliche Achsen neigen.")
            if rms > max_rms_px:
                reasons.append("Schaerfe, Planheit, Druckmasse und Verzerrungsmodell pruefen.")
            status = ("high_error" if rms > max_rms_px else
                      "few_samples" if images < min_views or count < min_corners else
                      "low_tilt_span" if span < min_normal_span_deg else "observed")
        cells.append(dict(row=row + 1, column=col + 1,
                          x_min_px=col * width / cols, x_max_px=(col + 1) * width / cols,
                          y_min_px=row * height / rows, y_max_px=(row + 1) * height / rows,
                          corners=count, images=images, rms_px=rms,
                          mean_dx_px=mean[0], mean_dy_px=mean[1], normal_span_deg=span,
                          status=status, recommendations=reasons))
    return dict(schema_version=1, source=source, image_size=list(image_size),
                grid=list(grid), thresholds=dict(min_views=min_views, min_corners=min_corners,
                min_normal_span_deg=min_normal_span_deg, max_rms_px=max_rms_px),
                outside_image_corners=outside, cells=cells,
                note="Heuristischer Aufnahmeindikator. Unbeobachtete Zellen werden nicht interpoliert. "
                "Trainingsfehler sind keine unabhaengige Validierung; Pixel-RMS belegt keine mm-Genauigkeit.")


def render_spatial_quality(report):
    """Three aligned panels; missing observations stay visibly missing."""
    canvas = np.full((850, 1920, 3), 245, np.uint8)
    width, height = report["image_size"]
    cols, rows = report["grid"]
    scale = min(570 / width, 430 / height)
    plot_w, plot_h = int(width * scale), int(height * scale)
    cv2.putText(canvas, "Kalibrierung: Aufnahmebedarf und lokale Projektionsfehler", (35, 40),
                cv2.FONT_HERSHEY_SIMPLEX, .85, (30, 30, 30), 2, cv2.LINE_AA)
    label = "Separate Validierung" if report["source"] == "validation" else "Kalibrierungsbilder (Training)"
    cv2.putText(canvas, label + " | Originales Kamerabild: x nach rechts, y nach unten",
                (35, 75), cv2.FONT_HERSHEY_SIMPLEX, .6, (45, 45, 45), 1, cv2.LINE_AA)
    values = [cell["rms_px"] for cell in report["cells"] if cell["rms_px"] is not None]
    rms_max = max(2 * report["thresholds"]["max_rms_px"], max(values, default=0.))
    images_max = max(1, max(cell["images"] for cell in report["cells"]))
    titles = ["Aufnahmehinweise", "Bilder / Ecken pro Zelle", f"RMS [px], Farbskala 0 - {rms_max:.2f}"]
    palette = cv2.applyColorMap(np.arange(256, dtype=np.uint8).reshape(-1, 1), cv2.COLORMAP_VIRIDIS)
    for panel in range(3):
        ox, oy = 35 + panel * 635, 145
        cv2.putText(canvas, titles[panel], (ox, 115), cv2.FONT_HERSHEY_SIMPLEX,
                    .65, (30, 30, 30), 1, cv2.LINE_AA)
        for cell in report["cells"]:
            col, row = cell["column"] - 1, cell["row"] - 1
            x1, x2 = ox + round(col * plot_w / cols), ox + round((col + 1) * plot_w / cols)
            y1, y2 = oy + round(row * plot_h / rows), oy + round((row + 1) * plot_h / rows)
            if panel == 0:
                color = STATUS[cell["status"]][1]
                text = f"R{row+1} C{col+1}"
            elif not cell["corners"]:
                color, text = (95, 95, 95), "--"
            else:
                value = cell["images"] / images_max if panel == 1 else cell["rms_px"] / rms_max
                color = tuple(int(c) for c in palette[min(255, round(255 * value)), 0])
                text = f"{cell['images']} / {cell['corners']}" if panel == 1 else f"{cell['rms_px']:.2f}"
            cv2.rectangle(canvas, (x1, y1), (x2, y2), color, -1)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (235, 235, 235), 1)
            if cols <= 8 and rows <= 8:
                tw = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, .4, 1)[0][0]
                cv2.putText(canvas, text, ((x1 + x2 - tw) // 2, (y1 + y2) // 2 + 5),
                            cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1, cv2.LINE_AA)
        for fraction in (0., .5, 1.):
            cv2.putText(canvas, str(round(width * fraction)), (ox + round(plot_w * fraction) - 10, oy + plot_h + 23),
                        cv2.FONT_HERSHEY_SIMPLEX, .4, (40, 40, 40), 1, cv2.LINE_AA)
        cv2.putText(canvas, f"y: 0 oben, {height} unten; x [px]", (ox, oy + plot_h + 49),
                    cv2.FONT_HERSHEY_SIMPLEX, .45, (40, 40, 40), 1, cv2.LINE_AA)
    for index, (text, color) in enumerate(STATUS.values()):
        x = 35 + index * 370
        cv2.rectangle(canvas, (x, 660), (x + 24, 684), color, -1)
        cv2.putText(canvas, text, (x + 32, 678), cv2.FONT_HERSHEY_SIMPLEX,
                    .5, (30, 30, 30), 1, cv2.LINE_AA)
    limits = report["thresholds"]
    cv2.putText(canvas, f"Heuristik: mind. {limits['min_views']} Bilder, {limits['min_corners']} Ecken, "
                f"{limits['min_normal_span_deg']:g} Grad Normalenspanne; RMS-Grenze {limits['max_rms_px']:g} px.",
                (35, 730), cv2.FONT_HERSHEY_SIMPLEX, .6, (30, 30, 30), 1, cv2.LINE_AA)
    cv2.putText(canvas, "Grau: keine Aussage zum lokalen Fehler. Gruen: Kriterien erfuellt, keine Genauigkeitsgarantie.",
                (35, 775), cv2.FONT_HERSHEY_SIMPLEX, .6, (30, 30, 30), 1, cv2.LINE_AA)
    cv2.putText(canvas, "Erkannte Schachbrettecken gezielt in markierte Bereiche bringen; dort auch unterschiedlich neigen.",
                (35, 815), cv2.FONT_HERSHEY_SIMPLEX, .6, (30, 30, 30), 1, cv2.LINE_AA)
    return canvas


def write_spatial_outputs(output, report, prefix="spatial_quality"):
    output = Path(output)
    (output / (prefix + ".json")).write_text(json.dumps(report, ensure_ascii=False,
                indent=2, allow_nan=False), encoding="utf-8")
    columns = [key for key in report["cells"][0] if key != "recommendations"]
    with (output / (prefix + ".csv")).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns + ["recommendations"], delimiter=";")
        writer.writeheader()
        for cell in report["cells"]:
            writer.writerow({**cell, "recommendations": " | ".join(cell["recommendations"])})
    ok, encoded = cv2.imencode(".png", render_spatial_quality(report))
    if not ok:
        raise RuntimeError("Raumindikator konnte nicht als PNG gespeichert werden.")
    encoded.tofile(output / (prefix + ".png"))
