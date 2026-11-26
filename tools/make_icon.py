"""Zeichnet das Transcriber-Symbol (Schallwelle wird zu Textzeilen) und speichert es als .ico
mit mehreren Grössen. Läuft mit dem Python aus dem Build-Ordner (braucht PySide6).

    python make_icon.py ziel.ico
"""

import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath

GROESSEN = [16, 24, 32, 48, 64, 128, 256]


def zeichnen(n: int) -> QImage:
    bild = QImage(n, n, QImage.Format_ARGB32)
    bild.fill(Qt.transparent)
    p = QPainter(bild)
    p.setRenderHint(QPainter.Antialiasing)
    s = n / 256

    # abgerundetes Quadrat mit Verlauf
    rand = 8 * s
    flaeche = QRectF(rand, rand, n - 2 * rand, n - 2 * rand)
    verlauf = QLinearGradient(0, 0, n, n)
    verlauf.setColorAt(0, QColor("#2b6cff"))
    verlauf.setColorAt(1, QColor("#8a3ffc"))
    pfad = QPainterPath()
    pfad.addRoundedRect(flaeche, 56 * s, 56 * s)
    p.fillPath(pfad, verlauf)

    p.setPen(Qt.NoPen)
    p.setBrush(QColor("white"))
    # links: Schallwelle (Balken)
    hoehen = [50, 110, 160, 90, 130]
    breite = 14 * s
    x = 44 * s
    for h in hoehen:
        h *= s
        p.drawRoundedRect(QRectF(x, n / 2 - h / 2, breite, h), breite / 2, breite / 2)
        x += 24 * s
    # rechts: Textzeilen
    p.setBrush(QColor(255, 255, 255, 230))
    for i, laenge in enumerate([62, 46, 62, 34]):
        y = (78 + i * 30) * s
        p.drawRoundedRect(QRectF(166 * s, y, laenge * s, 14 * s), 7 * s, 7 * s)
    p.end()
    return bild


def png_bytes(bild: QImage) -> bytes:
    daten = QByteArray()
    puffer = QBuffer(daten)
    puffer.open(QIODevice.WriteOnly)
    bild.save(puffer, "PNG")
    return bytes(daten)


def main(ziel: str):
    QGuiApplication(sys.argv[:1])
    bilder = [png_bytes(zeichnen(n)) for n in GROESSEN]
    kopf = struct.pack("<HHH", 0, 1, len(bilder))
    offset = 6 + 16 * len(bilder)
    eintraege, inhalt = b"", b""
    for n, png in zip(GROESSEN, bilder):
        eintraege += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(png), offset + len(inhalt))
        inhalt += png
    with open(ziel, "wb") as f:
        f.write(kopf + eintraege + inhalt)
    # zusätzlich PNG für das Fenstersymbol der App
    zeichnen(256).save(ziel[:-4] + ".png")


if __name__ == "__main__":
    main(sys.argv[1])
