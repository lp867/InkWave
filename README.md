# InkWave

Verwandelt Audio und Video lokal und offline in Text – mit Whisper (faster-whisper).
Hell oder dunkel wie Windows, mit dem Logo «Wellen zu Worten». Bis Version 1.0.1 hiess die App «Transcriber».
Eigene Windows-App statt Terminal. Läuft mit NVIDIA-Grafikkarte (schnell) und ohne (CPU, int8).

## Installieren

- `InkWave-<Version>.exe` in einen eigenen Ordner legen (z. B. `Desktop\InkWave`) und starten.
  Beim ersten Start einer Version richtet sie sich in etwa 30 Sekunden unter
  `%LOCALAPPDATA%\InkWave\program` ein (ältere Versionen dort räumt sie weg), danach startet sie sofort.
- Alternativ `InkWave-<Version>-Ordner.zip` entpacken und `InkWave.exe` im Ordner starten.
- Fragt Windows «Der Computer wurde durch Windows geschützt»: «Weitere Informationen» → «Trotzdem ausführen».

Neben der exe legt die App an:

| Ordner/Datei | Inhalt |
|---|---|
| `output\` | fertige `.txt` (und `.srt`), nie überschrieben (`name_2.txt`, …) |
| `modelle\` | eigene und umgewandelte Modelle |
| `einstellungen.json` | zuletzt gewähltes Modell und Optionen |

Heruntergeladene Standardmodelle liegen im Hugging-Face-Cache (`%USERPROFILE%\.cache\huggingface\hub`).
In `%LOCALAPPDATA%\InkWave` liegen ausserdem das Programm selbst und – falls eingerichtet – das
GPU-Paket (`gpu\`) und das Umwandlungs-Paket (`umwandlung\`).

## Umstieg von Transcriber 1.x

`InkWave.exe` an die Stelle von `Transcriber.exe` legen (Ordner `modelle`, `output` und `einstellungen.json`
bleiben gleich). Beim ersten Start übernimmt InkWave GPU- und Umwandlungs-Paket aus
`%LOCALAPPDATA%\Transcriber` (verschieben, kein neuer Download) und räumt die alten Programmversionen weg.

## Auf einem anderen PC (z. B. Laptop ohne Grafikkarte)

1. `InkWave.exe` in einen eigenen Ordner kopieren (USB-Stick oder Download aus den Releases).
2. Eigene Modelle mitnehmen: den Ordner `modelle\Flix-AI__flix-swissgerman-full` (3 GB) daneben in
   `modelle\` legen. Umwandeln ist dort nicht mehr nötig.
3. Starten. Beim ersten Start fragt die App, welche Modelle sie laden soll; ohne Grafikkarte ist
   `small` empfohlen (`base` ist noch schneller, aber ungenauer).
4. Mit NVIDIA-Grafikkarte: «GPU einrichten …» oben rechts → GPU-Paket herunterladen (1.3 GB) oder
   aus einer vorhandenen Python-Installation übernehmen.

### Tempo ohne Grafikkarte

Auf der CPU rechnet die App mit int8, im Batch-Modus (8 Abschnitte gleichzeitig) und mit höchstens
8 Threads. Gemessen auf einem Core Ultra 7 255HX (2 min deutsche Sprache):

| Modell | normal | Batch, 8 Threads (so läuft die App) |
|---|---|---|
| base | 12x Echtzeit | 19–29x |
| large-v3-turbo | 3.9x | 5.5x |
| Flix (large-v3) | – | 2.7x |

Mehr als 8 Threads bringt nichts (Effizienzkerne) und der Batch-Modus stürzt ab etwa 16 Threads
mit einem Stack-Überlauf ab – deshalb ist die Zahl fest begrenzt. Ein Laptop mit 4 Kernen ist etwa
halb so schnell. «Schonend rechnen» senkt die Priorität, damit der Laptop bedienbar bleibt.

## Warum ein C#-Starter und kein PyInstaller?

Smart App Control blockiert unbekannte, unsignierte Programme. Die `InkWave.exe` ist nur ein kleiner
Starter (übersetzt mit dem `csc.exe`, das in Windows dabei ist). Sie enthält Python, die Pakete und die App
als ZIP, entpackt sie einmal und startet dann das **signierte** `pythonw.exe` von python.org.
Alle übrigen Dateien sind unverändert aus python.org bzw. PyPI. Gleiches Prinzip wie SkyWave.

## Bauen

Auf einem Windows-PC mit Python 3.11:

```
python build.py
```

Ergebnis in `dist\`: `InkWave-<Version>.exe` und `InkWave-<Version>-Ordner.zip`.
Die Version steht in `launcher\InkWaveLauncher.cs` (`AssemblyVersion`), angezeigt wird sie zweistellig (2.0, 2.1 …).

## Aufbau

- `launcher\InkWaveLauncher.cs` – Starter (entpacken, pythonw starten)
- `app\main.pyw` – Einstieg der App
- `app\kern.py` – Logik ohne Oberfläche (Modelle, GPU/CPU, Transkription, Umwandlung, Zusatzpakete)
- `app\gui.py` – Hauptfenster
- `app\theme.py` – Aussehen: hell/dunkel wie Windows, Lila-Akzente aus dem Logo
- `app\modelle_dialog.py` – Fenster «Modelle» (Download, Umwandeln, Zusatzpakete)
- `tools\logo.png` – das Logo «Wellen zu Worten»; `tools\make_icon.py` macht daraus die .ico
- `requirements.txt` – Pakete in der exe (exakte Versionen)

## Bekannte Stolpersteine

- PyAV ab Version 19 geht nicht mit faster-whisper → `av<19` (18.1.1).
- GPU: CUDA-DLLs kommen aus den pip-Paketen `nvidia-cublas-cu12` und `nvidia-cudnn-cu12==9.*`
  (GPU-Paket, lädt die App nach). Beim Laden auf der GPU läuft 1 s Stille als Test, sonst Wechsel auf CPU.
- Stille Datei + automatische Spracherkennung → IndexError in faster-whisper, wird abgefangen.
