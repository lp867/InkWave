"""Baut InkWave-<Version>.exe (eine Datei) und InkWave-<Version>-Ordner.zip (portabel).

Läuft mit dem normalen Python 3.11 auf dem Entwicklungs-PC:
    python build.py

Ablauf:
 1. Python 3.11.9 «embeddable» von python.org holen (signiert) -> build\stage\python
 2. Pakete aus requirements.txt dort hinein installieren (pip --target)
 3. App-Code nach build\stage\app kopieren, alles vorkompilieren (.pyc)
 4. Symbol zeichnen, alles als ZIP packen
 5. Starter (launcher\InkWaveLauncher.cs) mit dem csc.exe von Windows übersetzen,
    das ZIP kommt als Ressource hinein -> dist\InkWave-<Version>.exe
"""

import compileall
import hashlib
import py_compile
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PY_VERSION = "3.11.9"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
# SHA-256 des Archivs (dieselbe Datei wie auf python.org, dort MD5 6d9aa08531d48fcc261ba667e2df17c4).
# Die Signatur deckt nur die .exe/.dll ab – die Prüfsumme auch die Standardbibliothek (python311.zip).
# Bei einer neuen PY_VERSION anpassen.
PY_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
CSC =Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
CACHE = BUILD / "cache"
STAGE = BUILD / "stage"
DIST = ROOT / "dist"
LAUNCHER = ROOT / "launcher" / "InkWaveLauncher.cs"


def version() -> str:
    m = re.search(r'AssemblyVersion\("(\d+\.\d+)', LAUNCHER.read_text(encoding="utf-8"))
    if m is None:
        sys.exit(f"Keine AssemblyVersion in {LAUNCHER} gefunden.")
    return m.group(1)


def sha256(datei: Path) -> str:
    summe = hashlib.sha256()
    with open(datei, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            summe.update(block)
    return summe.hexdigest()


def schritt(text):
    print(f"\n=== {text}", flush=True)


def python_holen():
    schritt(f"Python {PY_VERSION} embeddable")
    CACHE.mkdir(parents=True, exist_ok=True)
    archiv = CACHE / Path(PY_URL).name
    if not archiv.exists():
        print(f"Lade {PY_URL}")
        teil = archiv.with_name(archiv.name + ".part")     # ein abgebrochener Download bleibt nicht als
        urllib.request.urlretrieve(PY_URL, teil)           # kaputtes Archiv im Cache liegen
        teil.replace(archiv)
    if sha256(archiv) != PY_SHA256:
        sys.exit(f"Prüfsumme von {archiv.name} stimmt nicht – Datei löschen und neu bauen:\n  {archiv}")
    ziel = STAGE / "python"
    with zipfile.ZipFile(archiv) as z:
        z.extractall(ziel)
    # Suchpfade: Standardbibliothek, Pakete, App. «import site» für .pth-Dateien der Pakete.
    (ziel / "python311._pth").write_text(
        "python311.zip\n.\nLib\\site-packages\n..\\app\nimport site\n", encoding="utf-8")
    # Signatur prüfen (Smart App Control lässt nur bekannte/signierte Programme zu)
    signiert = ["pythonw.exe", "python.exe", "python311.dll"]
    pfade = ",".join("'" + str(ziel / n).replace("'", "''") + "'" for n in signiert)
    ausgabe = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-AuthenticodeSignature {pfade} "
         "| ForEach-Object { $_.Status.ToString() + ' ' + $_.SignerCertificate.Subject.Split(',')[0] }"],
        capture_output=True, text=True).stdout.strip().splitlines()
    print("Signaturen:", *ausgabe, sep="\n  ")
    # Genau eine Zeile je Datei, jede gültig und von der Python Software Foundation. (Vorher galt auch
    # eine leere Ausgabe – z. B. wenn PowerShell scheitert – als «alles in Ordnung».)
    if len(ausgabe) != len(signiert) or any(
            not z.startswith("Valid ") or "Python Software Foundation" not in z for z in ausgabe):
        sys.exit("Python-Dateien sind nicht gültig signiert (oder die Prüfung ist gescheitert) – Abbruch.")


def pakete_installieren():
    schritt("Pakete installieren")
    ziel = STAGE / "python" / "Lib" / "site-packages"
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                    "--target", str(ziel), "--only-binary=:all:", "--no-compile",
                    "--python-version", "3.11", "--platform", "win_amd64", "--implementation", "cp",
                    "-r", str(ROOT / "requirements.txt")], check=True)
    # Für die App unnötige, grosse Teile entfernen
    for muster in ["bin", "*.dist-info/RECORD"]:
        for p in ziel.glob(muster):
            if p.is_dir():
                shutil.rmtree(p)


def app_kopieren():
    schritt("App kopieren und vorkompilieren")
    shutil.copytree(ROOT / "app", STAGE / "app", ignore=shutil.ignore_patterns("__pycache__"))
    compileall.compile_dir(str(STAGE / "python" / "Lib" / "site-packages"), quiet=1, workers=0,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    compileall.compile_dir(str(STAGE / "app"), quiet=1,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)


def symbol() -> Path:
    schritt("Symbol zeichnen")
    ico = BUILD / "inkwave.ico"
    subprocess.run([str(STAGE / "python" / "python.exe"), str(ROOT / "tools" / "make_icon.py"), str(ico)],
                   check=True)
    shutil.copy(ico, STAGE / "app" / "inkwave.ico")
    shutil.copy(ico.with_suffix(".png"), STAGE / "app" / "inkwave.png")
    return ico


def packen() -> Path:
    schritt("Packen")
    payload = BUILD / "payload.zip"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for datei in sorted(STAGE.rglob("*")):
            if datei.is_file():
                z.write(datei, datei.relative_to(STAGE).as_posix())
    print(f"payload.zip: {payload.stat().st_size / 1e6:.0f} MB")
    return payload


def starter(ico: Path, payload: Path | None, ausgabe: Path):
    befehl = [str(CSC), "/nologo", "/target:winexe", "/optimize+", f"/win32icon:{ico}",
              "/r:System.IO.Compression.dll", "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
              f"/out:{ausgabe}"]
    if payload:
        befehl.append(f"/resource:{payload},InkWave.payload")
    befehl.append(str(LAUNCHER))
    subprocess.run(befehl, check=True)


def main():
    v = version()
    print(f"InkWave {v}")
    if STAGE.exists():
        shutil.rmtree(STAGE)
    DIST.mkdir(exist_ok=True)
    python_holen()
    pakete_installieren()
    app_kopieren()
    ico = symbol()
    payload = packen()

    schritt("Starter übersetzen")
    exe = DIST / f"InkWave-{v}.exe"
    starter(ico, payload, exe)
    print(f"{exe.name}: {exe.stat().st_size / 1e6:.0f} MB")

    # Portabler Ordner als ZIP: Starter ohne Ressource + stage-Inhalt
    ordner_exe = BUILD / "InkWave.exe"
    starter(ico, None, ordner_exe)
    zip_pfad = DIST / f"InkWave-{v}-Ordner.zip"
    with zipfile.ZipFile(zip_pfad, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(ordner_exe, "InkWave/InkWave.exe")
        for datei in sorted(STAGE.rglob("*")):
            if datei.is_file():
                z.write(datei, "InkWave/" + datei.relative_to(STAGE).as_posix())
    print(f"{zip_pfad.name}: {zip_pfad.stat().st_size / 1e6:.0f} MB")
    print("\nFertig.")


if __name__ == "__main__":
    main()
