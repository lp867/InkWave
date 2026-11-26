# Transcriber

Wandelt Audio und Video lokal und offline in Text um – mit Whisper (faster-whisper).
Eigene Windows-App statt Terminal. Läuft mit NVIDIA-Grafikkarte (schnell) und ohne (CPU, int8).

## Installieren

- `Transcriber-<Version>.exe` in einen eigenen Ordner legen (z. B. `Dokumente\Transcriber`) und starten.
  Beim ersten Start einer Version richtet sie sich in etwa 30 Sekunden unter
  `%LOCALAPPDATA%\Transcriber\program` ein (ältere Versionen dort räumt sie weg), danach startet sie sofort.
- Alternativ `Transcriber-<Version>-Ordner.zip` entpacken und `Transcriber.exe` im Ordner starten.
- Fragt Windows «Der Computer wurde durch Windows geschützt»: «Weitere Informationen» → «Trotzdem ausführen».

Neben der exe legt die App an:

| Ordner/Datei | Inhalt |
|---|---|
| `output\` | fertige `.txt` (und `.srt`), nie überschrieben (`name_2.txt`, …) |
| `modelle\` | eigene und umgewandelte Modelle |
| `einstellungen.json` | zuletzt gewähltes Modell und Optionen |

Heruntergeladene Standardmodelle liegen im Hugging-Face-Cache (`%USERPROFILE%\.cache\huggingface\hub`).

## Warum ein C#-Starter und kein PyInstaller?

Smart App Control blockiert unbekannte, unsignierte Programme. Die `Transcriber.exe` ist nur ein kleiner
Starter (übersetzt mit dem `csc.exe`, das in Windows dabei ist). Sie enthält Python, die Pakete und die App
als ZIP, entpackt sie einmal und startet dann das **signierte** `pythonw.exe` von python.org.
Alle übrigen Dateien sind unverändert aus python.org bzw. PyPI. Gleiches Prinzip wie SkyWave.

## Bauen

Auf einem Windows-PC mit Python 3.11:

```
python build.py
```

Ergebnis in `dist\`: `Transcriber-<Version>.exe` und `Transcriber-<Version>-Ordner.zip`.
Die Version steht in `launcher\TranscriberLauncher.cs` (`AssemblyVersion`).

## Aufbau

- `launcher\TranscriberLauncher.cs` – Starter (entpacken, pythonw starten)
- `app\main.pyw` – Einstieg der App
- `app\kern.py` – Logik ohne Oberfläche (Modelle, GPU/CPU, Transkription)
- `tools\make_icon.py` – zeichnet das Symbol
- `requirements.txt` – Pakete in der exe (exakte Versionen)

## Bekannte Stolpersteine

- PyAV ab Version 19 geht nicht mit faster-whisper → `av<19` (18.1.1).
- GPU: CUDA-DLLs kommen aus den pip-Paketen `nvidia-cublas-cu12` und `nvidia-cudnn-cu12==9.*`
  (GPU-Paket, lädt die App nach). Beim Laden auf der GPU läuft 1 s Stille als Test, sonst Wechsel auf CPU.
- Stille Datei + automatische Spracherkennung → IndexError in faster-whisper, wird abgefangen.
