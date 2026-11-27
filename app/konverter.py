"""
konverter.py – Videos in MP3 umwandeln, ohne Oberfläche.

Abgeleitet aus Datachanger\\mp4_to_mp3\\mp4_zu_mp3.py. Statt eines eigenen ffmpeg.exe (unsigniert –
Smart App Control könnte es blockieren, und die App würde 80 MB grösser) rechnet hier PyAV, das für
die Transkription ohnehin dabei ist: dieselben FFmpeg-Bibliotheken samt MP3-Encoder (LAME), geladen
im signierten pythonw.exe.

Die Oberfläche (konverter_seite.py) ruft nur Funktionen von hier auf; Fortschritt kommt über den
Rückruf `melden(anteil, position, dauer)`, abgebrochen wird über ein threading.Event.
"""

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import kern

# Aus Ordnern werden nur diese Endungen gesammelt; ausdrücklich gewählte Dateien werden immer versucht
# (auch Audiodateien wie .m4a oder .wav lassen sich so in MP3 umwandeln).
VIDEO_ENDUNGEN = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg",
                  ".ts", ".3gp"}

# kbit/s, Anzeige
BITRATEN = [(128, "128 kbit/s  ·  klein"), (192, "192 kbit/s  ·  Standard"),
            (256, "256 kbit/s  ·  hoch"), (320, "320 kbit/s  ·  beste Qualität")]
STANDARD_BITRATE = 192

# Abtastraten, die MP3 kennt. Alles andere (z. B. 96 kHz) wird auf 48 kHz umgerechnet.
_MP3_RATEN = (48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000)
# Angaben, die aus dem Video in die MP3 übernommen werden (Titel usw.)
_METADATEN = ("title", "artist", "album", "album_artist", "date", "genre", "track", "composer", "comment")


@dataclass
class Mp3Ergebnis:
    datei: Path           # das Video
    mp3: Path             # die fertige MP3
    dauer: float          # Länge der Tonspur in Sekunden
    benoetigt: float      # Rechenzeit in Sekunden
    groesse: int          # Grösse der MP3 in Bytes

    @property
    def tempo(self) -> float:
        return self.dauer / max(self.benoetigt, 0.01)


def videos_sammeln(eingaben):
    """Dateien und Ordner (rekursiv) -> (Videodateien, nicht gefundene Eingaben)."""
    dateien, fehlend = [], []
    for e in eingaben:
        p = Path(e)
        try:
            if p.is_dir():
                dateien += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_ENDUNGEN)
            elif p.is_file():
                dateien.append(p)
            else:
                fehlend.append(str(e))
        except OSError:
            fehlend.append(str(e))
    return list(dict.fromkeys(dateien)), fehlend


def parallel_anzahl() -> int:
    """Wie viele Videos gleichzeitig umgewandelt werden: halbe Kernzahl, höchstens 4."""
    return max(1, min(4, kern.physische_kerne() // 2))


def ziel_reservieren(name: str, ordner: Path) -> Path:
    """Legt ordner\\name.mp3 leer an und gibt den Pfad zurück – ist der Name vergeben: name_2, name_3 …
    «Exklusiv» angelegt: so überschreibt nie etwas eine vorhandene Datei, auch nicht, wenn mehrere
    Videos mit gleichem Namen gleichzeitig laufen."""
    ordner.mkdir(parents=True, exist_ok=True)
    kandidaten = [name]
    if len(name) > 100:                       # sehr lange Namen: notfalls gekürzt statt gar nicht
        kandidaten.append(name[:100].rstrip(" .") or "audio")
    for nr, kandidat in enumerate(kandidaten, 1):
        try:
            n = 1
            while True:
                ziel = ordner / (f"{kandidat}.mp3" if n == 1 else f"{kandidat}_{n}.mp3")
                try:
                    with open(ziel, "x"):
                        return ziel
                except FileExistsError:
                    n += 1
        except OSError:
            if nr == len(kandidaten):
                raise


def _loeschen(*pfade):
    for p in pfade:
        try:
            p.unlink()
        except OSError:
            pass


def in_mp3_umwandeln(quelle: Path, ordner: Path | None = None, bitrate: int = STANDARD_BITRATE,
                     melden=lambda **k: None, abbrechen: threading.Event | None = None) -> Mp3Ergebnis:
    """Schreibt die erste Tonspur von quelle als MP3 nach ordner (Standard: Output-Ordner der App).
    Wirft kern.KeineTonspur (keine lesbare Datei / kein Ton) und kern.Abgebrochen."""
    import av

    start = time.time()
    quelle = Path(quelle)
    ordner = Path(ordner) if ordner else kern.OUTPUT_ORDNER
    try:
        eingang = av.open(str(quelle))
    except Exception as e:
        raise kern.KeineTonspur(f"Datei kann nicht gelesen werden – keine Video- oder Audiodatei? "
                                f"({type(e).__name__})") from e
    ziel = teil = None
    try:
        with eingang:
            if not eingang.streams.audio:
                raise kern.KeineTonspur("Das Video hat keine Tonspur.")
            ton = eingang.streams.audio[0]
            ton.thread_type = "AUTO"
            kontext = ton.codec_context
            if ton.duration and ton.time_base:
                dauer = float(ton.duration * ton.time_base)
            else:
                dauer = (eingang.duration or 0) / 1_000_000
            rate = kontext.sample_rate if kontext.sample_rate in _MP3_RATEN else 48000
            kanaele = len(kontext.layout.channels) if kontext.layout else 2

            ziel = ziel_reservieren(quelle.stem, ordner)
            # Erst in eine .part-Datei: bricht etwas ab, bleibt keine halbe MP3 liegen
            teil = ziel.with_name(ziel.name + ".part")
            with av.open(str(teil), "w", format="mp3") as ausgang:
                angaben = {str(k).lower(): v for k, v in eingang.metadata.items()}   # mkv schreibt ARTIST gross
                for schluessel in _METADATEN:
                    if angaben.get(schluessel):
                        ausgang.metadata[schluessel] = angaben[schluessel]
                mp3 = ausgang.add_stream("libmp3lame", rate=rate)
                mp3.bit_rate = int(bitrate) * 1000
                mp3.codec_context.layout = "mono" if kanaele == 1 else "stereo"   # Surround -> Stereo
                proben, gemeldet = 0, 0.0
                for frame in eingang.decode(ton):
                    if abbrechen is not None and abbrechen.is_set():
                        raise kern.Abgebrochen()
                    proben += frame.samples
                    position = proben / (frame.sample_rate or rate)
                    frame.pts = None            # der Encoder zählt selbst – verträgt so auch Lücken im Video
                    for paket in mp3.encode(frame):
                        ausgang.mux(paket)
                    if position - gemeldet >= 2.0:          # nicht bei jedem Häppchen melden
                        gemeldet = position
                        if dauer:
                            melden(anteil=min(position / dauer, 0.999), position=position, dauer=dauer)
                for paket in mp3.encode(None):
                    ausgang.mux(paket)
            if not proben:
                raise kern.KeineTonspur("Die Tonspur ist leer.")
            dauer = dauer or proben / rate
        os.replace(teil, ziel)
        melden(anteil=1.0, position=dauer, dauer=dauer)
        return Mp3Ergebnis(quelle, ziel, dauer, time.time() - start, ziel.stat().st_size)
    except BaseException as e:
        _loeschen(*(p for p in (teil, ziel) if p is not None))
        if isinstance(e, (kern.Abgebrochen, kern.KeineTonspur)) or not isinstance(e, Exception):
            raise
        if isinstance(e, av.error.FFmpegError) and not isinstance(e, OSError):
            raise kern.KeineTonspur(f"Die Tonspur lässt sich nicht dekodieren – Datei beschädigt? "
                                    f"({type(e).__name__})") from e
        raise
