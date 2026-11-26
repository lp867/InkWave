"""Einstieg des Transcribers. Wird vom Starter (Transcriber.exe) mit pythonw.exe gestartet:

    pythonw.exe app\\main.pyw [Dateien oder Ordner ...]

Ohne Konsole gibt es kein stdout/stderr – alles Unerwartete landet in
%LOCALAPPDATA%\\Transcriber\\fehler.log und wird als Meldung angezeigt.
"""

import os
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LOG = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Transcriber" / "fehler.log"


def _log_datei():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    return open(LOG, "a", encoding="utf-8", buffering=1)


# pythonw: stdout/stderr fehlen -> Bibliotheken, die etwas ausgeben (tqdm, Warnungen), würden abstürzen
if sys.stdout is None or sys.stderr is None:
    try:
        _ausgabe = _log_datei()
    except OSError:
        _ausgabe = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _ausgabe
    sys.stderr = sys.stderr or _ausgabe


def fehler_melden(text: str):
    try:
        with _log_datei() as f:
            f.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
    except OSError:
        pass
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "Transcriber – Fehler",
                                 f"{text.strip().splitlines()[-1]}\n\nDetails stehen in:\n{LOG}")
            return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text[-1500:], "Transcriber – Fehler", 0x10)
    except Exception:
        pass


def _ausnahme(typ, wert, tb):
    fehler_melden("".join(traceback.format_exception(typ, wert, tb)))


def _thread_ausnahme(args):
    fehler_melden("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


def main() -> int:
    sys.excepthook = _ausnahme
    threading.excepthook = _thread_ausnahme
    try:
        # eigenes Symbol in der Taskleiste statt dem von Python
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Elias.Transcriber")
    except Exception:
        pass

    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication(sys.argv)
    app.setApplicationName("Transcriber")

    import kern
    problem = kern.av_version_pruefen()
    if problem:
        QMessageBox.critical(None, "Transcriber", problem)
        return 1

    import gui
    fenster = gui.Hauptfenster(sys.argv[1:])
    fenster.show()
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        fehler_melden(traceback.format_exc())
        sys.exit(1)
