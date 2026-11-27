"""Einstieg von InkWave. Wird vom Starter (InkWave.exe) mit pythonw.exe gestartet:

    pythonw.exe app\\main.pyw [Dateien oder Ordner ...]

Ohne Konsole gibt es kein stdout/stderr – alles Unerwartete landet in
%LOCALAPPDATA%\\InkWave\\fehler.log und wird als Meldung angezeigt.
"""

import os
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

APP_NAME = "InkWave"
LOG = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "InkWave" / "fehler.log"


def _log_datei():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    return open(LOG, "a", encoding="utf-8", buffering=1)


def _log_kuerzen(grenze: int = 1_000_000):
    """Das Protokoll wächst sonst über die Jahre unbegrenzt: ab 1 MB nur das letzte Viertel behalten."""
    try:
        if LOG.stat().st_size > grenze:
            LOG.write_bytes(LOG.read_bytes()[-grenze // 4:])
    except OSError:
        pass


_log_kuerzen()

# pythonw: stdout/stderr fehlen -> Bibliotheken, die etwas ausgeben (tqdm, Warnungen), würden abstürzen
if sys.stdout is None or sys.stderr is None:
    try:
        _ausgabe = _log_datei()
    except OSError:
        _ausgabe = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _ausgabe
    sys.stderr = sys.stderr or _ausgabe


_melder = None            # ab dem Start der Oberfläche: bringt Meldungen aus anderen Threads ins Fenster
_meldung_offen = False


def _fenster_meldung(text: str):
    """Fehlerfenster – nur im Fenster-Thread aufrufen. Höchstens eines gleichzeitig: wiederholt sich ein
    Fehler (z. B. bei jedem Textabschnitt), stapeln sich sonst Dutzende Fenster; alles steht im Protokoll."""
    global _meldung_offen
    if _meldung_offen:
        return
    _meldung_offen = True
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox
        zeilen = text.strip().splitlines() or ["Unbekannter Fehler"]
        box = QMessageBox(QMessageBox.Icon.Critical, f"{APP_NAME} – Fehler",
                          f"{zeilen[-1][:600]}\n\nDetails stehen in:\n{LOG}", QMessageBox.StandardButton.Ok)
        box.setTextFormat(Qt.TextFormat.PlainText)      # Fehlertexte nie als HTML deuten
        box.exec()
    finally:
        _meldung_offen = False


def fehler_melden(text: str):
    try:
        with _log_datei() as f:
            f.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
    except OSError:
        pass
    try:
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is not None:
            if threading.current_thread() is threading.main_thread():
                _fenster_meldung(text)
                return
            if _melder is not None:
                # Fenster dürfen nur im Fenster-Thread entstehen (sonst Absturz) -> per Signal hinüberreichen
                _melder.zeigen.emit(text)
                return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text[-1500:], f"{APP_NAME} – Fehler", 0x10)
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
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Elias.InkWave")
    except Exception:
        pass

    import kern
    kern.alte_daten_uebernehmen()      # einmalig: Daten aus «Transcriber» (bis 1.0.1) übernehmen

    from PySide6.QtCore import QObject, Signal, Slot
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox
    # Nur den Programmnamen an Qt geben: die übrigen Argumente sind Dateien, und Qt würde solche,
    # die wie Qt-Optionen aussehen (-style, -platformpluginpath …), selbst auswerten.
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)

    class _Melder(QObject):
        """Lebt im Fenster-Thread; ein Signal aus einem anderen Thread kommt hier eingereiht an."""
        zeigen = Signal(str)

        def __init__(self, eltern):
            super().__init__(eltern)
            self.zeigen.connect(self._zeigen)

        @Slot(str)
        def _zeigen(self, text):
            _fenster_meldung(text)

    global _melder
    _melder = _Melder(app)
    ico = Path(__file__).resolve().parent / "inkwave.ico"
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))      # gilt für alle Fenster und Dialoge

    import theme
    thema = theme.Thema(app)                   # hell/dunkel wie Windows, Lila aus dem Logo

    problem = kern.av_version_pruefen()
    if problem:
        QMessageBox.critical(None, APP_NAME, problem)
        return 1

    import gui
    fenster = gui.Hauptfenster(sys.argv[1:], thema)
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
