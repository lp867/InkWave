"""
modelle_dialog.py – Fenster «Modelle»: anzeigen, herunterladen (mit Fortschritt), umwandeln,
Hugging-Face-Modelle hinzufügen, Zusatzpakete (GPU, Umwandlung) einrichten.
Beim ersten Start öffnet es sich von selbst, das empfohlene Modell ist vorausgewählt.

Alles Lange läuft als Auftrag in einem eigenen Prozess (kern.prozess_starten): Abbrechen beendet ihn
sofort; angefangene Downloads setzt Hugging Face beim nächsten Mal fort.
"""

import re
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QCheckBox, QDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

import kern

ROLLE_MODELL = Qt.ItemDataRole.UserRole
REPO_MUSTER = re.compile(r"^[A-Za-z0-9][\w.\-]*/[\w.\-]+$")
UMWANDLUNG_GROESSE = "ca. 1.5 GB"


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
        self.warteschlange = []           # Aufträge: dict(art, name, ...)
        self.auftrag = None
        self.proc = None
        self.gesamt = 0
        self.start_zeit = 0.0
        self.start_bytes = 0
        self.vorgemerkt = set()           # neu hinzugefügte Repos: gleich angehakt
        self.nvidia_quelle = kern.vorhandene_nvidia_quelle() if gpu else None
        self.protokoll_pfad = kern.LOKAL / "auftrag.log"
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self._pruefen)

        self.setWindowTitle("Modelle – Transcriber")
        self.resize(940, 680)
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
        self.eingabe.setPlaceholderText("Name oder Link, z. B. Flix-AI/flix-swissgerman-full")
        self.eingabe.returnPressed.connect(self._hinzufuegen)
        zeile.addWidget(self.eingabe, 1)
        self.btn_hinzu = QPushButton("Hinzufügen")
        self.btn_hinzu.clicked.connect(self._hinzufuegen)
        zeile.addWidget(self.btn_hinzu)
        lay.addLayout(zeile)

        # Zusatzpakete
        zusatz = QGroupBox("Zusatzpakete")
        z = QGridLayout(zusatz)
        z.setColumnStretch(1, 1)
        z.addWidget(QLabel("<b>GPU-Paket</b> (NVIDIA cuBLAS + cuDNN)"), 0, 0)
        self.lbl_gpu = QLabel()
        z.addWidget(self.lbl_gpu, 0, 1)
        self.btn_gpu_kopie = QPushButton("Aus Python übernehmen")
        self.btn_gpu_kopie.setToolTip("Kopiert die GPU-Dateien aus der vorhandenen Python-Installation "
                                      "(kein Download).")
        self.btn_gpu_kopie.clicked.connect(lambda: self._zusatz({"art": "gpu", "name": "GPU-Paket",
                                                                 "quelle": str(self.nvidia_quelle)}))
        self.btn_gpu_laden = QPushButton("Herunterladen (1.3 GB)")
        self.btn_gpu_laden.clicked.connect(lambda: self._zusatz({"art": "gpu", "name": "GPU-Paket",
                                                                 "quelle": ""}))
        z.addWidget(self.btn_gpu_kopie, 0, 2)
        z.addWidget(self.btn_gpu_laden, 0, 3)
        z.addWidget(QLabel("<b>Umwandlungs-Paket</b> (transformers + torch)"), 1, 0)
        self.lbl_umw = QLabel()
        z.addWidget(self.lbl_umw, 1, 1)
        self.btn_umw = QPushButton(f"Einrichten ({UMWANDLUNG_GROESSE})")
        self.btn_umw.clicked.connect(lambda: self._zusatz({"art": "umwandlung", "name": "Umwandlungs-Paket"}))
        z.addWidget(self.btn_umw, 1, 3)
        lay.addWidget(zusatz)

        # Originale nach dem Umwandeln
        orig = QHBoxLayout()
        self.chk_original = QCheckBox("Original nach dem Umwandeln aus dem Hugging-Face-Cache löschen "
                                      "(spart Platz – lässt sich jederzeit neu laden)")
        self.chk_original.setChecked(bool(kern.einstellungen_lesen().get("original_loeschen", True)))
        self.chk_original.toggled.connect(lambda an: kern.einstellungen_speichern(original_loeschen=an))
        orig.addWidget(self.chk_original, 1)
        self.btn_originale = QPushButton()
        self.btn_originale.setToolTip("Löscht Originale im Hugging-Face-Cache, deren umgewandelte Fassung "
                                      "schon im Ordner «modelle» liegt.")
        self.btn_originale.clicked.connect(self._originale_loeschen)
        orig.addWidget(self.btn_originale)
        lay.addLayout(orig)

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
        self.btn_laden.clicked.connect(self._angehakte_starten)
        self.btn_abbrechen = QPushButton("Abbrechen")
        self.btn_abbrechen.clicked.connect(self._abbrechen)
        self.btn_abbrechen.setEnabled(False)
        btn_ordner = QPushButton("Ordner «modelle» öffnen")
        btn_ordner.clicked.connect(lambda: self._oeffnen(kern.MODELL_ORDNER))
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
        angehakt = {a["name"] for a in self._angehakt()}
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
                self.baum.addTopLevelItem(g)
                g.setFirstColumnSpanned(True)
            beschreibung = m.beschreibung + ("   ← empfohlen" if m.name == self.empfohlen else "")
            zustand = {"bereit": "✓ heruntergeladen", "fehlt": "nicht heruntergeladen",
                       "umwandeln": "⟳ anhaken zum Umwandeln"}[m.zustand]
            it = QTreeWidgetItem([m.name, m.groesse, beschreibung, zustand])
            it.setData(0, ROLLE_MODELL, m.name)
            it.setData(1, ROLLE_MODELL, {"repo": m.repo, "zustand": m.zustand, "roh_pfad": m.roh_pfad})
            if (m.zustand == "fehlt" and m.repo) or m.zustand == "umwandeln":
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                vor = (m.name in angehakt or m.repo in self.vorgemerkt
                       or (self.erster_start and m.name == self.empfohlen and not angehakt))
                it.setCheckState(0, Qt.CheckState.Checked if vor else Qt.CheckState.Unchecked)
            else:
                # schon vorhanden: festes Häkchen (nicht änderbar), damit die Liste bündig bleibt
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                it.setCheckState(0, Qt.CheckState.Checked if m.zustand == "bereit" else Qt.CheckState.Unchecked)
            if m.zustand == "umwandeln":
                it.setToolTip(3, "Transformers-Format: wird ins CTranslate2-Format umgewandelt "
                                 "(braucht das Umwandlungs-Paket).")
            self.baum.addTopLevelItem(it)
        self.baum.blockSignals(False)
        self._zusatz_anzeigen()
        self._knoepfe()

    def _zusatz_anzeigen(self):
        if not self.gpu:
            self.lbl_gpu.setText("nicht nötig – keine NVIDIA-Grafikkarte")
        elif kern.gpu_paket_vorhanden():
            self.lbl_gpu.setText("✓ eingerichtet – die Grafikkarte wird genutzt")
        else:
            self.lbl_gpu.setText("fehlt – ohne Paket rechnet die App auf der CPU (langsamer)")
        self.lbl_umw.setText("✓ eingerichtet" if kern.umwandlung_vorhanden()
                             else "nur nötig für Modelle im Transformers-Format (z. B. Flix)")
        self.originale = kern.ueberfluessige_originale()
        summe = sum(b for _, b in self.originale)
        self.btn_originale.setText(f"Überflüssige Originale löschen ({kern.groesse_text(summe)})")
        self.btn_originale.setVisible(bool(self.originale))

    def _originale_loeschen(self):
        if not self.originale or self.proc is not None:
            return
        liste = "\n".join(f"  • {repo} ({kern.groesse_text(b)})" for repo, b in self.originale)
        antwort = QMessageBox.question(
            self, "Originale löschen",
            f"Diese Originale im Hugging-Face-Cache werden nicht mehr gebraucht, weil die umgewandelte "
            f"Fassung im Ordner «modelle» liegt:\n\n{liste}\n\nEndgültig löschen? (Lässt sich jederzeit "
            "neu herunterladen.)")
        if antwort != QMessageBox.StandardButton.Yes:
            return
        frei, fehler = 0, []
        for repo, _ in self.originale:
            try:
                frei += kern.original_loeschen(repo)
            except Exception as e:
                fehler.append(f"{repo}: {e}")
        self.lbl_status.setText(f"Originale gelöscht – {kern.groesse_text(frei)} frei."
                                + (f"  Nicht gelöscht: {'; '.join(fehler)}" if fehler else ""))
        self.fuellen()

    def _eintraege(self):
        for i in range(self.baum.topLevelItemCount()):
            it = self.baum.topLevelItem(i)
            if it.data(0, ROLLE_MODELL):
                yield it

    def _angehakt(self):
        auftraege = []
        for it in self._eintraege():
            if it.flags() & Qt.ItemFlag.ItemIsUserCheckable and it.checkState(0) == Qt.CheckState.Checked:
                d = it.data(1, ROLLE_MODELL)
                name = it.data(0, ROLLE_MODELL)
                if d["zustand"] == "fehlt":
                    auftraege.append({"art": "download", "name": name, "repo": d["repo"]})
                else:
                    auftraege.append({"art": "umwandeln", "name": name, "repo": d["repo"],
                                      "quelle": d["roh_pfad"]})
        return auftraege

    def _knoepfe(self):
        laeuft = self.proc is not None
        auftraege = self._angehakt()
        n_laden = sum(a["art"] == "download" for a in auftraege)
        n_umw = len(auftraege) - n_laden
        text = " + ".join(t for t in (f"Herunterladen ({n_laden})" if n_laden else "",
                                      f"Umwandeln ({n_umw})" if n_umw else "") if t)
        self.btn_laden.setText(text or "Herunterladen")
        self.btn_laden.setEnabled(not laeuft and bool(auftraege))
        self.btn_abbrechen.setEnabled(laeuft)
        self.btn_hinzu.setEnabled(not laeuft)
        gpu_fehlt = self.gpu and not kern.gpu_paket_vorhanden()
        self.btn_gpu_kopie.setVisible(gpu_fehlt and self.nvidia_quelle is not None)
        self.btn_gpu_laden.setVisible(gpu_fehlt)
        self.btn_gpu_kopie.setEnabled(not laeuft)
        self.btn_gpu_laden.setEnabled(not laeuft)
        self.btn_umw.setVisible(not kern.umwandlung_vorhanden())
        self.btn_umw.setEnabled(not laeuft)
        self.btn_originale.setEnabled(not laeuft)

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
        art = {"ct2": "direkt nutzbar (CTranslate2-Format)",
               "transformers": "Transformers-Format – wird nach dem Download umgewandelt",
               "unbekannt": "unbekanntes Format"}[info["format"]]
        self.lbl_status.setText(f"«{info['repo']}» hinzugefügt: {kern.groesse_text(info['gesamt'])}, {art}. "
                                "Zum Laden angehakt.")
        self.eingabe.clear()
        self.fuellen()

    # ------------------------------------------------------------ Aufträge

    def _angehakte_starten(self):
        self.warteschlange = self._angehakt()
        self._weiter()

    def _zusatz(self, auftrag):
        if self.proc is not None:
            return
        frei = shutil.disk_usage(kern.LOKAL.anchor if not kern.LOKAL.exists() else kern.LOKAL).free
        if frei < 3e9:
            QMessageBox.warning(self, "Zusatzpaket", f"Zu wenig Speicherplatz: nur {kern.groesse_text(frei)} frei "
                                                     "(nötig: etwa 3 GB).")
            return
        self.warteschlange = [auftrag]
        self._weiter()

    def _weiter(self):
        """Nächsten Auftrag starten (oder fertig)."""
        if not self.warteschlange:
            self.proc = None
            self.auftrag = None
            self.timer.stop()
            self.fuellen()
            return
        a = self.warteschlange.pop(0)
        if a["art"] == "umwandeln" and not kern.umwandlung_vorhanden():
            antwort = QMessageBox.question(
                self, "Umwandeln",
                f"«{a['name']}» ist im Transformers-Format und muss ins CTranslate2-Format umgewandelt werden.\n\n"
                f"Dafür braucht es einmalig das Umwandlungs-Paket (transformers + torch, {UMWANDLUNG_GROESSE}). "
                "Jetzt einrichten?")
            if antwort != QMessageBox.StandardButton.Yes:
                self.lbl_status.setText(f"«{a['name']}» nicht umgewandelt (Umwandlungs-Paket fehlt).")
                self._weiter()
                return
            self.warteschlange.insert(0, a)
            a = {"art": "umwandlung", "name": "Umwandlungs-Paket"}

        self.auftrag = a
        self.gesamt = 0
        self.start_zeit = time.time()
        self.start_bytes = kern.cache_bytes(a["repo"]) if a["art"] == "download" else 0
        self.balken.show()
        self.balken.setRange(0, 0)     # «beschäftigt», bis der erste Fortschritt kommt
        argumente = {
            "download": lambda: ["--download", a["repo"]],
            "umwandeln": lambda: ["--umwandeln", a["quelle"], a.get("repo", "")],
            "gpu": lambda: ["--gpu-paket"] + ([a["quelle"]] if a.get("quelle") else []),
            "umwandlung": lambda: ["--umwandlung-einrichten"],
        }[a["art"]]()
        anfang = {"download": "Verbinde mit Hugging Face …", "umwandeln": "Umwandlung startet …",
                  "gpu": "GPU-Paket wird eingerichtet …", "umwandlung": "Umwandlungs-Paket wird eingerichtet …"}
        self.lbl_status.setText(f"«{a['name']}»: {anfang[a['art']]}")
        try:
            self.proc = kern.prozess_starten(argumente, self.protokoll_pfad)
        except Exception as e:
            self.proc = None
            self.lbl_status.setText(f"«{a['name']}» konnte nicht gestartet werden: {e}")
            self._weiter()
            return
        self._knoepfe()
        self.timer.start()

    def _protokoll(self) -> dict:
        werte = {}
        try:
            for zeile in self.protokoll_pfad.read_text(encoding="utf-8", errors="replace").splitlines():
                art, _, rest = zeile.partition(" ")
                if art in ("GESAMT", "FERTIG", "FEHLER", "SCHRITT", "ANTEIL"):
                    werte[art] = rest
        except OSError:
            pass
        return werte

    def _anteil(self, anteil):
        if self.balken.maximum() == 0:
            self.balken.setRange(0, 1000)
        self.balken.setValue(int(min(anteil, 1.0) * 1000))

    def _pruefen(self):
        if self.proc is None:
            return
        a = self.auftrag
        p = self._protokoll()
        ende = self.proc.poll()
        if ende is None:
            if a["art"] == "download":
                self._download_fortschritt(a, p)
            else:
                if "ANTEIL" in p:
                    self._anteil(float(p["ANTEIL"]))
                if "SCHRITT" in p:
                    self.lbl_status.setText(f"«{a['name']}»: {p['SCHRITT']}")
            return

        # Prozess ist fertig
        self.balken.setRange(0, 1000)
        if ende == 0:
            self.balken.setValue(1000)
            text = {"download": "ist heruntergeladen ✓", "umwandeln": "ist umgewandelt ✓ und bereit",
                    "gpu": "ist eingerichtet ✓ – die Grafikkarte wird ab jetzt genutzt",
                    "umwandlung": "ist eingerichtet ✓"}[a["art"]]
            self.lbl_status.setText(f"«{a['name']}» {text}")
            if (a["art"] == "umwandeln" and a.get("repo") and self.chk_original.isChecked()
                    and kern.cache_ordner(a["repo"]).is_dir()):
                try:
                    frei = kern.original_loeschen(a["repo"])
                    self.lbl_status.setText(f"«{a['name']}» {text}  ·  Original im Cache gelöscht "
                                            f"({kern.groesse_text(frei)} frei)")
                except Exception as e:
                    self.lbl_status.setText(f"«{a['name']}» {text}  ·  Original nicht gelöscht: {e}")
            if a["art"] == "download":
                m = kern.modell_finden(a["name"])
                if m is not None and m.zustand == "umwandeln":
                    # Transformers-Format: gleich umwandeln
                    self.warteschlange.insert(0, {"art": "umwandeln", "name": m.name, "repo": m.repo,
                                                  "quelle": m.roh_pfad})
        else:
            self.lbl_status.setText(f"«{a['name']}»: {p.get('FEHLER', f'abgebrochen (Code {ende})')}")
            self.balken.setValue(0)
        self.proc = None
        self._weiter()

    def _download_fortschritt(self, a, p):
        repo = a["repo"]
        if "GESAMT" in p and not self.gesamt:
            self.gesamt = int(p["GESAMT"])
            self.start_zeit = time.time()            # Tempo ab jetzt messen (ohne Verbindungsaufbau)
            self.start_bytes = kern.cache_bytes(repo)
            cache = kern.hf_cache()
            frei = shutil.disk_usage(cache if cache.exists() else cache.anchor).free
            noetig = self.gesamt - self.start_bytes
            if frei < noetig + 300e6:
                self.warteschlange.clear()
                self._abbrechen()
                self.lbl_status.setText(f"Zu wenig Speicherplatz für «{a['name']}»: nötig "
                                        f"{kern.groesse_text(noetig)}, frei {kern.groesse_text(frei)}.")
                return
        if not self.gesamt:
            return
        jetzt = kern.cache_bytes(repo)
        self._anteil(jetzt / self.gesamt)
        dt = max(time.time() - self.start_zeit, 0.1)
        tempo = (jetzt - self.start_bytes) / dt
        rest = (self.gesamt - jetzt) / tempo if tempo > 1e4 else 0
        text = (f"«{a['name']}»: {kern.groesse_text(jetzt)} von {kern.groesse_text(self.gesamt)}"
                f"  ·  {tempo / 1e6:.1f} MB/s")
        if rest:
            text += f"  ·  noch {kern.zeit(rest)}"
        n = len(self.warteschlange)
        if n:
            text += f"   (danach noch {n})"
        self.lbl_status.setText(text)

    def _abbrechen(self):
        self.warteschlange.clear()
        if self.proc is not None:
            self.timer.stop()
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:
                pass
            a = self.auftrag or {"name": "", "art": ""}
            self.proc = None
            self.auftrag = None
            self.balken.setRange(0, 1000)
            weiter = (" Beim nächsten Mal geht es dort weiter, wo er aufgehört hat."
                      if a["art"] == "download" else "")
            self.lbl_status.setText(f"«{a['name']}» abgebrochen.{weiter}")
        self.fuellen()

    # ------------------------------------------------------------ Ordner, Schliessen

    def _oeffnen(self, ordner: Path):
        ordner.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ordner)))

    def closeEvent(self, e):
        if self.proc is not None:
            antwort = QMessageBox.question(self, "Modelle", "Es läuft noch etwas. Abbrechen und schliessen?")
            if antwort != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self._abbrechen()
        kern.einstellungen_speichern(eingerichtet=True)
        e.accept()

    def reject(self):          # Esc
        self.close()
