# ArUco Live-Addon – Insta360 Link 2 Pro

Das Addon `aruco_live.py` ist ein **separates Frontend** für das vorhandene
`aruco_verarbeitung.py` aus `jan1412/Aruco` (Schnittstelle 2.2.1).
Es kopiert keine mathematischen Berechnungen: Die Markerdecodierung, Referenzpose,
Parallelitätsannahme, Nullkorrektur, gemeinsame Messebene, Höhenkontrollen und
Reprojektionsfehler-Prüfung stammen aus `aruco_verarbeitung.py`.

## Installation

Alle Dateien dieses Ordners in denselben Ordner wie `aruco_verarbeitung.py`
kopieren. Das Projekt benötigt die Abhängigkeiten aus seiner `requirements.txt`.
Unter Windows im Projektverzeichnis:

```powershell
python -m pip install -r requirements.txt
Copy-Item live.config.example.json live.config.local.json
python aruco_live.py --probe 5 --backend DSHOW
```

Den physikalischen Insta360-Stream über den Kamerafinder identifizieren. Die
virtuelle Kamera und andere USB-Kameras können weitere Indizes erhalten.
Den richtigen Index in `live.config.local.json` bei `KAMERA.INDEX` eintragen.

Die bereits vorhandene `config.local.json` aus dem Rechenskript **weiterverwenden**:
Sie definiert Referenz-IDs, Mess-IDs, reale Markerabmessungen, Referenzpositionen,
Kalibrierdatei und Qualitätsgrenzen. `VERARBEITUNGS_CONFIG` in
`live.config.local.json` zeigt auf genau diese Datei. Die relative Auflösung
von `VERARBEITUNGS_CONFIG` und `AUSGABEORDNER` erfolgt relativ zur
**Live-Konfigurationsdatei**. Die geladenen Verarbeitungspfade bleiben durch
das vorhandene Verarbeitungsskript relativ zur Verarbeitungs-Config.

Start:

```powershell
python aruco_live.py --config live.config.local.json
```

## Live-Status und Bedienung

Die Vorschau zeigt:

- Status, Alter der letzten Analyse und Referenzqualität
- `sichtbare Referenzmarker / konfigurierte Referenzmarker`, benötigtes Minimum
- `Referenz-RMS` in Pixel sowie eingestellte Warn- und Maximalgrenze
- `sichtbare Messmarker / konfigurierte Messmarker` und **gültige Höhen** getrennt
- Anzahl erkannter Höhenkontrollen sowie IDs aktuell nicht erkannter Referenz-/Messmarker
- Farbrahmen mit Marker-IDs (blau: Referenz, grün: Messhöhe gültig,
  rot: Messmarker ohne gültige Höhe, cyan: Höhenkontrolle)
- korrigierte Höhen in mm direkt am jeweiligen erkannten Marker (nur gültige)
- Fehler, Warnungen und Status `VERALTET` nach `MAX_ANZEIGEALTER_S`

Steuerung (Fenster muss fokussiert sein):

| Taste | Funktion |
|---|---|
| `R` | Referenzframe in Originalauflösung als PNG erfassen; Pose und Nullwerte neu initialisieren |
| `S` | Aktuellen Frame frisch auswerten; Original-PNG, annotiertes PNG, JSON, TXT, CSV speichern |
| `F` | PNG aus dem Videostream speichern, ohne Auswertung |
| `Q` oder `Esc` | Kamera/Fenster schließen |

Die **Vorschau darf auf 1600 Pixel verkleinert** werden; der Algorithmus
berechnet alles auf dem **unverkleinerten Originalbild**. Die Kamera kann
z.B. mit 30 FPS liefern, während `BERECHNUNGEN_PRO_SEKUNDE` nur 2 betragen:
Die sichtbaren Höhen und Markerumrandungen stammen aus dem letzten
analysierten Frame, nicht zwingend aus dem gerade angezeigten Previewframe.
Bei Bewegung können Umrandungen kurz versetzt erscheinen. Messfoto (`S`)
rechnet **immer den wirklich gespeicherten Frame erneut** durch. Bei zu alten
Werten erscheint der Status `VERALTET`, und die Vorschau markiert die Höhen
nicht mehr als gültig.

## Referenz und Nullhöhe

`REFERENZ_START="live"` (empfohlen): Die Referenz- und ggf. Nullaufnahme
an der Kamera mit `R` auslösen. Für `NULL_MODUS="referenzbild"` müssen dabei
alle Messmarker, für die eine Nullhöhe bestimmt werden soll, auf der Nullebene
zu sehen sein. Wenn deren Marker erst später eingesetzt werden, kann
stattdessen `NULL_MODUS="konfiguriert"` mit bekannten Null-Offsets sinnvoll sein.

`REFERENZ_START="bild"`: Das bestehende `REFERENZBILD` aus der
Verarbeitungs-Config laden. Kamera und Messgeometrie dürfen sich gegenüber
diesem Bild nicht in einer Weise verändert haben, die das Referenzmodell
entwertet. Auch bei diesem Modus sind im Livebild ausreichend Referenzmarker
verpflichtend. Die bestehende Einstellung `REFERENZPOSE_MODUS="pro_bild"`
ist empfehlenswert, wenn die Referenzmarker in jedem Livebild sichtbar sind.

Bei zu wenigen Referenzmarkern oder Überschreitung der RMS-Maximalgrenze
werden Höhen **nicht freigegeben**. Es werden keine alten Werte unter einem
neuen Zeitstempel gespeichert. Ein fehlgeschlagenes Messfoto enthält
Fehlerdiagnose und keine gültige Höhe.

Wichtig: Der RMS-Wert misst den Reprojektionsfehler in **Pixeln**, keine
empirisch bestimmte Höhenunsicherheit in **mm**. Auch bei grünem RMS können
optische und geometrische systematische Fehler vorhanden sein.

## Insta360 Link 2 Pro und Kalibrierung

Standardkonfiguration (Windows): `BACKEND=DSHOW`, Auflösung `3840 × 2160`,
`FPS=30`, `FOURCC=MJPG`. Die tatsächliche Auflösung wird mit der
Kalibrierungsauflösung verglichen; eine Abweichung ist ein Fehler.
Die tatsächlichen Modi hängen von Kamera, USB-Verbindung, Betriebssystem und
Treiber ab. `VideoCapture` kann nicht beweisen, welches Gerätemodell geöffnet
ist. `--probe` hilft beim Finden des Kameraindex.

**Fokus, Belichtung, Weißabgleich, Zoom, HDR, Tracking, virtuelle Hintergrund-
und Bildkorrektur-Funktionen möglichst vor der Kalibrierung festsetzen.**
Viele Kameraoptionen sind nicht zuverlässig über OpenCV/UVC erreichbar;
`None` lässt eine Kameraeigenschaft unangetastet. Setzen über `cap.set()`
und die von `cap.get()` gemeldeten Werte garantieren nicht, dass sich
Bildpipeline oder Hardware tatsächlich verändert haben. Falls notwendig,
Kamera-Einstellungen in der Herstellersoftware setzen und deren Persistenz
nach Neustart testen. VideoCapture liefert *Frames des Videostreams*, nicht
den separaten hochauflösenden Fotomodus einer Herstellersoftware.

Falls sich die optische Abbildung verändert, ist eine neue Kalibrierung
oder die reproduzierbare Wiederherstellung der Kalibrierbedingungen nötig.
Das Skript verhindert Auflösungs-Mismatches, aber kann nicht sämtliche
optischen Modusänderungen erkennen.

## Testen

```powershell
python -m unittest -v test_aruco_live.py
```

Die Tests prüfen Konfiguration, Statuslogik und Overlay auf synthetischen
Bildern, **nicht die physische Insta360-Kamera** und nicht die reale
Millimeter-Messgenauigkeit. End-to-End-Tests am Messaufbau bleiben erforderlich.

**Datenschutz:** `live.config.local.json`, echte Kalibrierungsdateien,
Referenzbilder und `live_aufnahmen/` nicht in öffentliche Git-Commits aufnehmen.
Sinnvolle `.gitignore`-Einträge:

```gitignore
live.config.local.json
live_aufnahmen/
```


## Starres Messraster und Referenzbild ohne sichtbare Referenzen

Das Messraster wird in der **Verarbeitungs-Config** konfiguriert, nicht in der Live-Config.
`MESSMODELL: "gemeinsame_ebene"`, `MESSGRUPPEN` und `MESS_LAYOUTS` koennen
relative Markerpositionen oder Abstaende festlegen. Details und JSON-Beispiel
stehen in [README.md](README.md#vermessenes-messraster-ab-version-230).

Soll die Pose ausschliesslich aus dem Referenzbild stammen und keine Referenzmarker
im aktuellen Livebild erforderlich sein, in `config.local.json` setzen:

```json
{"REFERENZPOSE_MODUS": "fest"}
```

Und in `live.config.local.json` setzen:

```json
{"REFERENZ_START": "bild", "REFERENZ_KONTROLLE_IM_MESSBILD": false}
```

Die Referenz-RMS-Kontrolle des Livebildes ist dann deaktiviert. Das Overlay
zeigt `FEST / UNGEPRUEFT` statt einer gruener Referenzfreigabe. Solange
die Kamera nicht absolut stabil und unveraendert ist, kann ein fester Bezug
zu systematisch falschen Hoehen fuehren.

