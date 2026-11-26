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

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QMainWindow, QScrollArea, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSplitter, QStackedWidget, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

import kern
import theme

APP_NAME = "InkWave"
ROLLE_PFAD = Qt.ItemDataRole.UserRole
ROLLE_ERGEBNIS = Qt.ItemDataRole.UserRole + 1
ROLLE_ZUSTAND = Qt.ItemDataRole.UserRole + 2     # "wartet" | "läuft" | "fertig" | "fehler"
ROLLE_FARBE = Qt.ItemDataRole.UserRole + 3       # Farbname aus theme.f für die Status-Spalte

GERAETE = [("auto", "Automatisch"), ("cuda", "Grafikkarte (GPU)"), ("cpu", "Prozessor (CPU)")]


def sprach_name(code: str) -> str:
    for c, name in kern.SPRACHEN:
        if c == code:
            return name
    return code


def logo_pixmap(widget, groesse: int):
    pfad = kern.APP_ORDNER / "inkwave.png"
    if not pfad.exists():
        return None
    dpr = widget.devicePixelRatioF()
    bild = QPixmap(str(pfad)).scaled(int(groesse * dpr), int(groesse * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
    bild.setDevicePixelRatio(dpr)
    return bild


def karte() -> QFrame:
    k = QFrame()
    k.setObjectName("karte")
    return k


def beschriftung(text: str, art: str = "") -> QLabel:
    lbl = QLabel(text)
    if art:
        lbl.setObjectName(art)
    return lbl


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
        self._modell = self._batch = self._dev = None
        self._schluessel = None

    def _melden(self, art, **d):
        if art in ("status", "warnung"):
            self.protokoll.emit(d["text"])
            if art == "status":
                self.status.emit(d["text"])

    @Slot()
    def pruefen(self):
        """Beim Start im Hintergrund: Grafikkarte, Kerne, Modelle (Import von faster-whisper dauert)."""
        gpu = kern.gpu_vorhanden()
        self.hardware.emit({
            "gpu": gpu,
            "name": kern.gpu_name() if gpu else "",
            "paket": kern.gpu_paket_vorhanden() if gpu else False,
            "kerne": kern.physische_kerne(),
            "modelle": kern.modell_liste(),
        })

    @Slot(object)
    def arbeiten(self, auftrag: dict):
        self.abbrechen.clear()
        kern.schonend_rechnen(auftrag["schonend"])
        try:
            m = kern.modell_finden(auftrag["modell"])
            if m is None or not m.bereit:
                raise RuntimeError(f"Modell «{auftrag['modell']}» ist nicht heruntergeladen.")
            # GPU-Paket gehört dazu: nach dem Einrichten wird das Modell neu (auf der GPU) geladen
            schluessel = (m.name, m.pfad, auftrag["geraet"], auftrag["threads"], kern.gpu_paket_vorhanden())
            if schluessel != self._schluessel:
                self._modell = self._batch = None      # altes Modell zuerst freigeben (Speicher)
                gc.collect()
                self._modell, self._batch, self._dev = kern.modell_laden(
                    m, auftrag["geraet"], self._melden, threads=auftrag["threads"])
                self._schluessel = schluessel
            self.geraet_bereit.emit(self._dev)

            with kern.WachHalten():
                for zeile, datei in auftrag["dateien"]:
                    if self.abbrechen.is_set():
                        break
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

    def __init__(self):
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
        t = beschriftung("Audio- oder Videodateien hierher ziehen", "gross")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        u = beschriftung("auch ganze Ordner · mp3, m4a, wav, flac, ogg, mp4, mkv, mov …", "gedaempft")
        u.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(u)
        lay.addSpacing(14)
        knoepfe = QHBoxLayout()
        knoepfe.addStretch(1)
        self.btn_dateien = QPushButton("Dateien wählen …")
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
        self.setMinimumSize(860, 560)
        self.setAcceptDrops(True)

        self.gpu = False
        self.gpu_name = ""
        self.laeuft = False
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
        self.pruef_anfrage.emit()

    # ------------------------------------------------------------ Aufbau

    def _oberflaeche_bauen(self):
        zentral = QWidget()
        self.setCentralWidget(zentral)
        haupt = QVBoxLayout(zentral)
        haupt.setContentsMargins(22, 16, 22, 18)
        haupt.setSpacing(14)
        haupt.addLayout(self._kopf_bauen())

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
        haupt.addWidget(teiler, 1)

    def _teiler_anpassen(self):
        """Beim Start: oben so viel Platz, dass die Einstellungen ganz sichtbar sind; Rest für den Text."""
        noetig = self.innen.sizeHint().height() + 8
        gesamt = sum(self.teiler.sizes())
        oben = max(min(noetig, gesamt - 170), int(gesamt * 0.5))
        self.teiler.setSizes([oben, gesamt - oben])

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_angepasst", False):
            self._angepasst = True
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._teiler_anpassen)

    def _kopf_bauen(self):
        kopf = QHBoxLayout()
        kopf.setSpacing(12)
        logo = QLabel()
        bild = logo_pixmap(self, 48)
        if bild is not None:
            logo.setPixmap(bild)
            kopf.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)
        t = QVBoxLayout()
        t.setSpacing(0)
        t.addWidget(beschriftung(APP_NAME, "titel"))
        t.addWidget(beschriftung("Audio und Video lokal in Text verwandeln – offline, mit Whisper", "untertitel"))
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
        return kopf

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

        zeile = QHBoxLayout()
        zeile.addWidget(beschriftung("Modell", "gedaempft"))
        zeile.addStretch(1)
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
        self.lbl_modell_info.setMinimumWidth(1)
        lay.addWidget(self.lbl_modell_info)

        lay.addWidget(beschriftung("Sprache", "gedaempft"))
        self.cb_sprache = QComboBox()
        self.cb_sprache.setEditable(True)
        for code, name in kern.SPRACHEN:
            self.cb_sprache.addItem(name if not code else f"{code} – {name}", code)
        self.cb_sprache.setToolTip("Automatisch erkennen oder fest einstellen.\n"
                                   "Auch andere Whisper-Kürzel gehen, z. B. «ja» oder «sv».")
        lay.addWidget(self.cb_sprache)
        lay.addSpacing(4)

        lay.addWidget(beschriftung("Rechnen auf", "gedaempft"))
        self.cb_geraet = QComboBox()
        for code, name in GERAETE:
            self.cb_geraet.addItem(name, code)
        lay.addWidget(self.cb_geraet)
        lay.addSpacing(10)

        self.chk_srt = QCheckBox(".srt-Untertitel erstellen")
        self.chk_zeit = QCheckBox("Zeitstempel in der .txt")
        self.chk_schonend = QCheckBox("Schonend rechnen")
        self.chk_schonend.setToolTip("Niedrigere Priorität: der PC bleibt gut bedienbar, "
                                     "die Transkription dauert etwas länger.")
        for c in (self.chk_srt, self.chk_zeit, self.chk_schonend):
            lay.addWidget(c)
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
        code = e.get("sprache", "")
        i = self.cb_sprache.findData(code)
        if i >= 0:
            self.cb_sprache.setCurrentIndex(i)
        else:
            self.cb_sprache.setEditText(code)
        i = self.cb_geraet.findData(e.get("geraet", "auto"))
        self.cb_geraet.setCurrentIndex(max(i, 0))
        self.chk_srt.setChecked(bool(e.get("srt", False)))
        self.chk_zeit.setChecked(bool(e.get("zeitstempel", False)))
        self.chk_schonend.setChecked(bool(e.get("schonend", False)))

    def _einstellungen_speichern(self):
        try:
            sprache = self._sprache_code() or ""
        except ValueError:
            sprache = ""
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
        if not kern.einstellungen_lesen().get("eingerichtet"):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(200, lambda: self.modelle_oeffnen(erster_start=True))

    def modelle_oeffnen(self, erster_start=False):
        from modelle_dialog import ModellDialog
        dialog = ModellDialog(self, self.gpu, erster_start)
        dialog.exec()
        self.modelle_fuellen()
        self._hardware_anzeigen()
        dialog.deleteLater()

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
                                                kern.einstellungen_lesen().get("letzter_ordner", ""), filter_)
        if pfade:
            kern.einstellungen_speichern(letzter_ordner=str(Path(pfade[0]).parent))
            self.dateien_hinzufuegen(pfade)

    def _ordner_waehlen(self):
        ordner = QFileDialog.getExistingDirectory(self, "Ordner wählen (wird mit Unterordnern durchsucht)",
                                                  kern.einstellungen_lesen().get("letzter_ordner", ""))
        if ordner:
            kern.einstellungen_speichern(letzter_ordner=ordner)
            self.dateien_hinzufuegen([ordner])

    def _entfernen(self):
        for item in self.liste.selectedItems():
            if item.data(0, ROLLE_ZUSTAND) != "läuft":
                self.liste.takeTopLevelItem(self.liste.indexOfTopLevelItem(item))
        self._knoepfe_aktualisieren()

    def _leeren(self):
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
        kern.OUTPUT_ORDNER.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(kern.OUTPUT_ORDNER)))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if _pfade(e):
            self.dateien_hinzufuegen(_pfade(e))

    # ------------------------------------------------------------ Starten / Abbrechen

    def _sprache_code(self):
        """None = automatisch, sonst Whisper-Kürzel. Wirft ValueError bei Unbekanntem."""
        text = self.cb_sprache.currentText().strip()
        i = self.cb_sprache.currentIndex()
        if i >= 0 and text == self.cb_sprache.itemText(i):
            code = self.cb_sprache.itemData(i)
        else:
            code = text.split("–")[0].strip().lower()
        if not code or code.startswith("automatisch"):
            return None
        from faster_whisper.tokenizer import _LANGUAGE_CODES
        if code not in _LANGUAGE_CODES:
            raise ValueError(f"Unbekannte Sprache «{code}». Beispiele: de, en, fr, it, es.")
        return code

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
                  self.chk_schonend, self.btn_modelle):
            w.setEnabled(not self.laeuft)
        self.btn_leeren.setEnabled(not self.laeuft and n > 0)
        self.btn_entfernen.setEnabled(n > 0)

    def _gesamt(self, anteil: float):
        self.balken.setValue(int(anteil * 1000))
        self.lbl_prozent.setText(f"{round(anteil * 100)} %")

    def starten(self):
        try:
            sprache = self._sprache_code()
        except ValueError as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        modell = self.cb_modell.currentData()
        if not modell:
            QMessageBox.information(self, APP_NAME, "Bitte zuerst ein Modell herunterladen.")
            return
        dateien = []
        for i, item in enumerate(self._zeilen()):
            if item.data(0, ROLLE_ZUSTAND) == "fertig":
                continue
            item.setData(0, ROLLE_ZUSTAND, "wartet")
            self._status_setzen(item, "Wartet")
            self._zeilen_balken(item).setValue(0)
            dateien.append((i, Path(item.data(0, ROLLE_PFAD))))
        if not dateien:
            self.lbl_status.setText("Alle Dateien sind schon fertig. Neue Dateien hinzufügen oder Liste leeren.")
            return

        kern.einstellungen_speichern(modell=modell)
        self._einstellungen_speichern()
        self.laeuft = True
        self.anzahl_auftrag = len(dateien)
        self.fertig_auftrag = 0
        self._gesamt(0)
        self._knoepfe_aktualisieren()
        self.protokollieren(f"Start: {len(dateien)} Datei(en), Modell {modell}, "
                            f"Sprache {sprache or 'automatisch'}")
        self.auftrag.emit({
            "dateien": dateien, "modell": modell, "geraet": self.cb_geraet.currentData(), "sprache": sprache,
            "srt": self.chk_srt.isChecked(), "zeitstempel": self.chk_zeit.isChecked(),
            "schonend": self.chk_schonend.isChecked(),
            "threads": kern.cpu_threads(int(kern.einstellungen_lesen().get("cpu_threads") or 0)),
        })

    def abbrechen(self):
        self.arbeiter.abbrechen.set()
        self.btn_abbrechen.setEnabled(False)
        self.lbl_status.setText("Wird abgebrochen …")

    # ------------------------------------------------------------ Rückmeldungen des Arbeiters

    def protokollieren(self, text):
        self.txt_protokoll.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    def _geraet_bereit(self, dev):
        if dev == "cpu" and self.gpu and self.cb_geraet.currentData() != "cpu":
            self.protokollieren("Hinweis: läuft auf der CPU, weil die GPU nicht nutzbar ist.")

    def _datei_start(self, zeile):
        item = self.liste.topLevelItem(zeile)
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
        item = self.liste.topLevelItem(zeile)
        self._zeilen_balken(item).setValue(int(anteil * 1000))
        vergangen = max(time.time() - self.datei_start_zeit, 0.1)
        tempo = position / vergangen
        self._status_setzen(item, f"Läuft {anteil:.0%} · {tempo:.1f}x Echtzeit", "akzent_text")
        self._gesamt((self.fertig_auftrag + anteil) / self.anzahl_auftrag)
        name = Path(item.data(0, ROLLE_PFAD)).name
        teile = [f"Datei {self.fertig_auftrag + 1} von {self.anzahl_auftrag}: {name}"]
        if self.datei_sprache:
            teile.append(self.datei_sprache)
        teile.append(f"{kern.zeit(position)} / {kern.zeit(dauer)}")
        teile.append(f"{tempo:.1f}x Echtzeit")
        self.lbl_status.setText("  ·  ".join(teile))

    def _segment(self, zeile, text):
        if self.vorschau_kopf:
            if not self.txt_vorschau.document().isEmpty():
                self.txt_vorschau.appendPlainText("")
            self.txt_vorschau.appendPlainText(self.vorschau_kopf)
            self.vorschau_kopf = ""
        self.txt_vorschau.appendPlainText(text)

    def _datei_fertig(self, zeile, e):
        item = self.liste.topLevelItem(zeile)
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
        item = self.liste.topLevelItem(zeile)
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
                                    + ". Die Texte liegen im Output-Ordner.")
        self.protokollieren("Abgebrochen." if abgebrochen else "Erledigt.")

    # ------------------------------------------------------------ Schliessen

    def closeEvent(self, e):
        if self.laeuft:
            antwort = QMessageBox.question(self, APP_NAME,
                                           "Es wird gerade transkribiert. Wirklich beenden?\n"
                                           "Die laufende Datei wird nicht gespeichert.")
            if antwort != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.arbeiter.abbrechen.set()
        self._einstellungen_speichern()
        self.thread_.quit()
        if not self.thread_.wait(3000):
            os._exit(0)        # Modell hängt noch in einer langen Berechnung – hart beenden
        e.accept()
