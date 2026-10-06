"""
Thin Python wrapper around the bundled `ffpfsc-pkg-tool` native binary.

The binary is a self-contained .NET 9 build of drakmor's LibProsperoPkg 1.2.0
(GPL-3-or-later, sourced from the a53-fpkg 0.5 release). It exposes fPKG
inspect / extract / build without a .NET runtime install and without loading
the Sony `libScePubTools.dll`.

Kraken compression uses LibProsperoPkg's own managed encoder ("BuiltIn"
backend). On-console acceptance is only truly proven for packages built with
Sony's Publishing Tools DLL; the built-in encoder ships format-compatible
Kraken blocks and drakmor's own extractor round-trips them, but the console
verdict is up to the user's PS5 install test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

# Located in backend/native/ next to this module.
_TOOL_NAME = "ffpfsc-pkg-tool"


def _tool_search_paths() -> list[Path]:
    """Where the CLI binary might live. Order matters: first hit wins."""
    here = Path(__file__).resolve().parent
    frozen_dir = getattr(sys, "_MEIPASS", None)
    paths: list[Path] = []
    # Regular dev / source-run layout (backend/native/ffpfsc-pkg-tool).
    paths.append(here / "native" / _TOOL_NAME)
    # PyInstaller-bundled layout (adjacent copy under the frozen backend folder).
    if frozen_dir:
        paths.append(Path(frozen_dir) / "backend" / "native" / _TOOL_NAME)
        paths.append(Path(frozen_dir) / "native" / _TOOL_NAME)
    # Allow an env-var override for developer overrides.
    env = os.environ.get("FFPFSC_PKG_TOOL")
    if env:
        paths.insert(0, Path(env))
    return paths


def tool_path() -> Path:
    """Locate the bundled ffpfsc-pkg-tool binary, or raise FileNotFoundError."""
    for p in _tool_search_paths():
        if p.is_file() and os.access(p, os.X_OK):
            return p
    tried = "\n  ".join(str(p) for p in _tool_search_paths())
    raise FileNotFoundError(
        f"ffpfsc-pkg-tool not found. Tried:\n  {tried}\n"
        "In a source checkout, build it with ./BUILD_PKG_TOOL.sh (needs the .NET SDK 9+); "
        "set FFPFSC_PKG_TOOL to override, or rebuild the app."
    )


def is_available() -> bool:
    try:
        tool_path()
        return True
    except FileNotFoundError:
        return False


def _run(argv: list[str], *, on_line=None, on_start=None) -> int:
    """Run the CLI, streaming its combined output through *on_line* (or stdout).
    *on_start(proc)* gets the running process (a progress meter reads its I/O)."""
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        # The tool echoes file names from the package; a byte that is not valid UTF-8
        # must not turn into a UnicodeDecodeError after the build has already run.
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    if on_start is not None:
        on_start(proc)
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if on_line is not None:
                on_line(line)
            else:
                print(line, flush=True)
    finally:
        proc.wait()
        proc.stdout.close()
    return proc.returncode


def version() -> str:
    """Return the CLI version banner, or an error string."""
    argv = [str(tool_path()), "version"]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout
    except Exception as e:
        return f"[fpkg tool unavailable: {e}]"


def inspect(pkg: Path, *, json_out: bool = False) -> int:
    argv = [str(tool_path()), "inspect", str(pkg)]
    if json_out:
        argv.append("--json")
    return _run(argv)


def extract(pkg: Path, out_dir: Path,
            *, passcode: str = "0" * 32,
            outer: bool = False,
            on_line=None,
            on_start=None) -> int:
    """
    Extract a finalized fPKG (\\x7FFIH) or metadata CNT (\\x7FCNT).

    outer=False (default) pulls the /app0-style inner-image content files.
    outer=True dumps the outer-PFS entries (uroot, pfs_image.dat itself, naps).
    """
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    cmd = "extract-outer" if outer else "extract-inner"
    argv = [str(tool_path()), cmd, str(pkg), str(out_dir), "--passcode", passcode]
    return _run(argv, on_line=on_line, on_start=on_start)


def list_inner(pkg: Path, *, passcode: str = "0" * 32) -> dict:
    """
    Directory tree of a package's inner image — plus the sce_sys metadata the CNT
    carries — WITHOUT decoding the whole image (the tool reads only the blocks it
    touches; a 240 MB package lists in ~0.1 s reading ~1.5 MiB).

    Returns the tool's JSON: {"root", "entries": [{"path", "type", "size", "source"}],
    "file_count", "dir_count", "errors"} — the same shape the PFS browser uses for
    .ffpfs/.ffpfsc listings. Raises RuntimeError with the tool's last message on failure.
    """
    argv = [str(tool_path()), "list-inner", str(pkg), "--passcode", passcode]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f"list-inner failed (rc={r.returncode})")
    return json.loads(r.stdout)


def extract_members(pkg: Path, out_dir: Path, members_file: Path,
                    *, passcode: str = "0" * 32, on_line=None) -> int:
    """
    Extract only the members listed in *members_file* (one inner path per line; a
    directory means its whole subtree) — decoded block-wise, never the whole image.
    Progress lines '[####] NN% extract (path)' stream through *on_line*.
    """
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    argv = [str(tool_path()), "extract-inner", str(pkg), str(out_dir),
            "--members", str(members_file), "--passcode", passcode]
    return _run(argv, on_line=on_line)


def build(src_dir: Path, out_dir: Path,
          *,
          content_id: str,
          title_id: str,
          title: str = "",
          version: str = "01.000.000",
          passcode: str = "0" * 32,
          inner_mode: str = "kraken",           # "none" | "zlib" | "kraken"
          kraken_backend: str = "builtin",      # "automatic" | "builtin" | "publishingtools" | "uncompressed"
          publishing_tools_dll: Optional[str] = None,
          deterministic: bool = False,
          temp_dir: Optional[str] = None,
          level: Optional[int] = None,
          retail_normalize: bool = True,
          hdr_flag: str = "auto",               # "auto" | "on" | "off" (bool accepted: True→on, False→off)
          regen_playgo: bool = False,
          fake_sign: bool = True,
          ampr_index: bool = True,
          stage_in_place: bool = False,
          consume_source: bool = False,
          parallelism: int = 0,
          on_line=None) -> int:
    """
    Build a debug fPKG from a prepared /app0-style source folder.

    - content_id must match XX0000-XXXX00000_00-XXXXXXXXXXXXXXXX (36 chars).
    - title_id must be XXXX00000 (9 chars).
    - Every build Kraken-packs each file individually (raw only when that would not
      shrink it) — that is the native package layout and cannot be switched off.
      inner_mode adds a codec LAYER over the whole inner image on top of that:
      'kraken' = block-level Kraken layer (v1.2.0 path; the configuration verified to
      launch on a retail PS5, default), 'none' = no extra layer (console-untested),
      'zlib' = the legacy whole-inner PFSC layer. Measured on already Kraken-packed
      data the three produce the same size; they differ in structure.
    - kraken_backend 'builtin' uses LibProsperoPkg's own managed encoder (no external DLL)
      and is the only backend whose output launches on a console. 'uncompressed' and
      'automatic' (stored blocks) install but fail to launch with CE-100096-6 (verified).
      'publishingtools' requires Sony's libScePubTools.dll at the given path AND
      64-bit Windows: on any other OS LibProsperoPkg throws ("the Reduced Oodle backend
      requires 64-bit Windows") and the build fails — there is no fallback. Verified
      with the real DLL on macOS.
    - retail_normalize: for a "standard"-DRM source, inject valid license entries, set
      the retail SELF flavour on executables and add Sony-style param.json fields
      (drm_type=16 comes from the patched LibProsperoPkg). Default on.
    - hdr_flag: param.json attribute bit 29 (HDR support). 'auto' (default) keeps what the
      source declares — the publisher's intent; a console on "HDR when supported" switches
      to HDR output for the title only when the bit is set. 'on' sets it, 'off' clears it.
    - regen_playgo: discard the source's sce_sys/playgo-*.dat even when they look valid.
      A CORRUPT set (wrong on-wire format — some containers ship these files with
      swapped contents) is always discarded and regenerated; that alone turned an
      "installs but will not start" package into a launching one.
    - fake_sign: fake-sign raw ELFs found in the source (idempotent). Default on.
    - ampr_index: rebuild ampr_emu.index over the packed files when the source ships the
      AMPR emulator (fakelib/libSceAmpr.sprx). Default on.
    - temp_dir: where LibProsperoPkg stages the inner image / CNT / outer image
      (defaults to $TMPDIR). Pass the app's fast temp drive for big games.
    - level: Kraken preset. Measured: 0..9 give byte-identical output (the encoder's
      'normal' regime); -4..-1 select the faster, slightly weaker preset. The GUI maps
      normal → 7 and fast → -4.
    - parallelism: Kraken (and outer-PFS) workers; 0 = the tool's default, one per core.
      Deterministic: any worker count gives the same bytes (measured 4/8/12 vs 1).
    """
    src_dir = Path(src_dir); out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        str(tool_path()), "build", str(src_dir), str(out_dir),
        "--content-id", content_id,
        "--title-id", title_id,
        "--version", version,
        "--passcode", passcode,
        "--mode", inner_mode,
        "--kraken-backend", kraken_backend,
    ]
    if title:
        argv += ["--title", title]
    if publishing_tools_dll:
        argv += ["--pubtools-dll", publishing_tools_dll]
    if deterministic:
        argv += ["--deterministic"]
    if temp_dir:
        argv += ["--temp", str(temp_dir)]
    if level is not None:
        argv += ["--level", str(int(level))]
    if not retail_normalize:
        argv += ["--no-retail-normalize"]
    if isinstance(hdr_flag, bool):
        hdr_flag = "on" if hdr_flag else "off"
    if hdr_flag in ("on", "off"):
        argv += ["--hdr-flag", hdr_flag]
    if regen_playgo:
        argv += ["--regen-playgo"]
    if not ampr_index:
        argv += ["--no-ampr-index"]
    if not fake_sign:
        argv += ["--no-fake-sign"]
    if stage_in_place:
        # src_dir is this app's own working copy (an image unpacked into its scratch): the
        # tool's pre-build changes go straight into it instead of into one more copy.
        argv += ["--stage-in-place"]
    if consume_source and stage_in_place:
        # each source file is deleted the moment the inner image holds it, so the unpacked
        # game shrinks while the image grows (only ever in our own working copy)
        argv += ["--consume-source"]
    if parallelism and int(parallelism) > 0:
        argv += ["--parallelism", str(int(parallelism))]
    return _run(argv, on_line=on_line)


def validate(pkg: Path, *, json_out: bool = False, on_line=None) -> int:
    """
    Run the CLI's diagnostic checklist against a package. Prints a
    pass/warn/fail table. Returns 0 iff no failures.
    """
    argv = [str(tool_path()), "validate", str(pkg)]
    if json_out:
        argv.append("--json")
    return _run(argv, on_line=on_line)
