"""Chain series averaging and height measurement through module-qualified APIs."""
from __future__ import annotations

import argparse
from pathlib import Path

import aruco_serienmittelung
import aruco_verarbeitung


def run(series_config_path, measurement_config_path):
    series_config = aruco_serienmittelung.load_configuration(series_config_path)
    series = aruco_serienmittelung.serien_auswerten(series_config)
    if series["status"] != "ok":
        raise RuntimeError("Serienpruefung fehlgeschlagen; Bericht in " + series["run_directory"])
    measurement_config = aruco_verarbeitung.load_configuration(measurement_config_path)
    measurement_config.update(series["processing_config_paths"])
    return aruco_verarbeitung.run_pipeline(measurement_config)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gepruefte Serienmittelbilder fuer die Hoehenmessung verwenden.")
    parser.add_argument("--series-config", type=Path, required=True)
    parser.add_argument("--measurement-config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run(args.series_config, args.measurement_config)
    except (ValueError, RuntimeError, OSError) as exc:
        print("FEHLER:", exc)
        return 1
    print("Hoehenergebnisse:", report["run_directory"])
    return 0 if report["valid_measurements"] and not report["images_incomplete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
