"""LZ4 asset packs for games that load through AMPR (drakmor's ampr_emu).

The game's data files go into seekable LZ4 packs (ampr_assets-*.pak) that ampr_emu, placed in
the game's fakelib/, reads while the game runs. The result is still a plain game folder: it
goes into a folder or an .exfat like any other game. The packer is drakmor's ampr_pack, vendored
in backend/ampr_pack/; the runtime that reads the packs ships in backend/native/ampr/."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PACK_DIR = _HERE / "ampr_pack"
if str(_PACK_DIR) not in sys.path:
    sys.path.insert(0, str(_PACK_DIR))

AMPR_LIBRARY = "libSceAmpr"
RUNTIME_NAME = "libSceAmpr.sprx"
INDEX_NAME = "ampr_emu.index"
ASSETS_INDEX = "ampr_assets.index"
RUNTIME_VERSION = "0.4.2.1"
MIN_GAIN_PERCENT = 5

# Packed: game data below the game's own folder. Loose, whatever a rule above said: the files
# a game or the console opens before AMPR runs or reads past it, as ps5-dump-forge and the
# LZ4 Compression payload keep them (executables, modules, system folders, indexes, configs,
# text, pictures, video and audio). The last matching rule wins. Patterns are fnmatch, where
# "*" also crosses "/", so the files in the game's own folder are listed one by one.
_LOOSE_DIRS = ("sce_sys", "sce_module", "fakelib", "system", "save", "mods")
_LOOSE_EXT = ("prx", "sprx", "elf", "self", "pak", "utoc", "ucas", "rpkg",
              "ini", "json", "xml", "txt", "cfg", "toml", "yaml", "yml",
              "srt", "vtt", "ass", "ssa", "smi",
              "uproject", "uplugin", "upluginmanifest",
              "png", "jpg", "jpeg", "dds", "bmp", "gif", "at9",
              "bik", "bk2", "mp4", "mpg", "mpeg", "m2v", "webm", "usm",
              "wem", "bnk", "ogg", "opus", "mp3", "wav", "fsb", "awb")


def _glob_literal(name: str) -> str:
    return "".join(f"[{c}]" if c in "[]*?" else c for c in name)


def _toml_list(items) -> str:
    import json
    return "[\n" + "".join(f"  {json.dumps(i)},\n" for i in items) + "]"


def default_profile(root: Path) -> str:
    """The conservative profile for *root*: data in subfolders packed, the rest loose."""
    top = sorted(_glob_literal(p.name) for p in Path(root).iterdir() if not p.is_dir())
    loose = list(top)
    for d in _LOOSE_DIRS:
        loose += [f"{d}/**", f"{d.upper()}/**"]
    loose += ["**/eboot.bin", "**/ampr_*"]
    for ext in _LOOSE_EXT:
        loose += [f"**/*.{ext}", f"**/*.{ext.upper()}"]
    return (
        "[pack]\n"
        'default_action = "loose"\n'
        'default_block_size = "64KiB"\n'
        'io_page_size = "64KiB"\n'
        'payload_alignment = "64KiB"\n'
        'chunk_alignment = "64B"\n'
        'compression_mode = "hc"\n'
        "compression_level = 12\n"
        "deduplicate = true\n"
        'deduplicate_scope = "lane"\n'
        "auto_loose_large_files = true\n\n"
        "[groups.assets]\n"
        "pack_count = 4\n"
        'assignment = "balanced"\n'
        'max_pack_size = "8GiB"\n\n'
        "[[rule]]\n"
        'action = "compress"\n'
        'include = ["*/**"]\n'
        'block_size = "64KiB"\n'
        'group = "assets"\n'
        'layout = "mixed"\n\n'
        "[[rule]]\n"
        'action = "loose"\n'
        f"include = {_toml_list(loose)}\n"
    )


class Lz4Error(RuntimeError):
    pass


def runtime_path() -> Path:
    """The bundled pack-capable libSceAmpr.sprx (source checkout or frozen app)."""
    candidates = [_HERE / "native" / "ampr" / RUNTIME_NAME]
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        candidates += [Path(frozen) / "backend" / "native" / "ampr" / RUNTIME_NAME]
    for p in candidates:
        if p.is_file():
            return p
    raise Lz4Error(f"the ampr_emu runtime ({RUNTIME_NAME} {RUNTIME_VERSION}) is missing from the app; "
                   "in a source checkout run ./FETCH_AMPR_EMU.sh")


def uses_ampr(root: Path) -> bool | None:
    """True when the game's eboot.bin imports libSceAmpr, False when it does not, None when it
    cannot be read (missing, encrypted)."""
    import backport
    eboot = Path(root) / "eboot.bin"
    try:
        data = eboot.read_bytes()
    except OSError:
        return None
    imports, _exports = backport.read_symbols(data)
    if not imports:
        return None
    return any(i.library.startswith(AMPR_LIBRARY) or i.module.startswith(AMPR_LIBRARY) for i in imports)


def _bar(pct: int, label: str, started: float, done: int = 0) -> str:
    width = 32
    pct = max(0, min(99, pct))
    fill = width * pct // 100
    line = f"[{'#' * fill}{'-' * (width - fill)}] {pct:3d}% {label}"
    elapsed = time.monotonic() - started
    if elapsed > 0.5 and done > 0 and pct > 0:
        rate = done / elapsed
        line += f" @ {rate / 1024 ** 2:.2f} MB/s"
        left = elapsed * (100 - pct) / pct
        line += f" ETA {int(left // 60)}m{int(left % 60):02d}s"
    return line


class _Progress:
    """ampr_pack's build progress as the backend's bar lines (the GUI reads those)."""

    def __init__(self, log):
        self.log, self.started, self.last = log, time.monotonic(), 0.0

    def __call__(self, p) -> None:
        now = time.monotonic()
        if now - self.last < 0.5 and p.phase not in ("complete",):
            return
        self.last = now
        if p.phase == "packing" and p.logical_bytes_total:
            pct = int(p.logical_bytes_done * 100 / p.logical_bytes_total)
            done = p.logical_bytes_done
            label = (f"LZ4 pack {p.logical_bytes_done / 1024 ** 3:.1f}/{p.logical_bytes_total / 1024 ** 3:.1f} GB"
                     f" ({p.files_done}/{p.files_total} files)")
        else:
            pct, done = (0 if p.phase != "complete" else 99), 0
            label = f"LZ4 {p.phase} ({p.files_done}/{p.files_total} files)"
        self.log(_bar(pct, label, self.started, done))


def pack_game(root: Path, *, profile: Path | None = None, workers: int = 0, log=print) -> dict:
    """Turn *root* (the app's own working copy of /app0) into an LZ4-packed game in place:
    install the runtime, index the complete game, pack, verify every block and every packed
    file against the original, then remove the packed originals and put the packs next to the
    loose files. *root* must be a copy the app may change. Returns the packer's numbers."""
    import ampr_pack
    import build_ampr_index

    root = Path(root)
    if uses_ampr(root) is False:
        raise Lz4Error("this game does not load through AMPR (its eboot.bin does not import libSceAmpr), "
                       "so LZ4 asset packs cannot be used for it")
    for leftover in root.glob("ampr_assets*"):
        raise Lz4Error(f"the game already holds LZ4 packs ({leftover.name}); unpack it first")

    # the runtime first: the index lists every file of the game as it is deployed
    fakelib = root / "fakelib"
    fakelib.mkdir(exist_ok=True)
    old = fakelib / RUNTIME_NAME
    if old.exists():
        log(f"[INFO] Replacing the game's {RUNTIME_NAME} with ampr_emu {RUNTIME_VERSION}, which reads LZ4 packs.")
    shutil.copyfile(runtime_path(), old)

    index = root / INDEX_NAME
    if build_ampr_index.build_index_local(root, index, False) != 0:
        raise Lz4Error("could not build ampr_emu.index for the game")

    cfg_path = profile
    work = root.parent / f".{root.name}.lz4-work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        if cfg_path is None:
            cfg_path = work / "profile.toml"
            cfg_path.write_text(default_profile(root), encoding="utf-8")
        config = ampr_pack.load_config(Path(cfg_path))
        config.workers = max(1, min(256, workers or (os.cpu_count() or 4)))
        out = work / "packs"
        log(f"[INFO] LZ4: packing with {'profile ' + str(profile) if profile else 'the default profile'}, "
            f"LZ4 HC level {config.compression_level}, "
            f"{config.workers} worker(s).")
        try:
            index_path, stats, warnings = ampr_pack.build_packs(
                root=root, ampr_index=index, output_dir=out, config=config, progress=_Progress(log))
        except ampr_pack.PackError as e:
            raise Lz4Error(f"LZ4 packing failed: {e}") from e
        for w in warnings:
            log(f"[WARN] LZ4: {w}")
        if not stats.logical_bytes:
            raise Lz4Error("the profile put no file of this game into a pack; nothing to gain")
        gain = stats.logical_bytes - stats.stored_bytes
        if gain * 100 < stats.logical_bytes * MIN_GAIN_PERCENT:
            raise Lz4Error(f"the packs would save only {gain / 1024 ** 2:.1f} MB of "
                           f"{stats.logical_bytes / 1024 ** 2:.1f} MB packed data; not worth it, "
                           "so the game stays as it is")

        log("[INFO] LZ4: checking every block and every packed file against the original…")
        ampr_pack.verify_packs(index_path, ())
        ampr_pack.verify_packs_against_root(index_path, root, ())
        ampr_pack.remove_packed_sources(index_path, root, remove_empty_dirs=True)

        for p in sorted(out.iterdir()):
            dest = root / p.name
            if dest.exists():
                raise Lz4Error(f"the game already has a file named {p.name}")
            shutil.move(str(p), str(dest))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    saved = stats.logical_bytes - stats.stored_bytes
    log(f"[OK] LZ4: {stats.logical_bytes / 1024 ** 3:.2f} GB of game data packed into "
        f"{stats.stored_bytes / 1024 ** 3:.2f} GB ({saved / 1024 ** 3:.2f} GB smaller).")
    return {"logical_bytes": stats.logical_bytes, "stored_bytes": stats.stored_bytes}


def is_packed(root: Path) -> bool:
    return (Path(root) / ASSETS_INDEX).is_file()


def unpack_game(root: Path, *, log=print) -> dict:
    """The way back: put every file of the LZ4 packs in *root* back where it was, remove the
    packs and rebuild ampr_emu.index for the files now there. *root* must be a copy the app
    may change. The ampr_emu runtime stays: it reads loose files as well."""
    import ampr_pack
    import build_ampr_index

    root = Path(root)
    index = root / ASSETS_INDEX
    if not index.is_file():
        raise Lz4Error(f"this game holds no LZ4 asset packs (no {ASSETS_INDEX})")
    if Path(str(index) + ".crc").is_file():
        log("[INFO] LZ4: checking every block of the packs before unpacking…")
        try:
            ampr_pack.verify_packs(index, ())
        except ampr_pack.PackError as e:
            raise Lz4Error(f"the LZ4 packs are damaged: {e}") from e
    else:
        log(f"[WARN] LZ4: {ASSETS_INDEX}.crc is missing, so the packs are not checked first; "
            "every block is still decoded with its size checked.")
    try:
        manifest = ampr_pack.load_manifest(index)
        volumes = [manifest.pack_name(i) for i in range(len(manifest.packs))]
        got = ampr_pack.extract_packs(index, root, (), False, True)
    except (ampr_pack.PackError, ValueError, OSError) as e:
        raise Lz4Error(f"LZ4 unpacking failed: {e}") from e
    for name in volumes:                      # the volumes the manifest names, wherever they sit
        vol = (index.parent / name).resolve()
        if vol.is_file() and root.resolve() in vol.parents:
            vol.unlink()
    for p in list(root.iterdir()):
        if p.is_file() and p.name.startswith(ASSETS_INDEX):
            p.unlink()
    if build_ampr_index.build_index_local(root, root / INDEX_NAME, False) != 0:
        raise Lz4Error("could not rebuild ampr_emu.index for the unpacked game")
    log(f"[OK] LZ4: unpacked {got['files']} file(s), {got['bytes'] / 1024 ** 3:.2f} GB, back to plain files.")
    return got
