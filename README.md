# Aruco

Python-Skripte für ChArUco-Kamerakalibrierung und ArUco-Höhenmessung relativ
zu einer Referenzebene. Messmarker werden als parallel zur Referenzebene
modelliert. Die Ausgabe umfasst CSV, JSON, Textberichte und optional Bilder
mit eingezeichneten Markern.

Neue Funktionen: gemeinsame parallele Messebenen, bekannte Hoehenkontrollen,
Referenzmarker auf verschiedenen Hoehen, robuste Messoptimierung und raeumliche
Kalibrierungsindikatoren. Der [Optimierungs-Guide](OPTIMIERUNGEN.md) beschreibt
die Funktionen, benoetigte Masse, Einstellungen und aktive Aufnahmeschritte.

## Installation

Python 3.10 oder neuer verwenden und die Abhängigkeiten in einer eigenen
Python-Umgebung installieren:

```sh
python -m pip install -r requirements.txt
```

`requirements.txt` legt die getestete Paketversion
`opencv-contrib-python==4.14.0.94` fest; `cv2.__version__` zeigt dafür
`4.14.0` an. Für die Einzelinstallation von OpenCV:

```sh
python -m pip install "opencv-contrib-python==4.14.0.94"
```

Das [Contrib-Paket](https://pypi.org/project/opencv-contrib-python/4.14.0.94/)
enthält die benötigten ArUco/ChArUco-Funktionen. Nur eine OpenCV-Paketvariante
pro Umgebung installieren, da alle den `cv2`-Namensraum verwenden.

## Kamerakalibrierung

`calibrate_charuco.py` bestimmt Kameramatrix, Verzerrung und Bildauflösung
aus Originalaufnahmen eines ChArUco-Boards. Bild- und Ausgabeordner sowie
die Boarddefinition sind absichtlich nicht vorbelegt. Sie können lokal im
Konfigurationsblock gesetzt oder beim Start angegeben werden.

```sh
python calibrate_charuco.py --help
```

Aufrufschema: Die Großbuchstaben sind Platzhalter und müssen durch eigene
Pfade beziehungsweise die tatsächlichen Boardwerte ersetzt werden:

```sh
python calibrate_charuco.py --images BILDORDNER --output NEUER_AUSGABEORDNER --squares-x QUADRATE_X --squares-y QUADRATE_Y --square-mm QUADRAT_MM --marker-mm MARKER_MM --dictionary OPENCV_DICTIONARY --save-debug
```

Die Markerkantenlänge umfasst den schwarzen Rand. Das Dictionary muss zum
Ausdruck passen; die Marker-IDs des Boards sind aufsteigend, beginnend mit 0.
Quadrate in X und Y müssen jeweils mindestens drei sein. Standardmäßig werden
mindestens zwölf innere Ecken pro Aufnahme verlangt; bei einem kleineren Board
`--min-corners` entsprechend anpassen, mindestens vier.

Der Ausgabeordner muss neu sein. Darin entstehen `camera_calibration.npz`,
JSON/YML-Dateien, Bildberichte und eine Qualitätszusammenfassung. Die NPZ
enthält `camera_matrix`, `dist_coeffs`, `image_size` und `rms_px`. Längen der
Kalibrierungsposen sind in Metern, die Intrinsik in Pixeln.

Die zweite Erkennung nutzt vorläufige Kameraparameter. Mit `--reject-outliers`
lassen sich begrenzt auffällige Aufnahmen entfernen; alle Entfernungen werden
dokumentiert. `--validation-images` erlaubt die Reprojektionsprüfung mit
separaten Aufnahmen. `--capture` bietet eine Webcam-Aufnahmefunktion.
Jeder Lauf erzeugt `spatial_quality.png`, CSV und JSON mit lokalen
Aufnahmehinweisen. Mit separaten Pruefbildern entsteht eine zusaetzliche
Validierungskarte. `--model compare --validation-images PRUEFBILDER` vergleicht
das Standardmodell und ein Modell mit festem `k3=0` auf denselben Pruefecken.

## Höhenmessung

Vor dem Start von `aruco_verarbeitung.py` die lokale `CONFIG` ausfüllen:

- `REFERENZBILD`, `MESSBILDER`, `AUSGABEORDNER`: eigene Eingaben und Ausgabeziel.
  Messbilder können als Ordner, Einzelpfade oder Globs angegeben werden.
- `KAMERAMATRIX_DATEI`, `VERZEICHNUNG_DATEI`: Kalibrierdateien. Bei einer
  gemeinsamen NPZ denselben Pfad in beide Felder eintragen. Die vorbelegten
  NPZ-Schlüssel passen zum Kalibrierungsskript. Alternativ sind NPY-Dateien
  möglich; dann `KALIBRIER_AUFLOESUNG` selbst setzen.
- `REFERENZ_IDS`, `MESS_IDS`, die zugehörigen Dictionaries und Markergrößen:
  tatsächliche Markerdefinitionen. Größen, Dictionaries und Nullhöhen können
  zusätzlich pro ID überschrieben werden.
- `REFERENZ_POSITIONEN_MM`: Markerzentren als gerichtete `(x, y)`-Koordinaten;
  `REFERENZ_WINKEL_GRAD`: Drehung jedes Referenzmarkers in der Referenzebene.
  Optional unterstützt `REFERENZ_LAYOUT_MODUS` auch ein Raster oder vollständige
  Paarabstände. Die dafür vorgesehenen Felder sind ebenfalls in `CONFIG`.
- Nullkorrektur und Qualitätsgrenzen: dem eigenen Aufbau und der Bildauflösung
  anpassen. Ohne bekannte Nullhöhe wird die Markeroberfläche einschließlich
  Papier-/Trägerdicke gemessen.

```sh
python aruco_verarbeitung.py
```

Alle Pfadfelder und aufbauspezifischen Geometriewerte sind leer beziehungsweise
`None`. Ein nicht konfigurierter Start bricht mit einer erklärenden Meldung ab.
Relative Konfigurationspfade beziehen sich auf den Skriptordner.

`REFERENZPOSE_MODUS="pro_bild"` schätzt die Referenzpose bei ausreichend
sichtbaren Referenzmarkern je Aufnahme. Andernfalls wird die Pose des
Referenzbildes verwendet und anhand sichtbarer Referenzen kontrolliert.
`"fest"` verwendet durchgehend die Pose des Referenzbildes.

Jeder Lauf erhält einen eigenen Ausgabeordner. In `hoehen.csv` stehen nur
akzeptierte Höhen in `hoehe_mm`. Bei zu hohem Referenzfehler bleibt diese
Spalte leer; berechnete Werte werden ausdrücklich als ungültige Diagnose in
`diagnostische_hoehe_mm` aufbewahrt. Warnungen, Posen und Bildecken stehen
zusätzlich in den Berichten. Exitcode 0 bedeutet vollständige Auswertung,
1 einen Startfehler und 2 mindestens ein unvollständiges oder verworfenes Bild.

## Qualität und lokale Daten

Fokus, Objektiv, Zoom, Zuschnitt und Aufnahmemodus müssen zur Kalibrierung
passen. Fotos nicht individuell skalieren oder drehen; EXIF-Drehung wird
ignoriert. Board und Referenzfläche müssen eben sein, Druckmaße nachgemessen
werden. Mehrere scharfe, unterschiedlich geneigte Ansichten über die gesamte
Bildfläche verbessern die Kalibrierung.

Reprojektionsfehler in Pixeln belegen keine Höhengenauigkeit in Millimetern.
Die gesamte Messkette mit bekannten Höhen und Wiederholungsmessungen prüfen.

Das Repository enthält ausschließlich allgemeinen Code, Dokumentation und
synthetische Tests. Eigene Bilder, Kalibrierparameter, Ergebnisse, Konfigurationen
mit lokalen Pfaden und Zugangsdaten gehören nicht in einen öffentlichen Commit.
`.gitignore` schließt übliche lokale Daten aus; Änderungen an den Skript-Configs
vor einem Commit trotzdem prüfen.

## Tests

```sh
python -m unittest -v
```

Die Tests verwenden künstliche Posen, Kameraparameter und Geometrien. Sie prüfen
bekannte Höhen, Posekorrektur, Qualitätsverwerfung, Auflösungsprüfung sowie
eine frei gewählte ChArUco-Boarddefinition und deren Kalibrierexport.
Weitere Tests pruefen gemeinsame Messebenen, nicht-koplanare Referenzen,
Kontrolloffsets, Fehlerverwerfung, raeumliche Indikatoren und Modellvergleich.

Technische Grundlage:
[OpenCV ChArUco](https://docs.opencv.org/4.x/df/d4a/tutorial_charuco_detection.html),
[OpenCV Kamerakalibrierung](https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html),
[OpenCV Poseberechnung](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html).
