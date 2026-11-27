"""
bausteine.py – kleine Bausteine, die mehrere Seiten brauchen.

InfoKnopf: das runde «i». Die Oberfläche kommt mit wenig Text aus; wo eine Erklärung hilft, steht
ein «i» – ein Klick zeigt sie in einer kleinen Sprechblase (Klick daneben oder Esc schliesst sie).
"""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget


class InfoKnopf(QToolButton):
    def __init__(self, text: str, titel: str = ""):
        super().__init__()
        self.info_text = text
        self.info_titel = titel
        self.setObjectName("info")
        self.setText("i")
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Info" + (f": {titel}" if titel else ""))
        self.setAccessibleDescription(text)
        self.blase = None
        self.clicked.connect(self.zeigen)

    def zeigen(self):
        # Durchsichtiges Fenster (für die runden Ecken), darin der eigentliche Rahmen mit Hintergrund
        blase = QWidget(self.window(), Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                        | Qt.WindowType.NoDropShadowWindowHint)
        blase.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        blase.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        aussen = QVBoxLayout(blase)
        aussen.setContentsMargins(0, 0, 0, 0)
        rahmen = QFrame()
        rahmen.setObjectName("infoblase")
        aussen.addWidget(rahmen)
        lay = QVBoxLayout(rahmen)
        lay.setContentsMargins(14, 11, 14, 12)
        lay.setSpacing(4)
        if self.info_titel:
            t = QLabel(self.info_titel)
            t.setObjectName("info_titel")
            lay.addWidget(t)
        lbl = QLabel(self.info_text)
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setWordWrap(True)
        lbl.setFixedWidth(320)
        lay.addWidget(lbl)
        blase.adjustSize()
        # unter dem «i», aber immer ganz auf dem Bildschirm
        pos = self.mapToGlobal(QPoint(-12, self.height() + 6))
        schirm = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        x = min(max(pos.x(), schirm.left() + 8), schirm.right() - blase.width() - 8)
        y = pos.y()
        if y + blase.height() > schirm.bottom() - 8:
            y = self.mapToGlobal(QPoint(0, 0)).y() - blase.height() - 6
        blase.move(x, y)
        blase.show()
        self.blase = blase
        blase.destroyed.connect(lambda: setattr(self, "blase", None))


def mit_info(inhalt, text: str, titel: str = "") -> QHBoxLayout:
    """Zeile aus einem Widget (oder einer Beschriftung als Text) und dem «i» direkt dahinter."""
    zeile = QHBoxLayout()
    zeile.setSpacing(7)
    zeile.setContentsMargins(0, 0, 0, 0)
    if isinstance(inhalt, str):
        inhalt = QLabel(inhalt)
        inhalt.setObjectName("gedaempft")
    zeile.addWidget(inhalt)
    zeile.addWidget(InfoKnopf(text, titel), 0, Qt.AlignmentFlag.AlignVCenter)
    zeile.addStretch(1)
    return zeile


def karte() -> QFrame:
    k = QFrame()
    k.setObjectName("karte")
    return k


def beschriftung(text: str, art: str = "") -> QLabel:
    lbl = QLabel(text)
    if art:
        lbl.setObjectName(art)
    return lbl


def trennlinie() -> QWidget:
    linie = QFrame()
    linie.setObjectName("trennlinie")
    linie.setFixedHeight(1)
    return linie
