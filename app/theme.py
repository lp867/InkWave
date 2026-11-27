"""
theme.py – das Aussehen von InkWave: hell oder dunkel, je nach Windows-Einstellung, mit den Lila-Tönen
aus dem Logo («Wellen zu Worten»). Wechselt Windows zwischen hell und dunkel, zieht die App sofort mit.

Zum Testen erzwingen: Umgebungsvariable INKWAVE_THEMA=hell oder =dunkel.
"""

import os
from pathlib import Path
from string import Template

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

import kern

FARBEN = {
    "dunkel": {
        "hintergrund": "#100a18", "karte": "#1a1325", "karte_rand": "#2b2139", "feld": "#211a2d",
        "feld_rand": "#3b2e4c", "hover": "#2b2139", "auswahl": "#3a2458", "text": "#f3eefa",
        "gedaempft": "#a497b6", "akzent": "#b84dff", "akzent2": "#7c3aed", "akzent_hover": "#c86bff",
        "akzent_text": "#d9a6ff", "akzent_weich": "#2c1b40", "erfolg": "#4ade80", "warnung": "#fbbf24",
        "fehler": "#f87171", "drop": "#160f20",
    },
    "hell": {
        "hintergrund": "#f5f2fa", "karte": "#ffffff", "karte_rand": "#e6dff0", "feld": "#faf8fd",
        "feld_rand": "#d6cce4", "hover": "#f1e9fb", "auswahl": "#ead9fc", "text": "#1d1528",
        "gedaempft": "#6c607e", "akzent": "#9b30f0", "akzent2": "#6d28d9", "akzent_hover": "#ad52f5",
        "akzent_text": "#7a1fc9", "akzent_weich": "#f2e6fe", "erfolg": "#15803d", "warnung": "#b45309",
        "fehler": "#dc2626", "drop": "#fbf9fe",
    },
}

QSS = Template("""
QWidget { color: $text; font-size: 10pt; }
QMainWindow, QDialog { background: $hintergrund; }
QLabel, QCheckBox { background: transparent; }
QFrame#karte { background: $karte; border: 1px solid $karte_rand; border-radius: 14px; }
QLabel#titel { font-size: 21pt; font-weight: 800; }
QLabel#untertitel, QLabel#gedaempft { color: $gedaempft; }
QLabel#abschnitt { font-size: 11.5pt; font-weight: 700; }
QLabel#gross { font-size: 15pt; font-weight: 700; }
QLabel#prozent { font-size: 11pt; font-weight: 700; color: $akzent_text; }
QLabel#chip { background: $akzent_weich; color: $akzent_text; border-radius: 12px; padding: 5px 12px;
              font-weight: 600; }
QLabel#zaehler { background: $akzent_weich; color: $akzent_text; border-radius: 10px; padding: 2px 9px;
                 font-weight: 700; }

QPushButton { background: $feld; border: 1px solid $feld_rand; border-radius: 9px; padding: 7px 14px; }
QPushButton:hover { background: $hover; border-color: $akzent; }
QPushButton:pressed { background: $auswahl; }
QPushButton:disabled { color: $gedaempft; background: $karte; border-color: $karte_rand; }
QPushButton#primaer { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 $akzent, stop:1 $akzent2);
                      color: white; border: none; border-radius: 12px; padding: 12px 24px;
                      font-size: 11.5pt; font-weight: 700; }
QPushButton#primaer:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 $akzent_hover,
                                                        stop:1 $akzent); }
QPushButton#primaer:disabled { background: $feld; color: $gedaempft; border: 1px solid $karte_rand; }
QPushButton#link { background: transparent; border: none; color: $akzent_text; padding: 5px 8px;
                   font-weight: 600; }
QPushButton#link:hover { background: $akzent_weich; border-radius: 8px; }
QPushButton#link:disabled { color: $gedaempft; }

QComboBox { combobox-popup: 0; }   /* Liste mit höchstens maxVisibleItems Einträgen, dann scrollen */
QComboBox, QLineEdit { background: $feld; border: 1px solid $feld_rand; border-radius: 9px; padding: 6px 10px;
                       min-height: 22px; selection-background-color: $akzent; selection-color: white; }
QComboBox:hover, QLineEdit:hover, QComboBox:focus, QLineEdit:focus { border-color: $akzent; }
QComboBox:disabled, QLineEdit:disabled { color: $gedaempft; background: $karte; }
QComboBox::drop-down { border: none; width: 28px; }
QComboBox::down-arrow { image: url("$pfeil"); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: $karte; border: 1px solid $karte_rand; padding: 4px; outline: 0;
                              selection-background-color: $auswahl; selection-color: $text; }
QComboBox QAbstractItemView::item { min-height: 26px; padding: 2px 6px; }

QCheckBox { spacing: 9px; }
QCheckBox::indicator, QTreeView::indicator { width: 18px; height: 18px; border-radius: 6px;
                                             border: 1px solid $feld_rand; background: $feld; }
QCheckBox::indicator:hover, QTreeView::indicator:hover { border-color: $akzent; }
QCheckBox::indicator:checked, QTreeView::indicator:checked { background: $akzent; border-color: $akzent;
                                                              image: url("$haken"); }
QCheckBox::indicator:disabled { background: $karte; }

QTreeView { background: transparent; border: none; outline: 0; }
QTreeView::item { padding: 7px 4px; border: none; }
QTreeView::item:hover { background: $hover; }
QTreeView::item:selected { background: $auswahl; color: $text; }
QHeaderView, QHeaderView::section { background: transparent; }
QHeaderView::section { color: $gedaempft; border: none; border-bottom: 1px solid $karte_rand;
                       padding: 6px 6px; font-weight: 600; }

QProgressBar { background: $feld; border: none; border-radius: 5px; min-height: 10px; max-height: 10px;
               color: transparent; }
QProgressBar::chunk { border-radius: 5px;
                      background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 $akzent, stop:1 $akzent2); }
QProgressBar#zeile { min-height: 6px; max-height: 6px; border-radius: 3px; }
QProgressBar#zeile::chunk { border-radius: 3px; }

QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; color: $gedaempft; padding: 6px 14px; border-radius: 8px;
               margin-right: 4px; font-weight: 600; }
QTabBar::tab:selected { background: $akzent_weich; color: $akzent_text; }
QTabBar::tab:hover:!selected { color: $text; }

QPlainTextEdit { background: $feld; border: 1px solid $karte_rand; border-radius: 10px; padding: 8px;
                 selection-background-color: $akzent; selection-color: white; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle { background: $feld_rand; border-radius: 4px; min-height: 30px; min-width: 30px; }
QScrollBar::handle:hover { background: $akzent; }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { background: none;
                                                                                       border: none; width: 0; height: 0; }

QGroupBox { background: $karte; border: 1px solid $karte_rand; border-radius: 12px; margin-top: 14px;
            padding: 16px 12px 10px 12px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 4px; color: $gedaempft; }

QScrollArea#rollen, QWidget#innen, QScrollArea#rollen > QWidget#qt_scrollarea_viewport { background: transparent;
                                                                                        border: none; }
QLabel#begruessung { font-size: 21pt; font-weight: 800; }
QFrame#kachel { background: $karte; border: 2px solid $karte_rand; border-radius: 22px; }
QFrame#kachel[aktiv="true"] { border-color: $akzent; background: $drop; }
QLabel#kachel_titel { font-size: 17pt; font-weight: 800; }
QLabel#kachel_los { color: $akzent_text; font-size: 11pt; font-weight: 700; }
QLabel#kachel_hinweis { background: $akzent_weich; color: $akzent_text; border-radius: 10px; padding: 3px 10px;
                        font-weight: 600; }
QPushButton#zurueck { background: $feld; border: 1px solid $feld_rand; border-radius: 15px; padding: 6px 14px;
                      font-weight: 600; }
QPushButton#zurueck:hover { border-color: $akzent; color: $akzent_text; }
QPushButton#segment { border-radius: 0; padding: 7px 18px; margin: 0; }
QPushButton#segment[lage="links"] { border-top-left-radius: 9px; border-bottom-left-radius: 9px; }
QPushButton#segment[lage="rechts"] { border-top-right-radius: 9px; border-bottom-right-radius: 9px; }
QPushButton#segment:checked { background: $akzent; border-color: $akzent; color: white; font-weight: 700; }
QToolButton#info { background: transparent; border: 1px solid $feld_rand; border-radius: 10px; color: $gedaempft;
                   font-size: 9pt; font-weight: 700; font-style: italic; padding: 0; }
QToolButton#info:hover, QToolButton#info:focus { border-color: $akzent; color: $akzent_text; background: $akzent_weich; }
QFrame#infoblase { background: $karte; border: 1px solid $akzent; border-radius: 12px; }
QLabel#info_titel { font-weight: 700; color: $akzent_text; }
QFrame#trennlinie { background: $karte_rand; border: none; }
QLabel#pfad { background: $feld; border: 1px solid $karte_rand; border-radius: 8px; padding: 6px 10px; }
QFrame#dropzone { background: $drop; border: 2px dashed $feld_rand; border-radius: 16px; }
QFrame#dropzone[aktiv="true"] { background: $akzent_weich; border-color: $akzent; }
QSplitter::handle { background: transparent; }
QToolTip { background: $karte; color: $text; border: 1px solid $karte_rand; padding: 6px; }
""")

_HAKEN = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18"><path d="M4 9.5l3.2 3.2L14 5.8" '
          'fill="none" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
_PFEIL = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12"><path d="M2.5 4.5L6 8l3.5-3.5" '
          'fill="none" stroke="{farbe}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>')

# aktuelle Farben (für Widgets, die selbst zeichnen oder einfärben)
f = dict(FARBEN["dunkel"])


def _bilder(name: str, farben: dict) -> dict:
    """Kleine SVG-Bilder (Häkchen, Pfeil) ablegen – Qt-Stylesheets brauchen dafür Dateien.
    Die Pfade stehen im Stylesheet in Anführungszeichen (Benutzernamen mit Leerzeichen oder Klammern)."""
    ordner = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "InkWave" / "thema"
    haken = ordner / "haken.svg"
    pfeil = ordner / f"pfeil_{name}.svg"
    try:
        ordner.mkdir(parents=True, exist_ok=True)
        haken.write_text(_HAKEN, encoding="utf-8")
        pfeil.write_text(_PFEIL.format(farbe=farben["gedaempft"]), encoding="utf-8")
    except OSError:
        pass          # dann fehlen höchstens Häkchen und Pfeil – kein Grund, den Start abzubrechen
    return {"haken": haken.as_posix(), "pfeil": pfeil.as_posix()}


def gewaehltes_thema() -> str:
    """Die Wahl aus den Einstellungen: "system" (wie Windows), "hell" oder "dunkel"."""
    wahl = kern.einstellung("thema", "system")
    return wahl if wahl in ("hell", "dunkel") else "system"


def windows_dunkel() -> bool:
    erzwungen = os.environ.get("INKWAVE_THEMA", "").lower()      # nur zum Testen
    if erzwungen not in ("hell", "dunkel"):
        erzwungen = gewaehltes_thema()                            # Einstellungen der App
    if erzwungen in ("hell", "dunkel"):
        return erzwungen == "dunkel"
    schema = QApplication.styleHints().colorScheme()
    if schema == Qt.ColorScheme.Dark:
        return True
    if schema == Qt.ColorScheme.Light:
        return False
    try:   # Qt weiss es nicht: in der Registry nachsehen
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


class Thema(QObject):
    """Setzt Palette und Stylesheet und hält sie mit Windows hell/dunkel synchron."""
    geaendert = Signal()

    def __init__(self, app: QApplication):
        super().__init__(app)
        self.app = app
        self.name = ""
        app.setStyle("Fusion")
        app.styleHints().colorSchemeChanged.connect(lambda *_: self.anwenden())
        self.anwenden()

    def anwenden(self):
        name = "dunkel" if windows_dunkel() else "hell"
        if name == self.name:
            return
        self.name = name
        farben = FARBEN[name]
        f.clear()
        f.update(farben)

        pal = QPalette()
        c = lambda k: QColor(farben[k])  # noqa: E731
        for rolle, k in ((QPalette.ColorRole.Window, "hintergrund"), (QPalette.ColorRole.WindowText, "text"),
                         (QPalette.ColorRole.Base, "feld"), (QPalette.ColorRole.AlternateBase, "karte"),
                         (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.Button, "feld"),
                         (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.ToolTipBase, "karte"),
                         (QPalette.ColorRole.ToolTipText, "text"), (QPalette.ColorRole.Highlight, "akzent"),
                         (QPalette.ColorRole.PlaceholderText, "gedaempft"), (QPalette.ColorRole.Link, "akzent_text")):
            pal.setColor(rolle, c(k))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
        for rolle in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
            pal.setColor(QPalette.ColorGroup.Disabled, rolle, c("gedaempft"))
        self.app.setPalette(pal)
        self.app.setStyleSheet(QSS.substitute(**farben, **_bilder(name, farben)))
        self.geaendert.emit()


def neu_polieren(widget):
    """Nach dem Ändern einer Eigenschaft (z. B. aktiv=true) das Stylesheet neu anwenden."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
