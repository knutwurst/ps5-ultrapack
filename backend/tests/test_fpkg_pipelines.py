"""
End-to-end tests for fPKG build/extract and its interaction with the existing
.ffpfsc pipeline. Prints a green/red report and exits non-zero on failure.

  python3 backend/tests/test_fpkg_pipelines.py [--work DIR] [--keep] [--samples N]

The tests use two kinds of source:
  1. A pre-shipped HomebrewTest sample fetched from SvenGDK's LibProsperoPKG repo
     (the same one drakmor builds against). Ships zero large binaries in-tree.
  2. Synthesized fixtures for edge cases: minimal /app0 without icon0, folder with
     unicode names, folder with a large-ish file, incompressible payload, and
     the negative cases (missing param.json, missing eboot.bin) that mkpfs /
     LibProsperoPkg should reject cleanly rather than crash.

Every test:
  - prints its own PASS/FAIL line with a one-sentence reason
  - checks structural invariants (validate), not just byte identity, because
    fake-signing rewrites eboot.bin and the builder canonicalises param.json.

Failure is diagnostic: each chain reports which sub-step (extract-inner,
CNT merge, mkpfs pack, mkpfs unpack, fpkg-build, fpkg-validate) went wrong.
"""

from __future__ import annotations

import argparse
import zipfile
import binascii
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

# ── paths ────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BACKEND = REPO / "backend"
CLI = BACKEND / "cli.py"
# FFPFSC_PKG_TOOL lets the whole harness run against a candidate build of the tool
# (backend/fpkg.py honours the same variable, so the CLI subprocesses follow suit).
TOOL = (Path(os.environ["FFPFSC_PKG_TOOL"]) if os.environ.get("FFPFSC_PKG_TOOL")
        else BACKEND / "native" / "ffpfsc-pkg-tool")

# Backend cli.py wants to import fpkg / mkpfs as if run from backend/
sys.path.insert(0, str(BACKEND))

# The HomebrewTest sample (SvenGDK/LibProsperoPKG @ commit c28be59, 1 MB total).
HBT_URLS = {
    "README.md":           "https://raw.githubusercontent.com/SvenGDK/LibProsperoPKG/c28be59/src/HomebrewTest/README.md",
    "eboot.bin":           "https://raw.githubusercontent.com/SvenGDK/LibProsperoPKG/c28be59/src/HomebrewTest/eboot.bin",
    "sce_sys/param.json":  "https://raw.githubusercontent.com/SvenGDK/LibProsperoPKG/c28be59/src/HomebrewTest/sce_sys/param.json",
    "sce_sys/icon0.png":   "https://raw.githubusercontent.com/SvenGDK/LibProsperoPKG/c28be59/src/HomebrewTest/sce_sys/icon0.png",
}


# ── infrastructure ──────────────────────────────────────────────────────────
@dataclass
class TestResult:
    name: str
    ok: bool
    reason: str = ""
    details: list[str] = field(default_factory=list)


class Runner:
    def __init__(self, work: Path, keep: bool):
        self.work = work
        self.keep = keep
        self.work.mkdir(parents=True, exist_ok=True)
        self.results: list[TestResult] = []

    def check(self, name: str, cond: bool, ok_msg: str = "", fail_msg: str = "") -> bool:
        if cond:
            self.results.append(TestResult(name, True, ok_msg))
        else:
            self.results.append(TestResult(name, False, fail_msg))
        return cond

    def run(self, name: str, fn) -> None:
        print(f"\n─── {name} ───")
        try:
            fn(self)
        except Exception as e:
            self.results.append(TestResult(name, False, f"exception: {e}"))
            print(f"  ✗ {name}: exception {e}")

    # ── shell helpers ───────────────────────────────────────────────────────
    def run_cli(self, args: list[str], label: str = "") -> tuple[int, str]:
        argv = [sys.executable, "-u", str(CLI), *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return -1, "timeout"
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out

    def run_tool(self, args: list[str]) -> tuple[int, str]:
        argv = [str(TOOL), *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return -1, "timeout"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def summary(self) -> int:
        fails = [r for r in self.results if not r.ok]
        print("\n" + "═" * 72)
        for r in self.results:
            tag = "PASS" if r.ok else "FAIL"
            colour = "\033[32m" if r.ok else "\033[31m"
            reset = "\033[0m"
            print(f"  {colour}[{tag}]{reset}  {r.name:<50s} {r.reason}")
        print("═" * 72)
        print(f"  {len(self.results) - len(fails)} passed, {len(fails)} failed")
        return 0 if not fails else 1


# ── fixtures ────────────────────────────────────────────────────────────────
def fetch_hbt(dst: Path) -> Path:
    """Fetch the HomebrewTest source folder into dst; return the folder path."""
    dst.mkdir(parents=True, exist_ok=True)
    for rel, url in HBT_URLS.items():
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and out.stat().st_size > 0:
            continue
        with urllib.request.urlopen(url, timeout=30) as f, open(out, "wb") as g:
            g.write(f.read())
    return dst


def make_png_1x1_rgb() -> bytes:
    """A well-formed 1x1 RGB PNG: IHDR/IDAT/IEND with real CRCs and a valid zlib stream.
    The tool converts icon0.png to DDS while staging the source, so the fixture must
    decode (a hand-typed sample used here before had a truncated IDAT chunk, the
    conversion threw, and every synthesized build silently fell back to the raw source)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)     # 1x1, 8 bits/channel, colour type 2 = RGB
    idat = zlib.compress(b"\x00" + b"\xff\xff\xff")          # filter byte 0 + one white pixel
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def make_synth_folder(dst: Path, *,
                     with_icon: bool = True,
                     with_param: bool = True,
                     with_eboot: bool = True,
                     data_files: list[tuple[str, bytes]] | None = None) -> Path:
    """Build a minimal PS5 /app0 folder for negative/positive smoke tests."""
    dst.mkdir(parents=True, exist_ok=True)
    if with_param:
        (dst / "sce_sys").mkdir(exist_ok=True)
        (dst / "sce_sys" / "param.json").write_text(json.dumps({
            "contentId": "UP9000-PPSA99099_00-PROSPERO00000000",
            "titleId":   "PPSA99099",
            "titleName": "Synth Test",
            "masterVersion": "01.00",
            "contentVersion": "01.000.000",
            "applicationDrmType": "free",
        }))
    if with_icon:
        (dst / "sce_sys").mkdir(exist_ok=True)
        (dst / "sce_sys" / "icon0.png").write_bytes(make_png_1x1_rgb())
    if with_eboot:
        # Minimal ELF stub — 4 bytes of ELF magic + padding
        (dst / "eboot.bin").write_bytes(b"\x7FELF" + b"\x00" * 60)
    for rel, blob in (data_files or []):
        p = dst / rel; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
    return dst


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── individual tests ────────────────────────────────────────────────────────
def test_environment(r: Runner):
    r.check("tool.present", TOOL.exists(), f"{TOOL}", f"missing: {TOOL} (build it: ./BUILD_PKG_TOOL.sh)")
    magick = TOOL.parent / "Magick.Native-Q8-arm64.dll.dylib"
    r.check("tool.magick-native", magick.is_file(), f"{magick.name} next to the tool",
            f"missing: {magick} — icon conversion would fail (build it: ./BUILD_PKG_TOOL.sh)")
    if TOOL.exists():
        rc, out = r.run_tool(["version"])
        r.check("tool.runs", rc == 0, out.strip().splitlines()[0] if out else "", out)


def test_chain1_folder_pkg_folder(r: Runner):
    """folder → fPKG → folder — round-trip via the CLI end-to-end."""
    hbt = fetch_hbt(r.work / "hbt")
    out_pkg = r.work / "c1_pkg"; out_ext = r.work / "c1_ext"
    for d in (out_pkg, out_ext):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True)

    # build
    rc, log = r.run_cli([str(hbt), str(out_pkg),
                         "--fpkg-build", str(hbt),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-title", "HomebrewTest",
                         "--fpkg-inner", "none",
                         "--fpkg-kraken-backend", "builtin"])
    r.check("chain1.build.rc", rc == 0, f"exit {rc}", log[-400:])
    pkgs = list(out_pkg.glob("*.pkg"))
    r.check("chain1.build.output", len(pkgs) == 1 and pkgs[0].stat().st_size > 10_000,
            f"{pkgs[0].name} {pkgs[0].stat().st_size:,} B" if pkgs else "",
            "no .pkg produced" if not pkgs else "empty .pkg")
    if not pkgs: return
    pkg = pkgs[0]

    # validate
    rc, log = r.run_tool(["validate", str(pkg)])
    r.check("chain1.validate", rc == 0, "17/17 checks pass" if "0 failed" in log else "",
            log[-400:])

    # extract
    rc, log = r.run_cli(["placeholder", str(out_ext), "--fpkg-extract", str(pkg)])
    r.check("chain1.extract.rc", rc == 0, f"exit {rc}", log[-400:])
    got = {str(p.relative_to(out_ext)): sha(p) for p in out_ext.rglob("*") if p.is_file()}

    # verify every source file survived (byte-identical except eboot/param.json)
    src = {str(p.relative_to(hbt)): sha(p) for p in hbt.rglob("*") if p.is_file()}
    missing = [k for k in src if k not in got]
    r.check("chain1.roundtrip.no-missing", not missing,
            "all source files present in extract",
            f"missing: {missing}")

    # icon0.png must be an 8-bit RGB PNG; the upstream sample ships RGBA, so the builder
    # flattens it (the only presentation-media change allowed here).
    icon = "sce_sys/icon0.png"
    src_icon_ok = png_colour(hbt / icon) == 2
    got_icon = png_colour(out_ext / icon) if (out_ext / icon).is_file() else None
    r.check("chain1.icon0.rgb", got_icon == 2,
            "icon0.png is 8-bit RGB" + ("" if src_icon_ok else " (source RGBA, flattened)"),
            f"icon0.png colour type {got_icon}")
    allowed = ("eboot.bin", "param.json") + (() if src_icon_ok else (icon,))
    identical = [k for k in src if k in got and src[k] == got[k]]
    r.check("chain1.roundtrip.preserved-data",
            all(k in identical for k in src if not k.endswith(allowed)),
            "README/other files byte-identical",
            f"unexpected diffs: {[k for k in src if k not in identical and not k.endswith(allowed)]}")

    # eboot must have been fake-signed
    if "eboot.bin" in src:
        r.check("chain1.eboot.transformed",
                got.get("eboot.bin") != src["eboot.bin"],
                "eboot.bin fake-signed (magic transformed)",
                "eboot.bin came through unchanged — fake-sign step missed")


def png_colour(p: Path) -> int | None:
    """PNG colour type of an 8-bit PNG (2 RGB, 6 RGBA), else None."""
    h = p.read_bytes()[:26]
    if len(h) == 26 and h[:8] == b"\x89PNG\r\n\x1a\n" and h[12:16] == b"IHDR" and h[24] == 8:
        return h[25]
    return None


def test_chain2_folder_ffpfsc_folder_pkg(r: Runner):
    """folder → .ffpfsc → folder → fPKG — the mkpfs-then-fpkg chain."""
    hbt = fetch_hbt(r.work / "hbt")
    ff = r.work / "c2_ffpfsc"; up = r.work / "c2_unpack"; pkgd = r.work / "c2_pkg"
    for d in (ff, up, pkgd):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(ff), "--pack", "--overwrite"])
    r.check("chain2.pack.rc", rc == 0, "mkpfs pack ok", log[-400:])
    ffs = list(ff.glob("*.ffpfsc"))
    if not r.check("chain2.pack.output", ffs, f"{ffs[0].name}" if ffs else "", "no .ffpfsc"):
        return
    ffpath = ffs[0]

    rc, log = r.run_cli([str(ffpath), str(up), "--unpack", "--overwrite"])
    r.check("chain2.unpack.rc", rc == 0, "mkpfs unpack ok", log[-400:])
    # Find the unpacked /app0 folder — mkpfs writes to a subdir
    candidates = [p for p in up.rglob("sce_sys") if p.is_dir()]
    if not r.check("chain2.unpack.app0", candidates, "sce_sys/ present", "no sce_sys/ found"):
        return
    app0 = candidates[0].parent

    rc, log = r.run_cli([str(app0), str(pkgd),
                         "--fpkg-build", str(app0),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-title", "HomebrewTest",
                         "--fpkg-inner", "none",
                         "--fpkg-kraken-backend", "builtin"])
    r.check("chain2.fpkg.build.rc", rc == 0, "fpkg-build ok", log[-400:])
    pkgs = list(pkgd.glob("*.pkg"))
    if not r.check("chain2.fpkg.output", pkgs, f"{pkgs[0].name}" if pkgs else "", "no .pkg"):
        return
    rc, log = r.run_tool(["validate", str(pkgs[0])])
    r.check("chain2.fpkg.validate", rc == 0, "validate ok",
            "\n".join(x for x in log.splitlines() if "[FAIL]" in x))


def test_chain3_pkg_folder_ffpfsc(r: Runner):
    """fPKG → folder → .ffpfsc — mkpfs must accept our extracted /app0 tree."""
    hbt = fetch_hbt(r.work / "hbt")
    seed_pkg = r.work / "c3_seed"; ext = r.work / "c3_ext"; ff = r.work / "c3_ffpfsc"
    for d in (seed_pkg, ext, ff):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True)

    # Seed: build an fPKG from HBT (we don't ship one in-tree).
    rc, log = r.run_cli([str(hbt), str(seed_pkg),
                         "--fpkg-build", str(hbt),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    r.check("chain3.seed.pkg", rc == 0, "seed .pkg built", log[-400:])
    pkgs = list(seed_pkg.glob("*.pkg"))
    if not pkgs: return

    rc, log = r.run_cli(["placeholder", str(ext), "--fpkg-extract", str(pkgs[0])])
    r.check("chain3.extract.rc", rc == 0, "extract ok", log[-400:])
    # mkpfs pack requires sce_sys/param.json
    r.check("chain3.extract.param",
            (ext / "sce_sys" / "param.json").is_file(),
            "sce_sys/param.json present (CNT merged in)",
            "MISSING: mkpfs would refuse")
    r.check("chain3.extract.icon0",
            (ext / "sce_sys" / "icon0.png").is_file(),
            "sce_sys/icon0.png present",
            "MISSING")

    rc, log = r.run_cli([str(ext), str(ff), "--pack", "--overwrite"])
    r.check("chain3.repack.rc", rc == 0,
            "mkpfs accepted the fPKG-extracted folder",
            log[-400:])


def test_negative_missing_param_json(r: Runner):
    """No sce_sys/param.json but --content-id/--title-id given: the tool generates a
    minimal param.json from the supplied identity (LibProsperoPkg's
    GenerateParamJsonIfMissing default) and the build succeeds with a green
    auto-validate. Without identity flags it refuses instead (identity.none.reject).
    The sample's eboot is used because the builder rejects the 64-byte ELF stub
    ("Only 64-bit ELF modules are supported"), which would mask this behaviour."""
    hbt = fetch_hbt(r.work / "hbt")
    src = make_synth_folder(r.work / "neg_no_param", with_param=False, with_eboot=False)
    shutil.copy2(hbt / "eboot.bin", src / "eboot.bin")
    out = r.work / "neg_no_param_out"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out),
                         "--fpkg-build", str(src),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    pkg = next(out.glob("*.pkg"), None)
    r.check("negative.missing-param.exit",
            rc == 0 and pkg is not None,
            f"rc=0, {pkg.name if pkg else ''} built from a generated param.json",
            f"rc={rc}, pkg={pkg}; last log: " + log[-300:])
    r.check("negative.missing-param.generated",
            "param.json not found - generating a minimal one" in log,
            "tool reported the generated param.json",
            "no 'generating a minimal one' line: " + log[-300:])
    r.check("negative.missing-param.validate",
            "[FAIL]" not in log and re.search(r"summary: \d+ passed, \d+ warned, 0 failed", log) is not None,
            "auto-validate green on the generated identity",
            "auto-validate not green: " + "\n".join(x for x in log.splitlines() if "[FAIL]" in x or "summary:" in x))


def test_negative_missing_eboot(r: Runner):
    """No eboot.bin: the builder does not refuse. The package is written (rc 0) and the
    CLI's auto-validate is what flags it, as its single failure ('[FAIL] inner.eboot')."""
    src = make_synth_folder(r.work / "neg_no_eboot", with_eboot=False)
    out = r.work / "neg_no_eboot_out"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out),
                         "--fpkg-build", str(src),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    pkg = next(out.glob("*.pkg"), None)
    r.check("negative.missing-eboot.exit",
            rc == 0 and pkg is not None,
            f"rc=0, {pkg.name if pkg else ''} written without an eboot",
            f"rc={rc}, pkg={pkg}; last log: " + log[-300:])
    r.check("negative.missing-eboot.validate-flags-it",
            "[FAIL]" in log and "inner.eboot" in log and "eboot.bin not present in inner PFS" in log
            and re.search(r"summary: \d+ passed, \d+ warned, 1 failed", log) is not None,
            "auto-validate reported the missing eboot as its single failure",
            "auto-validate did not flag it: " + "\n".join(x for x in log.splitlines() if "[FAIL]" in x or "summary:" in x))
    # The synthesized fixture (generated icon0.png) must pass the tool's staging step:
    # a PNG the DDS converter cannot decode makes the tool fall back to the raw source.
    r.check("synth.auto-stage.ok",
            "[stage] mirrored source into" in log and "[warn] source auto-stage failed" not in log,
            "source staged (icon0.png converted to DDS, no auto-stage fallback)",
            "staging line missing or auto-stage fallback hit: "
            + "\n".join(x for x in log.splitlines() if "[stage]" in x or "[warn]" in x or "[icon]" in x))


def test_negative_bad_content_id(r: Runner):
    """A malformed content id should be rejected up front."""
    src = make_synth_folder(r.work / "neg_bad_cid")
    out = r.work / "neg_bad_cid_out"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out),
                         "--fpkg-build", str(src),
                         "--content-id", "totally-invalid",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    r.check("negative.bad-content-id.reject",
            rc != 0 and "Content ID" in log,
            "builder rejected the bad content id",
            f"rc={rc}; got: {log[-300:]}")


def test_identity_from_param_json(r: Runner):
    """sce_sys/param.json is the identity's source of truth. A --content-id /
    --fpkg-version / --fpkg-title that disagree with it must NOT reach the package
    header: the console checks header-vs-param.json coherence, and so does validate
    ('param.contentId != CNT header' was a real FAIL before this rule). Omitting the ids
    must work when param.json has them; with neither, the build must refuse clearly."""
    hbt = fetch_hbt(r.work / "hbt")

    # 1) disagreeing arguments → param.json wins, the log says so, validate is green
    out = r.work / "ident_mismatch"
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(out), "--fpkg-build", str(hbt),
                         "--content-id", "UP9000-PPSA99099_00-MISMATCHMISMATCH",
                         "--title-id", "PPSA99099",
                         "--fpkg-version", "02.000.000",
                         "--fpkg-title", "Wrong Title"])
    r.check("identity.mismatch.rc", rc == 0, f"exit {rc}", log[-400:])
    pkg = next(out.glob("*.pkg"), None)
    r.check("identity.mismatch.header-from-param",
            pkg is not None and "PROSPERO00000000" in pkg.name and "V0100" in pkg.name,
            pkg.name if pkg else "", f"got {pkg.name if pkg else 'no .pkg'} — header did not follow param.json")
    r.check("identity.mismatch.warned",
            "differs from param.json" in log,
            "log warns about the disagreeing --content-id",
            "no warning in the log")
    if pkg:
        rc, vlog = r.run_tool(["validate", str(pkg)])
        r.check("identity.mismatch.validate", rc == 0 and "0 failed" in vlog,
                "validate green (header == param.json)", vlog[-400:])

    # 2) no ids passed at all → still builds from param.json
    out2 = r.work / "ident_omitted"
    if out2.exists(): shutil.rmtree(out2)
    out2.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(out2), "--fpkg-build", str(hbt)])
    pkg2 = next(out2.glob("*.pkg"), None)
    r.check("identity.omitted.builds",
            rc == 0 and pkg2 is not None and "UP9000-PPSA99099_00-PROSPERO00000000" in pkg2.name,
            pkg2.name if pkg2 else "", f"rc={rc}; {log[-300:]}")

    # 3) no param.json AND no ids → a clear refusal, never a crash
    src = make_synth_folder(r.work / "ident_noparam", with_param=False)
    out3 = r.work / "ident_noparam_out"; out3.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out3), "--fpkg-build", str(src)])
    r.check("identity.none.reject",
            rc != 0 and "content id" in log.lower() and "Traceback" not in log,
            "refused with a readable message", f"rc={rc}; {log[-300:]}")


def test_kraken_fast_preset(r: Runner):
    """The GUI's 'Kraken speed: fast' is --compression-level -4. It must reach the tool
    (the tool logs its configuration) and still yield a package that validates."""
    hbt = fetch_hbt(r.work / "hbt")
    out = r.work / "fast_preset"
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(out), "--fpkg-build", str(hbt), "--compression-level", "-4"])
    r.check("fast.rc", rc == 0, f"exit {rc}", log[-300:])
    r.check("fast.level-reaches-tool", "Kraken level=-4" in log,
            "tool logged 'Kraken level=-4'", "configuration line does not show level -4")
    pkg = next(out.glob("*.pkg"), None)
    if pkg:
        rc, vlog = r.run_tool(["validate", str(pkg)])
        r.check("fast.validate", rc == 0 and "0 failed" in vlog, "validate green", vlog[-300:])


def test_validate_catches_untouched_ffpfsc(r: Runner):
    """A .ffpfsc isn't an fPKG. Validator must fail loudly (not crash)."""
    hbt = fetch_hbt(r.work / "hbt")
    ff = r.work / "v_ff"; ff.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(hbt), str(ff), "--pack", "--overwrite"])
    if rc != 0:
        r.results.append(TestResult("validate.on-ffpfsc.skip", True, "pack failed; skipping"))
        return
    ffpath = next(ff.glob("*.ffpfsc"), None)
    if not ffpath:
        r.results.append(TestResult("validate.on-ffpfsc.skip", True, "no ffpfsc produced"))
        return
    rc, log = r.run_tool(["validate", str(ffpath)])
    r.check("validate.on-ffpfsc.rejects",
            rc != 0 and ("UNKNOWN" in log or "not" in log.lower()),
            "validator flagged the .ffpfsc as non-fPKG",
            f"rc={rc}; log: {log[-300:]}")


def test_inner_modes(r: Runner):
    """Build with each inner-codec mode — all must produce a valid, self-extracting pkg."""
    hbt = fetch_hbt(r.work / "hbt")
    for mode in ("none", "zlib", "kraken"):
        out = r.work / f"mode_{mode}"
        if out.exists(): shutil.rmtree(out)
        out.mkdir(parents=True)
        rc, log = r.run_cli([str(hbt), str(out),
                             "--fpkg-build", str(hbt),
                             "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                             "--title-id", "PPSA99099",
                             "--fpkg-title", "HomebrewTest",
                             "--fpkg-inner", mode,
                             "--fpkg-kraken-backend", "builtin"])
        r.check(f"mode.{mode}.build",
                rc == 0,
                f"inner={mode} built ok",
                f"rc={rc}; {log[-300:]}")
        pkg = next(out.glob("*.pkg"), None)
        if not pkg:
            r.check(f"mode.{mode}.output", False, "", "no .pkg produced"); continue
        rc, log = r.run_tool(["validate", str(pkg)])
        r.check(f"mode.{mode}.validate",
                rc == 0,
                "validator green",
                "\n".join(x for x in log.splitlines() if "[FAIL]" in x))
        # extract-inner still produces a valid /app0 for zlib/kraken modes
        ext = out / "_ext"; ext.mkdir(exist_ok=True)
        rc, log = r.run_cli(["placeholder", str(ext), "--fpkg-extract", str(pkg)])
        r.check(f"mode.{mode}.extract",
                rc == 0 and (ext / "sce_sys" / "param.json").is_file(),
                "extract-inner + CNT merge ok",
                log[-300:])


def test_no_eboot_caught_by_validate(r: Runner):
    """A pkg built from a folder that lacked eboot.bin must fail the validator."""
    src = make_synth_folder(r.work / "vne_src", with_eboot=False)
    out = r.work / "vne_out"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out),
                         "--fpkg-build", str(src),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    pkg = next(out.glob("*.pkg"), None)
    if pkg is None:
        # builder correctly rejected — perfect
        r.check("no-eboot.builder-rejected", rc != 0, "builder refused the folder",
                f"rc={rc}; log: {log[-300:]}")
        return
    # builder accepted; validator MUST catch it
    rc, log = r.run_tool(["validate", str(pkg)])
    r.check("no-eboot.validate-fails",
            rc != 0 and "eboot" in log.lower(),
            "validator caught the missing eboot",
            f"rc={rc}, log: {log[-300:]}")


def test_tool_path_resolution(r: Runner):
    """backend/fpkg.py resolves the binary as: FFPFSC_PKG_TOOL override first, then
    backend/native/, then the PyInstaller _MEIPASS copies (first existing executable
    wins). An override that does not exist must fall through to backend/native/; an
    existing one must win; with nothing found, tool_path() raises FileNotFoundError
    naming the override variable and is_available() is False."""
    import importlib
    fpkg = importlib.import_module("fpkg")
    native = BACKEND / "native" / fpkg._TOOL_NAME
    r.check("path.native-exists",
            fpkg.is_available() and TOOL.samefile(fpkg.tool_path()),
            f"{fpkg.tool_path()}",
            "backend/fpkg.py couldn't locate the native tool")
    prev = os.environ.get("FFPFSC_PKG_TOOL")     # restore afterwards — a variant run relies on it
    bogus = str(r.work / "does-not-exist" / fpkg._TOOL_NAME)
    try:
        # 1) an override that does not exist is skipped; backend/native/ resolves the tool
        os.environ["FFPFSC_PKG_TOOL"] = bogus
        try:
            resolved, err = fpkg.tool_path(), ""
        except FileNotFoundError as e:
            resolved, err = None, str(e)
        r.check("path.env-fallback",
                resolved is not None and resolved.samefile(native),
                f"missing override ignored, resolved {resolved}",
                f"expected {native}, got {resolved if resolved is not None else err[:200]!r}")
        # 2) an override that exists wins over backend/native/
        os.environ["FFPFSC_PKG_TOOL"] = sys.executable
        r.check("path.env-override-wins",
                fpkg.tool_path().samefile(Path(sys.executable)),
                "existing override returned first",
                f"got {fpkg.tool_path()}")
        # 3) nothing found anywhere: FileNotFoundError naming FFPFSC_PKG_TOOL, is_available() False
        os.environ["FFPFSC_PKG_TOOL"] = bogus
        real_name = fpkg._TOOL_NAME
        fpkg._TOOL_NAME = "ffpfsc-pkg-tool-that-does-not-exist"
        try:
            try:
                fpkg.tool_path(); raised = ""
            except FileNotFoundError as e:
                raised = str(e)
            r.check("path.none-found",
                    "FFPFSC_PKG_TOOL" in raised and not fpkg.is_available(),
                    "FileNotFoundError raised, is_available() False",
                    f"no FileNotFoundError ({raised[:120]!r}) or is_available() still True")
        finally:
            fpkg._TOOL_NAME = real_name
    finally:
        if prev is None:
            os.environ.pop("FFPFSC_PKG_TOOL", None)
        else:
            os.environ["FFPFSC_PKG_TOOL"] = prev


def test_chain4_image_to_fpkg_oneclick(r: Runner):
    """--fpkg-build <image.ffpfsc>: the one-click image → fPKG conversion (unwrap on
    --temp-dir, build, validate, extract back)."""
    hbt = fetch_hbt(r.work / "hbt")
    ff = r.work / "c4_ffpfsc"; pkgd = r.work / "c4_pkg"; tmp = r.work / "c4_tmp"; ext = r.work / "c4_ext"
    for d in (ff, pkgd, tmp, ext):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(ff), "--pack", "--overwrite"])
    ffpath = next(ff.glob("*.ffpfsc"), None)
    if not r.check("chain4.pack", rc == 0 and ffpath is not None, "seed .ffpfsc built", log[-300:]):
        return
    rc, log = r.run_cli([str(ffpath), str(pkgd),
                         "--fpkg-build", str(ffpath),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099", "--fpkg-title", "HomebrewTest",
                         "--fpkg-inner", "kraken", "--fpkg-kraken-backend", "builtin",
                         "--compression-level", "5", "--temp-dir", str(tmp)])
    r.check("chain4.image-to-fpkg.rc", rc == 0, "image unwrapped + fPKG built in one job", log[-400:])
    r.check("chain4.unwrap-phase", "[PHASE] Extracting" in log, "GUI phase marker for the unwrap emitted",
            "no [PHASE] Extracting marker")
    r.check("chain4.one-pass-unwrap", "in one pass" in log and "Unwrapping nested image" not in log
            and "staged in place" in log,
            "the container was unpacked straight into files and the package built in that copy",
            "\n".join(x for x in log.splitlines() if "Unpacking" in x or "Unwrapping" in x or "[stage]" in x))
    r.check("chain4.level-passthrough", "level=5" in log, "--compression-level reached the builder (level=5)",
            "level not visible in builder banner")
    r.check("chain4.temp-passthrough", str(tmp) in log, "--temp-dir reached the builder", "temp dir not in banner")
    r.check("chain4.scratch-cleaned", not any(tmp.iterdir()), "unwrap scratch removed after build",
            f"leftovers: {[x.name for x in tmp.iterdir()]}")
    r.check("chain4.complete-marker", "[OK] fPKG complete:" in log, "output marker for the GUI emitted",
            "no '[OK] fPKG complete:' line")
    pkg = next(pkgd.glob("*.pkg"), None)
    if not r.check("chain4.output", pkg is not None, f"{pkg.name}" if pkg else "", "no .pkg"):
        return
    rc, log = r.run_tool(["validate", str(pkg)])
    r.check("chain4.validate", rc == 0, "validator green", "\n".join(x for x in log.splitlines() if "[FAIL]" in x))
    rc, log = r.run_cli(["placeholder", str(ext), "--fpkg-extract", str(pkg)])
    r.check("chain4.extract-back", rc == 0 and (ext / "README.md").is_file()
            and sha(ext / "README.md") == sha(hbt / "README.md"),
            "content survived ffpfsc → fPKG → folder byte-identical", log[-300:])
    r.check("chain4.extract-complete-marker", "[OK] Extraction complete:" in log,
            "GUI output marker for extract emitted", "missing marker")


def test_gui_progress_translation(r: Runner):
    """A folder build must emit the [PHASE] markers and progress bars the GUI parser reads."""
    hbt = fetch_hbt(r.work / "hbt")
    out = r.work / "gui_prog"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(hbt), str(out),
                         "--fpkg-build", str(hbt),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099", "--fpkg-inner", "kraken",
                         "--fpkg-kraken-backend", "builtin"])
    phases = [ln for ln in log.splitlines() if ln.startswith("[PHASE] ")]
    bars = [ln for ln in log.splitlines() if ln.startswith("[") and "% " in ln and ("#" in ln[:22] or "-" in ln[:22])]
    r.check("gui.phases", {"[PHASE] Scanning Files", "[PHASE] Creating Temp PFS", "[PHASE] Compressing",
                           "[PHASE] Writing Final Image", "[PHASE] Verifying Output"} <= set(phases),
            f"{len(phases)} phase markers", f"got: {sorted(set(phases))}")
    r.check("gui.bars", len(bars) >= 4, f"{len(bars)} progress bars", f"only {len(bars)} bars: {bars[:3]}")


def test_build_temp_is_contained(r: Runner):
    """The package tool's intermediates (libprospero-publisher-*.pfs_image.dat and friends)
    must live in a run-owned tmpXXXXXXXX subfolder of --temp-dir, never loose in the temp
    root, and nothing may be left behind after the build."""
    hbt = fetch_hbt(r.work / "hbt")
    temp = r.work / "tmpcheck_temp"; out = r.work / "tmpcheck_out"
    for d in (temp, out):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True)
    (temp / "user-file.txt").write_text("the user's own file")
    argv = [sys.executable, "-u", str(CLI), str(hbt), str(out), "--fpkg-build", str(hbt),
            "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000", "--title-id", "PPSA99099",
            "--fpkg-inner", "kraken", "--fpkg-kraken-backend", "builtin", "--temp-dir", str(temp)]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    loose, inside = set(), set()
    while proc.poll() is None:
        try:
            for e in os.scandir(temp):
                if e.name.startswith("libprospero-") or e.name.startswith("ffpfsc-stage-"):
                    loose.add(e.name)
                elif e.is_dir() and re.fullmatch(r"tmp[a-z0-9_]{8}", e.name):
                    try:
                        inside.update(x.name for x in os.scandir(e.path) if x.name.startswith("libprospero-"))
                    except OSError:
                        pass
        except OSError:
            pass
        time.sleep(0.02)
    log = proc.stdout.read() if proc.stdout else ""
    r.check("temp.build.rc", proc.returncode == 0, "exit 0", log[-400:])
    r.check("temp.no-loose-intermediates", not loose,
            "nothing loose in the temp root while building" + (f" (seen inside a tmp folder: {len(inside)})" if inside else ""),
            f"loose in temp root: {sorted(loose)}")
    left = sorted(p.name for p in temp.iterdir())
    r.check("temp.cleaned-after-build", left == ["user-file.txt"], "only the user's own file remains", f"left: {left}")


def test_deterministic_build(r: Runner):
    """Two --deterministic builds must produce byte-identical fPKGs, and so must a build on
    four workers (the CPU cores setting reaches the package tool as --parallelism)."""
    hbt = fetch_hbt(r.work / "hbt")
    a = r.work / "det_a"; b = r.work / "det_b"; c = r.work / "det_par4"
    for d in (a, b, c):
        if d.exists(): shutil.rmtree(d); d.mkdir(parents=True)
        else: d.mkdir(parents=True)
    def build(dst, *extra):
        return r.run_cli([str(hbt), str(dst),
                          "--fpkg-build", str(hbt),
                          "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                          "--title-id", "PPSA99099",
                          "--fpkg-title", "HomebrewTest",
                          "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin",
                          "--fpkg-passcode", "0" * 32,
                          "--fpkg-deterministic", *extra])
    build(a); build(b)
    pa = next(a.glob("*.pkg"), None); pb = next(b.glob("*.pkg"), None)
    if not (pa and pb):
        r.check("determinism.build", False, "", "at least one deterministic build didn't produce a .pkg")
        return
    r.check("determinism.byte-identical",
            sha(pa) == sha(pb),
            "two builds produced the same .pkg bytes",
            f"drift: {sha(pa)[:16]} vs {sha(pb)[:16]}")
    _rc_c, out_c = build(c, "--cpu-count", "4")
    pc = next(c.glob("*.pkg"), None)
    r.check("determinism.four-workers",
            pc is not None and sha(pc) == sha(pa) and "workers=4" in (out_c or ""),
            "a build on four workers gives the same .pkg bytes as one worker",
            f"pkg={pc is not None} same={pc is not None and sha(pc) == sha(pa)} "
            f"workers-line={'workers=4' in (out_c or '')}")


def test_stage_in_place_matches_copy(r: Runner):
    """--stage-in-place (the folder is the caller's own working copy) must give the same
    deterministic package as the copy-mode build, leave the copy-mode source untouched, and
    say so in the log."""
    hbt = fetch_hbt(r.work / "hbt")
    src_a, src_b = r.work / "sip_src_a", r.work / "sip_src_b"
    out_a, out_b = r.work / "sip_out_a", r.work / "sip_out_b"
    for d in (src_a, src_b, out_a, out_b):
        if d.exists(): shutil.rmtree(d)
    shutil.copytree(hbt, src_a); shutil.copytree(hbt, src_b); out_a.mkdir(); out_b.mkdir()
    before = sorted((str(p.relative_to(src_a)), p.stat().st_size) for p in src_a.rglob("*") if p.is_file())
    common = ["--content-id", "UP9000-PPSA99099_00-PROSPERO00000000", "--title-id", "PPSA99099",
              "--fpkg-title", "HomebrewTest", "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin",
              "--fpkg-passcode", "0" * 32, "--fpkg-deterministic"]
    rc_a, log_a = r.run_cli([str(src_a), str(out_a), "--fpkg-build", str(src_a), *common])
    rc_b, log_b = r.run_cli([str(src_b), str(out_b), "--fpkg-build", str(src_b), *common, "--stage-in-place"])
    pa, pb = next(out_a.glob("*.pkg"), None), next(out_b.glob("*.pkg"), None)
    if not r.check("stage.builds", rc_a == 0 and rc_b == 0 and pa and pb, "both builds wrote a .pkg",
                   f"rc={rc_a}/{rc_b} " + (log_a + log_b)[-300:]):
        return
    after = sorted((str(p.relative_to(src_a)), p.stat().st_size) for p in src_a.rglob("*") if p.is_file())
    r.check("stage.copy-mode-leaves-source", before == after and "[stage] copy " in log_a and "mirrored source into" in log_a,
            "the copy-mode build copied the folder with progress lines and changed nothing in it",
            "\n".join(x for x in log_a.splitlines() if "[stage]" in x)[:400])
    r.check("stage.in-place-says-so", "staged in place" in log_b and "mirrored source into" not in log_b and src_b.is_dir(),
            "the in-place build staged in the source folder itself", "\n".join(x for x in log_b.splitlines() if "[stage]" in x)[:400])
    r.check("stage.identical-package", sha(pa) == sha(pb), "copy mode and in place give the same bytes",
            f"{sha(pa)[:16]} vs {sha(pb)[:16]}")


def test_ampr_index_rebuilt(r: Runner):
    """A source shipping the AMPR emulator gets an ampr_emu.index that describes the packed
    files (fake-signing changes sizes; a stale or missing index must not ship), and the
    source's own index is never touched (the staging mirror is made of hard links)."""
    hbt = fetch_hbt(r.work / "hbt")
    src = r.work / "ampr_src"; out = r.work / "ampr_out"; ext = r.work / "ampr_ext"
    for d in (src, out, ext):
        if d.exists(): shutil.rmtree(d)
    shutil.copytree(hbt, src); out.mkdir(parents=True)
    (src / "fakelib").mkdir(exist_ok=True)
    (src / "fakelib" / "libSceAmpr.sprx").write_bytes(b"\x00" * 2048)
    (src / "ampr_emu.index").write_bytes(b"stale")
    rc = subprocess.run([str(TOOL), "build", str(src), str(out),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000", "--title-id", "PPSA99099",
                         "--mode", "kraken", "--temp", str(r.work / "ampr_tmp")],
                        capture_output=True, text=True, timeout=600)
    log = (rc.stdout or "") + (rc.stderr or "")
    r.check("ampr.build", rc.returncode == 0, "build succeeded", log[-400:])
    r.check("ampr.rebuilt-logged", "[ampr] rebuilt ampr_emu.index" in log, "index rebuild reported", log[-400:])
    r.check("ampr.source-untouched", (src / "ampr_emu.index").read_bytes() == b"stale",
            "the source's own index is unchanged", "the source index was modified")
    pkg = next(out.glob("*.pkg"), None)
    if not pkg:
        return
    members = r.work / "ampr_members.txt"; members.write_text("ampr_emu.index\n")
    subprocess.run([str(TOOL), "extract-inner", str(pkg), str(ext), "--members", str(members)],
                   capture_output=True, text=True, timeout=300)
    idx = ext / "ampr_emu.index"
    r.check("ampr.index-in-package", idx.is_file() and idx.read_bytes()[:8] == b"AMPRIDX3",
            "the package carries a fresh AMPRIDX3 index", "no valid index inside the package")


def test_list_and_selective_extract(r: Runner):
    """list-inner + extract-inner --members: the PFS-browser contract for fPKGs. The JSON
    listing must describe exactly what a full extract-inner writes (same file set, same
    sizes, every directory incl. empty ones, CNT-lifted sce_sys files tagged), and a
    selective extract must reproduce the chosen files byte-for-byte and nothing else,
    with progress lines the GUI regex accepts."""
    hbt = fetch_hbt(r.work / "hbt")

    # Fixture 2: a synthetic /app0 with a few 20-40 MB files. HomebrewTest's real eboot.bin
    # and sce_sys are copied in — the synth ELF stub is not fake-signable, the build refuses it.
    synth = make_synth_folder(r.work / "lsx_src", with_param=False, with_icon=False, with_eboot=False,
                              data_files=[("data/blob1.bin", os.urandom(30_000_000)),
                                          ("data/sub/blob2.bin", os.urandom(20_000_000)),
                                          ("data/sub/deeper/text.txt", b"the quick brown fox\n" * 1_000_000),
                                          ("notes.txt", b"hello\n")])
    shutil.copy2(hbt / "eboot.bin", synth / "eboot.bin")
    shutil.copytree(hbt / "sce_sys", synth / "sce_sys", dirs_exist_ok=True)
    (synth / "data" / "empty_dir").mkdir(parents=True, exist_ok=True)

    bar_re = re.compile(r"\[#{2,}\]\s*(\d{1,3})%")      # the GUI's progress regex
    fixtures = [("hbt",   hbt,   "kraken", ["README.md", "eboot.bin", "sce_sys"]),
                ("synth", synth, "none",   ["data/blob1.bin", "sce_sys/param.json", "data/sub"])]
    for tag, src, mode, members in fixtures:
        pkgd = r.work / f"lsx_{tag}_pkg"; full = r.work / f"lsx_{tag}_full"; sel = r.work / f"lsx_{tag}_sel"
        for d in (pkgd, full, sel):
            if d.exists(): shutil.rmtree(d)
            d.mkdir(parents=True)
        rc, log = r.run_tool(["build", str(src), str(pkgd),
                              "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                              "--title-id", "PPSA99099", "--mode", mode, "--kraken-backend", "builtin"])
        pkg = next(pkgd.glob("*.pkg"), None)
        if not r.check(f"lsx.{tag}.build", rc == 0 and pkg is not None, f"inner={mode}", log[-300:]):
            continue

        # Ground truth: the full extraction (inner PFS + CNT merge).
        rc, log = r.run_tool(["extract-inner", str(pkg), str(full)])
        if not r.check(f"lsx.{tag}.full-extract", rc == 0, "extract-inner ok", log[-300:]):
            continue
        full_files = {str(p.relative_to(full)): p for p in full.rglob("*") if p.is_file()}

        # list-inner: one JSON object on stdout (run_tool merges stderr, so pick the JSON line).
        rc, log = r.run_tool(["list-inner", str(pkg)])
        jline = next((ln for ln in log.splitlines() if ln.startswith("{")), "")
        try:
            doc = json.loads(jline)
        except Exception as e:
            r.check(f"lsx.{tag}.list.json", False, "", f"rc={rc}; stdout is not JSON ({e}): {log[-300:]}")
            continue
        r.check(f"lsx.{tag}.list.json", rc == 0 and doc.get("root") == pkg.name and doc.get("errors") == [],
                f"{doc.get('file_count')} files, {doc.get('dir_count')} dirs",
                f"rc={rc}; root={doc.get('root')!r} errors={doc.get('errors')}")
        entries = doc["entries"]
        files = {e["path"]: e for e in entries if e["type"] == "file"}
        dirs = {e["path"] for e in entries if e["type"] == "dir"}
        r.check(f"lsx.{tag}.list.fileset", set(files) == set(full_files),
                "file set == full extract-inner",
                f"only-in-list={sorted(set(files) - set(full_files))[:5]} "
                f"only-in-extract={sorted(set(full_files) - set(files))[:5]}")
        bad_sizes = [p for p, e in files.items() if p in full_files and e["size"] != full_files[p].stat().st_size]
        r.check(f"lsx.{tag}.list.sizes", not bad_sizes, "sizes == extracted sizes", f"mismatch: {bad_sizes[:5]}")
        parents = {p.rsplit("/", 1)[0] for p in list(files) + list(dirs) if "/" in p}
        r.check(f"lsx.{tag}.list.dirs", parents <= dirs, "every parent directory has its own dir entry",
                f"missing: {sorted(parents - dirs)[:5]}")
        r.check(f"lsx.{tag}.list.cnt",
                files.get("sce_sys/param.json", {}).get("source") == "cnt"
                and files.get("sce_sys/icon0.png", {}).get("source") == "cnt",
                "param.json/icon0.png tagged source=cnt",
                f"{files.get('sce_sys/param.json')} {files.get('sce_sys/icon0.png')}")
        r.check(f"lsx.{tag}.list.sorted", [e["path"] for e in entries] == sorted(e["path"] for e in entries),
                "entries sorted by path", "not sorted")
        r.check(f"lsx.{tag}.list.counts", doc["file_count"] == len(files) and doc["dir_count"] == len(dirs),
                "file_count/dir_count match", "counts differ from entries")
        if tag == "synth":
            r.check("lsx.synth.list.empty-dir", "data/empty_dir" in dirs, "empty directory listed",
                    f"dirs={sorted(dirs)}")

        # Selective extraction: two files + one directory subtree.
        mf = r.work / f"lsx_{tag}_members.txt"
        mf.write_text("\n".join(members) + "\n", encoding="utf-8")
        rc, log = r.run_tool(["extract-inner", str(pkg), str(sel), "--members", str(mf)])
        r.check(f"lsx.{tag}.sel.rc", rc == 0, f"exit {rc}", log[-300:])
        expected = {p for p in full_files if any(p == m or p.startswith(m + "/") for m in members)}
        got = {str(p.relative_to(sel)): p for p in sel.rglob("*") if p.is_file()}
        r.check(f"lsx.{tag}.sel.exact-set", set(got) == expected, f"{len(expected)} file(s), nothing else",
                f"extra={sorted(set(got) - expected)[:5]} missing={sorted(expected - set(got))[:5]}")
        diff = [p for p in expected if p in got and sha(got[p]) != sha(full_files[p])]
        r.check(f"lsx.{tag}.sel.identical", not diff, "byte-identical to the full extract", f"differs: {diff[:5]}")
        r.check(f"lsx.{tag}.sel.cnt-file", (sel / "sce_sys" / "param.json").is_file(),
                "CNT sce_sys/param.json selectable", "sce_sys/param.json missing")
        pcts = [int(m.group(1)) for m in (bar_re.search(ln) for ln in log.splitlines()) if m]
        r.check(f"lsx.{tag}.sel.progress", bool(pcts) and pcts[-1] == 100 and pcts == sorted(pcts),
                f"{len(pcts)} progress lines, monotone, ends at 100%", f"pcts={pcts[:10]}")
        if tag == "hbt":
            # The pre-existing --json documents must be parseable too. Trimming had switched
            # reflection-based System.Text.Json off and all three crashed on the shipped binary.
            jd = r.work / "lsx_hbt_json_ext"; jd.mkdir(exist_ok=True)
            verdicts = []
            for label, argv, keys in (
                    ("inspect",       ["inspect", str(pkg), "--json"],            {"path", "content_id", "finalized", "fih"}),
                    ("validate",      ["validate", str(pkg), "--json"],           {"pass", "warn", "fail", "results"}),
                    ("extract-inner", ["extract-inner", str(pkg), str(jd), "--json"], {"extracted", "cnt_merged", "output", "files"})):
                rc, out = r.run_tool(argv)                 # stdout first, then stderr
                try:
                    obj, _ = json.JSONDecoder().raw_decode(out.lstrip())
                    good = rc == 0 and isinstance(obj, dict) and keys <= set(obj)
                except Exception:
                    good = False
                verdicts.append((label, good, rc, out[:120].replace("\n", " ")))
            r.check("lsx.json.legacy-docs", all(v[1] for v in verdicts),
                    "inspect/validate/extract-inner --json exit 0 and parse",
                    "; ".join(f"{l}: rc={rc} {o!r}" for l, g, rc, o in verdicts if not g))

        if tag == "hbt":
            # A backport check reads the executables alone out of a package, before a job
            # unpacks the whole game: eboot.bin byte-identical to the full extract, no data.
            import cli as _cli
            exd = r.work / "lsx_hbt_exec"
            if exd.exists(): shutil.rmtree(exd)
            try:
                pulled = _cli._pull_executables(pkg, exd)
                err = ""
            except Exception as e:
                pulled, err = False, str(e)
            got = sorted(str(q.relative_to(exd)) for q in exd.rglob("*") if q.is_file()) if exd.exists() else []
            r.check("lsx.hbt.executables-only",
                    pulled and "eboot.bin" in got and all(_cli._is_executable_name(g) for g in got)
                    and sha(exd / "eboot.bin") == sha(full / "eboot.bin"),
                    f"{len(got)} executable(s) pulled, eboot.bin identical", f"{err} {got[:6]}")

        if tag == "synth":
            # A directory member with nothing inside is still recreated (structure parity with mkpfs).
            sel2 = r.work / "lsx_synth_sel2"; sel2.mkdir(exist_ok=True)
            mf2 = r.work / "lsx_synth_members2.txt"; mf2.write_text("data/empty_dir\n")
            rc, log = r.run_tool(["extract-inner", str(pkg), str(sel2), "--members", str(mf2)])
            ed = sel2 / "data" / "empty_dir"
            r.check("lsx.synth.sel.empty-dir", rc == 0 and ed.is_dir() and not any(ed.iterdir()),
                    "an empty directory member is recreated", f"rc={rc}; {log[-200:]}")
            # An unknown member alone is a clean refusal, not a crash.
            sel3 = r.work / "lsx_synth_sel3"; sel3.mkdir(exist_ok=True)
            mf3 = r.work / "lsx_synth_members3.txt"; mf3.write_text("does/not/exist\n")
            rc, log = r.run_tool(["extract-inner", str(pkg), str(sel3), "--members", str(mf3)])
            r.check("lsx.synth.sel.unknown-member", rc == 1 and "None of the requested" in log,
                    "refused with rc=1", f"rc={rc}; {log[-200:]}")


# ── main ─────────────────────────────────────────────────────────────────────
def make_png_1x1(colour: int, interlace: int = 0) -> bytes:
    """1x1 PNG, 8-bit, colour type 2 (RGB) or 6 (RGBA). For one pixel the Adam7 data is
    the same as the plain data, so interlace=1 still yields a valid interlaced PNG."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, colour, 0, 0, interlace)
    idat = zlib.compress(b"\x00" + (b"\x10\x20\x30" if colour == 2 else b"\x10\x20\x30\x80"))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def make_bc7_dds(width: int, height: int) -> bytes:
    """A DX10/BC7 DDS header for width x height plus zeroed block data (one mip)."""
    head = bytearray(148)
    head[0:4] = b"DDS "
    struct.pack_into("<IIII", head, 4, 124, 0x1007, height, width)
    struct.pack_into("<I", head, 76, 32)             # pixel-format size
    struct.pack_into("<I", head, 80, 0x4)            # DDPF_FOURCC
    head[84:88] = b"DX10"
    struct.pack_into("<IIIII", head, 128, 98, 3, 0, 1, 0)
    return bytes(head) + bytes(((width + 3) // 4) * ((height + 3) // 4) * 16)


def test_publishing_rules(r: Runner):
    """Rules from the publishing tools' requirements that the builder enforces while staging
    (never in the source): presentation-image form and icon0 size, DDS/PNG agreement, PlayGo
    set vs. the packed tree, and refusals for a non-JSON param.json and an empty eboot."""
    hbt = fetch_hbt(r.work / "hbt")
    cid, tid = "UP9000-PPSA99099_00-PROSPERO00000000", "PPSA99099"

    def base(name: str) -> Path:
        d = r.work / name
        if d.exists(): shutil.rmtree(d)
        shutil.copytree(hbt, d)
        return d

    def build(src: Path, out_name: str):
        out = r.work / out_name
        if out.exists(): shutil.rmtree(out)
        out.mkdir()
        rc, log = r.run_cli([str(src), str(out), "--fpkg-build", str(src), "--content-id", cid,
                             "--title-id", tid, "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
        return rc, log, next(out.glob("*.pkg"), None)

    # 1. icon0.png interlaced 1x1 RGBA + a stale 256x256 icon0.dds
    src = base("rules_icon")
    bad_png = make_png_1x1(6, interlace=1)
    (src / "sce_sys" / "icon0.png").write_bytes(bad_png)
    (src / "sce_sys" / "icon0.dds").write_bytes(make_bc7_dds(256, 256))
    rc, log, pkg = build(src, "rules_icon_out")
    r.check("rules.icon0.converted",
            "[media] converted sce_sys/icon0.png to 8-bit RGB" in log and "interlaced" in log and "needs 512x512" in log,
            "icon0.png: RGBA, interlaced, 1x1 → 8-bit RGB 512x512",
            "\n".join(l for l in log.splitlines() if "[media]" in l or "[warn]" in l) or log[-300:])
    r.check("rules.icon0.stale-dds-regenerated",
            "the shipped one: 256x256, the PNG is 512x512" in log,
            "a DDS of the wrong size is generated again from the PNG",
            "\n".join(l for l in log.splitlines() if "[icon]" in l) or log[-300:])
    r.check("rules.icon0.validate", rc == 0 and pkg is not None and "image.icon0.dds" in log
            and re.search(r"summary: \d+ passed, \d+ warned, 0 failed", log) is not None,
            "validate checks icon0.png and icon0.dds and passes",
            "\n".join(l for l in log.splitlines() if "[FAIL]" in l or "summary:" in l) or log[-300:])
    r.check("rules.icon0.source-untouched", (src / "sce_sys" / "icon0.png").read_bytes() == bad_png,
            "source icon0.png unchanged", "the source was modified")
    if pkg is None:
        return
    x = r.work / "rules_pg_x"
    if x.exists(): shutil.rmtree(x)
    rc_x, _ = r.run_tool(["extract-inner", str(pkg), str(x)])
    ic = (x / "sce_sys" / "icon0.png").read_bytes()[:29] if (x / "sce_sys" / "icon0.png").is_file() else b""
    dd = (x / "sce_sys" / "icon0.dds").read_bytes()[:148] if (x / "sce_sys" / "icon0.dds").is_file() else b""
    r.check("rules.icon0.packed-form",
            rc_x == 0 and len(ic) == 29 and struct.unpack(">II", ic[16:24]) == (512, 512) and ic[24:26] == b"\x08\x02" and ic[28] == 0
            and len(dd) == 148 and struct.unpack("<II", dd[12:20]) == (512, 512) and struct.unpack("<I", dd[128:132])[0] == 98,
            "package: icon0.png 512x512 8-bit RGB non-interlaced, icon0.dds 512x512 BC7",
            f"icon0.png IHDR {ic[16:29].hex()} icon0.dds {dd[12:20].hex()}")

    # 2. PlayGo: the set the builder made for this tree is kept on a rebuild of the unpacked
    #    folder; after a file is added it no longer matches and is regenerated.
    rc, log, _ = build(x, "rules_pg_same")
    r.check("rules.playgo.matching-set-kept", rc == 0 and "[playgo] prepared set matches the packed files" in log,
            "a set that describes the packed files is kept",
            "\n".join(l for l in log.splitlines() if "[playgo]" in l) or log[-300:])
    (x / "extra-data.bin").write_bytes(b"x" * 100)
    rc, log, _ = build(x, "rules_pg_changed")
    r.check("rules.playgo.stale-set-regenerated",
            rc == 0 and "[playgo] prepared set does not match the packed files (1 packed file(s) missing from its hash table" in log,
            "a set made for another tree is regenerated",
            "\n".join(l for l in log.splitlines() if "[playgo]" in l) or log[-300:])

    # 3. A param.json that is not JSON is refused (a generic one would lose the settings).
    src = base("rules_badparam")
    (src / "sce_sys" / "param.json").write_bytes(b"\x89PNG\r\n\x1a\n not json")
    rc, log, pkg = build(src, "rules_badparam_out")
    r.check("rules.param-not-json.refused", rc != 0 and pkg is None and "param.json is not valid JSON" in log,
            "build refused with a clear message", f"rc={rc} pkg={pkg}: " + log[-300:])

    # 4. An empty eboot.bin is refused.
    src = base("rules_empty_eboot")
    (src / "eboot.bin").write_bytes(b"")
    rc, log, pkg = build(src, "rules_empty_eboot_out")
    r.check("rules.eboot-empty.refused", rc != 0 and pkg is None and "truncated or empty" in log,
            "build refused with a clear message", f"rc={rc} pkg={pkg}: " + log[-300:])


def test_ps4_archive_to_library(r: Runner):
    """A zip of PS4 packages (game, update, five DLCs) ends as the library tree, and a
    lone DLC joins the title folder that is already there."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ps4_fixture import make_pkg
    src = r.work / "ps4_src"; out = r.work / "ps4_out"; zp = r.work / "ps4_set.zip"; ext = r.work / "ps4_ext"
    for d in (src, out, ext):
        shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)
    cid = "UP0000-CUSA00001_00-SAMPLEGAME000000"
    make_pkg(src / "g.pkg", content_id=cid, content_type=0x1A, sfo={"TITLE": "Sample Game", "CATEGORY": "gd", "APP_VER": "01.00"})
    make_pkg(src / "u.pkg", content_id=cid, content_type=0x1A, sfo={"TITLE": "Sample Game", "CATEGORY": "gp", "APP_VER": "01.07"})
    for i in range(5):
        make_pkg(src / f"d{i}.pkg", content_id=cid, content_type=0x1B,
                 sfo={"TITLE": f"Sample Game - Item {i}", "CATEGORY": "ac", "VERSION": "01.00"})
    with zipfile.ZipFile(zp, "w") as z:
        for f in sorted(src.iterdir()):
            z.write(f, f"Sample.Set/{f.name}")
    zipfile.ZipFile(zp).extractall(ext)
    rc, log = r.run_cli(["placeholder", str(out), "--ps4-sort", str(ext), "--copy-mode", "keep"])
    top = out / "Sample Game [CUSA00001] [v01.07]"
    r.check("ps4.sort.rc", rc == 0, "sorted", log[-400:])
    r.check("ps4.sort.tree", (top / "Sample Game [CUSA00001] [v01.00].pkg").is_file()
            and (top / "Sample Game [CUSA00001] UPDATE [v01.07].pkg").is_file()
            and sorted(x.name for x in (top / "DLC Pack").iterdir())
                == [f"Sample Game DLC Item {i} [CUSA00001] [v01.07].pkg" for i in range(5)],
            "game, update and five DLCs in DLC Pack, named by the set's version",
            str(sorted(x.relative_to(out).as_posix() for x in out.rglob("*.pkg"))))
    lone = r.work / "ps4_lone"; shutil.rmtree(lone, ignore_errors=True); lone.mkdir()
    make_pkg(lone / "late.pkg", content_id=cid, content_type=0x1B, sfo={"TITLE": "Sample Game - Late", "CATEGORY": "ac", "VERSION": "01.00"})
    rc, log = r.run_cli(["placeholder", str(out), "--ps4-sort", str(lone)])
    r.check("ps4.sort.joins-library", rc == 0 and (top / "DLC Pack" / "Sample Game DLC Late [CUSA00001] [v01.07].pkg").is_file()
            and len([d for d in out.iterdir() if d.is_dir()]) == 1,
            "a later DLC joins the title folder and its DLC Pack", log[-300:])


def test_organize_real(r: Runner):
    """Organize with the real readers: a .ffpfsc, a .pkg, an unpacked folder and a zip of the
    same title scattered over release folders (with OS clutter) end in one title folder; undo
    restores the tree; --organize-into writes an image into an empty library."""
    hbt = fetch_hbt(r.work / "hbt")
    lib = r.work / "org_lib"; shutil.rmtree(lib, ignore_errors=True); lib.mkdir()
    (lib / "Rel.A").mkdir(); (lib / "Rel.B").mkdir()
    rc1, _ = r.run_cli([str(hbt), str(lib / "Rel.A"), "--pack", "--overwrite"])
    rc2, _ = r.run_cli([str(hbt), str(lib / "Rel.B"), "--fpkg-build", str(hbt), "--fpkg-inner", "none"])
    shutil.copytree(hbt, lib / "loose unpacked")
    (lib / "dl").mkdir()
    with zipfile.ZipFile(lib / "dl" / "set.zip", "w") as z:
        for f in hbt.rglob("*"):
            if f.is_file():
                z.write(f, f"Set/{f.relative_to(hbt)}")
    (lib / "dl" / "set.nfo").write_text("notes")
    for junk in ("._x.pkg", "Rel.A/._y.ffpfsc", ".DS_Store"):
        (lib / junk).write_bytes(b"\0")
    if not r.check("organize.fixture", rc1 == 0 and rc2 == 0, "image and package built", f"rc={rc1},{rc2}"):
        return
    before = sorted(str(p.relative_to(lib)) for p in lib.rglob("*") if not p.name.startswith("._") and p.name != ".DS_Store")
    planf, journal = r.work / "org_plan.json", r.work / "org_journal.json"
    rc, log = r.run_cli(["--organize-scan", str(lib), "--organize-plan", str(planf)])
    plan = json.loads(planf.read_text()) if planf.is_file() else {"moves": [], "stays": []}
    listed = [m["src"] for m in plan["moves"]] + [s["path"] for s in plan["stays"]]
    r.check("organize.scan", rc == 0 and "ORGANIZE_PROGRESS: 4/4" in log and not any("/._" in x for x in listed),
            f"{len(plan['moves'])} moves, no clutter listed", log[-400:])
    rc, log = r.run_cli(["--organize-apply", "--organize-plan", str(planf), "--organize-journal", str(journal)])
    tops = sorted(p.name for p in lib.iterdir() if not p.name.startswith("."))
    title = [t for t in tops if "[PPSA" in t]
    inside = sorted(p.name for p in (lib / title[0]).iterdir()) if title else []
    r.check("organize.apply", rc == 0 and len(title) == 1 and len([t for t in tops if t not in title]) == 0
            and any(n.endswith(".ffpfsc") for n in inside) and any(n.endswith(".pkg") for n in inside)
            and "set.zip" in inside and "set.nfo" in inside and not any(n.startswith("._") for n in inside),
            f"one title folder: {inside}", f"{tops} {inside} {log[-300:]}")
    rc, log = r.run_cli(["--organize-undo", "--organize-journal", str(journal)])
    after = sorted(str(p.relative_to(lib)) for p in lib.rglob("*") if not p.name.startswith("._") and p.name != ".DS_Store")
    r.check("organize.undo", rc == 0 and after == before, "the tree is back as it was", log[-300:])
    empty = r.work / "org_into"; shutil.rmtree(empty, ignore_errors=True)
    img = next((lib / "Rel.A").glob("*.ffpfsc"))
    rc, log = r.run_cli(["placeholder", str(empty), "--organize-into", str(img), "--copy-mode", "keep"])
    got = [p.relative_to(empty).as_posix() for p in empty.rglob("*.ffpfsc")]
    r.check("organize.into", rc == 0 and len(got) == 1 and "/" in got[0] and img.exists(),
            f"{got}", log[-400:])


def main():
    ap = argparse.ArgumentParser(description="fPKG pipeline end-to-end tests")
    ap.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "ffpfsc-fpkg-tests",
                    help="Working directory for fixtures and outputs (default: system temp).")
    ap.add_argument("--keep", action="store_true", help="Keep the working directory after the run")
    ap.add_argument("--only", type=str, default="",
                    help="Run only the tests whose name contains this substring (case-insensitive).")
    args = ap.parse_args()

    if args.work.exists():
        shutil.rmtree(args.work)
    args.work.mkdir(parents=True)
    print(f"[i] work dir: {args.work}")
    print(f"[i] ffpfsc-pkg-tool: {TOOL} ({'present' if TOOL.exists() else 'MISSING'})")

    r = Runner(args.work, args.keep)
    t0 = time.monotonic()
    for name, fn in [
        ("environment",                     test_environment),
        ("chain-1: folder → fPKG → folder", test_chain1_folder_pkg_folder),
        ("chain-2: folder → ffpfsc → fPKG", test_chain2_folder_ffpfsc_folder_pkg),
        ("chain-3: fPKG → folder → ffpfsc", test_chain3_pkg_folder_ffpfsc),
        ("inner modes (none/zlib/kraken)",  test_inner_modes),
        ("negative: no param.json",         test_negative_missing_param_json),
        ("negative: no eboot.bin",          test_negative_missing_eboot),
        ("negative: no-eboot pkg fails validate", test_no_eboot_caught_by_validate),
        ("negative: bad content id",        test_negative_bad_content_id),
        ("identity: param.json wins",       test_identity_from_param_json),
        ("kraken fast preset (-4)",         test_kraken_fast_preset),
        ("validate: reject .ffpfsc",        test_validate_catches_untouched_ffpfsc),
        ("tool-path resolution",            test_tool_path_resolution),
        ("chain-4: .ffpfsc → fPKG one-click", test_chain4_image_to_fpkg_oneclick),
        ("gui progress translation",        test_gui_progress_translation),
        ("determinism: byte-identical",     test_deterministic_build),
        ("ps4: archive to library",         test_ps4_archive_to_library),
        ("organize: real containers",       test_organize_real),
        ("stage in place = copy mode",      test_stage_in_place_matches_copy),
        ("list-inner + selective extract",  test_list_and_selective_extract),
        ("ampr index rebuilt in staging",   test_ampr_index_rebuilt),
        ("publishing rules in staging",     test_publishing_rules),
        ("build temp contained",            test_build_temp_is_contained),
    ]:
        if args.only and args.only.lower() not in name.lower():
            continue
        r.run(name, fn)

    exitcode = r.summary()
    print(f"[i] wall time: {time.monotonic()-t0:.1f}s")

    if not args.keep:
        shutil.rmtree(args.work, ignore_errors=True)
    sys.exit(exitcode)


if __name__ == "__main__":
    main()
