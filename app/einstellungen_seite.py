"""
einstellungen_seite.py – die Seite «Einstellungen»: Design (wie Windows / hell / dunkel), Allgemeines,
Speicherort, Leistung, Modelle und Infos zur App. Jede Änderung gilt sofort und wird gleich in
einstellungen.json gespeichert – einen «Speichern»-Knopf gibt es nicht.

Wenig Text: Erklärungen stecken hinter dem «i» (bausteine.InfoKnopf).
"""

import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget)

import kern
from bausteine import InfoKnopf, beschriftung, karte, mit_info

THEMEN = [("system", "Wie Windows"), ("hell", "Hell"), ("dunkel", "Dunkel")]
START_SEITEN = [("start", "Startseite"), ("transkribieren", "Transkribieren"), ("konverter", "MP4 → MP3")]
THREADS = [(0, "Automatisch"), (2, "2"), (4, "4"), (6, "6"), (8, "8")]
# alles, was «Zurücksetzen» auf den Standard stellt (Modelle und Downloads bleiben)
EIGENE_SCHLUESSEL = ("thema", "start_seite", "fenster_merken", "fenster", "fertig_ton", "fertig_ordner",
                     "output_ordner", "cpu_threads", "mp3_bitrate", "mp3_ziel", "sprache", "geraet", "srt",
                     "zeitstempel", "schonend", "letzter_ordner")
FEHLER_LOG = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "InkWave" / "fehler.log"


class EinstellungenSeite(QWidget):
    thema_geaendert = Signal()
    modelle_oeffnen = Signal()
    zurueckgesetzt = Signal()

    def __init__(self):
        super().__init__()
        self._laedt = False
        aussen = QVBoxLayout(self)
        aussen.setContentsMargins(0, 0, 0, 0)
        rollen = QScrollArea()
        rollen.setObjectName("rollen")
        rollen.setWidgetResizable(True)
        rollen.setFrameShape(QFrame.Shape.NoFrame)
        rollen.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        innen = QWidget()
        innen.setObjectName("innen")
        rollen.setWidget(innen)
        aussen.addWidget(rollen)
        mitte = QHBoxLayout(innen)
        mitte.setContentsMargins(0, 0, 6, 0)
        mitte.addStretch(1)
        spalte = QVBoxLayout()
        spalte.setSpacing(14)
        halter = QWidget()
        halter.setLayout(spalte)
        halter.setMaximumWidth(760)
        spalte.setContentsMargins(0, 0, 0, 0)
        mitte.addWidget(halter, 100)
        mitte.addStretch(1)

        spalte.addWidget(self._darstellung())
        spalte.addWidget(self._allgemein())
        spalte.addWidget(self._speicherort())
        spalte.addWidget(self._leistung())
        spalte.addWidget(self._ueber())
        spalte.addStretch(1)
        self.laden()

    # ------------------------------------------------------------ Bausteine

    @staticmethod
    def _abschnitt(titel: str):
        k = karte()
        lay = QVBoxLayout(k)
        lay.setContentsMargins(20, 16, 20, 18)
        lay.setSpacing(10)
        lay.addWidget(beschriftung(titel, "abschnitt"))
        return k, lay

    @staticmethod
    def _zeile(lay, name: str, info: str, widget) -> QHBoxLayout:
        """Name (+ «i») links, Bedienelement rechts."""
        zeile = QHBoxLayout()
        zeile.setSpacing(7)
        zeile.addWidget(QLabel(name))
        if info:
            zeile.addWidget(InfoKnopf(info, name))
        zeile.addStretch(1)
        zeile.addWidget(widget)
        lay.addLayout(zeile)
        return zeile

    def _darstellung(self):
        k, lay = self._abschnitt("Darstellung")
        segment = QWidget()
        sl = QHBoxLayout(segment)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.thema_gruppe = QButtonGroup(self)
        self.thema_knoepfe = {}
        for i, (code, text) in enumerate(THEMEN):
            b = QPushButton(text)
            b.setObjectName("segment")
            b.setProperty("lage", "links" if i == 0 else "rechts" if i == len(THEMEN) - 1 else "mitte")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.thema_gruppe.addButton(b)
            self.thema_knoepfe[code] = b
            b.clicked.connect(lambda _=False, c=code: self._thema(c))
            sl.addWidget(b)
        self._zeile(lay, "Design", "«Wie Windows» folgt der Einstellung von Windows (hell oder dunkel) und "
                                   "wechselt automatisch mit.", segment)
        return k

    def _allgemein(self):
        k, lay = self._abschnitt("Allgemein")
        self.cb_start = QComboBox()
        for code, text in START_SEITEN:
            self.cb_start.addItem(text, code)
        self.cb_start.setMinimumWidth(190)
        self.cb_start.currentIndexChanged.connect(lambda: self._speichern(start_seite=self.cb_start.currentData()))
        self._zeile(lay, "Beim Start öffnen", "", self.cb_start)

        self.chk_fenster = QCheckBox("Fenstergrösse und -position merken")
        self.chk_fenster.toggled.connect(lambda an: self._speichern(fenster_merken=an))
        lay.addWidget(self.chk_fenster)
        self.chk_ton = QCheckBox("Hinweiston, wenn alles fertig ist")
        self.chk_ton.toggled.connect(lambda an: self._speichern(fertig_ton=an))
        lay.addLayout(mit_info(self.chk_ton, "Spielt den Windows-Hinweiston, sobald eine Transkription oder "
                                             "Umwandlung fertig ist – praktisch bei langen Aufnahmen."))
        self.chk_ordner = QCheckBox("Output-Ordner öffnen, wenn alles fertig ist")
        self.chk_ordner.toggled.connect(lambda an: self._speichern(fertig_ordner=an))
        lay.addWidget(self.chk_ordner)
        return k

    def _speicherort(self):
        k, lay = self._abschnitt("Speicherort")
        lay.addLayout(mit_info(
            "Output-Ordner",
            "Hier landen die fertigen Texte (.txt, .srt) und MP3s. Standard ist der Ordner «output» "
            "neben InkWave.exe.\n\nVorhandene Dateien werden nie überschrieben.", "Output-Ordner"))
        self.lbl_output = QLabel()
        self.lbl_output.setObjectName("pfad")
        self.lbl_output.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_output.setMinimumWidth(1)
        lay.addWidget(self.lbl_output)
        knoepfe = QHBoxLayout()
        btn_aendern = QPushButton("Ändern …")
        btn_aendern.clicked.connect(self._output_aendern)
        self.btn_output_standard = QPushButton("Standard")
        self.btn_output_standard.clicked.connect(lambda: self._output_setzen(""))
        btn_oeffnen = QPushButton("Öffnen")
        btn_oeffnen.clicked.connect(lambda: self._oeffnen(kern.OUTPUT_ORDNER, anlegen=True))
        for b in (btn_aendern, self.btn_output_standard, btn_oeffnen):
            knoepfe.addWidget(b)
        knoepfe.addStretch(1)
        lay.addLayout(knoepfe)
        return k

    def _leistung(self):
        k, lay = self._abschnitt("Transkription")
        self.cb_threads = QComboBox()
        for wert, text in THREADS:
            self.cb_threads.addItem(text, wert)
        self.cb_threads.setMinimumWidth(190)
        self.cb_threads.currentIndexChanged.connect(lambda: self._speichern(cpu_threads=self.cb_threads.currentData()))
        self._zeile(lay, "Prozessor-Threads",
                    "Wie viele Rechen-Threads die Transkription auf dem Prozessor nutzt (ohne Grafikkarte).\n\n"
                    "«Automatisch» nimmt die echten Kerne, höchstens 8 – mehr bringt nichts. Weniger Threads "
                    "lassen den PC flüssiger, die Transkription dauert länger.", self.cb_threads)
        btn_modelle = QPushButton("Modelle verwalten …")
        btn_modelle.clicked.connect(self.modelle_oeffnen.emit)
        self._zeile(lay, "Modelle und Zusatzpakete",
                    "Whisper-Modelle herunterladen, eigene Hugging-Face-Modelle hinzufügen und das GPU-Paket "
                    "für NVIDIA-Grafikkarten einrichten.", btn_modelle)
        return k

    def _ueber(self):
        k, lay = self._abschnitt("Über InkWave")
        version = f"Version {kern.VERSION}" if kern.VERSION else "Entwicklungsversion"
        lay.addLayout(mit_info(
            beschriftung(f"{version}  ·  alles läuft lokal auf diesem PC", "gedaempft"),
            "InkWave schickt keine Dateien ins Internet. Eine Verbindung braucht es nur, um Modelle und "
            "Zusatzpakete herunterzuladen.\n\n"
            f"Daten-Ordner (Einstellungen, eigene Modelle):\n{kern.DATEN}", "Über InkWave"))
        knoepfe = QHBoxLayout()
        btn_daten = QPushButton("Daten-Ordner öffnen")
        btn_daten.clicked.connect(lambda: self._oeffnen(kern.DATEN))
        btn_log = QPushButton("Fehlerprotokoll öffnen")
        btn_log.clicked.connect(self._log_oeffnen)
        btn_reset = QPushButton("Einstellungen zurücksetzen")
        btn_reset.clicked.connect(self._zuruecksetzen)
        for b in (btn_daten, btn_log):
            knoepfe.addWidget(b)
        knoepfe.addStretch(1)
        knoepfe.addWidget(btn_reset)
        lay.addLayout(knoepfe)
        return k

    # ------------------------------------------------------------ Laden / Speichern

    def laden(self):
        """Zeigt den aktuellen Stand aus einstellungen.json an."""
        self._laedt = True
        try:
            thema = kern.einstellung("thema", "system")
            self.thema_knoepfe.get(thema, self.thema_knoepfe["system"]).setChecked(True)
            self.cb_start.setCurrentIndex(max(self.cb_start.findData(kern.einstellung("start_seite", "start")), 0))
            self.chk_fenster.setChecked(kern.einstellung("fenster_merken", True))
            self.chk_ton.setChecked(kern.einstellung("fertig_ton", False))
            self.chk_ordner.setChecked(kern.einstellung("fertig_ordner", False))
            self.cb_threads.setCurrentIndex(max(self.cb_threads.findData(kern.cpu_threads_einstellung()), 0))
            self._output_anzeigen()
        finally:
            self._laedt = False

    def _speichern(self, **werte):
        if not self._laedt:
            kern.einstellungen_speichern(**werte)

    def _thema(self, code: str):
        self._speichern(thema=code)
        self.thema_geaendert.emit()

    def _output_anzeigen(self):
        self.lbl_output.setText(str(kern.OUTPUT_ORDNER))
        self.lbl_output.setToolTip(str(kern.OUTPUT_ORDNER))
        self.btn_output_standard.setEnabled(kern.OUTPUT_ORDNER != kern.STANDARD_OUTPUT)

    def _output_aendern(self):
        start = str(kern.OUTPUT_ORDNER if kern.OUTPUT_ORDNER.is_dir() else kern.DATEN)
        ordner = QFileDialog.getExistingDirectory(self, "Output-Ordner wählen", start)
        if ordner:
            self._output_setzen(ordner)

    def _output_setzen(self, ordner: str):
        vorher = kern.einstellung("output_ordner", "")
        kern.einstellungen_speichern(output_ordner=str(Path(ordner)) if ordner else "")
        kern.output_ordner_anwenden()
        problem = kern.ausgabe_pruefen()
        if problem:
            QMessageBox.warning(self, "InkWave", f"In diesen Ordner kann nicht geschrieben werden:\n{problem}")
            kern.einstellungen_speichern(output_ordner=vorher)
            kern.output_ordner_anwenden()
        self._output_anzeigen()

    def _oeffnen(self, ordner: Path, anlegen: bool = False):
        if anlegen:
            try:
                ordner.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ordner)))

    def _log_oeffnen(self):
        if FEHLER_LOG.is_file() and FEHLER_LOG.stat().st_size:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(FEHLER_LOG)))
        else:
            QMessageBox.information(self, "InkWave", "Das Fehlerprotokoll ist leer – bisher ist nichts schiefgegangen.")

    def _zuruecksetzen(self):
        antwort = QMessageBox.question(
            self, "InkWave", "Alle Einstellungen auf den Standard zurücksetzen?\n\n"
                             "Heruntergeladene Modelle, Zusatzpakete und fertige Dateien bleiben erhalten.")
        if antwort != QMessageBox.StandardButton.Yes:
            return
        kern.einstellungen_entfernen(*EIGENE_SCHLUESSEL)
        kern.output_ordner_anwenden()
        self.laden()
        self.thema_geaendert.emit()
        self.zurueckgesetzt.emit()
