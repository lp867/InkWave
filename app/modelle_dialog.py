"""
modelle_dialog.py – Fenster «Modelle»: anzeigen, herunterladen (mit Fortschritt), Hugging-Face-Modelle
hinzufügen. Beim ersten Start öffnet es sich von selbst, das empfohlene Modell ist vorausgewählt.

Jeder Download läuft in einem eigenen Prozess (kern.download_starten): Abbrechen beendet ihn sofort,
angefangene Dateien setzt Hugging Face beim nächsten Mal fort.
"""

import re
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

import kern

ROLLE_MODELL = Qt.ItemDataRole.UserRole
REPO_MUSTER = re.compile(r"^[A-Za-z0-9][\w.\-]*/[\w.\-]+$")


class _Hintergrund(QObject):
    fertig = Signal(object, object)    # ergebnis, fehler


def im_hintergrund(eltern, funktion, rueckruf):
    """funktion() in einem Thread ausführen, rueckruf(ergebnis, fehler) danach im Fenster-Thread."""
    h = _Hintergrund(eltern)

    def fertig(ergebnis, fehler):
        rueckruf(ergebnis, fehler)
        h.deleteLater()

    h.fertig.connect(fertig)

    def lauf():
        try:
            h.fertig.emit(funktion(), None)
        except Exception as e:
            h.fertig.emit(None, e)

    threading.Thread(target=lauf, daemon=True).start()


class ModellDialog(QDialog):
    def __init__(self, eltern, gpu: bool, erster_start: bool = False):
        super().__init__(eltern)
        self.gpu = gpu
        self.erster_start = erster_start
        self.empfohlen = kern.EMPFEHLUNG["cuda" if gpu else "cpu"]
        self.warteschlange = []           # [(name, repo)]
        self.proc = None
        self.aktuell = None
        self.gesamt = 0
        self.start_zeit = 0.0
        self.start_bytes = 0
        self.vorgemerkt = set()           # neu hinzugefügte Repos: gleich angehakt
        self.geaendert = False
        self.protokoll_pfad = kern.LOKAL / "download.log"
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._pruefen)

        self.setWindowTitle("Modelle – Transcriber")
        self.resize(900, 600)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        titel = QLabel("Willkommen! Welche Modelle sollen heruntergeladen werden?" if erster_start
                       else "Modelle")
        f = titel.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        titel.setFont(f)
        lay.addWidget(titel)
        geraet = "mit Grafikkarte" if gpu else "ohne Grafikkarte (CPU)"
        hinweis = QLabel(
            f"Empfohlen für diesen PC ({geraet}): <b>{self.empfohlen}</b>. "
            "Häkchen setzen und «Herunterladen» klicken – später lassen sich jederzeit weitere laden.<br>"
            "Die Modelle werden nur einmal geladen und liegen danach auf der Festplatte "
            "(Hugging-Face-Cache bzw. Ordner «modelle»), nicht in der App.")
        hinweis.setWordWrap(True)
        lay.addWidget(hinweis)

        self.baum = QTreeWidget()
        self.baum.setHeaderLabels(["Modell", "Grösse", "Beschreibung", "Zustand"])
        self.baum.setRootIsDecorated(False)
        self.baum.setUniformRowHeights(True)
        kopf = self.baum.header()
        kopf.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        kopf.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        kopf.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        kopf.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.baum.itemChanged.connect(lambda *_: self._knoepfe())
        lay.addWidget(self.baum, 1)

        # Hugging-Face-Modell hinzufügen
        zeile = QHBoxLayout()
        zeile.addWidget(QLabel("Hugging-Face-Modell:"))
        self.eingabe = QLineEdit()
        self.eingabe.setPlaceholderText("Name, z. B. Flix-AI/flix-swissgerman-full")
        self.eingabe.returnPressed.connect(self._hinzufuegen)
        zeile.addWidget(self.eingabe, 1)
        self.btn_hinzu = QPushButton("Hinzufügen")
        self.btn_hinzu.clicked.connect(self._hinzufuegen)
        zeile.addWidget(self.btn_hinzu)
        lay.addLayout(zeile)

        # Fortschritt
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_status)
        self.balken = QProgressBar()
        self.balken.setRange(0, 1000)
        self.balken.setFormat("%p %")
        self.balken.hide()
        lay.addWidget(self.balken)

        knoepfe = QHBoxLayout()
        self.btn_laden = QPushButton("Herunterladen")
        b = self.btn_laden.font()
        b.setBold(True)
        self.btn_laden.setFont(b)
        self.btn_laden.clicked.connect(self._herunterladen)
        self.btn_abbrechen = QPushButton("Abbrechen")
        self.btn_abbrechen.clicked.connect(self._abbrechen)
        self.btn_abbrechen.setEnabled(False)
        btn_ordner = QPushButton("Ordner «modelle» öffnen")
        btn_ordner.clicked.connect(self._ordner_modelle)
        btn_cache = QPushButton("Hugging-Face-Cache öffnen")
        btn_cache.clicked.connect(lambda: self._oeffnen(kern.hf_cache()))
        self.btn_schliessen = QPushButton("Später" if erster_start else "Schliessen")
        self.btn_schliessen.clicked.connect(self.close)
        knoepfe.addWidget(self.btn_laden)
        knoepfe.addWidget(self.btn_abbrechen)
        knoepfe.addStretch(1)
        knoepfe.addWidget(btn_ordner)
        knoepfe.addWidget(btn_cache)
        knoepfe.addWidget(self.btn_schliessen)
        lay.addLayout(knoepfe)

        self.fuellen()

    # ------------------------------------------------------------ Liste

    def fuellen(self):
        angehakt = {name for name, _ in self._angehakt()}
        self.baum.blockSignals(True)
        self.baum.clear()
        gruppen = {"standard": "Eingebaute Modelle", "eigen": "Eigene Modelle (Ordner «modelle»)",
                   "cache": "Weitere Hugging-Face-Modelle"}
        quelle = None
        for m in kern.modell_liste():
            if m.quelle != quelle:
                quelle = m.quelle
                g = QTreeWidgetItem([gruppen[quelle]])
                f = QFont(self.font())
                f.setBold(True)
                g.setFont(0, f)
                g.setFlags(Qt.ItemFlag.ItemIsEnabled)
                g.setFirstColumnSpanned(True)
                self.baum.addTopLevelItem(g)
                g.setFirstColumnSpanned(True)
            beschreibung = m.beschreibung + ("   ← empfohlen" if m.name == self.empfohlen else "")
            zustand = {"bereit": "✓ heruntergeladen", "fehlt": "nicht heruntergeladen",
                       "umwandeln": "⟳ muss umgewandelt werden"}[m.zustand]
            it = QTreeWidgetItem([m.name, m.groesse, beschreibung, zustand])
            it.setData(0, ROLLE_MODELL, m.name)
            it.setData(1, ROLLE_MODELL, m.repo)
            if m.zustand == "fehlt" and m.repo:
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                vor = (m.name in angehakt or m.repo in self.vorgemerkt
                       or (self.erster_start and m.name == self.empfohlen and not angehakt))
                it.setCheckState(0, Qt.CheckState.Checked if vor else Qt.CheckState.Unchecked)
            else:
                # schon vorhanden: festes Häkchen (nicht änderbar), damit die Liste bündig bleibt
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                it.setCheckState(0, Qt.CheckState.Checked if m.zustand != "fehlt" else Qt.CheckState.Unchecked)
            if m.zustand == "umwandeln":
                it.setToolTip(3, "Transformers-Format: wird in einem späteren Schritt automatisch "
                                 "ins CTranslate2-Format umgewandelt.")
            self.baum.addTopLevelItem(it)
        self.baum.blockSignals(False)
        self._knoepfe()

    def _eintraege(self):
        for i in range(self.baum.topLevelItemCount()):
            it = self.baum.topLevelItem(i)
            if it.data(0, ROLLE_MODELL):
                yield it

    def _angehakt(self):
        return [(it.data(0, ROLLE_MODELL), it.data(1, ROLLE_MODELL)) for it in self._eintraege()
                if it.flags() & Qt.ItemFlag.ItemIsUserCheckable and it.checkState(0) == Qt.CheckState.Checked]

    def _knoepfe(self):
        laeuft = self.proc is not None
        n = len(self._angehakt())
        self.btn_laden.setEnabled(not laeuft and n > 0)
        self.btn_laden.setText(f"Herunterladen ({n})" if n else "Herunterladen")
        self.btn_abbrechen.setEnabled(laeuft)
        self.btn_hinzu.setEnabled(not laeuft)

    # ------------------------------------------------------------ Hinzufügen

    def _hinzufuegen(self):
        repo = self.eingabe.text().strip().removeprefix("https://huggingface.co/").strip("/")
        if not REPO_MUSTER.match(repo):
            QMessageBox.information(self, "Modell hinzufügen",
                                    "Bitte den Namen so eingeben, wie er auf huggingface.co steht:\n"
                                    "Besitzer/Modell, z. B. Flix-AI/flix-swissgerman-full")
            return
        self.btn_hinzu.setEnabled(False)
        self.lbl_status.setText(f"Suche «{repo}» auf Hugging Face …")
        im_hintergrund(self, lambda: kern.hf_modell_pruefen(repo), self._hinzugefuegt)

    def _hinzugefuegt(self, info, fehler):
        self.btn_hinzu.setEnabled(True)
        if fehler is not None:
            self.lbl_status.setText(f"Nicht hinzugefügt: {kern.fehler_text(fehler)}")
            return
        if not info["whisper"]:
            antwort = QMessageBox.question(
                self, "Modell hinzufügen",
                f"«{info['repo']}» sieht nicht nach einem Whisper-Modell aus. Trotzdem hinzufügen?")
            if antwort != QMessageBox.StandardButton.Yes:
                self.lbl_status.setText("")
                return
        kern.hf_modell_merken(info["repo"], info["gesamt"])
        self.vorgemerkt.add(info["repo"])
        self.geaendert = True
        art = {"ct2": "direkt nutzbar (CTranslate2-Format)",
               "transformers": "Transformers-Format – wird nach dem Download umgewandelt",
               "unbekannt": "unbekanntes Format"}[info["format"]]
        self.lbl_status.setText(f"«{info['repo']}» hinzugefügt: {kern.groesse_text(info['gesamt'])}, {art}. "
                                "Zum Laden angehakt.")
        self.eingabe.clear()
        self.fuellen()

    # ------------------------------------------------------------ Herunterladen

    def _herunterladen(self):
        self.warteschlange = self._angehakt()
        if not self.warteschlange:
            return
        self.balken.show()
        self._naechster()

    def _naechster(self):
        if not self.warteschlange:
            self.proc = None
            self.aktuell = None
            self.timer.stop()
            self.fuellen()
            self._knoepfe()
            return
        name, repo = self.warteschlange.pop(0)
        self.aktuell = (name, repo)
        self.gesamt = 0
        self.start_zeit = time.time()
        self.start_bytes = kern.cache_bytes(repo)
        self.balken.setValue(0)
        self.lbl_status.setText(f"«{name}»: Verbinde mit Hugging Face …")
        try:
            self.proc = kern.download_starten(repo, self.protokoll_pfad)
        except Exception as e:
            self.proc = None
            self.lbl_status.setText(f"«{name}» konnte nicht gestartet werden: {e}")
            self._naechster()
            return
        self._knoepfe()
        self.timer.start()

    def _protokoll(self) -> dict:
        werte = {}
        try:
            for zeile in self.protokoll_pfad.read_text(encoding="utf-8", errors="replace").splitlines():
                art, _, rest = zeile.partition(" ")
                if art in ("GESAMT", "FERTIG", "FEHLER"):
                    werte[art] = rest
        except OSError:
            pass
        return werte

    def _pruefen(self):
        if self.proc is None:
            return
        name, repo = self.aktuell
        p = self._protokoll()
        if "GESAMT" in p and not self.gesamt:
            self.gesamt = int(p["GESAMT"])
            self.start_zeit = time.time()            # Tempo ab jetzt messen (ohne Verbindungsaufbau)
            self.start_bytes = kern.cache_bytes(repo)
            frei = shutil.disk_usage(kern.hf_cache().parent if not kern.hf_cache().exists()
                                     else kern.hf_cache()).free
            noetig = self.gesamt - kern.cache_bytes(repo)
            if frei < noetig + 300e6:
                self.proc.terminate()
                self.proc.wait()
                self.warteschlange.clear()
                self.lbl_status.setText(f"Zu wenig Speicherplatz für «{name}»: nötig {kern.groesse_text(noetig)}, "
                                        f"frei {kern.groesse_text(frei)}.")
                self._naechster()
                return
        jetzt = kern.cache_bytes(repo)
        ende = self.proc.poll()
        if ende is None:
            if self.gesamt:
                anteil = min(jetzt / self.gesamt, 1.0)
                self.balken.setValue(int(anteil * 1000))
                dt = max(time.time() - self.start_zeit, 0.1)
                tempo = (jetzt - self.start_bytes) / dt
                rest = (self.gesamt - jetzt) / tempo if tempo > 1e4 else 0
                text = (f"«{name}»: {kern.groesse_text(jetzt)} von {kern.groesse_text(self.gesamt)}"
                        f"  ·  {tempo / 1e6:.1f} MB/s")
                if rest:
                    text += f"  ·  noch {kern.zeit(rest)}"
                n = len(self.warteschlange)
                if n:
                    text += f"   (danach noch {n})"
                self.lbl_status.setText(text)
            return
        # Prozess ist fertig
        if ende == 0:
            self.balken.setValue(1000)
            m = kern.modell_finden(name)
            zusatz = "  Es muss noch umgewandelt werden." if m and m.zustand == "umwandeln" else ""
            self.lbl_status.setText(f"«{name}» ist heruntergeladen ✓{zusatz}")
            self.geaendert = True
        else:
            self.lbl_status.setText(f"«{name}» nicht geladen: {p.get('FEHLER', f'Abbruch (Code {ende})')}")
        self.proc = None
        self._naechster()

    def _abbrechen(self):
        self.warteschlange.clear()
        if self.proc is not None:
            self.timer.stop()
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:
                pass
            name = self.aktuell[0] if self.aktuell else ""
            self.proc = None
            self.aktuell = None
            self.lbl_status.setText(f"Download von «{name}» abgebrochen. Beim nächsten Mal geht es dort weiter, "
                                    "wo er aufgehört hat.")
        self.fuellen()
        self._knoepfe()

    # ------------------------------------------------------------ Ordner, Schliessen

    def _oeffnen(self, ordner: Path):
        ordner.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ordner)))

    def _ordner_modelle(self):
        self._oeffnen(kern.MODELL_ORDNER)

    def closeEvent(self, e):
        if self.proc is not None:
            antwort = QMessageBox.question(self, "Modelle", "Der Download läuft noch. Abbrechen und schliessen?")
            if antwort != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self._abbrechen()
        kern.einstellungen_speichern(eingerichtet=True)
        e.accept()

    def reject(self):          # Esc
        self.close()
