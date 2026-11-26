"""Schritt 0 – Testfenster: prüft, ob Smart App Control die Transcriber.exe samt Python und allen
Paketen (PySide6, ctranslate2, PyAV, onnxruntime, ...) laufen lässt. Wird später durch die echte App ersetzt."""

import os
import sys
import time
import traceback
from pathlib import Path

LOG = Path(os.environ.get("LOCALAPPDATA", ".")) / "Transcriber" / "sac_test.log"


def log(text: str):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")


def pruefen() -> list[str]:
    zeilen = [f"Python {sys.version.split()[0]}  ({sys.executable})"]
    for modul in ("PySide6", "numpy", "av", "ctranslate2", "onnxruntime", "tokenizers",
                  "huggingface_hub", "faster_whisper"):
        try:
            m = __import__(modul)
            zeilen.append(f"✓ {modul} {getattr(m, '__version__', '?')}")
        except Exception as e:
            zeilen.append(f"✗ {modul}: {type(e).__name__}: {e}")
    try:
        import ctranslate2
        zeilen.append(f"CUDA-Geräte: {ctranslate2.get_cuda_device_count()}")
    except Exception as e:
        zeilen.append(f"CUDA-Prüfung: {e}")
    zeilen.append(f"Daten-Ordner (neben der exe): {os.environ.get('TRANSCRIBER_DATA', '-')}")
    zeilen.append(f"Argumente: {sys.argv[1:]}")
    return zeilen


def main():
    log("Start")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
    except Exception:
        log("PySide6 fehlt:\n" + traceback.format_exc())
        raise
    app = QApplication(sys.argv)
    zeilen = pruefen()
    log(" | ".join(zeilen))

    fenster = QWidget()
    fenster.setWindowTitle("Transcriber – Test")
    layout = QVBoxLayout(fenster)
    text = QLabel("Smart-App-Control-Test bestanden, wenn du das hier siehst:\n\n" + "\n".join(zeilen))
    text.setTextInteractionFlags(Qt.TextSelectableByMouse)
    layout.addWidget(text)
    knopf = QPushButton("Schliessen")
    knopf.clicked.connect(fenster.close)
    layout.addWidget(knopf)
    fenster.resize(620, 320)
    fenster.show()
    code = app.exec()
    log("Ende")
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log("Fehler:\n" + traceback.format_exc())
        raise
