"""
gui.py – das Hauptfenster von InkWave (PySide6).

Aufbau: Kopfzeile (Logo, Name, Hardware) · links die Dateien (leer: grosse Ablagefläche) · rechts
Einstellungen, Start und Fortschritt · unten Text-Vorschau und Protokoll. Farben kommen aus theme.py.

Die eigentliche Arbeit (Modell laden, transkribieren) läuft im Arbeiter-Objekt in einem eigenen
Thread; es meldet sich nur über Signale. So bleibt das Fenster bedienbar.
"""

import gc
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QMainWindow, QScrollArea, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStackedWidget, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

import kern
import theme
from bausteine import beschriftung, karte, mit_info

APP_NAME = "InkWave"
# Seiten des Hauptfensters: Name -> (Position im Stapel, Untertitel in der Kopfzeile)
SEITEN = {
    "start": (0, "Audio und Video – lokal und offline"),
    "transkribieren": (1, "Transkribieren  ·  Audio und Video in Text"),
    "konverter": (2, "MP4 → MP3  ·  Tonspur aus Videos als MP3"),
    "einstellungen": (3, "Einstellungen"),
}
ROLLE_PFAD = Qt.ItemDataRole.UserRole
ROLLE_ERGEBNIS = Qt.ItemDataRole.UserRole + 1
ROLLE_ZUSTAND = Qt.ItemDataRole.UserRole + 2     # "wartet" | "läuft" | "fertig" | "fehler"
ROLLE_FARBE = Qt.ItemDataRole.UserRole + 3       # Farbname aus theme.f für die Status-Spalte
ROLLE_AUFTRAG = Qt.ItemDataRole.UserRole + 4     # Nummer im laufenden Auftrag (siehe Hauptfenster.starten)

GERAETE = [("auto", "Automatisch"), ("cuda", "Grafikkarte (GPU)"), ("cpu", "Prozessor (CPU)")]


def sprach_name(code: str) -> str:
    return kern.SPRACH_NAMEN.get(code, code)


def logo_pixmap(widget, groesse: int):
    pfad = kern.APP_ORDNER / "inkwave.png"
    if not pfad.exists():
        return None
    dpr = widget.devicePixelRatioF()
    bild = QPixmap(str(pfad)).scaled(int(groesse * dpr), int(groesse * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
    bild.setDevicePixelRatio(dpr)
    return bild


# ---------------------------------------------------------------- Arbeiter (eigener Thread)

class Arbeiter(QObject):
    status = Signal(str)
    protokoll = Signal(str)
    geraet_bereit = Signal(str)                    # "cuda" / "cpu"
    datei_start = Signal(int)
    sprache = Signal(int, str, float, bool)        # zeile, sprache, wahrscheinlichkeit, automatisch erkannt
    fortschritt = Signal(int, float, float, float)  # zeile, anteil, position, dauer
    segment = Signal(int, str)
    datei_fertig = Signal(int, object)             # zeile, kern.Ergebnis
    datei_fehler = Signal(int, str, str)           # zeile, kurz, ausführlich
    fertig = Signal(bool)                          # abgebrochen?
    hardware = Signal(object)                      # dict mit gpu, name, paket, kerne, modelle

    def __init__(self):
        super().__init__()
        self.abbrechen = threading.Event()
        self.ausgelassen = set()      # Auftragsnummern, die während des Laufs aus der Liste entfernt wurden
        self._modell = self._batch = self._dev = None
        self._schluessel = None

    def _melden(self, art, **d):
        if art in ("status", "warnung"):
            self.protokoll.emit(d["text"])
            self.status.emit(d["text"])

    @Slot()
    def pruefen(self):
        """Beim Start im Hintergrund: Grafikkarte, Kerne, Modelle (Import von faster-whisper dauert).
        Meldet sich immer zurück – sonst bliebe das Fenster bei «Modelle werden gesucht …» stehen."""
        info = {"gpu": False, "name": "", "paket": False, "kerne": max(1, (os.cpu_count() or 2) // 2),
                "modelle": []}
        try:
            info["gpu"] = kern.gpu_vorhanden()
            info["name"] = kern.gpu_name() if info["gpu"] else ""
            info["paket"] = kern.gpu_paket_vorhanden() if info["gpu"] else False
            info["kerne"] = kern.physische_kerne()
        except Exception as e:
            self.protokoll.emit(f"Hardware konnte nicht geprüft werden: {type(e).__name__}: {e}")
        try:
            info["modelle"] = kern.modell_liste()
        except Exception as e:
            self.protokoll.emit(f"Modelle konnten nicht gelesen werden: {type(e).__name__}: {e}")
        self.hardware.emit(info)

    @Slot(object)
    def arbeiten(self, auftrag: dict):
        # abbrechen wird beim Start im Fenster zurückgesetzt, nicht hier: sonst ginge ein Klick auf
        # «Abbrechen» verloren, der kommt, bevor dieser Thread den Auftrag aufgenommen hat
        kern.schonend_rechnen(auftrag["schonend"])
        try:
            m = kern.modell_finden(auftrag["modell"])
            if m is None or not m.bereit:
                raise RuntimeError(f"Modell «{auftrag['modell']}» ist nicht heruntergeladen.")
            # GPU-Paket gehört dazu: nach dem Einrichten wird das Modell neu (auf der GPU) geladen
            schluessel = (m.name, m.pfad, auftrag["geraet"], auftrag["threads"], kern.gpu_paket_vorhanden())
            if schluessel != self._schluessel:
                self._modell = self._batch = None      # altes Modell zuerst freigeben (Speicher)
                self._schluessel = None                # scheitert das Laden, gilt das alte nicht mehr als geladen
                gc.collect()
                self._modell, self._batch, self._dev = kern.modell_laden(
                    m, auftrag["geraet"], self._melden, threads=auftrag["threads"])
                self._schluessel = schluessel
            self.geraet_bereit.emit(self._dev)

            with kern.WachHalten():
                for zeile, datei in auftrag["dateien"]:      # zeile = Auftragsnummer, nicht Listenposition
                    if self.abbrechen.is_set():
                        break
                    if zeile in self.ausgelassen:
                        continue
                    self.datei_start.emit(zeile)

                    def melden(art, _z=zeile, **d):
                        if art == "sprache":
                            self.sprache.emit(_z, d["sprache"], d["wahrscheinlichkeit"], d["erkannt"])
                        elif art == "fortschritt":
                            self.fortschritt.emit(_z, d["anteil"], d["position"], d["dauer"])
                        elif art == "segment":
                            self.segment.emit(_z, d["text"])

                    try:
                        e = kern.transkribieren(self._modell, self._batch, datei, auftrag["sprache"],
                                                auftrag["srt"], auftrag["zeitstempel"], melden, self.abbrechen)
                        self.datei_fertig.emit(zeile, e)
                    except kern.Abgebrochen:
                        self.datei_fehler.emit(zeile, "Abgebrochen", "")
                        break
                    except kern.KeineSprache as e:
                        self.datei_fehler.emit(zeile, "Kein Text erkannt", str(e))
                    except kern.KeineTonspur as e:
                        self.datei_fehler.emit(zeile, "Keine Audio-/Videodatei", str(e))
                    except Exception as e:
                        self.datei_fehler.emit(zeile, "Fehler", f"{type(e).__name__}: {e}")
        except Exception as e:
            self.protokoll.emit(f"Fehler: {e}")
            self.status.emit(f"Fehler: {e}")
        finally:
            kern.schonend_rechnen(False)
            self.fertig.emit(self.abbrechen.is_set())


# ---------------------------------------------------------------- Ziehen & Ablegen

def _pfade(e):
    return [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]


class DropFlaeche(QFrame):
    """Grosse Ablagefläche, solange noch keine Dateien in der Liste sind."""
    abgelegt = Signal(list)

    def __init__(self, titel="Dateien hierher ziehen", untertitel="auch ganze Ordner",
                 knopf="Dateien wählen …"):
        super().__init__()
        self.setObjectName("dropzone")
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(6)
        lay.addStretch(1)
        self.logo = QLabel()
        bild = logo_pixmap(self, 64)
        if bild is not None:
            self.logo.setPixmap(bild)
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.logo)
        lay.addSpacing(8)
        t = beschriftung(titel, "gross")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        u = beschriftung(untertitel, "gedaempft")
        u.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(u)
        lay.addSpacing(14)
        knoepfe = QHBoxLayout()
        knoepfe.addStretch(1)
        self.btn_dateien = QPushButton(knopf)
        self.btn_ordner = QPushButton("Ordner wählen …")
        knoepfe.addWidget(self.btn_dateien)
        knoepfe.addWidget(self.btn_ordner)
        knoepfe.addStretch(1)
        lay.addLayout(knoepfe)
        lay.addStretch(1)

    def _aktiv(self, an: bool):
        self.setProperty("aktiv", an)
        theme.neu_polieren(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._aktiv(True)

    def dragLeaveEvent(self, e):
        self._aktiv(False)

    def dropEvent(self, e):
        self._aktiv(False)
        if _pfade(e):
            e.acceptProposedAction()
            self.abgelegt.emit(_pfade(e))


class DateiListe(QTreeWidget):
    abgelegt = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setHeaderLabels(["Datei", "Status", "Fortschritt"])
        kopf = self.header()
        kopf.setStretchLastSection(False)
        kopf.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        kopf.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        kopf.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(1, 225)
        self.setColumnWidth(2, 110)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if _pfade(e):
            e.acceptProposedAction()
            self.abgelegt.emit(_pfade(e))


# ---------------------------------------------------------------- Hauptfenster

class Hauptfenster(QMainWindow):
    auftrag = Signal(object)
    pruef_anfrage = Signal()

    def __init__(self, start_dateien=(), thema: "theme.Thema | None" = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        ico = kern.APP_ORDNER / "inkwave.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))
        # Grösse passend zum Bildschirm (auch für kleine Laptops)
        frei = self.screen().availableGeometry()
        self.resize(min(1140, int(frei.width() * 0.92)), min(840, int(frei.height() * 0.9)))
        # Mindestgrösse nie grösser als der Bildschirm (kleine Laptops mit 175–200 % Skalierung)
        self.setMinimumSize(max(480, min(860, frei.width() - 20)), max(360, min(560, frei.height() - 50)))
        if kern.einstellung("fenster_merken", True):
            try:   # Grösse und Position vom letzten Mal (Qt holt das Fenster selbst auf einen vorhandenen Bildschirm)
                self.restoreGeometry(QByteArray.fromBase64(kern.einstellung("fenster", "").encode("ascii")))
            except (UnicodeError, ValueError):
                pass
        self.setAcceptDrops(True)

        self.thema = thema
        self.seite = "start"
        self.vorige_seite = "start"
        self.hardware_da = False
        self.gpu = False
        self.gpu_name = ""
        self.laeuft = False
        self.hinweis = ""
        self.empfohlen = ""
        self.dialog_offen = False
        self.auftrag_nr = 0               # fortlaufende Nummer für jede Datei eines Auftrags
        self.auftrag_zeilen = {}          # Auftragsnummer -> Listenzeile (bleibt richtig, wenn Zeilen entfernt werden)
        self.modelle = []
        self.datei_start_zeit = 0.0
        self.datei_sprache = ""
        self.vorschau_kopf = ""
        self.anzahl_auftrag = 0
        self.fertig_auftrag = 0

        self._oberflaeche_bauen()
        self._einstellungen_laden()
        if thema is not None:
            thema.geaendert.connect(self._farben_auffrischen)

        # Arbeiter-Thread
        self.thread_ = QThread(self)
        self.arbeiter = Arbeiter()
        self.arbeiter.moveToThread(self.thread_)
        self.auftrag.connect(self.arbeiter.arbeiten)
        a = self.arbeiter
        a.status.connect(self.lbl_status.setText)
        a.protokoll.connect(self.protokollieren)
        a.geraet_bereit.connect(self._geraet_bereit)
        a.datei_start.connect(self._datei_start)
        a.sprache.connect(self._sprache)
        a.fortschritt.connect(self._fortschritt)
        a.segment.connect(self._segment)
        a.datei_fertig.connect(self._datei_fertig)
        a.datei_fehler.connect(self._datei_fehler)
        a.fertig.connect(self._alles_fertig)
        a.hardware.connect(self._hardware_bekannt)
        self.pruef_anfrage.connect(self.arbeiter.pruefen)
        self.thread_.start()

        if start_dateien:
            self.dateien_hinzufuegen(list(start_dateien))
        self.cb_modell.addItem("Modelle werden gesucht …")
        self._knoepfe_aktualisieren()
        # Mit Dateien gestartet (auf die exe gezogen): gleich zum Transkribieren, sonst die gewählte Startseite
        start = "transkribieren" if start_dateien else kern.einstellung("start_seite", "start")
        self.seite_zeigen(start if start in ("start", "transkribieren", "konverter") else "start")
        self.pruef_anfrage.emit()

    # ------------------------------------------------------------ Aufbau

    def _oberflaeche_bauen(self):
        from einstellungen_seite import EinstellungenSeite
        from konverter_seite import KonverterSeite
        from start_seite import StartSeite

        zentral = QWidget()
        self.setCentralWidget(zentral)
        haupt = QVBoxLayout(zentral)
        haupt.setContentsMargins(22, 16, 22, 18)
        haupt.setSpacing(14)
        haupt.addLayout(self._kopf_bauen())

        # Seite «Transkribieren»: oben Dateien und Einstellungen, unten Text und Protokoll
        oben = QWidget()
        reihe = QHBoxLayout(oben)
        reihe.setContentsMargins(0, 0, 0, 0)
        reihe.setSpacing(14)
        reihe.addWidget(self._dateien_bauen(), 1)
        reihe.addWidget(self._einstellungen_bauen(), 0)

        self.teiler = teiler = QSplitter(Qt.Orientation.Vertical)
        teiler.addWidget(oben)
        teiler.addWidget(self._ergebnis_bauen())
        teiler.setStretchFactor(0, 3)
        teiler.setStretchFactor(1, 2)
        teiler.setChildrenCollapsible(False)
        teiler.setHandleWidth(14)

        self.start = StartSeite()
        self.start.gewaehlt.connect(self.seite_zeigen)
        self.start.abgelegt.connect(self._auf_kachel_abgelegt)
        self.start.output_oeffnen.connect(self._output_oeffnen)
        self.konverter = KonverterSeite()
        self.konverter.zustand_geaendert.connect(self._start_hinweise)
        self.konverter.alles_fertig.connect(self._fertig_aktionen)
        self.einstellungen = EinstellungenSeite()
        self.einstellungen.thema_geaendert.connect(self._thema_anwenden)
        self.einstellungen.modelle_oeffnen.connect(lambda: self.modelle_oeffnen())
        self.einstellungen.zurueckgesetzt.connect(self._zurueckgesetzt)

        self.seiten = QStackedWidget()        # Reihenfolge wie in SEITEN
        for seite in (self.start, teiler, self.konverter, self.einstellungen):
            self.seiten.addWidget(seite)
        haupt.addWidget(self.seiten, 1)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Back), self, activated=self.zurueck)

    def _teiler_anpassen(self):
        """Beim ersten Öffnen: oben so viel Platz, dass die Einstellungen ganz sichtbar sind; Rest für den Text."""
        noetig = self.innen.sizeHint().height() + 8
        gesamt = sum(self.teiler.sizes())
        if gesamt <= 0:
            return
        oben = max(min(noetig, gesamt - 170), int(gesamt * 0.5))
        self.teiler.setSizes([oben, gesamt - oben])

    def _kopf_bauen(self):
        kopf = QHBoxLayout()
        kopf.setSpacing(12)
        self.btn_zurueck = QPushButton("‹  Start")
        self.btn_zurueck.setObjectName("zurueck")
        self.btn_zurueck.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_zurueck.clicked.connect(self.zurueck)
        self.btn_zurueck.hide()
        kopf.addWidget(self.btn_zurueck, 0, Qt.AlignmentFlag.AlignVCenter)
        logo = QLabel()
        bild = logo_pixmap(self, 48)
        if bild is not None:
            logo.setPixmap(bild)
            kopf.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)
        t = QVBoxLayout()
        t.setSpacing(0)
        t.addWidget(beschriftung(APP_NAME, "titel"))
        self.lbl_untertitel = beschriftung(SEITEN["start"][1], "untertitel")
        t.addWidget(self.lbl_untertitel)
        kopf.addLayout(t)
        kopf.addStretch(1)
        self.lbl_hardware = beschriftung("Hardware wird geprüft …", "chip")
        kopf.addWidget(self.lbl_hardware, 0, Qt.AlignmentFlag.AlignVCenter)
        self.btn_gpu = QPushButton("GPU einrichten …")
        self.btn_gpu.setObjectName("link")
        self.btn_gpu.setToolTip("Das GPU-Paket (cuBLAS + cuDNN) fehlt noch – ohne rechnet InkWave auf der CPU.")
        self.btn_gpu.clicked.connect(lambda: self.modelle_oeffnen())
        self.btn_gpu.hide()
        kopf.addWidget(self.btn_gpu, 0, Qt.AlignmentFlag.AlignVCenter)
        self.btn_einstellungen = QPushButton("Einstellungen")
        self.btn_einstellungen.setObjectName("link")
        self.btn_einstellungen.clicked.connect(lambda: self.seite_zeigen("einstellungen"))
        kopf.addWidget(self.btn_einstellungen, 0, Qt.AlignmentFlag.AlignVCenter)
        return kopf

    # ------------------------------------------------------------ Seiten

    def seite_zeigen(self, name: str):
        """Wechselt zu "start", "transkribieren", "konverter" oder "einstellungen"."""
        if name not in SEITEN:
            return
        if name != self.seite:
            self.vorige_seite = self.seite
        self.seite = name
        self.seiten.setCurrentIndex(SEITEN[name][0])
        self.lbl_untertitel.setText(SEITEN[name][1])
        self.btn_zurueck.setVisible(name != "start")
        # aus den Einstellungen geht es dorthin zurück, wo man herkam
        self.btn_zurueck.setText("‹  Zurück" if name == "einstellungen" and self.vorige_seite != "start"
                                 else "‹  Start")
        self.btn_einstellungen.setVisible(name != "einstellungen")
        if name == "einstellungen":
            self.einstellungen.laden()
        if name == "transkribieren":
            if not getattr(self, "_angepasst", False):
                self._angepasst = True
                QTimer.singleShot(0, self._teiler_anpassen)
            self._erster_start_pruefen()

    def zurueck(self):
        if self.seite == "einstellungen" and self.vorige_seite != "einstellungen":
            self.seite_zeigen(self.vorige_seite)
        else:
            self.seite_zeigen("start")

    def _auf_kachel_abgelegt(self, werkzeug: str, pfade: list):
        self.seite_zeigen(werkzeug)
        (self.konverter.dateien_hinzufuegen if werkzeug == "konverter" else self.dateien_hinzufuegen)(pfade)

    def _start_hinweise(self):
        """Kleine Schilder auf den Kacheln der Startseite: läuft gerade etwas, liegen Dateien bereit?"""
        def text(laeuft, n):
            return "läuft …" if laeuft else (f"{n} bereit" if n else "")
        self.start.hinweis("transkribieren", text(self.laeuft, self.liste.topLevelItemCount()))
        self.start.hinweis("konverter", text(self.konverter.laeuft, self.konverter.liste.topLevelItemCount()))

    def _thema_anwenden(self):
        if self.thema is not None:
            self.thema.anwenden()

    def _zurueckgesetzt(self):
        self._einstellungen_laden()
        self.konverter._einstellungen_laden()

    def _fertig_aktionen(self, anzahl_fertig: int):
        """Nach einem fertigen Lauf: Taskleiste blinkt; auf Wunsch Hinweiston und Output-Ordner öffnen."""
        QApplication.alert(self)
        if anzahl_fertig <= 0:
            return
        if kern.einstellung("fertig_ton", False):
            QApplication.beep()
        if kern.einstellung("fertig_ordner", False):
            self._output_oeffnen()

    def _erster_start_pruefen(self):
        """Beim allerersten Öffnen von «Transkribieren»: Modelle auswählen lassen."""
        if (self.hardware_da and self.seite == "transkribieren" and not getattr(self, "_erster_start_gezeigt", False)
                and not kern.einstellungen_lesen().get("eingerichtet")):
            self._erster_start_gezeigt = True
            QTimer.singleShot(200, lambda: self.modelle_oeffnen(erster_start=True))

    def _dateien_bauen(self):
        k = karte()
        lay = QVBoxLayout(k)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        zeile = QHBoxLayout()
        zeile.addWidget(beschriftung("Dateien", "abschnitt"))
        self.lbl_anzahl = beschriftung("", "zaehler")
        self.lbl_anzahl.hide()
        zeile.addWidget(self.lbl_anzahl)
        zeile.addStretch(1)
        self.btn_dateien = QPushButton("+ Dateien")
        self.btn_ordner = QPushButton("+ Ordner")
        self.btn_entfernen = QPushButton("Entfernen")
        self.btn_leeren = QPushButton("Leeren")
        for b in (self.btn_dateien, self.btn_ordner, self.btn_entfernen, self.btn_leeren):
            b.setObjectName("link")
            zeile.addWidget(b)
        self.btn_dateien.clicked.connect(self._dateien_waehlen)
        self.btn_ordner.clicked.connect(self._ordner_waehlen)
        self.btn_entfernen.clicked.connect(self._entfernen)
        self.btn_leeren.clicked.connect(self._leeren)
        lay.addLayout(zeile)

        self.stapel = QStackedWidget()
        self.drop = DropFlaeche()
        self.drop.abgelegt.connect(self.dateien_hinzufuegen)
        self.drop.btn_dateien.clicked.connect(self._dateien_waehlen)
        self.drop.btn_ordner.clicked.connect(self._ordner_waehlen)
        self.liste = DateiListe()
        self.liste.abgelegt.connect(self.dateien_hinzufuegen)
        self.liste.itemDoubleClicked.connect(self._zeile_oeffnen)
        self.stapel.addWidget(self.drop)
        self.stapel.addWidget(self.liste)
        lay.addWidget(self.stapel, 1)
        return k

    def _einstellungen_bauen(self):
        k = karte()
        k.setFixedWidth(380)
        aussen = QVBoxLayout(k)
        aussen.setContentsMargins(2, 2, 2, 2)
        # Inhalt in einem Scroll-Bereich: auf kleinen Bildschirmen wird gescrollt statt gequetscht
        rollen = QScrollArea()
        rollen.setObjectName("rollen")
        rollen.setWidgetResizable(True)
        rollen.setFrameShape(QFrame.Shape.NoFrame)
        rollen.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.innen = innen = QWidget()
        innen.setObjectName("innen")
        rollen.setWidget(innen)
        aussen.addWidget(rollen)
        lay = QVBoxLayout(innen)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(6)
        lay.addWidget(beschriftung("Einstellungen", "abschnitt"))
        lay.addSpacing(4)

        zeile = mit_info(
            "Modell",
            "Das Whisper-Modell, das den Text erkennt. Grössere Modelle sind genauer, aber langsamer.\n\n"
            "✓ = heruntergeladen und bereit. Weitere Modelle lädst du über «Modelle verwalten …».\n"
            "★ = für diesen PC empfohlen.", "Modell")
        self.btn_modelle = QPushButton("Modelle verwalten …")
        self.btn_modelle.setObjectName("link")
        self.btn_modelle.setToolTip("Modelle herunterladen, Hugging-Face-Modelle hinzufügen, Zusatzpakete")
        self.btn_modelle.clicked.connect(lambda: self.modelle_oeffnen())
        zeile.addWidget(self.btn_modelle)
        lay.addLayout(zeile)
        self.cb_modell = QComboBox()
        self.cb_modell.view().setMinimumWidth(380)
        self.cb_modell.currentIndexChanged.connect(self._modell_info)
        lay.addWidget(self.cb_modell)
        self.lbl_modell_info = beschriftung("", "gedaempft")     # eine Zeile, voller Text als Tooltip
        self.lbl_modell_info.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_modell_info.setMinimumWidth(1)
        lay.addWidget(self.lbl_modell_info)

        lay.addLayout(mit_info(
            "Sprache",
            "Automatisch erkennen oder fest einstellen – alle 100 Sprachen von Whisper. Fest eingestellt "
            "ist es etwas schneller und bei kurzen oder leisen Aufnahmen zuverlässiger.\n\n"
            "Für das Schweizer Modell (Flix): «Automatisch» oder «Deutsch».\n\n"
            "Tipp: Liste öffnen und den Anfangsbuchstaben tippen.", "Sprache"))
        self.cb_sprache = QComboBox()             # nur auswählen, nicht tippen (keine Tippfehler)
        self.cb_sprache.setMaxVisibleItems(16)
        for code, name in kern.sprach_auswahl():
            if code == "-":
                self.cb_sprache.insertSeparator(self.cb_sprache.count())
            else:
                self.cb_sprache.addItem(name, code)
        lay.addWidget(self.cb_sprache)
        lay.addSpacing(4)

        lay.addLayout(mit_info(
            "Rechnen auf",
            "Automatisch: nimmt die NVIDIA-Grafikkarte, wenn eine da ist und das GPU-Paket eingerichtet "
            "ist – sonst den Prozessor.\n\nMit Grafikkarte geht es um ein Vielfaches schneller.", "Rechnen auf"))
        self.cb_geraet = QComboBox()
        for code, name in GERAETE:
            self.cb_geraet.addItem(name, code)
        lay.addWidget(self.cb_geraet)
        lay.addSpacing(10)

        self.chk_srt = QCheckBox(".srt-Untertitel erstellen")
        self.chk_zeit = QCheckBox("Zeitstempel in der .txt")
        self.chk_schonend = QCheckBox("Schonend rechnen")
        lay.addLayout(mit_info(self.chk_srt, "Speichert zusätzlich zur .txt eine Untertitel-Datei (.srt) mit "
                                             "Zeitangaben – z. B. für Videoplayer oder Schnittprogramme."))
        lay.addWidget(self.chk_zeit)
        lay.addLayout(mit_info(self.chk_schonend, "Niedrigere Priorität: der PC bleibt gut bedienbar, die "
                                                  "Transkription dauert etwas länger."))
        lay.addStretch(1)

        self.btn_start = QPushButton("Transkribieren")
        self.btn_start.setObjectName("primaer")
        self.btn_start.setDefault(True)
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setMinimumHeight(48)
        self.btn_start.clicked.connect(self.starten)
        lay.addWidget(self.btn_start)
        self.btn_abbrechen = QPushButton("Abbrechen")
        self.btn_abbrechen.clicked.connect(self.abbrechen)
        self.btn_abbrechen.setMinimumHeight(48)
        self.btn_abbrechen.hide()
        lay.addWidget(self.btn_abbrechen)
        lay.addSpacing(6)

        fort = QHBoxLayout()
        self.balken = QProgressBar()
        self.balken.setRange(0, 1000)
        self.balken.setValue(0)
        self.balken.setTextVisible(False)
        fort.addWidget(self.balken, 1)
        self.lbl_prozent = beschriftung("0 %", "prozent")
        self.lbl_prozent.setMinimumWidth(44)
        self.lbl_prozent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fort.addWidget(self.lbl_prozent)
        lay.addLayout(fort)
        self.lbl_status = beschriftung("Bereit.", "gedaempft")
        # Reiner Text: hier landen Dateinamen und Fehlertexte von aussen – nie als HTML deuten
        self.lbl_status.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setMinimumHeight(self.lbl_status.fontMetrics().lineSpacing() * 2)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.lbl_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_status)
        return k

    def _ergebnis_bauen(self):
        k = karte()
        lay = QVBoxLayout(k)
        lay.setContentsMargins(12, 10, 12, 12)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)
        self.txt_vorschau = QPlainTextEdit()
        self.txt_vorschau.setReadOnly(True)
        self.txt_vorschau.setPlaceholderText("Hier erscheint der erkannte Text, während transkribiert wird.")
        self.txt_protokoll = QPlainTextEdit()
        self.txt_protokoll.setReadOnly(True)
        self.tabs.addTab(self.txt_vorschau, "Text")
        self.tabs.addTab(self.txt_protokoll, "Protokoll")
        self.btn_output = QPushButton("Output-Ordner öffnen")
        self.btn_output.setObjectName("link")
        self.btn_output.clicked.connect(self._output_oeffnen)
        self.tabs.setCornerWidget(self.btn_output, Qt.Corner.TopRightCorner)
        lay.addWidget(self.tabs)
        return k

    # ------------------------------------------------------------ Einstellungen

    def _einstellungen_laden(self):
        e = kern.einstellungen_lesen()
        code = e.get("sprache")
        i = self.cb_sprache.findData(code) if isinstance(code, str) and code else 0
        self.cb_sprache.setCurrentIndex(i if i >= 0 else 0)
        geraet = e.get("geraet")
        i = self.cb_geraet.findData(geraet if isinstance(geraet, str) else "auto")
        self.cb_geraet.setCurrentIndex(max(i, 0))
        self.chk_srt.setChecked(bool(e.get("srt", False)))
        self.chk_zeit.setChecked(bool(e.get("zeitstempel", False)))
        self.chk_schonend.setChecked(bool(e.get("schonend", False)))

    def _einstellungen_speichern(self):
        sprache = self._sprache_code() or ""
        kern.einstellungen_speichern(
            sprache=sprache,
            geraet=self.cb_geraet.currentData(), srt=self.chk_srt.isChecked(),
            zeitstempel=self.chk_zeit.isChecked(), schonend=self.chk_schonend.isChecked())

    # ------------------------------------------------------------ Hardware & Modelle

    def _hardware_anzeigen(self):
        if not self.gpu:
            return
        paket = kern.gpu_paket_vorhanden()
        self.lbl_hardware.setText(f"⚡ {self.gpu_name}" + ("" if paket else " · GPU-Paket fehlt"))
        self.btn_gpu.setVisible(not paket)

    def _hardware_bekannt(self, info):
        self.gpu = info["gpu"]
        self.gpu_name = info["name"]
        if self.gpu:
            self._hardware_anzeigen()
        else:
            self.lbl_hardware.setText(f"Prozessor · {info['kerne']} Kerne")
            i = self.cb_geraet.findData("cuda")
            self.cb_geraet.model().item(i).setEnabled(False)
            if self.cb_geraet.currentData() == "cuda":
                self.cb_geraet.setCurrentIndex(0)
        self.protokollieren(f"Daten-Ordner: {kern.DATEN}")
        self.protokollieren("Hardware: " + self.lbl_hardware.text().replace("⚡ ", ""))
        self.modelle_fuellen(info["modelle"])
        self.hardware_da = True
        self._erster_start_pruefen()

    def modelle_oeffnen(self, erster_start=False):
        if self.dialog_offen or self.laeuft:       # nie doppelt und nicht mitten in einer Transkription
            return
        from modelle_dialog import ModellDialog
        self.dialog_offen = True
        try:
            dialog = ModellDialog(self, self.gpu, erster_start)
            dialog.exec()
            dialog.deleteLater()
        finally:
            self.dialog_offen = False
        self.modelle_fuellen()
        self._hardware_anzeigen()

    def modelle_fuellen(self, modelle=None):
        letztes = self.cb_modell.currentData() or kern.einstellungen_lesen().get("modell")
        self.empfohlen = kern.EMPFEHLUNG["cuda" if self.gpu else "cpu"]
        self.modelle = modelle if modelle is not None else kern.modell_liste()
        self.cb_modell.blockSignals(True)
        self.cb_modell.clear()
        quelle = "standard"
        for m in self.modelle:
            if m.quelle != quelle:
                self.cb_modell.insertSeparator(self.cb_modell.count())
                quelle = m.quelle
            text = f"{'✓' if m.bereit else '     '}  {m.name}"
            if m.groesse:
                text += f"  ·  {m.groesse}"
            if m.zustand == "umwandeln":
                text += "  (umwandeln nötig)"
            self.cb_modell.addItem(text, m.name)
            self.cb_modell.setItemData(self.cb_modell.count() - 1, m.beschreibung, Qt.ItemDataRole.ToolTipRole)
            if not m.bereit:
                self.cb_modell.model().item(self.cb_modell.count() - 1).setEnabled(False)
        bereit = [m.name for m in self.modelle if m.bereit]
        wahl = next((n for n in (letztes, self.empfohlen) if n in bereit), bereit[0] if bereit else None)
        if wahl:
            self.cb_modell.setCurrentIndex(self.cb_modell.findData(wahl))
        self.cb_modell.blockSignals(False)
        self._modell_info()
        if not bereit:
            self.lbl_status.setText("Noch kein Modell heruntergeladen – «Modelle verwalten …» öffnen.")
        self._knoepfe_aktualisieren()

    def _modell_info(self, *_):
        name = self.cb_modell.currentData()
        m = next((m for m in self.modelle if m.name == name), None)
        if m is None:
            self.lbl_modell_info.setText("")
            return
        teile = [m.beschreibung] if m.beschreibung else []
        if m.name == getattr(self, "empfohlen", ""):
            teile.insert(0, "★ empfohlen")
        text = " · ".join(teile)
        self.lbl_modell_info.setToolTip(text)
        self.lbl_modell_info.setText(self.lbl_modell_info.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, 330))

    # ------------------------------------------------------------ Dateien

    def _zeilen(self):
        return [self.liste.topLevelItem(i) for i in range(self.liste.topLevelItemCount())]

    def _status_setzen(self, item, text, farbe="gedaempft"):
        item.setText(1, text)
        item.setData(0, ROLLE_FARBE, farbe)
        item.setForeground(1, QBrush(QColor(theme.f.get(farbe, theme.f["gedaempft"]))))

    def _farben_auffrischen(self):
        for item in self._zeilen():
            self._status_setzen(item, item.text(1), item.data(0, ROLLE_FARBE) or "gedaempft")
        bild = logo_pixmap(self, 64)
        if bild is not None:
            self.drop.logo.setPixmap(bild)
        self.konverter.farben_auffrischen()

    def dateien_hinzufuegen(self, eingaben):
        dateien, fehlend = kern.dateien_sammeln(eingaben)
        vorhanden = {item.data(0, ROLLE_PFAD) for item in self._zeilen()}
        neu = 0
        for d in dateien:
            if str(d) in vorhanden:
                continue
            item = QTreeWidgetItem([d.name, "", ""])
            item.setData(0, ROLLE_PFAD, str(d))
            item.setData(0, ROLLE_ZUSTAND, "wartet")
            item.setToolTip(0, str(d))
            self.liste.addTopLevelItem(item)
            self._status_setzen(item, "Wartet")
            balken = QProgressBar()
            balken.setObjectName("zeile")
            balken.setRange(0, 1000)
            balken.setValue(0)
            balken.setTextVisible(False)
            halter = QWidget()             # Balken senkrecht mittig in der Zeile
            hl = QHBoxLayout(halter)
            hl.setContentsMargins(4, 0, 8, 0)
            hl.addWidget(balken)
            self.liste.setItemWidget(item, 2, halter)
            neu += 1
        for f in fehlend:
            self.protokollieren(f"Nicht gefunden: {f}")
        if eingaben and not dateien:
            self.lbl_status.setText("Keine Audio- oder Videodateien gefunden.")
        elif neu:
            self.lbl_status.setText(f"{neu} Datei(en) hinzugefügt.")
        self._knoepfe_aktualisieren()

    def _zeilen_balken(self, item) -> QProgressBar:
        return self.liste.itemWidget(item, 2).findChild(QProgressBar)

    def _dateien_waehlen(self):
        filter_ = "Audio/Video (" + " ".join(f"*{e}" for e in sorted(kern.AUDIO_ENDUNGEN)) + ");;Alle Dateien (*.*)"
        pfade, _ = QFileDialog.getOpenFileNames(self, "Audio- oder Videodateien wählen",
                                                self._letzter_ordner(), filter_)
        if pfade:
            kern.einstellungen_speichern(letzter_ordner=str(Path(pfade[0]).parent))
            self.dateien_hinzufuegen(pfade)

    def _ordner_waehlen(self):
        ordner = QFileDialog.getExistingDirectory(self, "Ordner wählen (wird mit Unterordnern durchsucht)",
                                                  self._letzter_ordner())
        if ordner:
            kern.einstellungen_speichern(letzter_ordner=ordner)
            self.dateien_hinzufuegen([ordner])

    @staticmethod
    def _letzter_ordner() -> str:
        ordner = kern.einstellungen_lesen().get("letzter_ordner")
        return ordner if isinstance(ordner, str) else ""

    def _entfernen(self):
        for item in self.liste.selectedItems():
            if item.data(0, ROLLE_ZUSTAND) == "läuft":
                continue
            # Gehört die Zeile zum laufenden Auftrag: dem Arbeiter sagen, dass er sie auslassen soll.
            # (Früher liefen die Rückmeldungen über die Listenposition – nach dem Entfernen trafen sie
            # die falsche oder gar keine Zeile, und die entfernte Datei wurde trotzdem transkribiert.)
            nr = item.data(0, ROLLE_AUFTRAG)
            if self.auftrag_zeilen.pop(nr, None) is not None and self.laeuft:
                self.arbeiter.ausgelassen.add(nr)
                if item.data(0, ROLLE_ZUSTAND) == "wartet":
                    self.anzahl_auftrag = max(self.anzahl_auftrag - 1, 1)
            self.liste.takeTopLevelItem(self.liste.indexOfTopLevelItem(item))
        self._knoepfe_aktualisieren()

    def _leeren(self):
        self.auftrag_zeilen.clear()
        self.liste.clear()
        self.txt_vorschau.clear()
        self.balken.setValue(0)
        self.lbl_prozent.setText("0 %")
        self._knoepfe_aktualisieren()

    def _zeile_oeffnen(self, item, _spalte):
        e = item.data(0, ROLLE_ERGEBNIS)
        if e is not None and e.txt.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(e.txt)))

    def _output_oeffnen(self):
        try:
            kern.OUTPUT_ORDNER.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(kern.OUTPUT_ORDNER)))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        # irgendwo ins Fenster gezogen: gehört zum Werkzeug, das gerade offen ist (sonst zum Transkribieren)
        if _pfade(e):
            if self.seite == "konverter":
                self.konverter.dateien_hinzufuegen(_pfade(e))
            else:
                self.seite_zeigen("transkribieren")
                self.dateien_hinzufuegen(_pfade(e))

    # ------------------------------------------------------------ Starten / Abbrechen

    def _sprache_code(self):
        """None = automatisch, sonst Whisper-Kürzel (nur aus der Liste wählbar)."""
        return self.cb_sprache.currentData() or None

    def _knoepfe_aktualisieren(self):
        n = self.liste.topLevelItemCount()
        hat_modell = any(m.bereit for m in self.modelle)
        self.stapel.setCurrentIndex(1 if n else 0)
        self.lbl_anzahl.setText(str(n))
        self.lbl_anzahl.setVisible(n > 0)
        self.btn_start.setEnabled(not self.laeuft and n > 0 and hat_modell)
        self.btn_start.setVisible(not self.laeuft)
        self.btn_abbrechen.setVisible(self.laeuft)
        self.btn_abbrechen.setEnabled(self.laeuft)
        for w in (self.btn_dateien, self.btn_ordner, self.drop.btn_dateien, self.drop.btn_ordner,
                  self.cb_modell, self.cb_sprache, self.cb_geraet, self.chk_srt, self.chk_zeit,
                  self.chk_schonend, self.btn_modelle, self.btn_gpu):
            w.setEnabled(not self.laeuft)
        self.btn_leeren.setEnabled(not self.laeuft and n > 0)
        self.btn_entfernen.setEnabled(n > 0)
        self._start_hinweise()

    def _gesamt(self, anteil: float):
        anteil = min(max(anteil, 0.0), 1.0)
        self.balken.setValue(int(anteil * 1000))
        self.lbl_prozent.setText(f"{round(anteil * 100)} %")

    def _auftrag_zeile(self, nr):
        """Listenzeile zu einer Auftragsnummer – None, wenn sie inzwischen entfernt wurde."""
        return self.auftrag_zeilen.get(nr)

    def starten(self):
        if self.laeuft:
            return
        sprache = self._sprache_code()
        self._hardware_anzeigen()                 # GPU-Paket vor jedem Start neu prüfen
        modell = self.cb_modell.currentData()
        if not modell or not any(m.name == modell and m.bereit for m in self.modelle):
            QMessageBox.information(self, APP_NAME, "Bitte zuerst ein Modell herunterladen.")
            return
        problem = kern.ausgabe_pruefen()          # jetzt prüfen, nicht erst nach der langen Rechnung
        if problem:
            QMessageBox.warning(
                self, APP_NAME,
                f"In den Output-Ordner kann nicht geschrieben werden:\n{problem}\n\n"
                "Schreibgeschützter Ordner, oder blockiert der «Überwachte Ordnerzugriff» von Windows? "
                "Der Output-Ordner lässt sich in den Einstellungen ändern.")
            return
        dateien = []
        self.auftrag_zeilen = {}
        self.arbeiter.ausgelassen.clear()
        for item in self._zeilen():
            if item.data(0, ROLLE_ZUSTAND) == "fertig":
                continue
            item.setData(0, ROLLE_ZUSTAND, "wartet")
            self._status_setzen(item, "Wartet")
            self._zeilen_balken(item).setValue(0)
            self.auftrag_nr += 1
            item.setData(0, ROLLE_AUFTRAG, self.auftrag_nr)
            self.auftrag_zeilen[self.auftrag_nr] = item
            dateien.append((self.auftrag_nr, Path(item.data(0, ROLLE_PFAD))))
        if not dateien:
            self.lbl_status.setText("Alle Dateien sind schon fertig. Neue Dateien hinzufügen oder Liste leeren.")
            return

        self.hinweis = ""
        kern.einstellungen_speichern(modell=modell)
        self._einstellungen_speichern()
        self.laeuft = True
        self.anzahl_auftrag = len(dateien)
        self.fertig_auftrag = 0
        self._gesamt(0)
        self._knoepfe_aktualisieren()
        self.protokollieren(f"Start: {len(dateien)} Datei(en), Modell {modell}, "
                            f"Sprache {sprache or 'automatisch'}")
        self.arbeiter.abbrechen.clear()
        self.auftrag.emit({
            "dateien": dateien, "modell": modell, "geraet": self.cb_geraet.currentData(), "sprache": sprache,
            "srt": self.chk_srt.isChecked(), "zeitstempel": self.chk_zeit.isChecked(),
            "schonend": self.chk_schonend.isChecked(),
            "threads": kern.cpu_threads(kern.cpu_threads_einstellung()),
        })

    def abbrechen(self):
        self.arbeiter.abbrechen.set()
        self.btn_abbrechen.setEnabled(False)
        self.lbl_status.setText("Wird abgebrochen …")

    # ------------------------------------------------------------ Rückmeldungen des Arbeiters

    def protokollieren(self, text):
        self.txt_protokoll.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    def _geraet_bereit(self, dev):
        self.hinweis = ""
        if dev == "cpu" and self.cb_geraet.currentData() != "cpu" and (self.gpu or self.cb_geraet.currentData() == "cuda"):
            self.hinweis = "Hinweis: lief auf der CPU, weil die GPU nicht nutzbar war (Details im Protokoll)."
            self.protokollieren(self.hinweis)
        self._hardware_anzeigen()

    def _datei_start(self, zeile):
        item = self._auftrag_zeile(zeile)
        if item is None:
            return
        item.setData(0, ROLLE_ZUSTAND, "läuft")
        self._status_setzen(item, "Läuft …", "akzent_text")
        self.liste.scrollToItem(item)
        self.datei_start_zeit = time.time()
        self.datei_sprache = ""
        name = Path(item.data(0, ROLLE_PFAD)).name
        self.vorschau_kopf = f"── {name} ──"      # erst beim ersten Text schreiben (leere Dateien ohne Kopf)
        self.lbl_status.setText(f"Datei {self.fertig_auftrag + 1} von {self.anzahl_auftrag}: {name}")

    def _sprache(self, zeile, sprache, wahrscheinlichkeit, erkannt):
        self.datei_sprache = (f"{sprach_name(sprache)} ({wahrscheinlichkeit:.0%})" if erkannt
                              else sprach_name(sprache))
        if erkannt:
            self.protokollieren(f"Erkannte Sprache: {sprache} ({wahrscheinlichkeit:.0%})")

    def _fortschritt(self, zeile, anteil, position, dauer):
        item = self._auftrag_zeile(zeile)
        if item is None:
            return
        self._zeilen_balken(item).setValue(int(anteil * 1000))
        vergangen = max(time.time() - self.datei_start_zeit, 0.1)
        tempo = position / vergangen
        self._status_setzen(item, f"Läuft {anteil:.0%} · {tempo:.1f}x Echtzeit", "akzent_text")
        self._gesamt((self.fertig_auftrag + anteil) / max(self.anzahl_auftrag, 1))
        name = Path(item.data(0, ROLLE_PFAD)).name
        teile = [f"Datei {self.fertig_auftrag + 1} von {self.anzahl_auftrag}: {name}"]
        if self.datei_sprache:
            teile.append(self.datei_sprache)
        teile.append(f"{kern.zeit(position)} / {kern.zeit(dauer)}")
        teile.append(f"{tempo:.1f}x Echtzeit")
        self.lbl_status.setText("  ·  ".join(teile))

    def _segment(self, zeile, text):
        if self._auftrag_zeile(zeile) is None:
            return
        if self.vorschau_kopf:
            if not self.txt_vorschau.document().isEmpty():
                self.txt_vorschau.appendPlainText("")
            self.txt_vorschau.appendPlainText(self.vorschau_kopf)
            self.vorschau_kopf = ""
        self.txt_vorschau.appendPlainText(text)

    def _datei_fertig(self, zeile, e):
        item = self._auftrag_zeile(zeile)
        if item is None:
            self.protokollieren(f"Fertig (Zeile wurde entfernt): {e.datei.name} → {e.txt.name}")
            return
        item.setData(0, ROLLE_ZUSTAND, "fertig")
        item.setData(0, ROLLE_ERGEBNIS, e)
        self._status_setzen(item, f"✓ Fertig · {e.sprache} · {kern.zeit(e.dauer)} · {e.tempo:.1f}x", "erfolg")
        item.setToolTip(1, f"Gespeichert: {e.txt}" + (f"\n{e.srt}" if e.srt else "") + "\nDoppelklick öffnet die .txt")
        self._zeilen_balken(item).setValue(1000)
        self.fertig_auftrag += 1
        self.protokollieren(f"Fertig: {e.datei.name} → {e.txt.name}"
                            + (f" + {e.srt.name}" if e.srt else "")
                            + f" ({e.benoetigt:.0f} s, {e.tempo:.1f}x Echtzeit)")

    def _datei_fehler(self, zeile, kurz, lang):
        item = self._auftrag_zeile(zeile)
        if item is None:
            return
        if kurz == "Abgebrochen":
            item.setData(0, ROLLE_ZUSTAND, "wartet")
            self._status_setzen(item, "Abgebrochen")
        else:
            item.setData(0, ROLLE_ZUSTAND, "fehler")
            self._status_setzen(item, f"✗ {kurz}", "warnung" if kurz == "Kein Text erkannt" else "fehler")
            self.fertig_auftrag += 1
        if lang:
            item.setToolTip(1, lang)
        self.protokollieren(f"{Path(item.data(0, ROLLE_PFAD)).name}: {lang or kurz}")

    def _alles_fertig(self, abgebrochen):
        self.laeuft = False
        self._knoepfe_aktualisieren()
        zustaende = [item.data(0, ROLLE_ZUSTAND) for item in self._zeilen()]
        fertig, fehler = zustaende.count("fertig"), zustaende.count("fehler")
        if abgebrochen:
            self.lbl_status.setText("Abgebrochen. Mit «Transkribieren» geht es bei der unterbrochenen Datei weiter.")
        elif self.lbl_status.text().startswith("Fehler"):
            pass
        else:
            self._gesamt(1.0)
            self.lbl_status.setText(f"✓ Alles erledigt: {fertig} fertig" + (f", {fehler} mit Problem" if fehler else "")
                                    + ". Die Texte liegen im Output-Ordner."
                                    + (f"\n{self.hinweis}" if getattr(self, "hinweis", "") else ""))
            self._fertig_aktionen(sum(1 for item in self.auftrag_zeilen.values()
                                      if item.data(0, ROLLE_ZUSTAND) == "fertig"))
        self.protokollieren("Abgebrochen." if abgebrochen else "Erledigt.")

    # ------------------------------------------------------------ Schliessen

    def closeEvent(self, e):
        if self.laeuft or self.konverter.laeuft:
            was = "transkribiert" if self.laeuft else "umgewandelt"
            antwort = QMessageBox.question(self, APP_NAME,
                                           f"Es wird gerade {was}. Wirklich beenden?\n"
                                           "Die laufende Datei wird nicht gespeichert.")
            if antwort != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.arbeiter.abbrechen.set()
        self._einstellungen_speichern()
        if kern.einstellung("fenster_merken", True):
            kern.einstellungen_speichern(fenster=bytes(self.saveGeometry().toBase64()).decode("ascii"))
        konverter_fertig = self.konverter.beenden()      # bricht ab; halbe MP3s werden dabei gelöscht
        self.thread_.quit()
        if not self.thread_.wait(3000) or not konverter_fertig:
            os._exit(0)        # Modell hängt noch in einer langen Berechnung – hart beenden
        e.accept()
