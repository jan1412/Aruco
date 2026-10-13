# Guide: Kalibrierung, Messebenen und bekannte Höhen

Die Erweiterungen bleiben optional. Der Standard wertet Messmarker einzeln
als parallel zur Grundebene aus. Eine gemeinsame Messebene hat ebenfalls eine
fest vorgegebene Normale zur Grundebene; eine frei geneigte Messebene wird nicht
geschätzt. Alle Messlängen sind in Millimetern, Kalibrierungsposen in Metern.

## 1. Lokale Konfiguration und Installation

```sh
python -m pip install -r requirements.txt
```

Die festgelegte OpenCV-Paketversion ist `opencv-contrib-python==4.14.0.94`.
Nur eine OpenCV-Paketvariante pro Python-Umgebung installieren.

`config.example.json` enthält leere Eingaben und allgemeine Einstellungen.
Eine Kopie als `config.local.json` anlegen und dort eigene Pfade, Marker und
Geometrien eintragen. Die lokale Datei wird durch `.gitignore` ausgeschlossen.
JSON verwendet doppelte Anführungszeichen und `null` statt Python-`None`.
Marker-IDs in Objekt-Schlüsseln werden als Zeichenketten geschrieben und beim
Laden in Ganzzahlen umgewandelt. Unbekannte Feldnamen werden abgelehnt.

```sh
python aruco_verarbeitung.py --config config.local.json
```

Relative Pfade und Bild-Globs in dieser Datei beziehen sich auf ihren Ordner.
Ohne `--config` gilt weiterhin der Python-Konfigurationsblock im Skript;
dessen relative Pfade beziehen sich auf den Skriptordner. Lokale Konfigurationen,
Fotos, Kalibrierparameter und Ergebnisberichte vor jedem öffentlichen Commit
prüfen. Berichte enthalten auch lokale Eingabepfade.

## 2. Einzelmarker und gemeinsame parallele Messebene

| Modus | Berechnung | Wann verwenden? |
|---|---|---|
| `einzelmarker` | Eigene X/Y/Z-Position und Drehung in der Ebene je Marker | Verschiedene Oberflächen oder unabhängige Messstellen |
| `gemeinsame_ebene` | Gemeinsame Rohhöhe Z je Gruppe; eigene X/Y-Positionen und Drehungen | Marker liegen tatsächlich auf derselben parallelen Oberfläche |

Für den zweiten Modus physisch sicherstellen, dass die Markerflächen einer
Gruppe auf derselben ebenen Oberfläche liegen und parallel zur Grundebene sind.
Unterschiedliche Objekte oder Höhen in getrennte Gruppen aufteilen.

Beispiel mit frei gewählten IDs; an den eigenen Ausdruck anpassen:

```json
{
  "MESSMODELL": "gemeinsame_ebene",
  "MESSGRUPPEN": {"oberflaeche": [20, 21]},
  "MIN_MARKER_PRO_MESSEBENE": 2,
  "MESSEBENE_MAX_EINZELHOEHENSPANNE_MM": 1.0
}
```

Diese Felder in die vollständige lokale Config übernehmen. Die IDs müssen in
`MESS_IDS` vorkommen und dürfen keine Höhenkontrollen sein. Gruppen sind disjunkt
und enthalten mindestens zwei Marker. Nicht gruppierte Messmarker bleiben
Einzelmessungen. Bei zu wenigen gültigen Gruppenmarkern wird die Gruppenmessung
verworfen; es erfolgt kein automatischer Rückfall auf eine gültige Einzelhöhe.

Die Optimierung verwendet die Bildecken aller gültigen Gruppenmarker, keine
nachträgliche Mittelung ihrer Höhen. Die Einzel-Rohhöhen bleiben zum Vergleich
gespeichert. `MESSEBENE_MAX_EINZELHOEHENSPANNE_MM` begrenzt deren Differenz vor
der Kopplung; `null` deaktiviert diese zusätzliche Schranke. Der Beispielwert
ist keine universelle Messgenauigkeit und muss an Vergleichsmessungen angepasst
werden. Auch nach der Kopplung gelten die Pixel-RMS-Grenzen für jeden Marker.

Die gemeinsame Höhe entsteht zunächst für die bedruckten Markeroberflächen.
Danach werden Nullhöhen und eine gegebenenfalls aktivierte Kontrollkorrektur
berücksichtigt. Unterschiedliche Nullhöhen können unterschiedliche korrigierte
Markerwerte ergeben; dann wird keine einzelne korrigierte Gruppenhöhe behauptet.

`hoehen.csv` enthält `messebene` und `einzelhoehe_roh_mm`. `messebene.csv`
enthält Gruppenhöhe, verwendete IDs, Fehler und Status. JSON und Textberichte
enthalten ebenfalls die Gruppen. Eine schlechte Referenzpose entwertet weiterhin
alle Höhen einschließlich der gemeinsamen Ebene.

## 3. Bekannte Höhen als Kontrolle

Einen starren, ebenen Träger verwenden und die tatsächliche Höhe seiner
bedruckten Markerfläche über der Grundebene nachmessen. Papier, Kleber und
Träger zählen mit. Markerflächen müssen parallel zur Grundebene bleiben.

Die Kontrollmarker samt Dictionary und tatsächlicher Kantenlänge in `MESS_IDS`
und den zugehörigen Größen-/Dictionary-Feldern konfigurieren. Anschließend
ihre bekannten Höhen in `HOEHENREFERENZEN_MM` eintragen:

```json
{
  "MESS_IDS": [20, 21, 22, 23],
  "HOEHENREFERENZEN_MM": {"22": 10.0, "23": 15.0},
  "HOEHENREFERENZ_MAX_FEHLER_MM": 0.5,
  "KONTROLLKORREKTUR_MODUS": "aus"
}
```

Die Beispielhöhen sind reine Demonstrationswerte. IDs 20/21 bleiben Messmarker,
22/23 erhalten die Rolle `height_control`. Deren X/Y-Position und Drehung werden
geschätzt; für diese Kontrollfunktion müssen sie nicht vermessen sein.
Kontrollen verwenden die Grundebene direkt und erhalten keine Nullkorrektur.
Sie dürfen nicht in `NULL_OFFSETS_MM` oder `MESSGRUPPEN` vorkommen.

`bekannte_hoehe_mm` und `kontrollfehler_mm` zeigen Sollhöhe und Abweichung.
`HOEHENREFERENZ_MAX_FEHLER_MM=null` dokumentiert die Abweichung ohne harte
Schranke. Bei aktivierter Schranke und Überschreitung wird die Aufnahme
verworfen. Fehlende konfigurierte Kontrollen machen einen ansonsten gültigen
Lauf unvollständig. Kontrollwerte zählen nicht als gemessene Objekthöhen.

Kontrollkörper an mehreren Bildpositionen und in mehreren Wiederholungen
aufnehmen. Ein einzelner Kontrollmarker unterscheidet Offset, Maßstabsfehler
und positionsabhängige Fehler nicht zuverlässig.

## 4. Optionale Offsetkorrektur

Erst an Vergleichsmessungen prüfen, ob Kontrollen und Messobjekt tatsächlich
einen gemeinsamen Höhenoffset aufweisen. Dann ausdrücklich die Kontroll-IDs
auswählen, die zur Korrektur verwendet werden dürfen:

```json
{
  "KONTROLLKORREKTUR_MODUS": "offset",
  "KONTROLLKORREKTUR_IDS": [22],
  "KONTROLLKORREKTUR_MIN_MARKER": 1,
  "KONTROLLKORREKTUR_MAX_SPANNE_MM": 0.5,
  "KONTROLLKORREKTUR_MAX_OFFSET_MM": 2.0
}
```

Der Median von `berechnete Kontrollhöhe - bekannte Kontrollhöhe` wird von
den gültigen Messhöhen abgezogen. Rohgeometrie und vorherige Höhen bleiben
erhalten. Bei mehreren Kontrollen wird die Spanne ihrer Offsetwerte geprüft.
Die Beispielgrenzen müssen zur geforderten Genauigkeit und Messreihe passen.

Mindestens eine weitere Kontrolle, hier ID 23, zur unabhängigen Prüfung
zurückhalten. `zur_korrektur_verwendet` kennzeichnet die verwendeten IDs;
deren geringe Restabweichung ist keine unabhängige Bestätigung.
`kontrollfehler_vor_korrektur_mm` bleibt sichtbar. Die Fehlerschranke der
Höhenkontrollen gilt nach der Korrektur. Fehlende notwendige Kontrollen,
inkonsistente Offsets oder zu große Korrekturen verwerfen die Messhöhen.
Für mehrere zwingend erforderliche Kontrollen den Mindestwert entsprechend
erhöhen. Der Algorithmus schätzt hier ausschließlich einen Offset, keine
Maßstabskorrektur oder räumliche Korrekturfläche.

## 5. Erhöhte Marker als Teil der 3D-Referenz

Diese Funktion ist von den frei positionierbaren Höhenkontrollen getrennt.
Die Marker stehen in `REFERENZ_IDS`. Für jeden Referenzmarker müssen echte
Kantenlänge, gerichtete Mittelpunktposition X/Y, Drehung innerhalb der Ebene
und Höhe seiner bedruckten Fläche bekannt sein. Alle Flächen bleiben parallel
zur Grundebene. Ein starrer vermessener Träger stellt den Bezug zur Grundebene
her; physischer Kontakt jedes Markers mit dieser Ebene ist nicht erforderlich.

```json
{
  "REFERENZ_LAYOUT_MODUS": "positionen",
  "REFERENZ_POSITIONEN_MM": {
    "0": [0.0, 0.0], "1": [100.0, 0.0],
    "2": [0.0, -100.0], "3": [100.0, -100.0]
  },
  "REFERENZ_WINKEL_GRAD": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 0.0},
  "REFERENZ_EBENENVERSATZ_MM": 0.0,
  "REFERENZ_HOEHEN_MM": {"1": 12.0, "3": 25.0}
}
```

Auch dieses Layout ist nur ein Beispiel. Höhe und X/Y-Maße unabhängig
vermessen; die Ausrichtung der Marker anhand ihrer kanonischen Eckreihenfolge
festlegen. IDs ohne individuellen Höhenwert verwenden den gemeinsamen
`REFERENZ_EBENENVERSATZ_MM`. Der Rastermodus kann ebenfalls verwendet werden,
wenn seine Mittelpunktabstände tatsächlich stimmen. Der Paarabstandsmodus
steht für individuell erhöhte Referenzen nicht zur Verfügung.

Aus den sichtbaren Referenzecken wird je Aufnahme geprüft, ob sie koplanar
sind. Koplanare Punkte verwenden IPPE, räumliche Punkte SQPnP mit anschließender
Poseverfeinerung. Verdeckung kann die sichtbare Teilmenge wieder koplanar machen.
Die Mindestzahl der Referenzmarker und die bisherigen Qualitätsgrenzen gelten
weiterhin. Höhenstaffelung ersetzt keine gute Kamerakalibrierung.

## 6. Robuste Messoptimierung

`MESS_OPTIMIERUNG_LOSS="linear"` erhält die bisherige quadratische Anpassung.
Optional `soft_l1` oder `huber` wählen und `MESS_ROBUST_SKALA_PX` setzen.
Die Skala bestimmt, ab welcher Pixelabweichung Ecken schwächer gewichtet werden.
Diese Einstellung gilt für Einzelmarker, Höhenkontrollen und gemeinsame Ebenen.

Mit denselben scharfen Vergleichsaufnahmen prüfen, ob die Höhenabweichung und
Wiederholstreuung sinken. Der normale RMS aller Ecken wird weiterhin berechnet
und geprüft; robuste Gewichtung hebt die Qualitätsgrenzen nicht auf.

## 7. Räumlicher Kalibrierungsindikator

Die Kalibrierung erzeugt automatisch `spatial_quality.png`, `.csv` und `.json`.
Das Raster bezieht sich auf Originalpixel: Ursprung oben links, X nach rechts,
Y nach unten. Zeilen/Spalten in den Berichten beginnen bei 1.

Die PNG zeigt Aufnahmehinweise, Bild-/Eckenzahlen und lokale Fehler:

| Anzeige | Bedeutung | Aktiver Schritt |
|---|---|---|
| Grau: Keine Daten | Keine verwendeten Ecken in der Zelle | Erkannte Schachbrettecken dorthin bringen |
| Orange: Mehr Aufnahmen | Zu wenige Bilder oder Ecken | Weitere scharfe Ansichten aufnehmen |
| Gelb: Anders neigen | Zu geringe Vielfalt der Boardnormalen | Board dort um unterschiedliche Achsen neigen |
| Rot: Fehler prüfen | RMS oberhalb der gewählten Schranke | Schärfe, Planheit, Druckmaße und Modell untersuchen |
| Grün: Beobachtet | Eingestellte Kriterien erfüllt | Durch separate Aufnahmen und bekannte Höhen überprüfen |

Standard: Raster 6x4, mindestens drei Bilder und zwölf Ecken pro Zelle,
mindestens 10 Grad Normalenspanne, RMS-Grenze 1 Pixel. Diese Werte sind
anpassbare Aufnahmeheuristiken und keine allgemeingültigen Genauigkeitsgrenzen:

```sh
python calibrate_charuco.py --images BILDORDNER --output NEUER_AUSGABEORDNER --squares-x QUADRATE_X --squares-y QUADRATE_Y --square-mm QUADRAT_MM --marker-mm MARKER_MM --dictionary OPENCV_DICTIONARY --spatial-grid 6 4 --spatial-min-views 3 --spatial-min-corners 12 --spatial-min-tilt-span 10 --spatial-max-rms-px 1
```

Großbuchstaben ersetzen. Fokus/Kameramodus fixieren, Board eben befestigen,
Druckmaße prüfen und Bildränder mit ausreichend großen, scharfen Ecken erfassen.
Eine reine Drehung des Boards innerhalb seiner Fläche erhöht die Normalenspanne
nicht. Die Karte zählt Bilder, erkennt aber nicht automatisch nahezu identische
Aufnahmeposen als Duplikate. Entfernte Ausreißerbilder tragen nicht zur finalen
Abdeckung bei. Leere Zellen werden nicht mit Werten aus Nachbarzellen gefüllt.

RMS ist die Wurzel des mittleren quadrierten 2D-Eckenfehlers. JSON/CSV enthalten
auch mittlere X/Y-Fehlervektoren. Hoher lokaler Fehler kann aus dem Bild, dem
Board oder der angepassten Pose stammen; die Karte beweist keinen isolierten
Objektivfehler. Trainingsfehler und genügend Beobachtungen belegen keine
Höhengenauigkeit in Millimetern.

## 8. Separate Validierung und Modellvergleich

Neue Boardaufnahmen in einem separaten Ordner erfassen, besonders an den
Bildrändern. Trainingsbilder oder Kopien davon sind als Prüfung ausgeschlossen.
Mit `--validation-images PRUEFBILDER` entstehen zusätzlich
`validation_report.csv` und `validation_spatial_quality.png/.csv/.json`.
Kameraparameter bleiben dabei fest; nur die Boardpose wird pro Prüfaufnahme
angepasst.

`--model standard` verwendet die fünf Parameter k1/k2/p1/p2/k3.
`--model fix_k3` fixiert k3 auf null. Für den Vergleich den Kalibrieraufruf
um diese Optionen ergänzen:

```sh
--model compare --validation-images PRUEFBILDER
```

Beide Modelle verwenden dieselben finalen Trainingsbilder und dieselben
erkannten Prüfecken. Die Wahl erfolgt anhand des niedrigeren Prüf-RMS und wird
in `model_comparison.json` dokumentiert. Die gespeicherten Kamera-Dateien und
Karten gehören zum ausgewählten Modell. Die Bilder zur Modellauswahl sind danach
kein unabhängiger Abschlusstest: zusätzliche neue Bilder und bekannte Höhen
für die endgültige Prüfung zurückhalten. Die Kalibrierung ändert keine aktive
Mess-Config automatisch; dort die neue NPZ-Datei bewusst eintragen.

## 9. Vergleich und Ergebnisfreigabe

Mehrere nachgemessene Höhen an verschiedenen Bildpositionen wiederholt
aufnehmen. Zunächst Einzelmarker ohne Kontrollkorrektur auswerten, dann dieselben
Bilder mit gemeinsamer Ebene, robuster Optimierung oder neuer Kalibrierung
vergleichen. Mittlere Höhenabweichung, Wiederholstreuung und Positionsabhängigkeit
getrennt beurteilen. Pro Vergleich möglichst nur eine Einstellung verändern.

`status=error` bedeutet verworfen; solche Werte sind nur Diagnosen und gehören
nicht in einen Mittelwert gültiger Messungen. Exitcode 0: vollständige Auswertung;
1: Startfehler; 2: mindestens eine verworfene oder unvollständige Aufnahme.
Fehlende Kontrollen oder Gruppenmarker anhand CSV/JSON prüfen.

```sh
python -m unittest -v
```

Die Tests prüfen künstliche Geometrien und Fehlerfälle, keine zugesicherte
Genauigkeit einer realen Kamera. Weiterführend:
[OpenCV-Boards](https://docs.opencv.org/4.x/db/da9/tutorial_aruco_board_detection.html),
[Poseverfahren](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html),
[Kalibrierung](https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html).
