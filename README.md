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

Empfohlen: `config.example.json` nach `config.local.json` kopieren und die
Ein-/Ausgaben sowie Markerdefinitionen dort eintragen:

```sh
python aruco_verarbeitung.py --config config.local.json
```

Relative Pfade beziehen sich auf den Ordner der JSON-Config. UTF-8-Dateien mit
oder ohne BOM werden unterstützt. Die lokale Config wird nicht veröffentlicht.

Folgende Felder in der JSON-Config oder im Python-Block `CONFIG` ausfüllen:

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
Relative Pfade im Python-Konfigurationsblock beziehen sich auf den Skriptordner.

Der Python-Konfigurationsblock ist die Alternative zur externen JSON-Datei.

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

## Live-Hoehenmessung mit USB-Kamera

Das optionale [Live-Addon](README_LIVE.md) verarbeitet kontinuierlich Frames einer
USB-Kamera, z. B. der Insta360 Link 2 Pro. Es verwendet dieselben
Marker- und Hoehenmodelle wie `aruco_verarbeitung.py` und zeigt im Kamerabild
Marker-IDs, Hoehen, Referenz-RMS, Sichtbarkeit und Qualitaetswarnungen an.
Mit `R` wird eine Referenzaufnahme erstellt, mit `S` ein Messfoto mit
Auswertung und mit `F` ein Foto aus dem Videostream gespeichert.

```powershell
Copy-Item live.config.example.json live.config.local.json
python aruco_live.py --probe 5 --backend DSHOW
python aruco_live.py --config live.config.local.json
```

Die bestehende `config.local.json` muss vorher fuer Marker und Kalibrierung
eingerichtet sein. Die Live-Kameraauflösung muss zur Kalibrierung passen.
Unveraenderte Originalframes werden fuer Berechnung und PNG-Messfoto verwendet;
nur die Vorschau wird fuer die Anzeige skaliert. Qualitaetsstatus beruht auf
konfigurierten Pixel-Reprojektionsfehlern, nicht auf nachgewiesener
Millimeter-Messgenauigkeit. Details: [README_LIVE.md](README_LIVE.md).

## Serienmittelung

`aruco_serienmittelung.py` mittelt eine Referenzserie und getrennte Messserien.
`serien.config.example.json` als `serien.config.local.json` kopieren. Dort
`REFERENZ_ORDNER`, `MESS_ORDNER`, `AUSGABEORDNER` und die Markerdefinitionen
eintragen. Eingabe- und Ausgabeordner sind in der veröffentlichten Vorlage leer.

```sh
python aruco_serienmittelung.py --config serien.config.local.json
```

`MESS_ORDNER` enthält entweder eine Bildserie oder Unterordner mit getrennten
Serien. Beide Strukturen nicht mischen. Relative Ordner beziehen sich auf den
Ort der Config. Jeder Lauf erhält einen neuen Ergebnisordner mit unbeschrifteten
PNG-Mittelbildern und Qualitätsbericht. Geometrie und Auflösung bleiben erhalten.

`AUSGESCHLOSSENE_DATEIMUSTER` schließt standardmäßig `*_gemittelt*.png` aus,
damit vorherige Mittelbilder nicht nochmals als Eingang dienen. Die Prüfung
vergleicht zeitliche Markerecken, nicht den Reprojektionsfehler der Höhenmessung.
Eine verworfene Serie erzeugt kein freigegebenes Mittelbild. Details und aktive
Schritte stehen im [Guide](OPTIMIERUNGEN.md#10-serienmittelung-mit-eigener-inputoutput-config).

## Aufruf aus anderen Skripten

Das ganze Modul importieren und die Funktion über den Modulnamen aufrufen:

```python
import aruco_serienmittelung
import aruco_verarbeitung

serien_config = aruco_serienmittelung.load_configuration("serien.config.local.json")
serien = aruco_serienmittelung.serien_auswerten(serien_config)
if serien["status"] != "ok":
    raise RuntimeError("Serienprüfung fehlgeschlagen; Qualitätsbericht prüfen.")

mess_config = aruco_verarbeitung.load_configuration("config.local.json")
mess_config.update(serien["processing_config_paths"])
messung = aruco_verarbeitung.run_pipeline(mess_config)
```

`main()` dient dem CLI-Start. Für andere Skripte die oben genannten APIs nutzen;
sie lesen keine CLI-Argumente des aufrufenden Programms. Das ausführbare
`pipeline_example.py` zeigt diese Kopplung und stoppt bei verworfenen Serien:

```sh
python pipeline_example.py --series-config serien.config.local.json --measurement-config config.local.json
```

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


## Vermessenes Messraster (ab Version 2.3.0)

Optional kann die **relative Geometrie von Messmarkern auf einer gemeinsamen, starren
und zur Referenzebene parallelen Messoberflaeche** bekanntgegeben werden. Anders
als bei freien Einzelmarkerpositionen werden dann gemeinsame x/y-Verschiebung,
z-Hoehe und In-Plane-Drehung des gesamten Markerverbandes an **allen sichtbaren
Markerecken gleichzeitig** gefittet. Der bisherige Modus ohne Messlayout bleibt
unveraendert.

Beispiel: vier Messmarker, Mittelabstand 80 mm horizontal und 70 mm vertikal
(alle Zahlen sind gemessene **Markerzentrum-zu-Markerzentrum**-Abstaende):

```json
{
  "MESSMODELL": "gemeinsame_ebene",
  "MESSGRUPPEN": {"oberflaeche": [20, 21, 22, 23]},
  "MIN_MARKER_PRO_MESSEBENE": 2,
  "MESS_LAYOUTS": {
    "oberflaeche": {
      "modus": "raster",
      "raster_ids": [[20, 21], [22, 23]],
      "abstand_x_mm": 80.0,
      "abstand_y_mm": 70.0,
      "winkel_grad": {"20": 0, "21": 0, "22": 0, "23": 0}
    }
  }
}
```

Diesen Block zu `config.local.json` hinzufuegen, nicht in die Live-Config.
`MESS_IDS` und `MESS_GROESSEN_MM`/`MESS_GROESSE_MM` muessen zu den
tatsaechlichen Markern passen. JSON-ID-Schluessel werden beim Laden automatisch
in Ganzzahlen umgesetzt. Jede Gruppe in `MESS_LAYOUTS` muss als
`MESSGRUPPEN` existieren. Mehrere getrennte Oberflaechen koennen getrennte
Layouts besitzen.

Weitere Layout-Definitionen:

- `"modus": "positionen"`: `"positionen_mm": {"20":[0,0], "21":[80,0], ...}`
  fuer beliebige, vermessene XY-Markerzentren im **lokalen** Messraster.
- `"modus": "abstaende"`: `"abstaende_mm": [[20,21,80.0], ...]`.
  Bei drei oder mehr Markern sind **alle** Paarabstaende sowie
  `ursprung_id`, `x_achse_id`, `y_seite_id` erforderlich (wie bei
  Referenzabstaenden). Bei zwei Markern genuegt ihr einzelner Paarabstand.
  `max_abweichung_mm` begrenzt die geometrische Widerspruechlichkeit.
- `winkel_grad`: optionale gemessene Orientierung jedes Markers relativ zum
  **lokalen** Raster; fehlende Winkel werden als 0 Grad angenommen.

Die Referenz- und Messlayouts sind unabhaengig. Das Messraster darf als Ganzes
verschoben und in der Referenzebene gedreht sein. Das Verfahren setzt aber
**feste, starre Abstaende und eine gemeinsame parallele Ebene** voraus:
Bei Bewegung einzelner Marker gegeneinander oder schiefen/gebogenen
Oberflaechen ist das Rastermodell ungeeignet.

Die zusaetzlichen Layoutbedingungen reduzieren mathematische Freiheitsgrade,
garantieren jedoch **keine** bessere Hoehengenauigkeit. Schon kleine Fehler in
den eingegebenen Abstaenden oder relativen Winkeln koennen systematische
Hoehenfehler ausloesen. Das Reprojektions-RMS bleibt in den Berichten; zu
grosse Fehler werden verworfen. Vor/nach Aktivierung mit bekannten
Pruefhoehen und Wiederholmessungen vergleichen. `messebene.csv` kennzeichnet
`layout` und `messraster_starr`.

## Feste Referenzpose nur aus dem Referenzbild

Fuer das **Offline-Rechenskript** in `config.local.json` eintragen:

```json
{"REFERENZPOSE_MODUS": "fest"}
```

Damit wird die Pose aus `REFERENZBILD` fuer alle Messbilder verwendet.
Sofern Referenzmarker im Messbild sichtbar sind, werden diese weiterhin als
Qualitaetskontrolle gegen die feste Referenzpose ausgewertet. Fehlen sie,
findet im Offline-Modus keine entsprechende Kontrolle statt.

Fuer das **Live-Addon** zusaetzlich in `live.config.local.json` eintragen:

```json
{
  "REFERENZ_START": "bild",
  "REFERENZ_KONTROLLE_IM_MESSBILD": false
}
```

Das Live-Addon braucht dann keine sichtbaren Referenzmarker in den
Messbildern mehr und verwendet die fest gespeicherte Pose. Die Anzeige meldet
`FEST / UNGEPRUEFT`. **Jede Kamerabewegung seit dem Referenzbild ist in
diesem Modus unerkannt moeglich.** Fokus, Bildausschnitt, Zoom, Kalibrierung
und Aufloesung muessen identisch bleiben. Wer die Referenzmarker zur
laufenden Qualitaetskontrolle im Bild hat, sollte die Live-Kontrolle
`true` lassen.

