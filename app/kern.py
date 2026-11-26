"""
kern.py – die Logik des Transcribers, ohne Oberfläche.

Abgeleitet aus Transcriber\\mp3_to_text.py (das Skript selbst bleibt unverändert).
Die Oberfläche (gui.py) ruft nur Funktionen von hier auf; alles Langsame meldet sich über
einen Rückruf `melden(art, **daten)` und lässt sich über ein threading.Event abbrechen.

Ordner:
  DATEN            neben der exe (vom Starter über TRANSCRIBER_DATA gesetzt):
                   output\\, modelle\\, einstellungen.json
  LOKAL            %LOCALAPPDATA%\\Transcriber: gpu\\ (GPU-Paket), umwandlung\\ (transformers+torch)
  Hugging-Face-Cache  heruntergeladene Modelle (wie bisher)
"""

import ctypes
import json
import os
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

SPRACHEN = [("", "Automatisch erkennen"), ("de", "Deutsch"), ("en", "Englisch"), ("fr", "Französisch"),
            ("it", "Italienisch"), ("es", "Spanisch"), ("pt", "Portugiesisch"), ("nl", "Niederländisch"),
            ("pl", "Polnisch"), ("tr", "Türkisch"), ("ru", "Russisch"), ("uk", "Ukrainisch")]

APP_ORDNER = Path(__file__).resolve().parent
DATEN = Path(os.environ.get("TRANSCRIBER_DATA") or (APP_ORDNER.parent / "daten"))
OUTPUT_ORDNER = DATEN / "output"
MODELL_ORDNER = DATEN / "modelle"
EINSTELLUNGEN = DATEN / "einstellungen.json"

LOKAL = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Transcriber"
GPU_ORDNER = LOKAL / "gpu"
UMWANDLUNG_ORDNER = LOKAL / "umwandlung"


class Abgebrochen(Exception):
    """Der Benutzer hat abgebrochen."""


class KeineSprache(Exception):
    """In der Datei wurde keine Sprache gefunden (z. B. stille Aufnahme)."""


class KeineTonspur(Exception):
    """Die Datei ist keine lesbare Audio-/Videodatei oder hat keine Tonspur."""


# ---------------------------------------------------------------- Einstellungen, Dateien

def einstellungen_lesen() -> dict:
    try:
        return json.loads(EINSTELLUNGEN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
        if p.is_dir():
            dateien += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_ENDUNGEN)
        elif p.is_file():
            dateien.append(p)
        else:
            fehlend.append(str(e))
    return list(dict.fromkeys(dateien)), fehlend


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
        k32 = ctypes.windll.kernel32
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


def nvidia_dlls_einbinden():
    """CUDA-DLLs aus dem GPU-Paket auffindbar machen (kein separates CUDA nötig)."""
    if os.name != "nt":
        return
    for bin_ordner in _gpu_bin_ordner():
        if str(bin_ordner) in _eingebunden:
            continue
        os.add_dll_directory(str(bin_ordner))
        os.environ["PATH"] = str(bin_ordner) + os.pathsep + os.environ.get("PATH", "")
        _eingebunden.add(str(bin_ordner))


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
    basis = hf_cache() / ("models--" + repo.replace("/", "--"))
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


def umgewandelt_ordner(repo: str) -> Path:
    return MODELL_ORDNER / repo.replace("/", "__")


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
    if MODELL_ORDNER.is_dir():
        for ordner in sorted(p for p in MODELL_ORDNER.iterdir() if p.is_dir()):
            repo = ""
            try:
                repo = json.loads((ordner / "quelle.json").read_text(encoding="utf-8")).get("repo", "")
            except (OSError, ValueError):
                pass
            name = repo or ordner.name
            if ist_ct2(ordner):
                liste.append(Modell(name, "eigen", "bereit", pfad=str(ordner), repo=repo,
                                    beschreibung="eigenes Modell" if not repo else "umgewandelt"))
            elif ist_transformers(ordner):
                liste.append(Modell(name, "eigen", "umwandeln", roh_pfad=str(ordner), repo=repo,
                                    beschreibung="muss noch umgewandelt werden"))
            else:
                continue
            if repo:
                gesehen.add(repo.lower())

    # Hugging-Face-Cache und selbst hinzugefügte Namen
    repos = {}
    try:
        for d in hf_cache().glob("models--*"):
            teile = d.name.split("--", 1)[1]
            repos[teile.replace("--", "/", 1)] = None
    except OSError:
        pass
    e = einstellungen_lesen()
    for repo in e.get("hf_modelle", []):
        repos.setdefault(repo, None)
    groessen = e.get("hf_groessen", {})
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
    blobs = hf_cache() / ("models--" + repo.replace("/", "--")) / "blobs"
    try:
        return sum(p.stat().st_size for p in blobs.iterdir() if p.is_file())
    except OSError:
        return 0


def herunterladen(repo: str, muster=None) -> str:
    """Lädt ein Modell in den HF-Cache (blockiert). Gibt den Snapshot-Ordner zurück."""
    import huggingface_hub
    if muster is None:
        muster, _ = download_plan(repo)
    return huggingface_hub.snapshot_download(repo, allow_patterns=muster)


def download_starten(repo: str, protokoll: Path) -> subprocess.Popen:
    """Startet den Download in einem eigenen Prozess (lässt sich sauber abbrechen; angefangene
    Dateien werden beim nächsten Mal fortgesetzt). Der Prozess schreibt ins Protokoll:
      GESAMT <bytes>   FERTIG <ordner>   FEHLER <text>
    Fortschritt: cache_bytes(repo) im Vergleich zu GESAMT."""
    python = Path(sys.executable)
    konsole = python.with_name("python.exe")
    if konsole.exists():
        python = konsole
    protokoll.parent.mkdir(parents=True, exist_ok=True)
    umgebung = dict(os.environ, HF_HUB_DISABLE_PROGRESS_BARS="1", PYTHONUTF8="1")
    with open(protokoll, "w", encoding="utf-8") as aus:
        return subprocess.Popen([str(python), str(Path(__file__).resolve()), "--download", repo],
                                stdout=aus, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                env=umgebung, **_ohne_fenster())


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
    typ = ((info.config or {}).get("model_type") or "").lower()
    whisper = fmt == "ct2" or typ == "whisper" or "whisper" in repo.lower()
    return {"repo": info.id, "format": fmt, "gesamt": gesamt, "whisper": whisper}


def hf_modell_merken(repo: str, gesamt: int = 0):
    e = einstellungen_lesen()
    liste = e.get("hf_modelle", [])
    groessen = e.get("hf_groessen", {})
    if gesamt:
        groessen[repo] = gesamt
    einstellungen_speichern(hf_modelle=liste + ([repo] if repo not in liste else []), hf_groessen=groessen)


# ---------------------------------------------------------------- Modell laden

def modell_laden(m: Modell, geraet: str, melden=lambda *a, **k: None, cpu_threads: int = 0):
    """Lädt ein bereites Modell. geraet: "auto" | "cuda" | "cpu".
    Gibt (WhisperModel, BatchedInferencePipeline oder None, "cuda"/"cpu") zurück."""
    from faster_whisper import WhisperModel

    if not m.bereit:
        raise RuntimeError(f"Modell «{m.name}» ist nicht heruntergeladen.")
    gpu = geraet != "cpu" and gpu_vorhanden()
    versuche = []
    if gpu and geraet in ("auto", "cuda"):
        nvidia_dlls_einbinden()
        versuche.append(("cuda", "float16"))
    if geraet in ("auto", "cpu") or not versuche:
        versuche.append(("cpu", "int8"))

    letzter_fehler = None
    for dev, rechentyp in versuche:
        try:
            melden("status", text=f"Lade Modell «{m.name}» auf {'GPU' if dev == 'cuda' else 'CPU'} …")
            t0 = time.time()
            modell = WhisperModel(m.pfad, device=dev, compute_type=rechentyp,
                                  cpu_threads=cpu_threads if dev == "cpu" else 0)
            if dev == "cuda":
                # Kurzer Testlauf mit 1 s Stille: fehlende CUDA/cuDNN-DLLs fallen jetzt auf, nicht mitten drin
                import numpy as np
                segs, _ = modell.transcribe(np.zeros(16000, dtype=np.float32), language="en")
                list(segs)
            batch = None
            if dev == "cuda":
                from faster_whisper import BatchedInferencePipeline
                batch = BatchedInferencePipeline(model=modell)
            melden("status", text=f"Modell bereit auf {'GPU' if dev == 'cuda' else 'CPU'} "
                                  f"(geladen in {time.time() - t0:.1f} s)")
            return modell, batch, dev
        except Exception as e:
            letzter_fehler = e
            melden("warnung", text=f"{'GPU' if dev == 'cuda' else 'CPU'} geht nicht: "
                                   f"{type(e).__name__}: {str(e)[:200]}")
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
            # GPU: mehrere Abschnitte gleichzeitig -> deutlich schneller (VAD ist hier immer an)
            segmente, info = batch.transcribe(str(datei), language=sprache, batch_size=16, beam_size=5)
        else:
            segmente, info = modell.transcribe(str(datei), language=sprache, vad_filter=True, beam_size=5)
    except (IndexError, ValueError) as e:
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

    OUTPUT_ORDNER.mkdir(parents=True, exist_ok=True)
    stamm = freier_stamm(datei.stem, [".txt", ".srt"] if srt else [".txt"])
    txt = stamm.with_name(stamm.name + ".txt")
    txt.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    srt_datei = None
    if srt:
        srt_datei = stamm.with_name(stamm.name + ".srt")
        srt_datei.write_text("\n".join(srt_bloecke), encoding="utf-8")

    return Ergebnis(datei, txt, srt_datei, info.language, info.language_probability, dauer,
                    time.time() - start)


# ---------------------------------------------------------------- Aufruf von der Kommandozeile

def _cli():
    """Hilfsaufrufe: --download REPO (für den Download-Prozess) und --test DATEI [MODELL] [GERÄT]."""
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--download":
        try:
            muster, gesamt = download_plan(args[1])
            print(f"GESAMT {gesamt}", flush=True)
            print(f"FERTIG {herunterladen(args[1], muster)}", flush=True)
        except Exception as e:
            print(f"FEHLER {fehler_text(e)}", flush=True)
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
        modell, batch, dev = modell_laden(m, geraet, melden, cpu_threads=physische_kerne())
        with WachHalten():
            e = transkribieren(modell, batch, datei, melden=melden)
        print(f"  -> {e.txt} | {e.sprache} {e.sprache_wahrscheinlichkeit:.0%} | {e.dauer:.1f} s Audio "
              f"in {e.benoetigt:.1f} s = {e.tempo:.1f}x Echtzeit ({dev})")
        print("  Text:", e.txt.read_text(encoding="utf-8").strip()[:300])
        return
    print(__doc__)


if __name__ == "__main__":
    _cli()
