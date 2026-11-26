"""
gui.py – das Hauptfenster des Transcribers (PySide6).

Die eigentliche Arbeit (Modell laden, transkribieren) läuft im Arbeiter-Objekt in einem eigenen
Thread; es meldet sich nur über Signale. So bleibt das Fenster bedienbar.
"""

import gc
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QPainter, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

import kern

ROLLE_PFAD = Qt.ItemDataRole.UserRole
ROLLE_ERGEBNIS = Qt.ItemDataRole.UserRole + 1
ROLLE_ZUSTAND = Qt.ItemDataRole.UserRole + 2     # "wartet" | "läuft" | "fertig" | "fehler"

GERAETE = [("auto", "Automatisch"), ("cuda", "Grafikkarte (GPU)"), ("cpu", "Prozessor (CPU)")]


def sprach_name(code: str) -> str:
    for c, name in kern.SPRACHEN:
        if c == code:
            return name
    return code


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
            schluessel = (m.name, m.pfad, auftrag["geraet"], auftrag["threads"])
            if schluessel != self._schluessel:
                self._modell = self._batch = None      # altes Modell zuerst freigeben (Speicher)
                gc.collect()
                self._modell, self._batch, self._dev = kern.modell_laden(
                    m, auftrag["geraet"], self._melden, cpu_threads=auftrag["threads"])
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


# ---------------------------------------------------------------- Dateiliste mit Ziehen & Ablegen

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
        self.setColumnWidth(1, 260)
        self.setColumnWidth(2, 140)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        pfade = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if pfade:
            e.acceptProposedAction()
            self.abgelegt.emit(pfade)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.topLevelItemCount() == 0:
            p = QPainter(self.viewport())
            farbe = self.palette().placeholderText().color()
            p.setPen(farbe)
            schrift = QFont(self.font())
            schrift.setPointSizeF(schrift.pointSizeF() * 1.25)
            p.setFont(schrift)
            p.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                       "Audio- oder Videodateien hierher ziehen\n(auch ganze Ordner)\n\n"
                       "oder «Dateien…» / «Ordner…» wählen")
            p.end()


# ---------------------------------------------------------------- Hauptfenster

class Hauptfenster(QMainWindow):
    auftrag = Signal(object)
    pruef_anfrage = Signal()

    def __init__(self, start_dateien=()):
        super().__init__()
        self.setWindowTitle("Transcriber")
        ico = kern.APP_ORDNER / "transcriber.ico"
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))
        self.resize(1000, 760)
        self.setAcceptDrops(True)

        self.gpu = False
        self.laeuft = False
        self.modelle = []
        self.datei_start_zeit = 0.0
        self.datei_sprache = ""
        self.vorschau_kopf = ""
        self.anzahl_auftrag = 0
        self.fertig_auftrag = 0

        self._oberflaeche_bauen()
        self._einstellungen_laden()

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
        self.pruef_anfrage.emit()

    # ------------------------------------------------------------ Aufbau

    def _oberflaeche_bauen(self):
        zentral = QWidget()
        self.setCentralWidget(zentral)
        haupt = QVBoxLayout(zentral)
        haupt.setContentsMargins(16, 12, 16, 12)
        haupt.setSpacing(10)

        # Kopfzeile
        kopf = QHBoxLayout()
        titel = QLabel("Transcriber")
        f = titel.font()
        f.setPointSizeF(f.pointSizeF() * 1.7)
        f.setBold(True)
        titel.setFont(f)
        untertitel = QLabel("Audio und Video lokal in Text umwandeln – offline, mit Whisper")
        untertitel.setStyleSheet("color: palette(placeholder-text);")
        t = QVBoxLayout()
        t.setSpacing(0)
        t.addWidget(titel)
        t.addWidget(untertitel)
        kopf.addLayout(t)
        kopf.addStretch(1)
        self.lbl_hardware = QLabel("Hardware wird geprüft …")
        self.lbl_hardware.setFrameShape(QFrame.Shape.StyledPanel)
        self.lbl_hardware.setContentsMargins(10, 4, 10, 4)
        kopf.addWidget(self.lbl_hardware, 0, Qt.AlignmentFlag.AlignVCenter)
        haupt.addLayout(kopf)

        # Dateiliste + Knöpfe
        oben = QWidget()
        ol = QVBoxLayout(oben)
        ol.setContentsMargins(0, 0, 0, 0)
        self.liste = DateiListe()
        self.liste.abgelegt.connect(self.dateien_hinzufuegen)
        self.liste.itemDoubleClicked.connect(self._zeile_oeffnen)
        ol.addWidget(self.liste)
        knoepfe = QHBoxLayout()
        self.btn_dateien = QPushButton("Dateien …")
        self.btn_ordner = QPushButton("Ordner …")
        self.btn_entfernen = QPushButton("Entfernen")
        self.btn_leeren = QPushButton("Liste leeren")
        self.btn_dateien.clicked.connect(self._dateien_waehlen)
        self.btn_ordner.clicked.connect(self._ordner_waehlen)
        self.btn_entfernen.clicked.connect(self._entfernen)
        self.btn_leeren.clicked.connect(self._leeren)
        for b in (self.btn_dateien, self.btn_ordner, self.btn_entfernen, self.btn_leeren):
            knoepfe.addWidget(b)
        knoepfe.addStretch(1)
        self.btn_output = QPushButton("Output-Ordner öffnen")
        self.btn_output.clicked.connect(self._output_oeffnen)
        knoepfe.addWidget(self.btn_output)
        ol.addLayout(knoepfe)

        # Einstellungen
        gruppe = QGroupBox("Einstellungen")
        g = QGridLayout(gruppe)
        g.setHorizontalSpacing(12)
        self.cb_modell = QComboBox()
        self.cb_modell.setMinimumContentsLength(40)
        self.cb_sprache = QComboBox()
        self.cb_sprache.setEditable(True)
        for code, name in kern.SPRACHEN:
            self.cb_sprache.addItem(name if not code else f"{code} – {name}", code)
        self.cb_sprache.setToolTip("Automatisch erkennen oder fest einstellen.\n"
                                   "Auch andere Whisper-Kürzel gehen, z. B. «ja» oder «sv».")
        self.cb_geraet = QComboBox()
        for code, name in GERAETE:
            self.cb_geraet.addItem(name, code)
        self.chk_srt = QCheckBox(".srt-Untertitel erstellen")
        self.chk_zeit = QCheckBox("Zeitstempel in der .txt")
        self.chk_schonend = QCheckBox("Schonend rechnen")
        self.chk_schonend.setToolTip("Niedrigere Priorität: der PC bleibt gut bedienbar, "
                                     "die Transkription dauert etwas länger.")
        g.addWidget(QLabel("Modell"), 0, 0)
        g.addWidget(self.cb_modell, 0, 1, 1, 3)
        g.addWidget(QLabel("Sprache"), 1, 0)
        g.addWidget(self.cb_sprache, 1, 1)
        g.addWidget(QLabel("Rechnen auf"), 1, 2)
        g.addWidget(self.cb_geraet, 1, 3)
        optionen = QHBoxLayout()
        optionen.addWidget(self.chk_srt)
        optionen.addWidget(self.chk_zeit)
        optionen.addWidget(self.chk_schonend)
        optionen.addStretch(1)
        g.addLayout(optionen, 2, 1, 1, 3)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        ol.addWidget(gruppe)

        # Start / Abbrechen / Fortschritt
        lauf = QHBoxLayout()
        self.btn_start = QPushButton("Transkribieren")
        self.btn_start.setDefault(True)
        self.btn_start.setMinimumHeight(34)
        sf = self.btn_start.font()
        sf.setBold(True)
        self.btn_start.setFont(sf)
        self.btn_abbrechen = QPushButton("Abbrechen")
        self.btn_abbrechen.setMinimumHeight(34)
        self.btn_abbrechen.setEnabled(False)
        self.btn_start.clicked.connect(self.starten)
        self.btn_abbrechen.clicked.connect(self.abbrechen)
        self.balken = QProgressBar()
        self.balken.setRange(0, 1000)
        self.balken.setValue(0)
        self.balken.setFormat("%p %")
        lauf.addWidget(self.btn_start)
        lauf.addWidget(self.btn_abbrechen)
        lauf.addWidget(self.balken, 1)
        ol.addLayout(lauf)
        self.lbl_status = QLabel("Bereit.")
        self.lbl_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ol.addWidget(self.lbl_status)

        # Text-Vorschau und Protokoll
        self.tabs = QTabWidget()
        self.txt_vorschau = QPlainTextEdit()
        self.txt_vorschau.setReadOnly(True)
        self.txt_vorschau.setPlaceholderText("Hier erscheint der erkannte Text, während transkribiert wird.")
        self.txt_protokoll = QPlainTextEdit()
        self.txt_protokoll.setReadOnly(True)
        self.tabs.addTab(self.txt_vorschau, "Text")
        self.tabs.addTab(self.txt_protokoll, "Protokoll")

        teiler = QSplitter(Qt.Orientation.Vertical)
        teiler.addWidget(oben)
        teiler.addWidget(self.tabs)
        teiler.setStretchFactor(0, 3)
        teiler.setStretchFactor(1, 2)
        teiler.setChildrenCollapsible(False)
        haupt.addWidget(teiler, 1)

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

    def _hardware_bekannt(self, info):
        self.gpu = info["gpu"]
        if self.gpu:
            paket = "" if info["paket"] else "  (GPU-Paket fehlt noch → CPU)"
            self.lbl_hardware.setText(f"⚡ {info['name']}{paket}")
        else:
            self.lbl_hardware.setText(f"Prozessor · {info['kerne']} Kerne · ohne Grafikkarte")
            i = self.cb_geraet.findData("cuda")
            self.cb_geraet.model().item(i).setEnabled(False)
            if self.cb_geraet.currentData() == "cuda":
                self.cb_geraet.setCurrentIndex(0)
        self.protokollieren(f"Daten-Ordner: {kern.DATEN}")
        self.protokollieren(self.lbl_hardware.text().replace("⚡ ", "Grafikkarte: "))
        self.modelle_fuellen(info["modelle"])

    def modelle_fuellen(self, modelle=None):
        letztes = self.cb_modell.currentData() or kern.einstellungen_lesen().get("modell")
        empfohlen = kern.EMPFEHLUNG["cuda" if self.gpu else "cpu"]
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
            if m.beschreibung:
                text += f"  –  {m.beschreibung}"
            if m.name == empfohlen:
                text += "   ← empfohlen"
            if m.zustand == "umwandeln":
                text += "  (umwandeln nötig)"
            self.cb_modell.addItem(text, m.name)
            if not m.bereit:
                self.cb_modell.model().item(self.cb_modell.count() - 1).setEnabled(False)
        bereit = [m.name for m in self.modelle if m.bereit]
        wahl = next((n for n in (letztes, empfohlen) if n in bereit), bereit[0] if bereit else None)
        if wahl:
            self.cb_modell.setCurrentIndex(self.cb_modell.findData(wahl))
        self.cb_modell.blockSignals(False)
        self.cb_modell.setToolTip("✓ = heruntergeladen. Weitere Modelle: Menü «Modelle» (folgt).")
        if not bereit:
            self.lbl_status.setText("Noch kein Modell heruntergeladen.")
        self._knoepfe_aktualisieren()

    # ------------------------------------------------------------ Dateien

    def dateien_hinzufuegen(self, eingaben):
        dateien, fehlend = kern.dateien_sammeln(eingaben)
        vorhanden = {self.liste.topLevelItem(i).data(0, ROLLE_PFAD) for i in range(self.liste.topLevelItemCount())}
        neu = 0
        for d in dateien:
            if str(d) in vorhanden:
                continue
            item = QTreeWidgetItem([d.name, "Wartet", ""])
            item.setData(0, ROLLE_PFAD, str(d))
            item.setData(0, ROLLE_ZUSTAND, "wartet")
            item.setToolTip(0, str(d))
            self.liste.addTopLevelItem(item)
            balken = QProgressBar()
            balken.setRange(0, 1000)
            balken.setValue(0)
            balken.setTextVisible(False)
            balken.setMaximumHeight(14)
            self.liste.setItemWidget(item, 2, balken)
            neu += 1
        for f in fehlend:
            self.protokollieren(f"Nicht gefunden: {f}")
        if eingaben and not dateien:
            self.lbl_status.setText("Keine Audio- oder Videodateien gefunden.")
        elif neu:
            self.lbl_status.setText(f"{neu} Datei(en) hinzugefügt.")
        self._knoepfe_aktualisieren()

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
        pfade = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if pfade:
            self.dateien_hinzufuegen(pfade)

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
        hat_dateien = self.liste.topLevelItemCount() > 0
        hat_modell = any(m.bereit for m in self.modelle)
        self.btn_start.setEnabled(not self.laeuft and hat_dateien and hat_modell)
        self.btn_abbrechen.setEnabled(self.laeuft)
        for w in (self.btn_dateien, self.btn_ordner, self.btn_leeren, self.cb_modell, self.cb_sprache,
                  self.cb_geraet, self.chk_srt, self.chk_zeit, self.chk_schonend):
            w.setEnabled(not self.laeuft)
        self.btn_entfernen.setEnabled(hat_dateien)

    def starten(self):
        try:
            sprache = self._sprache_code()
        except ValueError as e:
            QMessageBox.warning(self, "Transcriber", str(e))
            return
        modell = self.cb_modell.currentData()
        if not modell:
            QMessageBox.information(self, "Transcriber", "Bitte zuerst ein Modell herunterladen.")
            return
        dateien = []
        for i in range(self.liste.topLevelItemCount()):
            item = self.liste.topLevelItem(i)
            if item.data(0, ROLLE_ZUSTAND) == "fertig":
                continue
            item.setData(0, ROLLE_ZUSTAND, "wartet")
            item.setText(1, "Wartet")
            self.liste.itemWidget(item, 2).setValue(0)
            dateien.append((i, Path(item.data(0, ROLLE_PFAD))))
        if not dateien:
            self.lbl_status.setText("Alle Dateien sind schon fertig. Neue Dateien hinzufügen oder Liste leeren.")
            return

        kern.einstellungen_speichern(modell=modell)
        self._einstellungen_speichern()
        self.laeuft = True
        self.anzahl_auftrag = len(dateien)
        self.fertig_auftrag = 0
        self.balken.setValue(0)
        self._knoepfe_aktualisieren()
        self.protokollieren(f"Start: {len(dateien)} Datei(en), Modell {modell}, "
                            f"Sprache {sprache or 'automatisch'}")
        self.auftrag.emit({
            "dateien": dateien, "modell": modell, "geraet": self.cb_geraet.currentData(), "sprache": sprache,
            "srt": self.chk_srt.isChecked(), "zeitstempel": self.chk_zeit.isChecked(),
            "schonend": self.chk_schonend.isChecked(),
            "threads": int(kern.einstellungen_lesen().get("cpu_threads") or kern.physische_kerne()),
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
        item.setText(1, "Läuft …")
        self.liste.scrollToItem(item)
        self.datei_start_zeit = time.time()
        self.datei_sprache = ""
        name = Path(item.data(0, ROLLE_PFAD)).name
        self.vorschau_kopf = f"── {name} ──"      # erst beim ersten Text schreiben (leere Dateien ohne Kopf)
        self.lbl_status.setText(f"Datei {self.fertig_auftrag + 1}/{self.anzahl_auftrag}: {name}")

    def _sprache(self, zeile, sprache, wahrscheinlichkeit, erkannt):
        self.datei_sprache = (f"{sprach_name(sprache)} ({wahrscheinlichkeit:.0%})" if erkannt
                              else sprach_name(sprache))
        if erkannt:
            self.protokollieren(f"Erkannte Sprache: {sprache} ({wahrscheinlichkeit:.0%})")

    def _fortschritt(self, zeile, anteil, position, dauer):
        item = self.liste.topLevelItem(zeile)
        self.liste.itemWidget(item, 2).setValue(int(anteil * 1000))
        vergangen = max(time.time() - self.datei_start_zeit, 0.1)
        tempo = position / vergangen
        item.setText(1, f"Läuft {anteil:.0%} · {tempo:.1f}x Echtzeit")
        self.balken.setValue(int((self.fertig_auftrag + anteil) / self.anzahl_auftrag * 1000))
        name = Path(item.data(0, ROLLE_PFAD)).name
        teile = [f"Datei {self.fertig_auftrag + 1}/{self.anzahl_auftrag}: {name}"]
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
        item.setText(1, f"✓ Fertig · {e.sprache} · {kern.zeit(e.dauer)} · {e.tempo:.1f}x Echtzeit")
        item.setToolTip(1, f"Gespeichert: {e.txt}" + (f"\n{e.srt}" if e.srt else "") + "\nDoppelklick öffnet die .txt")
        self.liste.itemWidget(item, 2).setValue(1000)
        self.fertig_auftrag += 1
        self.protokollieren(f"Fertig: {e.datei.name} → {e.txt.name}"
                            + (f" + {e.srt.name}" if e.srt else "")
                            + f" ({e.benoetigt:.0f} s, {e.tempo:.1f}x Echtzeit)")

    def _datei_fehler(self, zeile, kurz, lang):
        item = self.liste.topLevelItem(zeile)
        item.setData(0, ROLLE_ZUSTAND, "fehler" if kurz != "Abgebrochen" else "wartet")
        item.setText(1, f"✗ {kurz}" if kurz != "Abgebrochen" else "Abgebrochen")
        if lang:
            item.setToolTip(1, lang)
        if kurz != "Abgebrochen":
            self.fertig_auftrag += 1
        self.protokollieren(f"{Path(item.data(0, ROLLE_PFAD)).name}: {lang or kurz}")

    def _alles_fertig(self, abgebrochen):
        self.laeuft = False
        self._knoepfe_aktualisieren()
        fertig = sum(1 for i in range(self.liste.topLevelItemCount())
                     if self.liste.topLevelItem(i).data(0, ROLLE_ZUSTAND) == "fertig")
        fehler = sum(1 for i in range(self.liste.topLevelItemCount())
                     if self.liste.topLevelItem(i).data(0, ROLLE_ZUSTAND) == "fehler")
        if abgebrochen:
            self.lbl_status.setText("Abgebrochen. Mit «Transkribieren» geht es bei der unterbrochenen Datei weiter.")
        elif self.lbl_status.text().startswith("Fehler"):
            pass
        else:
            self.balken.setValue(1000)
            self.lbl_status.setText(f"Alles erledigt: {fertig} fertig" + (f", {fehler} mit Problem" if fehler else "")
                                    + f".  Ergebnisse in {kern.OUTPUT_ORDNER}")
        self.protokollieren("Abgebrochen." if abgebrochen else "Erledigt.")

    # ------------------------------------------------------------ Schliessen

    def closeEvent(self, e):
        if self.laeuft:
            antwort = QMessageBox.question(self, "Transcriber",
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
