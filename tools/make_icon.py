"""Macht aus tools\\logo.png («Wellen zu Worten») das InkWave-Symbol: eine .ico mit mehreren
Grössen (für die exe, Verknüpfungen und die Taskleiste) und eine .png fürs Fenster.
Läuft mit dem Python aus dem Build-Ordner (braucht PySide6).

    python make_icon.py ziel.ico
"""

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage

GROESSEN = [16, 20, 24, 32, 40, 48, 64, 128, 256]
LOGO = Path(__file__).resolve().parent / "logo.png"


def skaliert(logo: QImage, n: int) -> QImage:
    # in Stufen verkleinern (je höchstens halbieren) -> auch 16 px bleiben sauber
    bild = logo
    while bild.width() // 2 >= n:
        bild = bild.scaled(bild.width() // 2, bild.height() // 2,
                           Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return bild.scaled(n, n, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


def png_bytes(bild: QImage) -> bytes:
    daten = QByteArray()
    puffer = QBuffer(daten)
    puffer.open(QIODevice.OpenModeFlag.WriteOnly)
    bild.save(puffer, "PNG")
    return bytes(daten)


def main(ziel: str):
    QGuiApplication(sys.argv[:1])
    logo = QImage(str(LOGO)).convertToFormat(QImage.Format.Format_ARGB32)
    if logo.isNull():
        sys.exit(f"Logo fehlt: {LOGO}")
    bilder = [png_bytes(skaliert(logo, n)) for n in GROESSEN]
    kopf = struct.pack("<HHH", 0, 1, len(bilder))
    offset = 6 + 16 * len(bilder)
    eintraege, inhalt = b"", b""
    for n, png in zip(GROESSEN, bilder):
        eintraege += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(png), offset + len(inhalt))
        inhalt += png
    with open(ziel, "wb") as f:
        f.write(kopf + eintraege + inhalt)
    # PNG für Fenster und Kopfzeile der App
    skaliert(logo, 256).save(ziel[:-4] + ".png")


if __name__ == "__main__":
    main(sys.argv[1])
