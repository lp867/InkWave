"""
konverter_seite.py – die Seite «MP4 → MP3»: links die Videos (leer: grosse Ablagefläche), rechts
Qualität, Zielordner, Start und Fortschritt. Gleicher Aufbau wie die Transkriptions-Seite.

Gerechnet wird in konverter.py (PyAV). Der Arbeiter läuft in einem eigenen Thread und wandelt
mehrere Videos gleichzeitig um; er meldet sich nur über Signale.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QMessageBox, QProgressBar, QPushButton,
                               QStackedWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

import gui
import kern
import konverter
import theme
from bausteine import beschriftung, karte, mit_info

ROLLE_PFAD = Qt.ItemDataRole.UserRole
ROLLE_ERGEBNIS = Qt.ItemDataRole.UserRole + 1
ROLLE_ZUSTAND = Qt.ItemDataRole.UserRole + 2     # "wartet" | "läuft" | "fertig" | "fehler"
ROLLE_FARBE = Qt.ItemDataRole.UserRole + 3
ROLLE_AUFTRAG = Qt.ItemDataRole.UserRole + 4

ZIELE = [("output", "Output-Ordner"), ("quelle", "Neben dem Video")]


class KonverterArbeiter(QObject):
    datei_start = Signal(int)
    fortschritt = Signal(int, float)             # Auftragsnummer, Anteil
    datei_fertig = Signal(int, object)           # Auftragsnummer, konverter.Mp3Ergebnis
    datei_fehler = Signal(int, str, str)         # Auftragsnummer, kurz, ausführlich
    fertig = Signal(bool)                        # abgebrochen?

    def __init__(self):
        super().__init__()
        self.abbrechen = threading.Event()
        self.ausgelassen = set()                 # während des Laufs aus der Liste entfernt

    @Slot(object)
    def arbeiten(self, auftrag: dict):
        # abbrechen wird beim Start im Fenster zurückgesetzt, nicht hier: sonst ginge ein Klick auf
        # «Abbrechen» verloren, der kommt, bevor dieser Thread den Auftrag aufgenommen hat
        try:
            with kern.WachHalten(), ThreadPoolExecutor(max_workers=auftrag["parallel"]) as becken:
                for nr, datei in auftrag["dateien"]:
                    becken.submit(self._eine, nr, datei, auftrag)
        finally:
            self.fertig.emit(self.abbrechen.is_set())

    def _eine(self, nr: int, datei: Path, auftrag: dict):
        if self.abbrechen.is_set() or nr in self.ausgelassen:
            return
        self.datei_start.emit(nr)
        try:
            ordner = datei.parent if auftrag["ziel"] == "quelle" else None
            e = konverter.in_mp3_umwandeln(
                datei, ordner, auftrag["bitrate"],
                melden=lambda anteil, **_: self.fortschritt.emit(nr, anteil), abbrechen=self.abbrechen)
            self.datei_fertig.emit(nr, e)
        except kern.Abgebrochen:
            self.datei_fehler.emit(nr, "Abgebrochen", "")
        except kern.KeineTonspur as e:
            self.datei_fehler.emit(nr, "Keine Tonspur" if "keine Tonspur" in str(e) else "Nicht lesbar", str(e))
        except Exception as e:
            self.datei_fehler.emit(nr, "Fehler", f"{type(e).__name__}: {e}")


class KonverterSeite(QWidget):
    auftrag = Signal(object)
    zustand_geaendert = Signal()                 # läuft / Anzahl Dateien hat sich geändert
    alles_fertig = Signal(int)                   # Anzahl fertiger MP3s dieses Laufs (nicht bei Abbruch)

    def __init__(self):
        super().__init__()
        self.laeuft = False
        self.auftrag_nr = 0
        self.auftrag_zeilen = {}
        self.anteile = {}                        # Auftragsnummer -> Anteil 0..1 (für den Gesamtbalken)
        self.anzahl_auftrag = 0
        self.start_zeit = 0.0

        reihe = QHBoxLayout(self)
        reihe.setContentsMargins(0, 0, 0, 0)
        reihe.setSpacing(14)
        reihe.addWidget(self._dateien_bauen(), 1)
        reihe.addWidget(self._einstellungen_bauen(), 0)

        self.thread_ = QThread(self)
        self.arbeiter = KonverterArbeiter()
        self.arbeiter.moveToThread(self.thread_)
        self.auftrag.connect(self.arbeiter.arbeiten)
        a = self.arbeiter
        a.datei_start.connect(self._datei_start)
        a.fortschritt.connect(self._fortschritt)
        a.datei_fertig.connect(self._datei_fertig)
        a.datei_fehler.connect(self._datei_fehler)
        a.fertig.connect(self._alles_fertig)
        self.thread_.start()
        self._einstellungen_laden()
        self._knoepfe()

    # ------------------------------------------------------------ Aufbau

    def _dateien_bauen(self):
        k = karte()
        lay = QVBoxLayout(k)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        zeile = QHBoxLayout()
        zeile.addWidget(beschriftung("Videos", "abschnitt"))
        self.lbl_anzahl = beschriftung("", "zaehler")
        self.lbl_anzahl.hide()
        zeile.addWidget(self.lbl_anzahl)
        zeile.addStretch(1)
        self.btn_dateien = QPushButton("+ Videos")
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
        self.drop = gui.DropFlaeche("Videos hierher ziehen", "auch ganze Ordner", "Videos wählen …")
        self.drop.abgelegt.connect(self.dateien_hinzufuegen)
        self.drop.btn_dateien.clicked.connect(self._dateien_waehlen)
        self.drop.btn_ordner.clicked.connect(self._ordner_waehlen)
        self.liste = gui.DateiListe()
        self.liste.setHeaderLabels(["Video", "Status", "Fortschritt"])
        self.liste.abgelegt.connect(self.dateien_hinzufuegen)
        self.liste.itemDoubleClicked.connect(self._zeile_oeffnen)
        self.stapel.addWidget(self.drop)
        self.stapel.addWidget(self.liste)
        lay.addWidget(self.stapel, 1)
        return k

    def _einstellungen_bauen(self):
        k = karte()
        k.setFixedWidth(380)
        lay = QVBoxLayout(k)
        lay.setContentsMargins(18, 14, 18, 16)
        lay.setSpacing(6)
        lay.addWidget(beschriftung("Einstellungen", "abschnitt"))
        lay.addSpacing(4)

        lay.addLayout(mit_info(
            "Qualität",
            "Je höher die Zahl, desto besser der Klang und desto grösser die Datei.\n\n"
            "192 kbit/s reicht für Sprache und Musik gut aus. Mehr als die Tonspur im Video hergibt, "
            "kann die MP3 nicht enthalten – 320 kbit/s lohnt sich nur bei sehr guter Quelle.", "Qualität"))
        self.cb_bitrate = QComboBox()
        for wert, text in konverter.BITRATEN:
            self.cb_bitrate.addItem(text, wert)
        lay.addWidget(self.cb_bitrate)
        lay.addSpacing(4)

        lay.addLayout(mit_info(
            "Speichern in",
            "Output-Ordner: der Ordner «output» von InkWave (in den Einstellungen änderbar).\n\n"
            "Neben dem Video: die MP3 landet im selben Ordner wie das Video.\n\n"
            "Vorhandene Dateien werden nie überschrieben – gibt es den Namen schon, heisst die neue "
            "Datei name_2.mp3.", "Speichern in"))
        self.cb_ziel = QComboBox()
        for code, text in ZIELE:
            self.cb_ziel.addItem(text, code)
        lay.addWidget(self.cb_ziel)
        lay.addStretch(1)

        self.btn_start = QPushButton("In MP3 umwandeln")
        self.btn_start.setObjectName("primaer")
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setMinimumHeight(48)
        self.btn_start.clicked.connect(self.starten)
        lay.addWidget(self.btn_start)
        self.btn_abbrechen = QPushButton("Abbrechen")
        self.btn_abbrechen.setMinimumHeight(48)
        self.btn_abbrechen.clicked.connect(self.abbrechen)
        self.btn_abbrechen.hide()
        lay.addWidget(self.btn_abbrechen)
        lay.addSpacing(6)

        fort = QHBoxLayout()
        self.balken = QProgressBar()
        self.balken.setRange(0, 1000)
        self.balken.setTextVisible(False)
        fort.addWidget(self.balken, 1)
        self.lbl_prozent = beschriftung("0 %", "prozent")
        self.lbl_prozent.setMinimumWidth(44)
        self.lbl_prozent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fort.addWidget(self.lbl_prozent)
        lay.addLayout(fort)
        self.lbl_status = beschriftung("Bereit.", "gedaempft")
        self.lbl_status.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setMinimumHeight(self.lbl_status.fontMetrics().lineSpacing() * 2)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.lbl_status)
        self.btn_output = QPushButton("Output-Ordner öffnen")
        self.btn_output.setObjectName("link")
        self.btn_output.clicked.connect(self._output_oeffnen)
        lay.addWidget(self.btn_output, 0, Qt.AlignmentFlag.AlignLeft)
        return k

    def _einstellungen_laden(self):
        i = self.cb_bitrate.findData(kern.einstellung("mp3_bitrate", konverter.STANDARD_BITRATE))
        self.cb_bitrate.setCurrentIndex(i if i >= 0 else self.cb_bitrate.findData(konverter.STANDARD_BITRATE))
        self.cb_ziel.setCurrentIndex(max(self.cb_ziel.findData(kern.einstellung("mp3_ziel", "output")), 0))

    # ------------------------------------------------------------ Dateien

    def _zeilen(self):
        return [self.liste.topLevelItem(i) for i in range(self.liste.topLevelItemCount())]

    def _status_setzen(self, item, text, farbe="gedaempft"):
        item.setText(1, text)
        item.setData(0, ROLLE_FARBE, farbe)
        item.setForeground(1, QBrush(QColor(theme.f.get(farbe, theme.f["gedaempft"]))))

    def farben_auffrischen(self):
        for item in self._zeilen():
            self._status_setzen(item, item.text(1), item.data(0, ROLLE_FARBE) or "gedaempft")
        bild = gui.logo_pixmap(self, 64)
        if bild is not None:
            self.drop.logo.setPixmap(bild)

    def dateien_hinzufuegen(self, eingaben):
        dateien, _fehlend = konverter.videos_sammeln(eingaben)
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
            balken.setTextVisible(False)
            halter = QWidget()
            hl = QHBoxLayout(halter)
            hl.setContentsMargins(4, 0, 8, 0)
            hl.addWidget(balken)
            self.liste.setItemWidget(item, 2, halter)
            neu += 1
        if eingaben and not dateien:
            self.lbl_status.setText("Keine Videos gefunden.")
        elif neu:
            self.lbl_status.setText(f"{neu} Video(s) hinzugefügt.")
        self._knoepfe()

    def _zeilen_balken(self, item) -> QProgressBar:
        return self.liste.itemWidget(item, 2).findChild(QProgressBar)

    def _dateien_waehlen(self):
        filter_ = ("Videos (" + " ".join(f"*{e}" for e in sorted(konverter.VIDEO_ENDUNGEN))
                   + ");;Alle Dateien (*.*)")
        pfade, _ = QFileDialog.getOpenFileNames(self, "Videos wählen", kern.einstellung("letzter_ordner", ""),
                                                filter_)
        if pfade:
            kern.einstellungen_speichern(letzter_ordner=str(Path(pfade[0]).parent))
            self.dateien_hinzufuegen(pfade)

    def _ordner_waehlen(self):
        ordner = QFileDialog.getExistingDirectory(self, "Ordner wählen (wird mit Unterordnern durchsucht)",
                                                  kern.einstellung("letzter_ordner", ""))
        if ordner:
            kern.einstellungen_speichern(letzter_ordner=ordner)
            self.dateien_hinzufuegen([ordner])

    def _entfernen(self):
        for item in self.liste.selectedItems():
            if item.data(0, ROLLE_ZUSTAND) == "läuft":
                continue
            nr = item.data(0, ROLLE_AUFTRAG)
            if self.auftrag_zeilen.pop(nr, None) is not None and self.laeuft:
                self.arbeiter.ausgelassen.add(nr)
                if item.data(0, ROLLE_ZUSTAND) == "wartet":
                    self.anteile.pop(nr, None)
                    self.anzahl_auftrag = max(self.anzahl_auftrag - 1, 1)
            self.liste.takeTopLevelItem(self.liste.indexOfTopLevelItem(item))
        self._knoepfe()

    def _leeren(self):
        self.auftrag_zeilen.clear()
        self.liste.clear()
        self._gesamt(0)
        self._knoepfe()

    def _zeile_oeffnen(self, item, _spalte):
        e = item.data(0, ROLLE_ERGEBNIS)
        if e is not None and e.mp3.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(e.mp3)))

    def _output_oeffnen(self):
        try:
            kern.OUTPUT_ORDNER.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(kern.OUTPUT_ORDNER)))

    # ------------------------------------------------------------ Starten / Abbrechen

    def _knoepfe(self):
        n = self.liste.topLevelItemCount()
        self.stapel.setCurrentIndex(1 if n else 0)
        self.lbl_anzahl.setText(str(n))
        self.lbl_anzahl.setVisible(n > 0)
        self.btn_start.setEnabled(not self.laeuft and n > 0)
        self.btn_start.setVisible(not self.laeuft)
        self.btn_abbrechen.setVisible(self.laeuft)
        self.btn_abbrechen.setEnabled(self.laeuft)
        for w in (self.btn_dateien, self.btn_ordner, self.drop.btn_dateien, self.drop.btn_ordner,
                  self.cb_bitrate, self.cb_ziel):
            w.setEnabled(not self.laeuft)
        self.btn_leeren.setEnabled(not self.laeuft and n > 0)
        self.btn_entfernen.setEnabled(n > 0)
        self.zustand_geaendert.emit()

    def _gesamt(self, anteil: float):
        anteil = min(max(anteil, 0.0), 1.0)
        self.balken.setValue(int(anteil * 1000))
        self.lbl_prozent.setText(f"{round(anteil * 100)} %")

    def _gesamt_neu(self):
        self._gesamt(sum(self.anteile.values()) / max(self.anzahl_auftrag, 1))

    def starten(self):
        if self.laeuft:
            return
        ziel = self.cb_ziel.currentData()
        if ziel == "output":
            problem = kern.ausgabe_pruefen()
            if problem:
                QMessageBox.warning(self, "InkWave",
                                    f"In den Output-Ordner kann nicht geschrieben werden:\n{problem}\n\n"
                                    "Der Ordner lässt sich in den Einstellungen ändern – oder «Neben dem Video» wählen.")
                return
        dateien = []
        self.auftrag_zeilen = {}
        self.anteile = {}
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
            self.lbl_status.setText("Alle Videos sind schon umgewandelt.")
            return
        kern.einstellungen_speichern(mp3_bitrate=self.cb_bitrate.currentData(), mp3_ziel=ziel)
        self.laeuft = True
        self.anzahl_auftrag = len(dateien)
        self.start_zeit = time.time()
        self._gesamt(0)
        self._knoepfe()
        self.lbl_status.setText(f"{len(dateien)} Video(s) werden umgewandelt …")
        self.arbeiter.abbrechen.clear()
        self.auftrag.emit({"dateien": dateien, "bitrate": self.cb_bitrate.currentData(), "ziel": ziel,
                           "parallel": konverter.parallel_anzahl()})

    def abbrechen(self):
        self.arbeiter.abbrechen.set()
        self.btn_abbrechen.setEnabled(False)
        self.lbl_status.setText("Wird abgebrochen …")

    # ------------------------------------------------------------ Rückmeldungen des Arbeiters

    def _datei_start(self, nr):
        item = self.auftrag_zeilen.get(nr)
        if item is None:
            return
        item.setData(0, ROLLE_ZUSTAND, "läuft")
        self._status_setzen(item, "Läuft …", "akzent_text")

    def _fortschritt(self, nr, anteil):
        item = self.auftrag_zeilen.get(nr)
        if item is None or item.data(0, ROLLE_ZUSTAND) != "läuft":
            return
        self._zeilen_balken(item).setValue(int(anteil * 1000))
        self._status_setzen(item, f"Läuft {anteil:.0%}", "akzent_text")
        self.anteile[nr] = anteil
        self._gesamt_neu()
        erledigt = sum(1 for a in self.anteile.values() if a >= 1.0)
        self.lbl_status.setText(f"{erledigt} von {self.anzahl_auftrag} fertig …")

    def _datei_fertig(self, nr, e):
        item = self.auftrag_zeilen.get(nr)
        if item is None:
            return
        item.setData(0, ROLLE_ZUSTAND, "fertig")
        item.setData(0, ROLLE_ERGEBNIS, e)
        self._status_setzen(item, f"✓ Fertig · {kern.zeit(e.dauer)} · {kern.groesse_text(e.groesse)}", "erfolg")
        item.setToolTip(1, f"Gespeichert: {e.mp3}\nDoppelklick spielt die MP3 ab")
        self._zeilen_balken(item).setValue(1000)
        self.anteile[nr] = 1.0
        self._gesamt_neu()

    def _datei_fehler(self, nr, kurz, lang):
        item = self.auftrag_zeilen.get(nr)
        if item is None:
            return
        if kurz == "Abgebrochen":
            item.setData(0, ROLLE_ZUSTAND, "wartet")
            self._status_setzen(item, "Abgebrochen")
            self._zeilen_balken(item).setValue(0)
            self.anteile.pop(nr, None)
        else:
            item.setData(0, ROLLE_ZUSTAND, "fehler")
            self._status_setzen(item, f"✗ {kurz}", "fehler" if kurz == "Fehler" else "warnung")
            self.anteile[nr] = 1.0
            self._gesamt_neu()
        if lang:
            item.setToolTip(1, lang)

    def _alles_fertig(self, abgebrochen):
        self.laeuft = False
        self._knoepfe()
        zustaende = [item.data(0, ROLLE_ZUSTAND) for item in self.auftrag_zeilen.values()]
        fertig, fehler = zustaende.count("fertig"), zustaende.count("fehler")
        if abgebrochen:
            self.lbl_status.setText("Abgebrochen. Mit «In MP3 umwandeln» geht es bei den offenen Videos weiter.")
            return
        self._gesamt(1.0)
        dauer = time.time() - self.start_zeit
        self.lbl_status.setText(f"✓ {fertig} MP3 fertig" + (f", {fehler} mit Problem" if fehler else "")
                                + f" · {dauer:.0f} s")
        self.alles_fertig.emit(fertig)

    # ------------------------------------------------------------ Schliessen

    def beenden(self, warten_ms: int = 3000) -> bool:
        """Beim Schliessen der App: laufende Umwandlungen abbrechen. False = hängt noch."""
        self.arbeiter.abbrechen.set()
        self.thread_.quit()
        return self.thread_.wait(warten_ms)
