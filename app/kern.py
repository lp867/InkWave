"""
kern.py – die Logik von InkWave, ohne Oberfläche.

Abgeleitet aus Datachanger\\Transcriber\\mp3_to_text.py (das Skript selbst bleibt unverändert).
Die Oberfläche (gui.py) ruft nur Funktionen von hier auf; alles Langsame meldet sich über
einen Rückruf `melden(art, **daten)` und lässt sich über ein threading.Event abbrechen.

Ordner:
  DATEN            neben der exe (vom Starter über INKWAVE_DATA gesetzt):
                   output\\, modelle\\, einstellungen.json
  LOKAL            %LOCALAPPDATA%\\InkWave: gpu\\ (GPU-Paket), umwandlung\\ (transformers+torch)
                   (bis 1.0.1 hiess der Ordner «Transcriber» – alte_daten_uebernehmen() zieht um)
  Hugging-Face-Cache  heruntergeladene Modelle (wie bisher)
"""

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Hugging Face: Symlink-Warnung unter Windows ist harmlos; Telemetrie aus; ohne Xet laden,
# damit die Datei im Cache wächst und der Fortschritt messbar ist.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

AUDIO_ENDUNGEN = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac",
                  ".wma", ".mp4", ".mkv", ".webm", ".mov"}

# Name, ungefähre Download-Grösse, Beschreibung
MODELLE = [
    ("tiny",           "75 MB",  "sehr schnell, ungenau"),
    ("base",           "145 MB", "schnell, eher ungenau"),
    ("small",          "485 MB", "guter Kompromiss ohne Grafikkarte"),
    ("medium",         "1.5 GB", "genau, auf CPU langsam"),
    ("large-v3",       "3.1 GB", "am genauesten, langsam"),
    ("large-v3-turbo", "1.6 GB", "fast so genau wie large-v3, viel schneller"),
]
EMPFEHLUNG = {"cuda": "large-v3-turbo", "cpu": "small"}

# Alle Sprachen, die Whisper kennt, mit deutschem Namen. Oben in der Auswahl: HAEUFIGE_SPRACHEN.
SPRACH_NAMEN = {
    "af": "Afrikaans", "am": "Amharisch", "ar": "Arabisch", "as": "Assamesisch", "az": "Aserbaidschanisch",
    "ba": "Baschkirisch", "be": "Belarussisch", "bg": "Bulgarisch", "bn": "Bengalisch", "bo": "Tibetisch",
    "br": "Bretonisch", "bs": "Bosnisch", "ca": "Katalanisch", "cs": "Tschechisch", "cy": "Walisisch",
    "da": "Dänisch", "de": "Deutsch", "el": "Griechisch", "en": "Englisch", "es": "Spanisch", "et": "Estnisch",
    "eu": "Baskisch", "fa": "Persisch", "fi": "Finnisch", "fo": "Färöisch", "fr": "Französisch",
    "gl": "Galicisch", "gu": "Gujarati", "ha": "Hausa", "haw": "Hawaiisch", "he": "Hebräisch", "hi": "Hindi",
    "hr": "Kroatisch", "ht": "Haitianisch", "hu": "Ungarisch", "hy": "Armenisch", "id": "Indonesisch",
    "is": "Isländisch", "it": "Italienisch", "ja": "Japanisch", "jw": "Javanisch", "ka": "Georgisch",
    "kk": "Kasachisch", "km": "Khmer", "kn": "Kannada", "ko": "Koreanisch", "la": "Latein",
    "lb": "Luxemburgisch", "ln": "Lingala", "lo": "Laotisch", "lt": "Litauisch", "lv": "Lettisch",
    "mg": "Malagasy", "mi": "Maori", "mk": "Mazedonisch", "ml": "Malayalam", "mn": "Mongolisch",
    "mr": "Marathi", "ms": "Malaiisch", "mt": "Maltesisch", "my": "Birmanisch", "ne": "Nepalesisch",
    "nl": "Niederländisch", "nn": "Norwegisch (Nynorsk)", "no": "Norwegisch", "oc": "Okzitanisch",
    "pa": "Panjabi", "pl": "Polnisch", "ps": "Paschtu", "pt": "Portugiesisch", "ro": "Rumänisch",
    "ru": "Russisch", "sa": "Sanskrit", "sd": "Sindhi", "si": "Singhalesisch", "sk": "Slowakisch",
    "sl": "Slowenisch", "sn": "Shona", "so": "Somali", "sq": "Albanisch", "sr": "Serbisch", "su": "Sundanesisch",
    "sv": "Schwedisch", "sw": "Suaheli", "ta": "Tamil", "te": "Telugu", "tg": "Tadschikisch", "th": "Thailändisch",
    "tk": "Turkmenisch", "tl": "Tagalog", "tr": "Türkisch", "tt": "Tatarisch", "uk": "Ukrainisch", "ur": "Urdu",
    "uz": "Usbekisch", "vi": "Vietnamesisch", "yi": "Jiddisch", "yo": "Yoruba", "yue": "Kantonesisch",
    "zh": "Chinesisch",
}
HAEUFIGE_SPRACHEN = ["de", "en", "fr", "it", "es", "pt", "nl"]


def sprach_auswahl():
    """[(code, anzeige)] für die Auswahl: Automatisch, die häufigen, dann alle übrigen alphabetisch.
    Ein Eintrag ("-", "") steht für einen Trennstrich."""
    codes = list(SPRACH_NAMEN)      # = die 100 Sprachen von Whisper (faster_whisper.tokenizer._LANGUAGE_CODES)
    name = lambda c: SPRACH_NAMEN.get(c, c)  # noqa: E731
    auswahl = [("", "Automatisch erkennen"), ("-", "")]
    auswahl += [(c, f"{name(c)} ({c})") for c in HAEUFIGE_SPRACHEN if c in codes]
    auswahl.append(("-", ""))
    uebrige = sorted((c for c in codes if c not in HAEUFIGE_SPRACHEN), key=lambda c: name(c).lower())
    auswahl += [(c, f"{name(c)} ({c})") for c in uebrige]
    return auswahl

APP_ORDNER = Path(__file__).resolve().parent
VERSION = os.environ.get("INKWAVE_VERSION") or ""          # vom Starter gesetzt; beim Entwickeln leer
DATEN = Path(os.environ.get("INKWAVE_DATA") or (APP_ORDNER.parent / "daten"))
STANDARD_OUTPUT = DATEN / "output"
OUTPUT_ORDNER = STANDARD_OUTPUT        # in den Einstellungen änderbar, siehe output_ordner_anwenden()
MODELL_ORDNER = DATEN / "modelle"
EINSTELLUNGEN = DATEN / "einstellungen.json"

LOKAL = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "InkWave"
ALT_LOKAL = LOKAL.with_name("Transcriber")       # Name bis Version 1.0.1
GPU_ORDNER = LOKAL / "gpu"
UMWANDLUNG_ORDNER = LOKAL / "umwandlung"


def alte_daten_uebernehmen():
    """Einmalig nach dem Umstieg von «Transcriber» auf InkWave: GPU- und Umwandlungs-Paket aus
    %LOCALAPPDATA%\\Transcriber übernehmen (verschieben, kein neuer Download) und die alten
    Programmversionen dort wegräumen – nur, wenn keine davon mehr läuft."""
    import shutil
    if not ALT_LOKAL.is_dir():
        return
    LOKAL.mkdir(parents=True, exist_ok=True)
    for name in ("gpu", "umwandlung"):
        alt, neu = ALT_LOKAL / name, LOKAL / name
        if alt.is_dir() and not neu.exists():
            try:
                alt.rename(neu)              # gleiches Laufwerk -> sofort
            except OSError:
                pass
    programm = ALT_LOKAL / "program"
    if programm.is_dir():
        weg = ALT_LOKAL / "program.weg"
        try:
            programm.rename(weg)             # klappt nur, wenn keine alte Version mehr läuft
            shutil.rmtree(weg, ignore_errors=True)
        except OSError:
            pass
    for datei in ("fehler.log", "download.log", "auftrag.log", "sac_test.log"):
        try:
            (ALT_LOKAL / datei).unlink()
        except OSError:
            pass
    try:
        ALT_LOKAL.rmdir()                    # nur wenn leer
    except OSError:
        pass


class Abgebrochen(Exception):
    """Der Benutzer hat abgebrochen."""


class KeineSprache(Exception):
    """In der Datei wurde keine Sprache gefunden (z. B. stille Aufnahme)."""


class KeineTonspur(Exception):
    """Die Datei ist keine lesbare Audio-/Videodatei oder hat keine Tonspur."""


# ---------------------------------------------------------------- Einstellungen, Dateien

def einstellungen_lesen() -> dict:
    """Die Einstellungen als dict. Fehlt die Datei oder ist sie kaputt (von Hand bearbeitet): leer."""
    try:
        daten = json.loads(EINSTELLUNGEN.read_text(encoding="utf-8-sig"))   # -sig: verträgt den Editor-BOM
    except (OSError, ValueError):
        return {}
    return daten if isinstance(daten, dict) else {}


def einstellungen_entfernen(*schluessel):
    """Einzelne Einstellungen löschen (= zurück auf den Standard)."""
    daten = einstellungen_lesen()
    for s in schluessel:
        daten.pop(s, None)
    try:
        DATEN.mkdir(parents=True, exist_ok=True)
        tmp = EINSTELLUNGEN.with_suffix(".tmp")
        tmp.write_text(json.dumps(daten, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, EINSTELLUNGEN)
    except OSError:
        pass


def einstellung(schluessel: str, standard):
    """Eine Einstellung – nur wenn sie vom selben Typ ist wie der Standard (sonst der Standard)."""
    wert = einstellungen_lesen().get(schluessel, standard)
    return wert if type(wert) is type(standard) else standard


def output_ordner_anwenden() -> Path:
    """Setzt OUTPUT_ORDNER aus der Einstellung «output_ordner» (leer = Ordner «output» neben der exe)."""
    global OUTPUT_ORDNER
    wunsch = einstellung("output_ordner", "")
    try:
        OUTPUT_ORDNER = Path(wunsch) if wunsch and Path(wunsch).is_absolute() else STANDARD_OUTPUT
    except (OSError, ValueError):
        OUTPUT_ORDNER = STANDARD_OUTPUT
    return OUTPUT_ORDNER


def cpu_threads_einstellung() -> int:
    """Wunsch aus einstellungen.json («cpu_threads») oder 0 = selbst bestimmen."""
    try:
        return int(einstellungen_lesen().get("cpu_threads") or 0)
    except (TypeError, ValueError):
        return 0


# Hugging-Face-Name: «Besitzer/Modell» (oder nur «Modell»), nur Buchstaben, Ziffern, . _ -
_REPO_MUSTER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*(/[A-Za-z0-9_][A-Za-z0-9._\-]*)?")


def repo_gueltig(repo) -> bool:
    """Darf aus diesem Namen ein Ordnername werden? Aus dem Namen entstehen Pfade im Ordner «modelle»
    und im Hugging-Face-Cache, die auch gelöscht werden – ein Name mit «..» oder Pfadzeichen (z. B. aus
    einem manipulierten quelle.json eines kopierten Modells) dürfte sonst auf fremde Ordner zeigen."""
    return (isinstance(repo, str) and len(repo) <= 200 and bool(_REPO_MUSTER.fullmatch(repo))
            and ".." not in repo and not repo.endswith("."))


def einstellungen_speichern(**werte):
    daten = einstellungen_lesen()
    daten.update(werte)
    try:
        DATEN.mkdir(parents=True, exist_ok=True)
        tmp = EINSTELLUNGEN.with_suffix(".tmp")
        tmp.write_text(json.dumps(daten, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, EINSTELLUNGEN)
    except OSError:
        pass


output_ordner_anwenden()


def freier_stamm(name: str, endungen) -> Path:
    """output\\name – falls name.txt (oder name.srt) schon existiert: name_2, name_3, ...
    Gibt den Pfad ohne Endung zurück; alle gewünschten Endungen sind dort frei."""
    stamm = OUTPUT_ORDNER / name
    n = 2
    while any(stamm.with_name(stamm.name + e).exists() for e in endungen):
        stamm = OUTPUT_ORDNER / f"{name}_{n}"
        n += 1
    return stamm


def zeit(sek: float) -> str:
    """Sekunden -> hh:mm:ss"""
    sek = int(sek)
    return f"{sek // 3600:02d}:{sek % 3600 // 60:02d}:{sek % 60:02d}"


def srt_zeit(sek: float) -> str:
    """Sekunden -> hh:mm:ss,mmm (SRT-Format)"""
    ms = int(round(sek * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def dateien_sammeln(eingaben):
    """Dateien und Ordner (rekursiv) -> (Audiodateien, nicht gefundene Eingaben).
    Ausdrücklich gewählte Dateien werden immer versucht, aus Ordnern nur bekannte Endungen."""
    dateien, fehlend = [], []
    for e in eingaben:
        p = Path(e)
        try:
            if p.is_dir():
                dateien += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_ENDUNGEN)
            elif p.is_file():
                dateien.append(p)
            else:
                fehlend.append(str(e))
        except OSError:          # z. B. Netzlaufwerk weg, kein Zugriff
            fehlend.append(str(e))
    return list(dict.fromkeys(dateien)), fehlend


def ausgabe_pruefen():
    """Lässt sich in den Output-Ordner schreiben? Gibt None zurück oder eine Fehlermeldung.
    Wird vor dem Start geprüft, damit es nicht erst nach einer langen Transkription auffällt
    (exe in einem schreibgeschützten Ordner, «Überwachter Ordnerzugriff» von Windows, Stick voll)."""
    probe = OUTPUT_ORDNER / f".schreibtest-{os.getpid()}"
    try:
        OUTPUT_ORDNER.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"test")
        probe.unlink()
    except OSError as e:
        return f"{OUTPUT_ORDNER}\n({e.strerror or type(e).__name__})"
    return None


def av_version_pruefen():
    """faster-whisper 1.2.x kann mit PyAV ab Version 19 keine Dateien mehr öffnen.
    Gibt eine Fehlermeldung zurück oder None."""
    try:
        import av
    except ImportError:
        return "PyAV (av) fehlt – die App ist unvollständig."
    if int(av.__version__.split(".")[0]) >= 19:
        return f"PyAV {av.__version__} ist zu neu für faster-whisper (nötig: av<19)."
    return None


# ---------------------------------------------------------------- Windows: Kerne, wach bleiben, Priorität

def physische_kerne() -> int:
    """Anzahl echter Prozessorkerne (ohne Hyper-Threading)."""
    try:
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        laenge = wintypes.DWORD(0)
        k32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(laenge))  # 0 = RelationProcessorCore
        puffer = ctypes.create_string_buffer(laenge.value)
        if not k32.GetLogicalProcessorInformationEx(0, puffer, ctypes.byref(laenge)):
            raise OSError
        anzahl, pos, roh = 0, 0, puffer.raw
        while pos < laenge.value:
            groesse = int.from_bytes(roh[pos + 4:pos + 8], "little")
            if groesse <= 0:
                break
            anzahl += 1
            pos += groesse
        if anzahl:
            return anzahl
    except Exception:
        pass
    return max(1, (os.cpu_count() or 2) // 2)


def cpu_threads(wunsch: int = 0) -> int:
    """Rechen-Threads auf der CPU: echte Kerne, höchstens 8.
    Gemessen (Core Ultra 7 255HX): 8 Threads sind schneller als 20 (viele Effizienzkerne), und der
    Batch-Modus stürzt ab etwa 16 Threads mit Stack-Überlauf ab – deshalb fest begrenzt."""
    return max(1, min(wunsch or physische_kerne(), 8))


class WachHalten:
    """Verhindert Standby, solange transkribiert wird (Bildschirm darf aus)."""

    def __enter__(self):
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception:
            pass
        return self

    def __exit__(self, *_):
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        except Exception:
            pass


def schonend_rechnen(an: bool):
    """Prozess-Priorität «niedriger als normal», damit der Laptop bedienbar bleibt."""
    try:
        from ctypes import wintypes
        # Eigene kernel32-Instanz mit richtigen Typen: ohne sie wird das Handle auf 32 Bit gekürzt
        # und Windows lehnt den Aufruf ab (Fehler 6) – die Priorität änderte sich dann nie.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000 if an else 0x00000020)
    except Exception:
        pass


def _ohne_fenster() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW


# ---------------------------------------------------------------- GPU

def _gpu_bin_ordner():
    ordner = []
    basis = [GPU_ORDNER / "nvidia"]
    try:
        import nvidia  # beim Entwickeln: NVIDIA-Pakete im normalen Python
        basis += [Path(p) for p in nvidia.__path__]
    except ImportError:
        pass
    for b in basis:
        ordner += [p for p in b.glob("*/bin") if p.is_dir()]
    return ordner


_eingebunden = set()
_vorgeladen = {}          # Dateiname -> geladene DLL (Verweis halten, damit sie geladen bleibt)
# in dieser Reihenfolge vorladen: cuBLAS braucht cuBLASLt, cuDNN braucht cuBLAS
_VORLADEN = ("cublaslt64_12", "cublas64_12", "cudnn64_9")


def nvidia_dlls_einbinden():
    """CUDA-DLLs aus dem GPU-Paket auffindbar machen (kein separates CUDA nötig).
    Zusätzlich werden cuBLAS und cuDNN mit vollem Pfad vorgeladen: ctranslate2 lädt sie später nur
    über den Namen – ist die DLL schon im Prozess, nimmt Windows diese, egal wie die Suchpfade stehen."""
    if os.name != "nt":
        return
    for bin_ordner in _gpu_bin_ordner():
        if str(bin_ordner) in _eingebunden:
            continue
        try:
            os.add_dll_directory(str(bin_ordner))
        except OSError:
            continue              # Ordner gerade verschwunden -> fällt beim Testlauf auf
        os.environ["PATH"] = str(bin_ordner) + os.pathsep + os.environ.get("PATH", "")
        _eingebunden.add(str(bin_ordner))
    dlls = {p.name.lower(): p for o in _gpu_bin_ordner() for p in o.glob("*.dll")}
    for anfang in _VORLADEN:
        for name, pfad in sorted(dlls.items()):
            if name.startswith(anfang) and name not in _vorgeladen:
                try:
                    _vorgeladen[name] = ctypes.WinDLL(str(pfad))
                except OSError:
                    pass          # fällt beim Testlauf auf -> Ausweichen auf CPU mit Meldung


def gpu_paket_vorhanden() -> bool:
    namen = {p.name.lower() for o in _gpu_bin_ordner() for p in o.glob("*.dll")}
    return any(n.startswith("cublas64_12") for n in namen) and any(n.startswith("cudnn64_9") for n in namen)


def gpu_vorhanden() -> bool:
    """Ist eine NVIDIA-Grafikkarte für CTranslate2 sichtbar? (braucht das GPU-Paket noch nicht)"""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


# GPU-Paket: nur die DLLs (bin-Ordner) aus diesen PyPI-Paketen; Versionen = bei Elias getestet
GPU_PAKETE = [("nvidia-cublas-cu12", "12.9.2.10"), ("nvidia-cudnn-cu12", "9.27.0.42"),
              ("nvidia-cuda-nvrtc-cu12", "12.9.86")]


def vorhandene_nvidia_quelle():
    """nvidia-Ordner einer vorhandenen Python-Installation mit cuBLAS + cuDNN (zum Übernehmen statt
    Herunterladen) oder None."""
    kandidaten = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and (Path(d) / "python.exe").is_file():
            kandidaten.append(Path(d) / "Lib" / "site-packages" / "nvidia")
    for basis in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python",
                  Path(os.environ.get("ProgramFiles", r"C:\Program Files"))):
        try:
            kandidaten += sorted(basis.glob("Python3*/Lib/site-packages/nvidia"), reverse=True)
        except OSError:
            pass
    for k in kandidaten:
        try:
            if GPU_ORDNER in k.parents:
                continue
            if (any((k / "cublas" / "bin").glob("cublas64_12*.dll"))
                    and any((k / "cudnn" / "bin").glob("cudnn64_9*.dll"))):
                return k
        except OSError:
            continue
    return None


def gpu_paket_testen():
    """Lädt cuBLAS und cuDNN probeweise. Gibt None zurück, wenn alles da ist, sonst den Fehler."""
    nvidia_dlls_einbinden()
    gefunden = {}
    for o in _gpu_bin_ordner():
        for p in o.glob("*.dll"):
            gefunden.setdefault(p.name.lower(), p)
    for muster in ("cublas64_12", "cudnn64_9"):
        pfad = next((p for n, p in gefunden.items() if n.startswith(muster)), None)
        if pfad is None:
            return f"{muster}.dll fehlt"
        try:
            ctypes.WinDLL(str(pfad))
        except OSError as e:
            return f"{pfad.name}: {e}"
    return None


def _kopieren_mit_fortschritt(dateien, ziel_von, melden):
    gesamt = sum(q.stat().st_size for q, _ in dateien) or 1
    fertig = 0
    for quelle, ziel in dateien:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        with open(quelle, "rb") as ein, open(ziel, "wb") as aus:
            while True:
                block = ein.read(4 << 20)
                if not block:
                    break
                aus.write(block)
                fertig += len(block)
                melden(anteil=fertig / gesamt)


def gpu_paket_einrichten(quelle=None, melden=lambda **k: None):
    """Richtet das GPU-Paket in %LOCALAPPDATA%\\InkWave\\gpu ein: aus einer vorhandenen
    Python-Installation kopiert (quelle) oder die Pakete von PyPI geladen (nur bin-Ordner)."""
    import hashlib
    import shutil
    import urllib.request
    import zipfile

    ziel = GPU_ORDNER / "nvidia"
    teil = GPU_ORDNER / "nvidia.part"
    shutil.rmtree(teil, ignore_errors=True)
    teil.mkdir(parents=True)

    if quelle:
        quelle = Path(quelle)
        melden(schritt=f"Kopiere GPU-Dateien aus {quelle} …")
        dateien = [(p, teil / p.relative_to(quelle)) for p in quelle.glob("*/bin/*") if p.is_file()]
        _kopieren_mit_fortschritt(dateien, quelle, melden)
    else:
        laden = GPU_ORDNER / "download"
        shutil.rmtree(laden, ignore_errors=True)       # Reste eines abgebrochenen Versuchs
        laden.mkdir(parents=True, exist_ok=True)
        plan = []
        for paket, version in GPU_PAKETE:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{paket}/{version}/json", timeout=30) as r:
                info = json.load(r)
            datei = next((u for u in info["urls"] if u["filename"].endswith("win_amd64.whl")), None)
            # nur direkt vom PyPI-Dateiserver laden, Prüfsumme (SHA-256) wird unten verglichen
            if datei is None or not str(datei.get("url", "")).startswith("https://files.pythonhosted.org/"):
                raise RuntimeError(f"Kein passendes Windows-Paket für {paket} {version} auf PyPI gefunden.")
            plan.append(datei)
        gesamt = sum(d["size"] for d in plan) or 1
        geladen = 0
        for d in plan:
            melden(schritt=f"Lade {d['filename']} ({groesse_text(d['size'])}) …")
            whl = laden / Path(d["filename"]).name
            sha = hashlib.sha256()
            with urllib.request.urlopen(d["url"], timeout=60) as r, open(whl, "wb") as aus:
                while True:
                    block = r.read(1 << 20)
                    if not block:
                        break
                    aus.write(block)
                    sha.update(block)
                    geladen += len(block)
                    melden(anteil=geladen / gesamt * 0.95)
            if sha.hexdigest() != d["digests"]["sha256"]:
                raise RuntimeError(f"Prüfsumme von {d['filename']} stimmt nicht – Download beschädigt.")
            melden(schritt=f"Entpacke {d['filename']} …")
            with zipfile.ZipFile(whl) as z:
                for name in z.namelist():
                    teile = name.split("/")
                    if len(teile) == 4 and teile[0] == "nvidia" and teile[2] == "bin" and teile[3]:
                        z.extract(name, laden / "x")
            for p in (laden / "x" / "nvidia").glob("*/bin/*"):
                z_pfad = teil / p.relative_to(laden / "x" / "nvidia")
                z_pfad.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), z_pfad)
            shutil.rmtree(laden / "x", ignore_errors=True)
            whl.unlink()
        shutil.rmtree(laden, ignore_errors=True)

    melden(schritt="Prüfe GPU-Dateien …", anteil=1.0)
    shutil.rmtree(ziel, ignore_errors=True)
    teil.rename(ziel)
    fehler = gpu_paket_testen()
    if fehler:
        raise RuntimeError(f"GPU-Paket eingerichtet, aber nicht ladbar: {fehler}")


# Umwandlungs-Paket (Transformers-Modelle -> CTranslate2); getestete Versionen
UMWANDLUNG_PAKETE = ["torch==2.15.0", "transformers==5.24.1"]


def umwandlung_vorhanden() -> bool:
    return (UMWANDLUNG_ORDNER / "transformers").is_dir() and (UMWANDLUNG_ORDNER / "torch").is_dir()


def umwandlung_einrichten(melden=lambda **k: None):
    """Installiert transformers + torch (CPU) mit pip nach %LOCALAPPDATA%\\InkWave\\umwandlung."""
    import shutil
    teil = UMWANDLUNG_ORDNER.with_name(UMWANDLUNG_ORDNER.name + ".part")
    shutil.rmtree(teil, ignore_errors=True)
    python = Path(sys.executable).with_name("python.exe")
    if not python.exists():
        python = Path(sys.executable)
    befehl = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
              "--only-binary=:all:", "--no-warn-script-location", "--target", str(teil), *UMWANDLUNG_PAKETE]
    melden(schritt="Lade transformers und torch (CPU) …")
    proc = subprocess.Popen(befehl, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", **_ohne_fenster())
    letzte = []
    for zeile in proc.stdout:
        zeile = zeile.strip()
        if not zeile:
            continue
        letzte = (letzte + [zeile])[-15:]
        if zeile.startswith(("Downloading", "Collecting", "Installing", "Successfully")):
            melden(schritt=zeile[:150])
    if proc.wait() != 0:
        raise RuntimeError("pip ist fehlgeschlagen:\n" + "\n".join(letzte[-6:]))
    shutil.rmtree(UMWANDLUNG_ORDNER, ignore_errors=True)
    teil.rename(UMWANDLUNG_ORDNER)


PREPROCESSOR_STANDARD = {
    "chunk_length": 30, "feature_extractor_type": "WhisperFeatureExtractor", "hop_length": 160,
    "n_fft": 400, "n_samples": 480000, "nb_max_frames": 3000, "padding_side": "right",
    "padding_value": 0.0, "processor_class": "WhisperProcessor", "return_attention_mask": False,
    "sampling_rate": 16000,
}


def umwandeln(quelle, repo: str = "", melden=lambda **k: None) -> Path:
    """Wandelt ein Whisper-Modell im Transformers-Format ins CTranslate2-Format um
    (-> Ordner «modelle»). Braucht das Umwandlungs-Paket; läuft in einem eigenen Prozess."""
    import datetime
    import shutil
    quelle = Path(quelle)
    cfg = json.loads((quelle / "config.json").read_text(encoding="utf-8"))
    if cfg.get("model_type") != "whisper":
        raise RuntimeError(f"Kein Whisper-Modell (model_type = {cfg.get('model_type')!r}).")
    n_mels = int(cfg.get("num_mel_bins") or 80)

    ziel = umwandel_ziel(quelle, repo)
    teil = ziel.with_name(ziel.name + ".part")
    shutil.rmtree(teil, ignore_errors=True)
    MODELL_ORDNER.mkdir(parents=True, exist_ok=True)

    melden(schritt="Lade das Modell (das dauert einen Moment) …", anteil=0.05)
    if str(UMWANDLUNG_ORDNER) not in sys.path:
        sys.path.insert(0, str(UMWANDLUNG_ORDNER))
    from ctranslate2.converters import TransformersConverter
    kopieren = [f for f in ("tokenizer.json",) if (quelle / f).is_file()]
    melden(schritt="Wandle ins CTranslate2-Format um (float16) …", anteil=0.2)
    TransformersConverter(str(quelle), copy_files=kopieren, load_as_float16=True).convert(
        str(teil), quantization="float16", force=True)

    melden(schritt="Schreibe preprocessor_config.json und Sprach-Einstellungen …", anteil=0.9)
    # preprocessor_config.json: feature_size muss num_mel_bins sein (large-v3 = 128), sonst falsche Texte
    pre = {}
    if (quelle / "preprocessor_config.json").is_file():
        pre = json.loads((quelle / "preprocessor_config.json").read_text(encoding="utf-8"))
    elif (quelle / "processor_config.json").is_file():
        pc = json.loads((quelle / "processor_config.json").read_text(encoding="utf-8"))
        pre = dict(pc.get("feature_extractor") or {})
    pre = {**PREPROCESSOR_STANDARD, **pre, "feature_size": n_mels}
    (teil / "preprocessor_config.json").write_text(json.dumps(pre, indent=2), encoding="utf-8")

    # lang_ids, suppress_ids, suppress_ids_begin, alignment_heads direkt aus generation_config.json
    # (neue transformers-Versionen reichen sie nicht mehr weiter -> sonst stürzt die Spracherkennung ab)
    ct = json.loads((teil / "config.json").read_text(encoding="utf-8"))
    gen_pfad = quelle / "generation_config.json"
    if gen_pfad.is_file():
        gen = json.loads(gen_pfad.read_text(encoding="utf-8"))
        if gen.get("lang_to_id"):
            ct["lang_ids"] = sorted(gen["lang_to_id"].values())
        if gen.get("suppress_tokens") is not None:
            ct["suppress_ids"] = gen["suppress_tokens"]
        if gen.get("begin_suppress_tokens") is not None:
            ct["suppress_ids_begin"] = gen["begin_suppress_tokens"]
        if gen.get("alignment_heads"):
            ct["alignment_heads"] = gen["alignment_heads"]
    if not ct.get("lang_ids"):
        raise RuntimeError("Keine Sprach-IDs gefunden (lang_ids) – Spracherkennung würde abstürzen.")
    (teil / "config.json").write_text(json.dumps(ct, indent=2), encoding="utf-8")
    (teil / "quelle.json").write_text(json.dumps({
        "repo": repo, "quelle": str(quelle), "mel": n_mels,
        "umgewandelt": datetime.datetime.now().isoformat(timespec="seconds")}, indent=2), encoding="utf-8")

    shutil.rmtree(ziel, ignore_errors=True)
    teil.rename(ziel)
    melden(schritt="Fertig umgewandelt.", anteil=1.0)
    return ziel


def gpu_name() -> str:
    try:
        aus = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5, **_ohne_fenster())
        name = aus.stdout.strip().splitlines()[0].strip()
        if name:
            return name
    except Exception:
        pass
    return "NVIDIA-Grafikkarte"


# ---------------------------------------------------------------- Modelle finden

@dataclass
class Modell:
    name: str                 # Schlüssel und Anzeige, z. B. "small" oder "Flix-AI/flix-swissgerman-full"
    quelle: str               # "standard" | "eigen" | "cache"
    zustand: str              # "bereit" | "fehlt" | "umwandeln"
    pfad: str = ""            # ladbarer CTranslate2-Ordner (zustand "bereit")
    roh_pfad: str = ""        # Transformers-Ordner (zustand "umwandeln")
    repo: str = ""            # Hugging-Face-Name, falls bekannt
    groesse: str = ""
    beschreibung: str = ""

    @property
    def bereit(self) -> bool:
        return self.zustand == "bereit"


def ist_ct2(p: Path) -> bool:
    return (p / "model.bin").is_file() and (p / "config.json").is_file()


def ist_transformers(p: Path) -> bool:
    return any((p / f).is_file() for f in ("model.safetensors", "model.safetensors.index.json",
                                            "pytorch_model.bin", "pytorch_model.bin.index.json"))


def _standard_repos() -> dict:
    from faster_whisper.utils import _MODELS
    return dict(_MODELS)


def repo_von(name: str) -> str:
    return name if "/" in name else _standard_repos().get(name, "")


def hf_cache() -> Path:
    from huggingface_hub import constants
    return Path(constants.HF_HUB_CACHE)


def _cache_snapshot(repo: str):
    """Neuester Snapshot-Ordner eines Repos im HF-Cache (ohne Internet) oder None."""
    if not repo_gueltig(repo):
        return None
    basis = cache_ordner(repo)
    ref = basis / "refs" / "main"
    try:
        if ref.is_file():
            snap = basis / "snapshots" / ref.read_text().strip()
            if snap.is_dir():
                return snap
        snaps = sorted((basis / "snapshots").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        return snaps[0] if snaps else None
    except OSError:
        return None


def _repo_pruefen(repo: str) -> str:
    if not repo_gueltig(repo):
        raise ValueError(f"Ungültiger Modellname: {str(repo)[:80]!r}")
    return repo


def umgewandelt_ordner(repo: str) -> Path:
    return MODELL_ORDNER / _repo_pruefen(repo).replace("/", "__")


def cache_ordner(repo: str) -> Path:
    return hf_cache() / ("models--" + _repo_pruefen(repo).replace("/", "--"))


def umwandel_ziel(quelle, repo: str = "") -> Path:
    """Wohin ein Modell umgewandelt wird: «modelle\\Besitzer__Modell» bzw. «modelle\\<Ordner>-ct2»."""
    return umgewandelt_ordner(repo) if repo else MODELL_ORDNER / f"{Path(quelle).name}-ct2"


def _quelle_repo(ordner: Path) -> str:
    """Hugging-Face-Name aus quelle.json eines Modellordners – "" wenn er fehlt oder ungültig ist."""
    try:
        daten = json.loads((ordner / "quelle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    repo = daten.get("repo", "") if isinstance(daten, dict) else ""
    return repo if repo_gueltig(repo) else ""


def _modell_unterordner() -> list:
    try:
        return sorted(p for p in MODELL_ORDNER.iterdir() if p.is_dir())
    except OSError:
        return []


def _ordner_bytes(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def ueberfluessige_originale() -> list:
    """[(repo, bytes)] – Originale im HF-Cache, deren umgewandelte Fassung schon in «modelle» liegt."""
    gefunden = []
    for ordner in _modell_unterordner():
        if not ist_ct2(ordner):
            continue
        repo = _quelle_repo(ordner)
        # nur wenn die umgewandelte Fassung wirklich dort liegt, wo original_loeschen() sie erwartet
        if repo and ist_ct2(umgewandelt_ordner(repo)) and cache_ordner(repo).is_dir():
            if repo not in (r for r, _ in gefunden):
                gefunden.append((repo, _ordner_bytes(cache_ordner(repo))))
    return gefunden


def original_loeschen(repo: str) -> int:
    """Löscht das Original eines umgewandelten Modells aus dem HF-Cache. Nur wenn die umgewandelte
    Fassung vollständig da ist. Gibt die freigegebenen Bytes zurück."""
    import shutil
    if not ist_ct2(umgewandelt_ordner(repo)):
        raise RuntimeError(f"Keine umgewandelte Fassung von «{repo}» – Original wird nicht gelöscht.")
    ordner = cache_ordner(repo)
    frei = _ordner_bytes(ordner)
    shutil.rmtree(ordner)
    shutil.rmtree(hf_cache() / ".locks" / ordner.name, ignore_errors=True)
    return frei


def modell_liste() -> list:
    """Alle Modelle: eingebaute, eigene (Ordner «modelle») und weitere aus dem HF-Cache."""
    liste, gesehen = [], set()

    standard = _standard_repos()
    for name, groesse, info in MODELLE:
        repo = standard.get(name, "")
        snap = _cache_snapshot(repo) if repo else None
        bereit = snap is not None and ist_ct2(snap)
        liste.append(Modell(name, "standard", "bereit" if bereit else "fehlt", pfad=str(snap) if bereit else "",
                            repo=repo, groesse=groesse, beschreibung=info))
        gesehen.add(repo.lower())

    # eigene Modelle (auch umgewandelte Hugging-Face-Modelle, Name aus quelle.json)
    namen = {m.name for m in liste}
    for ordner in _modell_unterordner():
        if not (ist_ct2(ordner) or ist_transformers(ordner)):
            continue
        repo = _quelle_repo(ordner)
        name = repo or ordner.name
        if name in namen:              # heisst wie ein anderes Modell (z. B. Ordner «small») -> unterscheidbar
            name = f"{name} (Ordner {ordner.name})"
        namen.add(name)
        if ist_ct2(ordner):
            try:
                g = groesse_text((ordner / "model.bin").stat().st_size)
            except OSError:
                g = ""
            liste.append(Modell(name, "eigen", "bereit", pfad=str(ordner), repo=repo, groesse=g,
                                beschreibung="eigenes Modell" if not repo else "umgewandelt"))
        else:
            liste.append(Modell(name, "eigen", "umwandeln", roh_pfad=str(ordner), repo=repo,
                                beschreibung="muss noch umgewandelt werden"))
        if repo:
            gesehen.add(repo.lower())

    # Hugging-Face-Cache und selbst hinzugefügte Namen
    repos = {}
    try:
        for d in hf_cache().glob("models--*"):
            repo = d.name.split("--", 1)[1].replace("--", "/", 1)
            if repo_gueltig(repo):
                repos[repo] = None
    except OSError:
        pass
    gemerkt, groessen = _hf_gemerkt()
    for repo in gemerkt:
        repos.setdefault(repo, None)
    for repo in sorted(repos, key=str.lower):
        if repo.lower() in gesehen:
            continue
        snap = _cache_snapshot(repo)
        g = groesse_text(groessen[repo]) if groessen.get(repo) else ""
        if snap is not None and ist_ct2(snap):
            liste.append(Modell(repo, "cache", "bereit", pfad=str(snap), repo=repo, groesse=g,
                                beschreibung="aus dem Hugging-Face-Cache"))
        elif snap is not None and ist_transformers(snap):
            liste.append(Modell(repo, "cache", "umwandeln", roh_pfad=str(snap), repo=repo, groesse=g,
                                beschreibung="Transformers-Format, muss umgewandelt werden"))
        else:
            liste.append(Modell(repo, "cache", "fehlt", repo=repo, groesse=g,
                                beschreibung="noch nicht heruntergeladen"))
    return liste


def _hf_gemerkt():
    """(Namen, Grössen) der selbst hinzugefügten Hugging-Face-Modelle aus einstellungen.json –
    nur gültige Einträge, damit eine von Hand geänderte Datei die App nicht stolpern lässt."""
    e = einstellungen_lesen()
    liste, groessen = e.get("hf_modelle"), e.get("hf_groessen")
    liste = [r for r in liste if repo_gueltig(r)] if isinstance(liste, list) else []
    groessen = ({k: v for k, v in groessen.items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
                if isinstance(groessen, dict) else {})
    return liste, groessen


def modell_finden(name: str):
    for m in modell_liste():
        if m.name == name:
            return m
    return None


# ---------------------------------------------------------------- Herunterladen

CT2_DATEIEN = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
TRANSFORMERS_DATEIEN = ["*.json", "*.safetensors", "*.txt", "*.tiktoken"]


def download_plan(repo: str):
    """(Dateimuster, Gesamtgrösse in Bytes) – fragt Hugging Face nach den Dateien des Repos."""
    import fnmatch
    from huggingface_hub import HfApi
    info = HfApi().model_info(repo, files_metadata=True)
    dateien = {s.rfilename: (s.size or 0) for s in info.siblings}
    if "model.bin" in dateien:
        muster = CT2_DATEIEN
    else:
        muster = list(TRANSFORMERS_DATEIEN)
        if not any(f.endswith(".safetensors") for f in dateien):
            muster.append("pytorch_model*.bin")
    gesamt = sum(g for f, g in dateien.items() if "/" not in f and any(fnmatch.fnmatch(f, m) for m in muster))
    return muster, gesamt


def cache_bytes(repo: str) -> int:
    """Wie viel vom Repo schon im Cache liegt (inkl. halb geladener Dateien)."""
    try:
        blobs = cache_ordner(repo) / "blobs"
        return sum(p.stat().st_size for p in blobs.iterdir() if p.is_file())
    except (OSError, ValueError):
        return 0


def herunterladen(repo: str, muster=None) -> str:
    """Lädt ein Modell in den HF-Cache (blockiert). Gibt den Snapshot-Ordner zurück."""
    import huggingface_hub
    if muster is None:
        muster, _ = download_plan(repo)
    return huggingface_hub.snapshot_download(repo, allow_patterns=muster)


def prozess_starten(argumente, protokoll: Path) -> subprocess.Popen:
    """Startet kern.py mit argumente in einem eigenen Prozess ohne Fenster (lässt sich sauber
    abbrechen). Der Prozess schreibt Zeilen ins Protokoll:
      GESAMT <bytes>  SCHRITT <text>  ANTEIL <0..1>  FERTIG <ergebnis>  FEHLER <text>"""
    python = Path(sys.executable)
    konsole = python.with_name("python.exe")
    if konsole.exists():
        python = konsole
    protokoll.parent.mkdir(parents=True, exist_ok=True)
    umgebung = dict(os.environ, HF_HUB_DISABLE_PROGRESS_BARS="1", PYTHONUTF8="1")
    # stderr ins selbe Protokoll: stürzt der Auftrag unerwartet ab, steht dort wenigstens der Grund
    with open(protokoll, "w", encoding="utf-8") as aus:
        return subprocess.Popen([str(python), str(Path(__file__).resolve()), *argumente],
                                stdout=aus, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=umgebung, **_ohne_fenster())


def prozess_beenden(proc: subprocess.Popen, warten: float = 5.0):
    """Beendet einen Auftrag samt seinen Kindprozessen. terminate() allein trifft nur den Auftrag
    selbst – das pip darunter (Umwandlungs-Paket) lüde sonst im Hintergrund weiter."""
    if proc.poll() is None and os.name == "nt":
        try:
            taskkill = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "taskkill.exe"
            subprocess.run([str(taskkill), "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=10, **_ohne_fenster())
        except Exception:
            pass
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(warten)
    except Exception:
        pass


def auftrag_reste_loeschen(art: str, repo: str = "", quelle: str = ""):
    """Nach einem abgebrochenen Auftrag die halbfertigen Ordner wegräumen (bis zu mehreren GB).
    Angefangene Modell-Downloads bleiben liegen – die setzt Hugging Face beim nächsten Mal fort."""
    import shutil
    reste = []
    try:
        if art == "gpu":
            reste = [GPU_ORDNER / "nvidia.part", GPU_ORDNER / "download"]
        elif art == "umwandlung":
            reste = [UMWANDLUNG_ORDNER.with_name(UMWANDLUNG_ORDNER.name + ".part")]
        elif art == "umwandeln" and quelle:
            ziel = umwandel_ziel(quelle, repo)
            reste = [ziel.with_name(ziel.name + ".part")]
    except ValueError:
        return
    for r in reste:
        shutil.rmtree(r, ignore_errors=True)


def download_starten(repo: str, protokoll: Path) -> subprocess.Popen:
    """Download in eigenem Prozess; angefangene Dateien werden beim nächsten Mal fortgesetzt.
    Fortschritt: cache_bytes(repo) im Vergleich zu GESAMT."""
    return prozess_starten(["--download", repo], protokoll)


def groesse_text(b: float) -> str:
    if b >= 1e9:
        return f"{b / 1e9:.1f} GB"
    if b >= 1e6:
        return f"{b / 1e6:.0f} MB"
    return f"{b / 1e3:.0f} kB"


def fehler_text(e: Exception) -> str:
    """Verständliche Meldung für Fehler beim Herunterladen / Nachschlagen auf Hugging Face."""
    name = type(e).__name__
    if name == "RepositoryNotFoundError":
        return "Modell nicht gefunden – Name falsch geschrieben oder privat?"
    if name == "GatedRepoError":
        return "Dieses Modell muss zuerst auf huggingface.co freigegeben werden (Anmeldung nötig)."
    if name in ("LocalEntryNotFoundError", "ConnectionError", "ConnectError", "OfflineModeIsEnabled",
                "Timeout", "ReadTimeout", "ConnectTimeout") or "connect" in str(e).lower():
        return "Keine Verbindung zu Hugging Face – besteht eine Internetverbindung?"
    return f"{name}: {str(e)[:200]}"


def hf_modell_pruefen(repo: str) -> dict:
    """Schlägt ein Hugging-Face-Modell nach (Internet). Ergebnis: format ("ct2" / "transformers"),
    gesamt (Bytes), whisper (bool). Wirft bei Fehlern."""
    from huggingface_hub import HfApi
    info = HfApi().model_info(repo, files_metadata=True)
    namen = {s.rfilename for s in info.siblings}
    _, gesamt = download_plan(repo)
    if "model.bin" in namen:
        fmt = "ct2"
    elif any(n.endswith(".safetensors") or n.startswith("pytorch_model") for n in namen):
        fmt = "transformers"
    else:
        fmt = "unbekannt"
    typ = str((info.config or {}).get("model_type") or "").lower()
    whisper = fmt == "ct2" or typ == "whisper" or "whisper" in repo.lower()
    # info.id = Schreibweise von Hugging Face (Gross/Klein); nur übernehmen, wenn es ein sauberer Name ist
    return {"repo": info.id if repo_gueltig(info.id) else repo, "format": fmt, "gesamt": gesamt,
            "whisper": whisper}


def hf_modell_merken(repo: str, gesamt: int = 0):
    _repo_pruefen(repo)
    liste, groessen = _hf_gemerkt()
    if gesamt:
        groessen[repo] = gesamt
    einstellungen_speichern(hf_modelle=liste + ([repo] if repo not in liste else []), hf_groessen=groessen)


# ---------------------------------------------------------------- Modell laden

def modell_laden(m: Modell, geraet: str, melden=lambda *a, **k: None, threads: int = 0):
    """Lädt ein bereites Modell. geraet: "auto" | "cuda" | "cpu".
    Gibt (WhisperModel, BatchedInferencePipeline oder None, "cuda"/"cpu") zurück."""
    from faster_whisper import WhisperModel

    if not m.bereit:
        raise RuntimeError(f"Modell «{m.name}» ist nicht heruntergeladen.")
    versuche = []
    if geraet in ("auto", "cuda"):
        if not gpu_vorhanden():
            if geraet == "cuda":
                melden("warnung", text="Keine NVIDIA-Grafikkarte gefunden – rechne auf der CPU.")
        elif not gpu_paket_vorhanden():
            melden("warnung", text="GPU-Paket fehlt – rechne auf der CPU (langsamer). Einrichten: "
                                   "«Modelle verwalten …» → Zusatzpakete → GPU-Paket.")
        else:
            nvidia_dlls_einbinden()
            versuche.append(("cuda", "float16"))
    # Immer auch die CPU als Ausweg – auch wenn ausdrücklich «Grafikkarte» gewählt ist
    versuche.append(("cpu", "int8"))

    letzter_fehler = None
    for dev, rechentyp in versuche:
        try:
            melden("status", text=f"Lade Modell «{m.name}» auf {'GPU' if dev == 'cuda' else 'CPU'} …")
            t0 = time.time()
            modell = WhisperModel(m.pfad, device=dev, compute_type=rechentyp,
                                  cpu_threads=cpu_threads(threads) if dev == "cpu" else 0)
            if dev == "cuda":
                # Kurzer Testlauf mit 1 s Stille: fehlende CUDA/cuDNN-DLLs fallen jetzt auf, nicht mitten drin
                import numpy as np
                segs, _ = modell.transcribe(np.zeros(16000, dtype=np.float32), language="en")
                list(segs)
            # Batch-Modus: mehrere Abschnitte gleichzeitig. GPU 16, CPU 8 (gemessen: auf der CPU
            # etwa 1.4–2x schneller als normal, gleicher Text)
            from faster_whisper import BatchedInferencePipeline
            batch = BatchedInferencePipeline(model=modell)
            batch.groesse = 16 if dev == "cuda" else 8
            melden("status", text=f"Modell bereit auf {'GPU' if dev == 'cuda' else 'CPU'} "
                                  f"(geladen in {time.time() - t0:.1f} s)")
            return modell, batch, dev
        except Exception as e:
            letzter_fehler = e
            if dev == "cuda":
                melden("warnung", text=f"GPU nicht nutzbar ({str(e)[:160]}) – rechne auf der CPU.")
            else:
                melden("warnung", text=f"CPU geht nicht: {type(e).__name__}: {str(e)[:200]}")
    raise RuntimeError(f"Modell konnte nicht geladen werden: {letzter_fehler}")


# ---------------------------------------------------------------- Transkription

@dataclass
class Ergebnis:
    datei: Path
    txt: Path
    srt: Path | None
    sprache: str
    sprache_wahrscheinlichkeit: float
    dauer: float
    benoetigt: float

    @property
    def tempo(self) -> float:
        return self.dauer / max(self.benoetigt, 0.1)


def tonspur_pruefen(datei: Path):
    """Öffnet die Datei kurz mit PyAV. Wirft KeineTonspur, wenn sie nicht lesbar ist oder keinen Ton hat
    (z. B. eine .txt, die wegen ausgeblendeter Endungen wie eine .mp3 aussah)."""
    import av
    try:
        with av.open(str(datei)) as container:
            if not container.streams.audio:
                raise KeineTonspur("Keine Tonspur in der Datei.")
    except KeineTonspur:
        raise
    except Exception as e:
        raise KeineTonspur(f"Datei kann nicht gelesen werden – keine Audio- oder Videodatei? "
                           f"({type(e).__name__})") from e


def transkribieren(modell, batch, datei: Path, sprache=None, srt=False, zeitstempel=False,
                   melden=lambda *a, **k: None, abbrechen: threading.Event | None = None) -> Ergebnis:
    """Eine Datei -> output\\name.txt (+ .srt). sprache=None: automatisch erkennen.
    melden-Arten: "sprache" (sprache, wahrscheinlichkeit), "fortschritt" (anteil, position, dauer),
    "segment" (text, start, ende)."""
    start = time.time()
    tonspur_pruefen(datei)
    try:
        if batch is not None:
            # mehrere Abschnitte gleichzeitig -> deutlich schneller (VAD ist hier immer an)
            segmente, info = batch.transcribe(str(datei), language=sprache,
                                              batch_size=getattr(batch, "groesse", 8), beam_size=5)
        else:
            segmente, info = modell.transcribe(str(datei), language=sprache, vad_filter=True, beam_size=5)
    except (IndexError, ValueError) as e:
        if _ist_av_fehler(e):
            # Dekodierfehler von PyAV sind auch ValueError – das ist keine stille, sondern eine kaputte Datei
            raise KeineTonspur(f"Die Datei lässt sich nicht dekodieren – beschädigt? "
                               f"({type(e).__name__})") from e
        if sprache is None:
            # Randfall: stille Datei -> nach dem VAD bleibt nichts für die Spracherkennung übrig
            raise KeineSprache("Keine Sprache gefunden – ist die Datei still? "
                               "Tipp: Sprache fest einstellen.") from e
        raise

    melden("sprache", sprache=info.language, wahrscheinlichkeit=info.language_probability,
           erkannt=sprache is None)
    dauer = info.duration or 0

    zeilen, srt_bloecke = [], []
    try:
        for i, s in enumerate(segmente, 1):
            if abbrechen is not None and abbrechen.is_set():
                raise Abgebrochen()
            text = s.text.strip()
            zeilen.append(f"[{zeit(s.start)}] {text}" if zeitstempel else text)
            if srt:
                srt_bloecke.append(f"{i}\n{srt_zeit(s.start)} --> {srt_zeit(s.end)}\n{text}\n")
            melden("segment", text=text, start=s.start, ende=s.end)
            if dauer:
                melden("fortschritt", anteil=min(s.end / dauer, 1.0), position=s.end, dauer=dauer)
    except IndexError as e:
        if sprache is None and not zeilen:
            raise KeineSprache("Keine Sprache gefunden – ist die Datei still?") from e
        raise
    if not zeilen:
        # nichts erkannt (z. B. stille Datei): keine leere .txt schreiben
        raise KeineSprache("Kein Text erkannt – die Datei scheint still zu sein.")
    melden("fortschritt", anteil=1.0, position=dauer, dauer=dauer)

    txt, srt_datei = ergebnis_schreiben(datei.stem, "\n".join(zeilen) + "\n",
                                        "\n".join(srt_bloecke) if srt else None)
    return Ergebnis(datei, txt, srt_datei, info.language, info.language_probability, dauer,
                    time.time() - start)


def _ist_av_fehler(e: Exception) -> bool:
    try:
        import av
        return isinstance(e, av.error.FFmpegError)
    except Exception:
        return False


def ergebnis_schreiben(name: str, text: str, srt_text=None):
    """Schreibt output\\name.txt (und .srt) und gibt (txt, srt oder None) zurück.
    Überschreibt nie etwas: die .txt wird «exklusiv» angelegt – ist der Name inzwischen vergeben
    (zweites InkWave-Fenster), kommt der nächste freie dran. Ist der Name zu lang für Windows,
    wird er gekürzt, statt das fertige Ergebnis zu verlieren."""
    OUTPUT_ORDNER.mkdir(parents=True, exist_ok=True)
    endungen = [".txt", ".srt"] if srt_text is not None else [".txt"]
    kandidaten = [name]
    if len(name) > 100:
        kandidaten.append(name[:100].rstrip(" .") or "transkript")
    for nr, kandidat in enumerate(kandidaten, 1):
        angelegt = []                      # was dieser Versuch selbst angelegt hat
        try:
            while True:
                stamm = freier_stamm(kandidat, endungen)
                try:
                    for endung, inhalt in ((".txt", text), (".srt", srt_text)):
                        if inhalt is None:
                            continue
                        pfad = stamm.with_name(stamm.name + endung)
                        with open(pfad, "x", encoding="utf-8") as f:
                            angelegt.append(pfad)
                            f.write(inhalt)
                    break
                except FileExistsError:    # Name inzwischen vergeben -> nächster freier
                    _dateien_loeschen(angelegt)
            return angelegt[0], (angelegt[1] if len(angelegt) > 1 else None)
        except OSError:
            _dateien_loeschen(angelegt)    # halb geschriebene Dateien nicht liegen lassen
            if nr == len(kandidaten):
                raise


def _dateien_loeschen(pfade: list):
    while pfade:
        try:
            pfade.pop().unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- Aufruf von der Kommandozeile

def _cli():
    """Hilfsaufrufe: --download REPO (für den Download-Prozess) und --test DATEI [MODELL] [GERÄT]."""
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--download":
        try:
            _repo_pruefen(args[1])
            muster, gesamt = download_plan(args[1])
            print(f"GESAMT {gesamt}", flush=True)
            print(f"FERTIG {herunterladen(args[1], muster)}", flush=True)
        except Exception as e:
            print(f"FEHLER {fehler_text(e)}", flush=True)
            sys.exit(1)
        return
    if args and args[0] in ("--gpu-paket", "--umwandlung-einrichten", "--umwandeln"):
        zuletzt = {"anteil": -1.0}

        def melden(schritt=None, anteil=None):
            if schritt:
                print(f"SCHRITT {schritt}", flush=True)
            if anteil is not None and (anteil - zuletzt["anteil"] >= 0.002 or anteil >= 1):
                zuletzt["anteil"] = anteil
                print(f"ANTEIL {anteil:.4f}", flush=True)
        try:
            if args[0] == "--gpu-paket":
                gpu_paket_einrichten(args[1] if len(args) > 1 else None, melden)
                ergebnis = str(GPU_ORDNER)
            elif args[0] == "--umwandlung-einrichten":
                umwandlung_einrichten(melden)
                ergebnis = str(UMWANDLUNG_ORDNER)
            else:
                ergebnis = str(umwandeln(args[1], args[2] if len(args) > 2 else "", melden))
            print(f"FERTIG {ergebnis}", flush=True)
        except Exception as e:
            text = fehler_text(e) if "urlopen" in repr(e) or "connect" in str(e).lower() else f"{e}"
            print(f"FEHLER {text}".replace("\n", " | "), flush=True)
            sys.exit(1)
        return
    if args and args[0] == "--test":
        datei = Path(args[1])
        name = args[2] if len(args) > 2 else EMPFEHLUNG["cuda" if gpu_vorhanden() else "cpu"]
        geraet = args[3] if len(args) > 3 else "auto"
        def melden(art, **d):
            if art != "segment" and art != "fortschritt":
                print(f"  [{art}] {d}", flush=True)
        m = modell_finden(name)
        if m is None:
            sys.exit(f"Modell {name} unbekannt")
        modell, batch, dev = modell_laden(m, geraet, melden)
        with WachHalten():
            e = transkribieren(modell, batch, datei, melden=melden)
        print(f"  -> {e.txt} | {e.sprache} {e.sprache_wahrscheinlichkeit:.0%} | {e.dauer:.1f} s Audio "
              f"in {e.benoetigt:.1f} s = {e.tempo:.1f}x Echtzeit ({dev})")
        print("  Text:", e.txt.read_text(encoding="utf-8").strip()[:300])
        return
    print(__doc__)


if __name__ == "__main__":
    _cli()
