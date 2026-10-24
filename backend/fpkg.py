from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

_TOOL_NAME = "ffpfsc-pkg-tool.exe" if os.name == "nt" else "ffpfsc-pkg-tool"
# console program: without this Windows flashes a console window in your face
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _tool_search_paths() -> list[Path]:
    """first hit wins"""
    here = Path(__file__).resolve().parent
    frozen_dir = getattr(sys, "_MEIPASS", None)
    paths: list[Path] = []
    paths.append(here / "native" / _TOOL_NAME)
    if frozen_dir:
        paths.append(Path(frozen_dir) / "backend" / "native" / _TOOL_NAME)
        paths.append(Path(frozen_dir) / "native" / _TOOL_NAME)
    env = os.environ.get("FFPFSC_PKG_TOOL")
    if env:
        paths.insert(0, Path(env))
    return paths


def _use_bundled_openssl(tool: Path) -> None:
    """without Homebrew .NET finds /usr/lib/libcrypto and macOS kills the tool, so point dyld at ours"""
    if sys.platform != "darwin" or not (tool.parent / "libssl.3.dylib").is_file():
        return
    here = str(tool.parent)
    cur = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    if here not in cur.split(":"):
        rest = cur or f"{Path.home() / 'lib'}:/usr/local/lib:/usr/lib"   # dyld's own default
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = f"{here}:{rest}"


def tool_path() -> Path:
    for p in _tool_search_paths():
        if p.is_file() and os.access(p, os.X_OK):
            _use_bundled_openssl(p)
            return p
    tried = "\n  ".join(str(p) for p in _tool_search_paths())
    raise FileNotFoundError(
        f"ffpfsc-pkg-tool not found. Tried:\n  {tried}\n"
        "In a source checkout, build it with ./BUILD_PKG_TOOL.sh (BUILD_PKG_TOOL.ps1 on Windows; "
        "needs the .NET SDK 9+); "
        "set FFPFSC_PKG_TOOL to override, or rebuild the app."
    )


def is_available() -> bool:
    try:
        tool_path()
        return True
    except FileNotFoundError:
        return False


def _run(argv: list[str], *, on_line=None, on_start=None) -> int:
    """on_start(proc) gets the running process, a progress meter reads its I/O"""
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        # file names from the package may be invalid UTF-8; don't crash after the build ran
        encoding="utf-8",
        errors="replace",
        **_NO_WINDOW,
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
    argv = [str(tool_path()), "version"]
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                              **_NO_WINDOW).stdout
    except Exception as e:
        return f"[fpkg tool unavailable: {e}]"


def inspect(pkg: Path, *, json_out: bool = False) -> int:
    argv = [str(tool_path()), "inspect", str(pkg)]
    if json_out:
        argv.append("--json")
    return _run(argv)


def inspect_json(pkg: Path) -> dict:
    """header and entry table only; content_type 32 is a game, 33/34 add-ons"""
    r = subprocess.run([str(tool_path()), "inspect", str(pkg), "--json"], capture_output=True, text=True,
                       timeout=120, encoding="utf-8", errors="replace", **_NO_WINDOW)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f"inspect failed (rc={r.returncode})")
    return json.loads(r.stdout)


def extract(pkg: Path, out_dir: Path,
            *, passcode: str = "0" * 32,
            outer: bool = False,
            on_line=None,
            on_start=None) -> int:
    """outer=True dumps the outer PFS entries (uroot, pfs_image.dat, naps) instead of the app files"""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    if not outer and is_ps4(pkg):
        return _run([str(tool_path()), "ps4-extract", str(pkg), str(out_dir)], on_line=on_line, on_start=on_start)
    cmd = "extract-outer" if outer else "extract-inner"
    argv = [str(tool_path()), cmd, str(pkg), str(out_dir), "--passcode", passcode]
    return _run(argv, on_line=on_line, on_start=on_start)


def is_ps4(pkg: Path) -> bool:
    """PS4 (CUSA) packages go through the ps4-* commands, never as a PS5 fPKG"""
    import ps4pkg
    return ps4pkg.is_ps4_package(pkg)


def ps4_keystone(pkg: Path, keystone: Path, *, undo: Optional[Path] = None, on_line=None) -> int:
    """keystone is 96 bytes (e.g. from a save); undo keeps the overwritten bytes for restore"""
    argv = [str(tool_path()), "ps4-keystone", str(pkg), str(keystone)]
    if undo is not None:
        argv += ["--undo", str(undo)]
    return _run(argv, on_line=on_line)


def ps4_keystone_info(pkg: Path) -> Optional[dict]:
    """default means the builder's all-zero passcode keystone; None for updates and add-ons"""
    try:
        r = subprocess.run([str(tool_path()), "ps4-keystone", str(pkg)], capture_output=True, text=True,
                           timeout=60, encoding="utf-8", errors="replace", **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"current fingerprint=([0-9A-F]+) zero_passcode=(true|false)", r.stdout or "")
    if r.returncode != 0 or not m:
        return None
    v = re.search(r" valid=(true|false)", r.stdout or "")
    return {"fingerprint": m.group(1), "default": m.group(2) == "true", "valid": not v or v.group(1) == "true"}


def ps4_rebind(update: Path, base: Path, *, undo: Optional[Path] = None, on_line=None) -> int:
    """re-point an update at its base after the base's keystone changed, in place"""
    argv = [str(tool_path()), "ps4-rebind", str(update), str(base)]
    if undo is not None:
        argv += ["--undo", str(undo)]
    return _run(argv, on_line=on_line)


def ps4_sfo(pkg: Path, values: dict, *, undo: Optional[Path] = None, on_line=None) -> int:
    """in place and resealed; strings only when they fit"""
    argv = [str(tool_path()), "ps4-sfo", str(pkg)]
    for k, v in values.items():
        argv += ["--set", f"{k}={v}"]
    if undo is not None:
        argv += ["--undo", str(undo)]
    return _run(argv, on_line=on_line)


def ps4_keystone_restore(pkg: Path, undo: Path, *, on_line=None) -> int:
    return _run([str(tool_path()), "ps4-keystone", str(pkg), "--restore", str(undo)], on_line=on_line)


def list_inner(pkg: Path, *, passcode: str = "0" * 32) -> dict:
    """reads only the blocks it needs, never decodes the whole image"""
    argv = ([str(tool_path()), "ps4-list", str(pkg)] if is_ps4(pkg)
            else [str(tool_path()), "list-inner", str(pkg), "--passcode", passcode])
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                       **_NO_WINDOW)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f"list-inner failed (rc={r.returncode})")
    return json.loads(r.stdout)


def extract_members(pkg: Path, out_dir: Path, members_file: Path,
                    *, passcode: str = "0" * 32, on_line=None) -> int:
    """one inner path per line, a folder means its whole subtree; decoded block-wise"""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    if is_ps4(pkg):
        return _run([str(tool_path()), "ps4-extract", str(pkg), str(out_dir), "--members", str(members_file)],
                    on_line=on_line)
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
          hdr_flag: str = "auto",  # auto, on or off; a bool works too
          regen_playgo: bool = False,
          fake_sign: bool = True,
          ampr_index: bool = True,
          stage_in_place: bool = False,
          consume_source: bool = False,
          parallelism: int = 0,
          keystone: Optional[str] = None,
          on_line=None) -> int:
    """keystone: a 96-byte file that goes in as sce_sys/keystone instead of the source's"""
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
        # src_dir is our own scratch copy, so the tool stages straight into it
        argv += ["--stage-in-place"]
    if consume_source and stage_in_place:
        # deletes each source file once the image holds it
        # only ever on our own scratch copy
        argv += ["--consume-source"]
    if parallelism and int(parallelism) > 0:
        argv += ["--parallelism", str(int(parallelism))]
    if keystone:
        argv += ["--keystone", str(keystone)]
    return _run(argv, on_line=on_line)


def validate(pkg: Path, *, json_out: bool = False, temp_dir: Optional[str] = None, on_line=None) -> int:
    argv = [str(tool_path()), "validate", str(pkg)]
    if json_out:
        argv.append("--json")
    if temp_dir:
        argv += ["--temp", str(temp_dir)]
    return _run(argv, on_line=on_line)
