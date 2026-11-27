"""
start_seite.py – die Startseite von InkWave: zwei grosse Kacheln zur Auswahl, was man tun will
(Transkribieren oder MP4 → MP3). Eine Kachel lässt sich anklicken, mit Tab/Enter bedienen, und man
kann Dateien direkt darauf ziehen – sie landen dann gleich im passenden Werkzeug.
"""

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

import theme
from bausteine import InfoKnopf


class Symbol(QWidget):
    """Kleines gezeichnetes Symbol auf lila Verlauf: "welle" (Sprache) oder "note" (Musik)."""

    def __init__(self, art: str, groesse: int = 68):
        super().__init__()
        self.art = art
        self.setFixedSize(groesse, groesse)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = float(self.width())
        verlauf = QLinearGradient(0, 0, g, g)
        verlauf.setColorAt(0, QColor(theme.f["akzent"]))
        verlauf.setColorAt(1, QColor(theme.f["akzent2"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(verlauf))
        p.drawRoundedRect(QRectF(0, 0, g, g), g * 0.28, g * 0.28)
        weiss = QColor("white")
        if self.art == "welle":
            # Schallwelle: fünf Balken
            p.setBrush(weiss)
            breite = g * 0.075
            for i, hoehe in enumerate((0.20, 0.42, 0.58, 0.34, 0.16)):
                x = g * (0.26 + i * 0.12)
                h = g * hoehe
                p.drawRoundedRect(QRectF(x - breite / 2, (g - h) / 2, breite, h), breite / 2, breite / 2)
        else:
            # Doppelnote: zwei Köpfe, zwei Hälse, ein Balken
            p.setBrush(weiss)
            kopf = g * 0.115
            links, rechts = QPointF(g * 0.36, g * 0.68), QPointF(g * 0.66, g * 0.61)
            for mitte in (links, rechts):
                p.drawEllipse(mitte, kopf, kopf * 0.82)
            stift = QPen(weiss, g * 0.055, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(stift)
            oben_l = QPointF(links.x() + kopf * 0.82, g * 0.33)
            oben_r = QPointF(rechts.x() + kopf * 0.82, g * 0.26)
            p.drawLine(QPointF(oben_l.x(), links.y()), oben_l)
            p.drawLine(QPointF(oben_r.x(), rechts.y()), oben_r)
            balken = QPainterPath(oben_l)
            balken.lineTo(oben_r)
            p.setPen(QPen(weiss, g * 0.10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPath(balken)
        p.end()


class Kachel(QFrame):
    """Grosse, anklickbare Karte für ein Werkzeug."""
    geklickt = Signal()
    abgelegt = Signal(list)

    def __init__(self, symbol: str, titel: str, text: str, info: str, knopf: str):
        super().__init__()
        self.setObjectName("kachel")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.setAccessibleName(titel)
        self.setMinimumHeight(230)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 26, 28, 24)
        lay.setSpacing(8)
        kopf = QHBoxLayout()
        kopf.addWidget(Symbol(symbol))
        kopf.addStretch(1)
        self.lbl_hinweis = QLabel("")
        self.lbl_hinweis.setObjectName("kachel_hinweis")
        self.lbl_hinweis.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_hinweis.hide()
        kopf.addWidget(self.lbl_hinweis, 0, Qt.AlignmentFlag.AlignTop)
        self.info = InfoKnopf(info, titel)
        kopf.addWidget(self.info, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(kopf)
        lay.addSpacing(12)
        t = QLabel(titel)
        t.setObjectName("kachel_titel")
        lay.addWidget(t)
        b = QLabel(text)
        b.setObjectName("gedaempft")
        b.setWordWrap(True)
        lay.addWidget(b)
        lay.addStretch(1)
        los = QLabel(f"{knopf}  →")
        los.setObjectName("kachel_los")
        lay.addWidget(los)
        # Klicks und Ablegen gelten für die ganze Kachel, nicht für einzelne Beschriftungen (ausser dem «i»)
        for kind in self.findChildren(QWidget):
            if kind is not self.info:
                kind.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def hinweis(self, text: str):
        """Kleines Schild oben rechts, z. B. «läuft …» oder «3 Dateien»."""
        self.lbl_hinweis.setText(text)
        self.lbl_hinweis.setVisible(bool(text))

    def _aktiv(self, an: bool):
        if self.property("aktiv") != an:
            self.setProperty("aktiv", an)
            theme.neu_polieren(self)

    def enterEvent(self, e):
        self._aktiv(True)

    def leaveEvent(self, e):
        self._aktiv(self.hasFocus())

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self._aktiv(True)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._aktiv(self.underMouse())

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.geklickt.emit()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.geklickt.emit()
        else:
            super().keyPressEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._aktiv(True)

    def dragLeaveEvent(self, e):
        self._aktiv(False)

    def dropEvent(self, e):
        self._aktiv(False)
        pfade = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if pfade:
            e.acceptProposedAction()
            self.abgelegt.emit(pfade)


class StartSeite(QWidget):
    """Auswahl: Was möchtest du tun?"""
    gewaehlt = Signal(str)              # "transkribieren" | "konverter" | "einstellungen"
    abgelegt = Signal(str, list)        # Werkzeug, Pfade
    output_oeffnen = Signal()

    def __init__(self):
        super().__init__()
        aussen = QVBoxLayout(self)
        aussen.setContentsMargins(0, 0, 0, 0)
        aussen.addStretch(2)
        frage = QLabel("Was möchtest du tun?")
        frage.setObjectName("begruessung")
        frage.setAlignment(Qt.AlignmentFlag.AlignCenter)
        aussen.addWidget(frage)
        aussen.addSpacing(24)

        reihe = QHBoxLayout()
        reihe.setSpacing(18)
        reihe.addStretch(1)
        self.kacheln = {
            "transkribieren": Kachel(
                "welle", "Transkribieren", "Audio und Video in Text",
                "Verwandelt Sprache aus Audio- und Videodateien in Text – lokal und offline, mit Whisper.\n\n"
                "• mp3, m4a, wav, mp4, mkv … auch ganze Ordner\n"
                "• Ergebnis als .txt, auf Wunsch .srt-Untertitel\n"
                "• 100 Sprachen, mit oder ohne Grafikkarte\n\n"
                "Tipp: Dateien lassen sich direkt auf die Kachel ziehen.", "Öffnen"),
            "konverter": Kachel(
                "note", "MP4 → MP3", "Tonspur aus Videos als MP3",
                "Speichert die Tonspur von Videos als MP3 – lokal, ohne Upload.\n\n"
                "• mp4, mov, mkv, webm … auch ganze Ordner\n"
                "• Qualität wählbar: 128 bis 320 kbit/s\n"
                "• mehrere Videos gleichzeitig\n\n"
                "Tipp: Videos lassen sich direkt auf die Kachel ziehen.", "Öffnen"),
        }
        for name, kachel in self.kacheln.items():
            kachel.setMaximumWidth(460)
            kachel.geklickt.connect(lambda n=name: self.gewaehlt.emit(n))
            kachel.abgelegt.connect(lambda pfade, n=name: self.abgelegt.emit(n, pfade))
            reihe.addWidget(kachel, 10)
        reihe.addStretch(1)
        aussen.addLayout(reihe)
        aussen.addSpacing(18)

        fuss = QHBoxLayout()
        fuss.addStretch(1)
        for text, signal in (("Output-Ordner öffnen", self.output_oeffnen.emit),
                             ("Einstellungen", lambda: self.gewaehlt.emit("einstellungen"))):
            knopf = QPushButton(text)
            knopf.setObjectName("link")
            knopf.clicked.connect(lambda _=False, s=signal: s())
            fuss.addWidget(knopf)
        fuss.addStretch(1)
        aussen.addLayout(fuss)
        aussen.addStretch(3)

    def hinweis(self, werkzeug: str, text: str):
        self.kacheln[werkzeug].hinweis(text)
