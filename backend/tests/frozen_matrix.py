"""Every backend operation through the frozen app, with small test data only.
python -B backend/tests/frozen_matrix.py, MATRIX_EXE = the built app's executable (default: the Windows build)"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, time, urllib.request, zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXE = Path(os.environ.get("MATRIX_EXE") or REPO / "dist" / "windows" / "PS5 UltraPack" / "PS5 UltraPack.exe")
# the HomebrewTest sample from SvenGDK/LibProsperoPKG at c28be59, as in test_fpkg_pipelines
HBT = "https://raw.githubusercontent.com/SvenGDK/LibProsperoPKG/c28be59/src/HomebrewTest/"
W = Path(os.environ.get("MATRIX_WORK") or tempfile.gettempdir()) / "ps5u-matrix"
shutil.rmtree(W, ignore_errors=True)
W.mkdir(parents=True)
T = W / "tmp"
T.mkdir()
ENV = dict(os.environ, PS5_FFPFSC_APP_DIR=str(W / "appdir"))
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO))

GAME = W / "src" / "Gämé™ Test"
for rel in ("README.md", "eboot.bin", "sce_sys/param.json", "sce_sys/icon0.png"):
    (GAME / rel).parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(HBT + rel, timeout=60) as f:
        (GAME / rel).write_bytes(f.read())
EBOOT = hashlib.sha256((GAME / "eboot.bin").read_bytes()).hexdigest()
results = []


def run(label, args, check, timeout=600):
    t0 = time.time()
    try:
        p = subprocess.run([str(EXE), "--backend-internal", *map(str, args)], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=ENV,
                           stdin=subprocess.DEVNULL)
        out, rc = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, rc = "TIMEOUT", -1
    try:
        ok, why = check(rc, out)
    except Exception as e:
        ok, why = False, f"check raised {e!r}"
    results.append((label, ok))
    print(f"{'PASS' if ok else 'FAIL'} {label:<34} {time.time() - t0:5.1f}s  {why}", flush=True)
    if not ok:
        for line in out.strip().splitlines()[-12:]:
            print("      " + line[:200])
    return out


def one(d, pattern):
    hits = sorted(Path(d).rglob(pattern))
    return hits[0] if hits else None


def made(d, pattern):
    def check(rc, out):
        f = one(d, pattern)
        return (rc == 0 and f is not None and f.stat().st_size > 0,
                f"rc={rc} {f.name if f else 'no ' + pattern} {f.stat().st_size if f else ''}")
    return check


def same_game(d, signed=False):
    """The game came out: eboot.bin as in the source (a .pkg build fake-signs it, so there only
    its presence counts) and the README unchanged."""
    def check(rc, out):
        e, r = one(d, "eboot.bin"), one(d, "README.md")
        if e is None:
            return False, f"rc={rc} no eboot.bin; files: {[p.name for p in Path(d).rglob('*')][:8]}"
        h = hashlib.sha256(e.read_bytes()).hexdigest()
        same_readme = r is not None and r.read_bytes() == (GAME / "README.md").read_bytes()
        ok = rc == 0 and (signed or h == EBOOT) and same_readme
        return ok, f"rc={rc} eboot {'present' if signed else ('matches' if h == EBOOT else 'DIFFERENT')}, README {'same' if same_readme else 'DIFFERENT/missing'}"
    return check


def out(name):
    d = W / name
    d.mkdir(parents=True, exist_ok=True)
    return d


common = ["--temp-dir", T, "--overwrite"]

# ── folder sources ─────────────────────────────────────────────────────────────
run("folder -> .ffpfs", [GAME, out("f2ffpfs"), "--to", "ffpfs", *common], made(W / "f2ffpfs", "*.ffpfs"))
run("folder -> .ffpfsc", [GAME, out("f2ffpfsc"), "--to", "ffpfsc", *common], made(W / "f2ffpfsc", "*.ffpfsc"))
run("folder -> .pkg", [GAME, out("f2pkg"), "--to", "pkg", *common], made(W / "f2pkg", "*.pkg"))
run("folder -> .exfat", [GAME, out("f2exfat"), "--to", "exfat", *common], made(W / "f2exfat", "*.exfat"))
# an AMPR game (its eboot imports libSceAmpr) with data in a subfolder: LZ4 asset packs inside an .exfat
AMPR = W / "src" / "ampr-game"
shutil.copytree(GAME, AMPR)
sys.path.insert(0, str(REPO / "backend" / "tests"))
from test_backport import _elf_with_imports
(AMPR / "eboot.bin").write_bytes(_elf_with_imports([("mc36MRb8k1w", 0, "libSceAmpr", 0, "libSceAmpr")]))
(AMPR / "data").mkdir()
(AMPR / "data" / "level.bin").write_bytes(b"vertex texture mesh " * 200000)


def lz4_inside(d):
    def check(rc, o):
        f = one(d, "*.exfat")
        if rc != 0 or f is None:
            return False, f"rc={rc} {f.name if f else 'no .exfat'}"
        import cli
        back = W / "lz4-back"
        shutil.rmtree(back, ignore_errors=True)
        cli._extract_exfat_to(f, back)
        names = {p.relative_to(back).as_posix() for p in back.rglob("*") if p.is_file()}
        ok = ("ampr_assets.index" in names and "fakelib/libSceAmpr.sprx" in names
              and "data/level.bin" not in names and any(n.endswith(".pak") for n in names))
        return ok, f"rc={rc} {f.name} {f.stat().st_size} packs={sorted(n for n in names if n.endswith('.pak'))}"
    return check


run("folder -> .exfat with LZ4 packs", [AMPR, out("f2lz4") / "game.exfat", "--to", "exfat", "--lz4", *common],
    lz4_inside(W / "f2lz4"))
run("LZ4 refused for a game without AMPR", [GAME, out("f2lz4no") / "g.exfat", "--to", "exfat", "--lz4", *common],
    lambda rc, o: (rc != 0 and "does not load through AMPR" in o, f"rc={rc}"))
run("folder -> folder refused (no change)", [GAME, out("f2f"), "--to", "folder", *common],
    lambda rc, o: (rc == 1 and "Nothing to do" in o, f"rc={rc}"))
run("folder -> .ffpfsc via exFAT", [GAME, out("f2x"), "--to", "ffpfsc", "--via-exfat", *common],
    lambda rc, o: (made(W / "f2x", "*.ffpfsc")(rc, o)[0] and "exFAT image built" in o, f"rc={rc} exFAT built: {'exFAT image built' in o}"))
# Sign and Backport change a folder source in place (as the app says): on copies
for tag, extra in (("sign", ["--sign"]), ("bp", ["--backport-target", "7.61"])):
    shutil.copytree(GAME, W / "src" / f"copy-{tag}")
run("folder -> .ffpfsc, fake-signed", [W / "src" / "copy-sign", out("f2sign"), "--to", "ffpfsc", "--sign", *common],
    lambda rc, o: (made(W / "f2sign", "*.ffpfsc")(rc, o)[0] and "[OK] signed" in o, f"rc={rc} signed: {'[OK] signed' in o}"))
run("folder -> .ffpfsc, backport 7.61", [W / "src" / "copy-bp", out("f2bp"), "--to", "ffpfsc", "--backport-target", "7.61", *common],
    made(W / "f2bp", "*.ffpfsc"))
assert hashlib.sha256((GAME / "eboot.bin").read_bytes()).hexdigest() == EBOOT, "the source changed"

FFPFSC = one(W / "f2ffpfsc", "*.ffpfsc")
FFPFS = one(W / "f2ffpfs", "*.ffpfs")
PKG = one(W / "f2pkg", "*.pkg")
XFFPFSC = one(W / "f2x", "*.ffpfsc")

# ── image sources ──────────────────────────────────────────────────────────────
if FFPFSC:
    run(".ffpfsc -> folder", [FFPFSC, out("c2f"), "--to", "folder", *common], same_game(W / "c2f"))
    run(".ffpfsc -> .ffpfs", [FFPFSC, out("c2s"), "--to", "ffpfs", *common], made(W / "c2s", "*.ffpfs"))
    run(".ffpfsc -> .pkg", [FFPFSC, out("c2p"), "--to", "pkg", *common], made(W / "c2p", "*.pkg"))
    run(".ffpfsc -> folder (--unpack)", [FFPFSC, out("c2u"), "--unpack", *common], same_game(W / "c2u"))
if FFPFS:
    run(".ffpfs -> .ffpfsc", [FFPFS, out("s2c"), "--to", "ffpfsc", *common], made(W / "s2c", "*.ffpfsc"))
    run(".ffpfs -> folder", [FFPFS, out("s2f"), "--to", "folder", *common], same_game(W / "s2f"))
if PKG:
    run(".pkg -> folder", [PKG, out("p2f"), "--to", "folder", *common], same_game(W / "p2f", signed=True))
    run(".pkg -> .ffpfsc", [PKG, out("p2c"), "--to", "ffpfsc", *common], made(W / "p2c", "*.ffpfsc"))
    run(".pkg -> .ffpfs", [PKG, out("p2s"), "--to", "ffpfs", *common], made(W / "p2s", "*.ffpfs"))
    run(".pkg -> folder (--fpkg-extract)", ["placeholder", out("p2x"), "--fpkg-extract", PKG], same_game(W / "p2x", signed=True))
    run(".pkg validate", ["--fpkg-validate", PKG], lambda rc, o: (rc == 0, f"rc={rc}"))
if XFFPFSC:
    run("exFAT .ffpfsc -> folder", [XFFPFSC, out("x2f"), "--to", "folder", *common], same_game(W / "x2f"))

# an .exfat source, written by MkPFS's own writer
from mkpfs.exfat_writer import write_exfat_image
EXFAT = Path(write_exfat_image(GAME, W / "src" / "game.exfat", cluster_size=65536))
run(".exfat -> .ffpfsc", [EXFAT, out("e2c"), "--to", "ffpfsc", *common], made(W / "e2c", "*.ffpfsc"))
run(".exfat -> folder", [EXFAT, out("e2f"), "--to", "folder", *common], same_game(W / "e2f"))

# ── archives ───────────────────────────────────────────────────────────────────
ZIP = W / "src" / "Gämé zip.zip"
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
    for f in GAME.rglob("*"):
        z.write(f, Path("Gämé™ Test") / f.relative_to(GAME))
run(".zip -> .ffpfsc", [ZIP, out("z2c"), "--to", "ffpfsc", *common], made(W / "z2c", "*.ffpfsc"))
import py7zr
SEVEN = W / "src" / "Gämé 7z.7z"
with py7zr.SevenZipFile(SEVEN, "w") as z:
    z.writeall(GAME, "Gämé™ Test")
SEVEN_PW = W / "src" / "Locked 7z.7z"
with py7zr.SevenZipFile(SEVEN_PW, "w", password="pässwörd") as z:
    z.writeall(GAME, "Gämé™ Test")
# The app unpacks archives in its own process; here with the frozen build's layout, so the
# extractor finds the 7za.exe inside dist\
os.environ["PS5_FFPFSC_APP_DIR"] = str(W / "appdir")
sys._MEIPASS = str(EXE.parent.parent / "Frameworks") if sys.platform == "darwin" else str(EXE.parent / "_internal")
import ultra_core
AE = ultra_core.ArchiveExtractor
print("      7-Zip used by the app:", AE._find_native_7z())


def extracts(label, arc, password=""):
    t0, logs = time.time(), []
    d = out("ae_" + label.split()[0].strip(".") + str(len(results)))
    try:
        root = AE.extract(arc, d, log_fn=lambda lvl, m: logs.append(m), password=password)
        e = one(root, "eboot.bin")
        ok = e is not None and hashlib.sha256(e.read_bytes()).hexdigest() == EBOOT
        why = f"eboot {'matches' if ok else 'missing or different'}; tool: " + \
              next((m.strip() for m in logs if "7z" in m or "py7zr" in m or "native" in m), "zipfile")
    except Exception as ex:
        ok, why = False, f"{type(ex).__name__}: {ex}"
    results.append((label, ok))
    print(f"{'PASS' if ok else 'FAIL'} {label:<34} {time.time() - t0:5.1f}s  {why[:150]}", flush=True)


extracts(".7z (app extractor)", SEVEN)
extracts(".7z with umlaut password", SEVEN_PW, "pässwörd")
extracts(".zip AES (app extractor)", FIXTURES / "archives" / "AES Gämé.zip", "secret-pw")
extracts(".zip Deflate64 (app extractor)", FIXTURES / "archives" / "Deflate64 Gämé.zip")
extracts(".zip plain (app extractor)", ZIP)
RAR = REPO / ".wintest" / "rar" / "Multi.part1.rar"     # local only, not in git
if RAR.exists():
    run(".rar multi-part -> .ffpfsc", [RAR, out("r2c"), "--to", "ffpfsc", *common], made(W / "r2c", "*.ffpfsc"))

# ── tools ──────────────────────────────────────────────────────────────────────
SIGNDIR = W / "signme"
shutil.copytree(GAME, SIGNDIR)
run("Fake Sign (in place)", ["--fake-sign", SIGNDIR], lambda rc, o: (rc == 0, f"rc={rc}"))
PATCH = W / "src" / "Patch"
PATCH.mkdir()
(PATCH / "data").mkdir()
(PATCH / "data" / "patched.txt").write_text("patched file\n", encoding="utf-8")
run("patch overlay -> .ffpfsc", [GAME, out("patch"), "--to", "ffpfsc", "--patch", PATCH, *common], made(W / "patch", "*.ffpfsc"))
PATCHED = one(W / "patch", "*.ffpfsc")
if PATCHED:
    run("patched .ffpfsc carries the patch", [PATCHED, out("patch_f"), "--to", "folder", *common],
        lambda rc, o: (rc == 0 and one(W / "patch_f", "patched.txt") is not None
                       and one(W / "patch_f", "patched.txt").read_text(encoding="utf-8") == "patched file\n", f"rc={rc}"))
if FFPFSC:
    listing = run("Look inside: list .ffpfsc", ["--list-image", FFPFSC],
                  lambda rc, o: (rc == 0 and "eboot.bin" in o, f"rc={rc}"))
    mf = W / "members.txt"
    mf.write_text("eboot.bin\n", encoding="utf-8")
    run("Look inside: extract one file", ["--extract-from", FFPFSC, "--dest", out("look"), "--members-file", mf],
        lambda rc, o: (rc == 0 and one(W / "look", "eboot.bin") is not None and one(W / "look", "README.md") is None
                       and hashlib.sha256(one(W / "look", "eboot.bin").read_bytes()).hexdigest() == EBOOT,
                       f"rc={rc} only eboot.bin, unchanged"))
run("param report", ["--param-report", GAME], lambda rc, o: (rc == 0, f"rc={rc}"))
run("SDK of a folder", ["--sdk-of", GAME], lambda rc, o: (rc == 0 and o.strip() != "", f"rc={rc} {o.strip()[:60]!r}"))
run("backport analysis", ["--backport-analyze", GAME, "--backport-target", "7.61"],
    lambda rc, o: ("[verdict]" in o and "STATUS   LIBRARY" in o, f"rc={rc} (no firmware folder: 'not checked' by design)"))
run("title list download", ["--title-name", "PPSA99099"], lambda rc, o: ("fetch failed" not in o, f"rc={rc} {o.strip()[:60]!r}"))
import re
_ids = sorted({m for f in (W / "appdir" / "titles").rglob("*") if f.is_file()
               for m in re.findall(r"PPSA\d{5}", f.read_text(encoding="utf-8", errors="ignore"))})
if _ids:
    run("title name lookup (a listed id)", ["--title-name", _ids[len(_ids) // 2]],
        lambda rc, o: (rc == 0 and "TITLE_NAME:" in o, f"rc={rc} found: {'TITLE_NAME:' in o}"))
if FFPFSC:
    run("Copy (same format)", ["placeholder", out("copy"), "--copy", FFPFSC, "--copy-mode", "keep"],
        made(W / "copy", "*.ffpfsc"))

# Organize: scan, apply, undo on a library with a loose image in it
LIB = W / "library"
LIB.mkdir()
if FFPFSC:
    shutil.copy2(FFPFSC, LIB / "loose image.ffpfsc")
plan, journal = W / "plan.json", W / "journal.json"
run("Organize: scan", ["--organize-scan", LIB, "--organize-plan", plan], lambda rc, o: (rc == 0 and plan.is_file(), f"rc={rc}"))
before = sorted(p.relative_to(LIB).as_posix() for p in LIB.rglob("*"))
run("Organize: apply", ["--organize-apply", "--organize-plan", plan, "--organize-journal", journal],
    lambda rc, o: (rc == 0 and sorted(p.relative_to(LIB).as_posix() for p in LIB.rglob("*")) != before,
                   f"rc={rc} now {[p.relative_to(LIB).as_posix() for p in LIB.rglob('*.ffpfsc')]}"))
run("Organize: undo", ["--organize-undo", "--organize-journal", journal],
    lambda rc, o: (rc == 0 and sorted(p.relative_to(LIB).as_posix() for p in LIB.rglob("*")) == before, f"rc={rc}"))

# PS4: a synthetic package sorted into a library
sys.path.insert(0, str(REPO / "backend" / "tests"))
from ps4_fixture import make_pkg
PS4 = W / "src" / "ps4"
PS4.mkdir()
cid = "UP0000-CUSA00001_00-SAMPLEGAME000000"
make_pkg(PS4 / "g.pkg", content_id=cid, content_type=0x1A, sfo={"TITLE": "Sample Gämé", "CATEGORY": "gd", "APP_VER": "01.00"})
make_pkg(PS4 / "u.pkg", content_id=cid, content_type=0x1A, sfo={"TITLE": "Sample Gämé", "CATEGORY": "gp", "APP_VER": "01.07"})
run("PS4 sort", ["placeholder", out("ps4lib"), "--ps4-sort", PS4, "--copy-mode", "keep"],
    lambda rc, o: (rc == 0 and len(list((W / "ps4lib").rglob("*.pkg"))) == 2,
                   f"rc={rc} {[p.relative_to(W / 'ps4lib').as_posix() for p in (W / 'ps4lib').rglob('*.pkg')]}"))

failed = [l for l, ok in results if not ok]
print(f"\n{len(results) - len(failed)} of {len(results)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
shutil.rmtree(W, ignore_errors=True)
sys.exit(1 if failed else 0)
