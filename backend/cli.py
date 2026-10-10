#!/usr/bin/env python3
"""PS5 UltraPack - backend wrapper (MkPFS 1.0.0, vendored under backend/mkpfs)"""
import sys
import os

# ── Bundled mkpfs detection ───────────────────────────────────────────────────
# When this script lives inside a 'backend/' folder, look for 'backend/mkpfs/'
# and add 'backend/' to sys.path so 'import mkpfs' resolves to the bundled copy.
_CLI_DIR = os.path.dirname(os.path.abspath(__file__))
_BUNDLED_MKPFS = os.path.join(_CLI_DIR, "mkpfs", "__main__.py")
if os.path.isfile(_BUNDLED_MKPFS) and _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

# ── Frozen-mode internal mkpfs intercept ─────────────────────────────────────
if len(sys.argv) > 1 and sys.argv[1] == "--mkpfs-internal":
    try:
        from mkpfs.cli import cli_mkpfs_main
        sys.exit(cli_mkpfs_main(sys.argv[2:]))
    except Exception as e:
        print(f"[ERROR] Internal MkPFS call failed: {e}", file=sys.stderr)
        sys.exit(1)

import argparse
import contextlib
import json
import re
import unicodedata
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
import zlib
from pathlib import Path


def _exit_with_parent(poll: float = 2.0) -> None:
    """End this backend, and everything it started, once the app that started it is gone.
    A crashed app used to leave its job (and the job's MkPFS) running on the drive, writing
    for nobody and in the way of the job the restarted app runs again. A job backend leads a
    process group of its own and takes the whole group along; one in the app's group (a scan)
    takes its children."""
    if os.name == "nt":
        return
    parent = os.getppid()
    if parent <= 1:
        return

    def watch():
        while os.getppid() == parent:
            time.sleep(poll)
        try:
            if os.getpgrp() == os.getpid():
                os.killpg(os.getpid(), signal.SIGTERM)
            else:
                try:
                    import psutil
                    for c in psutil.Process().children(recursive=True):
                        c.kill()
                except Exception:
                    pass
        finally:
            os._exit(1)
    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


# ── PFS browse (list a .ffpfs/.ffpfsc tree + extract selected members) ─────────
# These read an image WITHOUT a full decompression: a _PfscReader presents the inner
# raw-PFS bytes of a compressed .ffpfsc and only decompresses the blocks actually
# touched, so listing the tree reads just the inode table + dirents, and extracting
# one file reads just that file's blocks.

def _import_mkpfs():
    """Import the bundled mkpfs pfs + consts modules (backend/ on sys.path)."""
    if _CLI_DIR not in sys.path:
        sys.path.insert(0, _CLI_DIR)
    from mkpfs import pfs as _pfs, consts as _consts  # type: ignore
    return _pfs, _consts


class _PfscReader:
    """Random-access, decompressing file-like over a compressed .ffpfsc that presents
    the inner raw .ffpfs bytes. Supports seek()/read()/tell() so the mkpfs image
    parsers can read it as if it were an uncompressed image. Only the logical blocks
    actually read are decompressed (small block cache), never the whole image."""

    def __init__(self, path, pfs, consts, base: int = 0):
        self._pfs = pfs
        self._base = base                       # start of the PFSC stream within the file
        self._fh = open(path, "rb")
        self._fh.seek(base)
        head = self._fh.read(consts.PFSC_HEADER_SIZE)
        (self._lbs, self._block_count, boff, self._data_offset,
         self._logical_size) = pfs._parse_pfsc_header(head)
        self._fh.seek(base + boff)
        raw = self._fh.read((self._block_count + 1) * consts.PFSC_OFFSET_ENTRY_SIZE)
        self._offsets = list(struct.unpack_from(f"<{self._block_count + 1}Q", raw, 0))
        self._pos = 0
        self._cache: dict[int, bytes] = {}
        self._order: list[int] = []

    def _block(self, idx: int) -> bytes:
        cached = self._cache.get(idx)
        if cached is not None:
            return cached
        start, end = self._offsets[idx], self._offsets[idx + 1]
        self._fh.seek(self._base + start)
        stored = self._fh.read(end - start)
        blk = self._pfs._decode_pfsc_block(stored, self._lbs, idx)
        self._cache[idx] = blk
        self._order.append(idx)
        if len(self._order) > 64:                      # ~4 MiB cap at 64 KiB blocks
            self._cache.pop(self._order.pop(0), None)
        return blk

    def seek(self, off: int, whence: int = 0) -> int:
        if whence == 1:
            self._pos += off
        elif whence == 2:
            self._pos = self._logical_size + off
        else:
            self._pos = off
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._logical_size - self._pos
        end = min(self._pos + size, self._logical_size)
        out = bytearray()
        pos = self._pos
        while pos < end:
            blk = self._block(pos // self._lbs)
            within = pos % self._lbs
            take = min(self._lbs - within, end - pos)
            out += blk[within:within + take]
            pos += take
        self._pos = pos
        return bytes(out)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()


class _SubRangeReader:
    """Plain seek/read window over [base, base+length) of a file - used when a
    .ffpfsc wraps an UNCOMPRESSED inner image (no per-block decode needed)."""

    def __init__(self, path, base: int, length: int):
        self._fh = open(path, "rb")
        self._base = base
        self._length = length
        self._pos = 0

    def seek(self, off: int, whence: int = 0) -> int:
        if whence == 1:
            self._pos += off
        elif whence == 2:
            self._pos = self._length + off
        else:
            self._pos = off
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._length - self._pos
        size = max(0, min(size, self._length - self._pos))
        self._fh.seek(self._base + self._pos)
        data = self._fh.read(size)
        self._pos += len(data)
        return data

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()


_NESTED_IMAGE_SUFFIXES = (".ffpfs", ".exfat", ".ffpkg")


def _open_inner_pfs(image_path, pfs, consts):
    """Return a seek/read handle over the GAME PFS bytes.

    A .ffpfs IS the game PFS directly. A .ffpfsc is an OUTER PFS holding one compressed
    member (the inner .ffpfs); this descends into that member and presents its
    decompressed bytes through a _PfscReader, so only touched blocks are decompressed - never the whole inner image. Raises ValueError for an unsupported nested format
    (exFAT / UFS)."""
    image_path = Path(image_path)
    outer = open(image_path, "rb")
    try:
        header = pfs.parse_image_header(outer)
        inodes = pfs.parse_image_inodes(outer, header)
        errors: list[str] = []
        uroot, *_rest = pfs.parse_superroot_and_indexes(outer, header, inodes, errors)
        if uroot < 0:
            raise ValueError("could not locate the filesystem root (superroot)")
        file_inodes, dir_inodes, _de = pfs.build_tree_from_uroot(outer, header, inodes, uroot, errors)
        real_dirs = [d for d in dir_inodes if d]
        files = list(file_inodes.items())
        # Wrapper image: exactly one nested-image member and no real directories.
        if len(files) == 1 and not real_dirs and files[0][0].lower().endswith(_NESTED_IMAGE_SUFFIXES):
            rel, ino = files[0]
            if rel.lower().endswith((".exfat", ".ffpkg")):
                raise ValueError(f"this image wraps a {Path(rel).suffix} volume, not a PFS - "
                                 f"browsing that format is not supported")
            inode = inodes[ino]
            if not getattr(inode, "db", None):
                raise ValueError("inner image member has no data blocks")
            base = inode.db[0] * header.block_size
            outer.close()
            if getattr(inode, "is_compressed", False):
                return _PfscReader(image_path, pfs, consts, base=base)
            return _SubRangeReader(image_path, base, int(inode.logical_size))
        # Not a wrapper: this file is the game PFS itself.
        outer.seek(0)
        return outer
    except Exception:
        try:
            outer.close()
        except Exception:
            pass
        raise


def required_firmware(src) -> dict:
    """{'fw': '10.00', 'ps5': .., 'ps4': ..} for the game's eboot.bin in *src*: a game
    folder, a .ffpfs/.ffpfsc (only the headers and the param struct are read, through the
    image) or a .pkg (the tool pulls eboot.bin out of it). {} when unreadable."""
    import backport as _bp
    src = Path(src)
    words = None
    suf = src.suffix.lower()
    if src.is_dir():
        words = _bp.sdk_words_of_file(src / "eboot.bin")
    elif suf in (".ffpfs", ".ffpfsc"):
        pfs, consts = _import_mkpfs()
        with _open_inner_pfs(src, pfs, consts) as fh:
            errors: list[str] = []
            header = pfs.parse_image_header(fh)
            inodes = pfs.parse_image_inodes(fh, header)
            uroot, *_rest = pfs.parse_superroot_and_indexes(fh, header, inodes, errors)
            file_inodes, _dirs, _de = pfs.build_tree_from_uroot(fh, header, inodes, uroot, errors)
            ino = next((i for rel, i in file_inodes.items() if rel.strip("/") == "eboot.bin"), None)
            if ino is not None:
                inode = inodes[ino]
                size = int(inode.logical_size)
                if getattr(inode, "is_compressed", False) or inode.db_sig or inode.ib_sig:
                    data = b"".join(pfs.iter_inode_logical_blocks(fh, header, inode))
                    read = lambda off, n: data[off:off + n]
                else:
                    base = inode.db[0] * header.block_size
                    read = lambda off, n: pfs.read_image_bytes(fh, header, base + off, max(0, min(n, size - off)))
                words = _bp.sdk_words_from_reader(read, size)
    elif suf == ".pkg":
        import fpkg as _fpkg
        with tempfile.TemporaryDirectory(prefix="sdk-of-") as td:
            members = Path(td) / "members.txt"
            members.write_text("eboot.bin\n", encoding="utf-8")
            _fpkg.extract_members(src, Path(td) / "out", members, on_line=lambda _l: None)
            words = _bp.sdk_words_of_file(Path(td) / "out" / "eboot.bin")
    if not words:
        return {}
    return {"fw": _bp.sdk_firmware(words[0]), "ps5": words[0], "ps4": words[1]}


def _organize_identify():
    """organize_lib's reader over this backend: param.json out of an image (only that member is
    decompressed), the firmware from eboot.bin, the archive passwords from Settings."""
    import organize_lib
    import ultra_core

    def image_member(img, member):
        with tempfile.TemporaryDirectory(prefix="organize-") as td:
            with contextlib.redirect_stdout(open(os.devnull, "w")):
                extract_pfs_members(img, [member], Path(td))
            f = Path(td) / member
            return f.read_bytes() if f.is_file() else None

    def firmware(path):
        with contextlib.redirect_stdout(open(os.devnull, "w")):
            return required_firmware(path).get("fw", "")

    try:
        pw = [str(p).strip() for p in (ultra_core.load_settings().get("archive_passwords") or []) if str(p).strip()]
    except Exception:
        pw = []
    return organize_lib.make_identify(image_member, firmware, pw)


def _organize_main(args) -> int:
    import organize_lib
    out = lambda line: print(line, flush=True)
    if args.organize_scan is not None and not args.organize_scan.strip():
        print("[ERROR] --organize-scan needs a folder", flush=True)
        return 2
    if args.organize_scan:
        root = Path(args.organize_scan).expanduser().resolve()
        if not root.is_dir():
            print(f"[ERROR] not a folder: {root}", flush=True)
            return 1
        entries, companions = organize_lib.scan(
            root, _organize_identify(), on_progress=lambda n, t, name: out(f"ORGANIZE_PROGRESS: {n}/{t} {name}"))
        data = organize_lib.plan_to_json(organize_lib.plan(root, entries, companions))
        if args.organize_plan:
            organize_lib._write_json(Path(args.organize_plan), data)
        else:
            out("ORGANIZE_PLAN: " + json.dumps(data))
        out(f"[OK] {len(data['moves'])} to move, {len(data['stays'])} staying, {data['in_place']} already in place")
        return 0
    if args.organize_apply:
        if not args.organize_plan or not args.organize_journal:
            print("[ERROR] --organize-apply needs --organize-plan and --organize-journal", flush=True)
            return 2
        plan = organize_lib.plan_from_json(json.loads(Path(args.organize_plan).read_text(encoding="utf-8")))
        only = {int(i) for i in args.organize_only.split(",") if i.strip()} if args.organize_only is not None else None
        res = organize_lib.apply(plan, Path(args.organize_journal), selected=only, on_line=out)
        for f in res["failed"]:
            out(f"[WARN] {f}")
        out(f"[OK] Organized: {res['moved']} moved" + (f", {len(res['failed'])} failed" if res["failed"] else ""))
        return 0 if not res["failed"] else 1
    if args.organize_undo:
        if not args.organize_journal:
            print("[ERROR] --organize-undo needs --organize-journal", flush=True)
            return 2
        res = organize_lib.undo(Path(args.organize_journal), on_line=out)
        for f in res["failed"]:
            out(f"[WARN] {f}")
        out(f"[OK] Undone: {res['moved']} moved back" + (f", {len(res['failed'])} failed" if res["failed"] else ""))
        return 0 if not res["failed"] else 1
    if not args.output or not args.organize_into.strip() or not args.output.strip():
        print("[ERROR] --organize-into needs SRC and OUTPUT", flush=True)       # Path("") would be "."
        return 2
    return organize_lib.organize_into(Path(args.organize_into).resolve(), Path(args.output).resolve(),
                                      _organize_identify(), mode="move" if args.copy_mode == "move" else "keep",
                                      if_exists=args.if_exists, on_line=out)


def list_pfs_image(image_path):
    """Return the directory tree of a .ffpfs/.ffpfsc as a dict (no full decompression)."""
    pfs, consts = _import_mkpfs()
    image_path = Path(image_path)
    errors: list[str] = []
    reader = _open_inner_pfs(image_path, pfs, consts)
    with reader as fh:
        header = pfs.parse_image_header(fh)
        inodes = pfs.parse_image_inodes(fh, header)
        uroot, _fpt, _coll, _special = pfs.parse_superroot_and_indexes(fh, header, inodes, errors)
        if uroot < 0:
            raise ValueError("could not locate the filesystem root (superroot)")
        file_inodes, dir_inodes, _dirents = pfs.build_tree_from_uroot(fh, header, inodes, uroot, errors)
    entries = [{"path": d, "type": "dir"} for d in sorted(k for k in dir_inodes if k)]
    for f in sorted(file_inodes):
        try:
            size = int(inodes[file_inodes[f]].logical_size)
        except Exception:
            size = 0
        entries.append({"path": f, "type": "file", "size": size})
    return {
        "root": image_path.name,
        "entries": entries,
        "file_count": len(file_inodes),
        "dir_count": len([k for k in dir_inodes if k]),
        "errors": errors,   # non-empty only on a structurally damaged image
    }


# param.json attribute bit 29: the application declares HDR support. The console switches
# a TV set to "HDR when supported" into HDR only for titles that set it (verified on a PS5).
PARAM_HDR_BIT = 0x20000000

from backport import SDK_TARGETS as _BACKPORT_TARGETS  # UI-visible target labels (relative to backend/)


def _backport_target_arg(value: str) -> str:
    """--backport-target: a public target (7.61, 6.02, 10.xx) or a firmware whose
    original libraries sit in --fw-libs-root (9.60, 11.00, ...)."""
    v = (value or "").strip()
    if v in _BACKPORT_TARGETS or re.fullmatch(r"\d{1,2}\.\d{2}", v):
        return v
    raise argparse.ArgumentTypeError(f"{value!r}: expected 7.61, 6.02, 10.xx or a firmware such as 9.60")


def _backport_dirs(args):
    """(--backport-libs, --fw-libs-root) as resolved paths, None when not given."""
    libs = getattr(args, "backport_libs", None)
    fw = getattr(args, "fw_libs_root", None)
    return (Path(libs).expanduser().resolve() if libs else None,
            Path(fw).expanduser().resolve() if fw else None)
_REPORT_SKIP_DIRS = {"_extracted", "_ffpfsc_extract", "_ffpfsc_temp", "_ffpfsc_inner", "__MACOSX"}


def _read_param_json(src: Path) -> dict | None:
    """sce_sys/param.json of a game folder, a .ffpfs/.ffpfsc image or a .pkg, read without
    unpacking (only the blocks of that one file are decoded). None when there is none."""
    import contextlib
    import io
    if src.is_dir():
        pj = src / "sce_sys" / "param.json"
        return json.loads(pj.read_text(encoding="utf-8", errors="replace")) if pj.is_file() else None
    with tempfile.TemporaryDirectory(prefix="ffpfsc_param_") as td:
        out = Path(td) / "x"
        with contextlib.redirect_stdout(io.StringIO()):
            if src.suffix.lower() == ".pkg":
                import fpkg as _fpkg
                entries = _fpkg.list_inner(src).get("entries") or []
                member = next((e["path"] for e in entries if e.get("path", "").lower() == "sce_sys/param.json"), None)
                if not member:
                    return None
                mf = Path(td) / "members.txt"
                mf.write_text(member + "\n", encoding="utf-8")
                _fpkg.extract_members(src, out, mf, on_line=lambda _l: None)
            else:
                listing = list_pfs_image(src)
                cands = sorted((e["path"] for e in listing.get("entries") or []
                                if e.get("type") == "file" and e.get("path", "").lower().endswith("sce_sys/param.json")),
                               key=len)
                if not cands:
                    return None
                member = cands[0]
                extract_pfs_members(src, [member], out)
        pj = out / member
        return json.loads(pj.read_text(encoding="utf-8", errors="replace")) if pj.is_file() else None


def _firmware_text(v) -> str:
    """param.json version "0x0910000000000000" → "9.10" (system software / SDK); "" if absent
    or malformed. The console refuses to start a title that needs newer system software."""
    m = re.match(r"^(?:0x)?([0-9a-fA-F]{2})([0-9a-fA-F]{2})[0-9a-fA-F]{12}$", str(v or ""))
    return f"{int(m.group(1), 16):X}.{m.group(2)}" if m else ""


def _param_title(param: dict) -> str:
    loc = param.get("localizedParameters") or {}
    lang = loc.get("defaultLanguage") or "en-US"
    return ((loc.get(lang) or {}).get("titleName") or next(
        (v.get("titleName") for v in loc.values() if isinstance(v, dict) and v.get("titleName")), "") or "")


def param_report(root: Path) -> int:
    """Print, for every game under *root* (folders, .ffpfsc/.ffpfs images, .pkg files),
    whether its param.json declares HDR support. Read-only; nothing is written next to
    the sources."""
    sources: list[Path] = []
    if root.is_file() or (root / "sce_sys" / "param.json").is_file():
        sources = [root]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            d = Path(dirpath)
            keep = []
            for dn in sorted(dirnames):
                if dn.startswith(".") or dn in _REPORT_SKIP_DIRS:
                    continue
                if (d / dn / "sce_sys" / "param.json").is_file():
                    sources.append(d / dn)          # a game folder: listed, not descended
                else:
                    keep.append(dn)
            dirnames[:] = keep
            for fn in sorted(filenames):
                if not fn.startswith(".") and fn.lower().endswith((".ffpfsc", ".ffpfs", ".pkg")):
                    sources.append(d / fn)
    if not sources:
        print(f"[INFO] No games found under {root}")
        return 0
    rows, yes = [], 0
    for src in sources:
        try:
            param = _read_param_json(src)
        except Exception as e:
            param = None
            print(f"[WARN] {src.name}: {e}")
        if not param:
            rows.append(("?", "", "", "", src.name))
            continue
        attr = int(param.get("attribute") or 0)
        hdr = bool(attr & PARAM_HDR_BIT)
        yes += hdr
        rows.append(("yes" if hdr else "no", param.get("titleId", ""), param.get("contentVersion", ""),
                     _firmware_text(param.get("requiredSystemSoftwareVersion")),
                     _firmware_text(param.get("sdkVersion")), _param_title(param), src.name))
    print(f"{'HDR':4} {'TITLE ID':10} {'VERSION':10} {'FW':6} {'SDK':6} TITLE  (SOURCE)")
    for r in rows:
        if len(r) == 5:
            r = (r[0], r[1], r[2], "", "", r[3], r[4])
        hdr, tid, ver, fw, sdk, title, name = r
        print(f"{hdr:4} {tid:10} {ver:10} {fw:6} {sdk:6} {title}  ({name})")
    print(f"\n{len(rows)} game(s): {yes} declare HDR, {sum(r[0] == 'no' for r in rows)} do not, "
          f"{sum(r[0] == '?' for r in rows)} unreadable.")
    return 0


def extract_pfs_members(image_path, members, dest_dir) -> int:
    """Extract the given files / directory subtrees from a .ffpfs/.ffpfsc into dest_dir,
    decompressing only their blocks. A member naming a directory extracts every file
    beneath it AND recreates the directory itself, including EMPTY subdirectories - so
    the result is structure-identical to mkpfs's own extractor (extract_pfs_image, which
    mkdirs every dir_inode). Prints '[####] N% extract (path)' progress the GUI parses."""
    pfs, consts = _import_mkpfs()
    image_path = Path(image_path)
    dest_dir = Path(dest_dir)
    wanted = {m.strip().strip("/") for m in members if m.strip()}
    errors: list[str] = []
    reader = _open_inner_pfs(image_path, pfs, consts)
    with reader as fh:
        header = pfs.parse_image_header(fh)
        inodes = pfs.parse_image_inodes(fh, header)
        uroot, *_rest = pfs.parse_superroot_and_indexes(fh, header, inodes, errors)
        file_inodes, dir_inodes, _de = pfs.build_tree_from_uroot(fh, header, inodes, uroot, errors)
        # Fail closed on a structurally invalid image instead of silently writing a
        # partial set - matches mkpfs extract_pfs_image, which refuses on any error.
        # build_tree only records here on real corruption (bad inode ref, type/mode
        # mismatch, directory cycle, duplicate name), never for a healthy image.
        if errors:
            for e in errors[:8]:
                print(f"[ERROR] Image structure problem: {e}", flush=True)
            print("[ERROR] Refusing to extract from a structurally invalid image.", flush=True)
            return 1

        def _under(reln: str) -> bool:
            return any(reln == m or reln.startswith(m + "/") for m in wanted)

        targets = sorted((rel, ino) for rel, ino in file_inodes.items() if _under(rel.strip("/")))
        # Directories at/under a requested member, INCLUDING childless ones. Sorting the
        # path strings is parent-first (a parent is a prefix of its children).
        dir_targets = sorted(d for d in dir_inodes if d and _under(d.strip("/")))
        if not targets and not dir_targets:
            print("[ERROR] None of the requested items were found in the image.", flush=True)
            return 1
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Recreate directories first so empty ones survive even when no file lands in them.
        for d in dir_targets:
            (dest_dir / Path(d)).mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        for _rel, ino in targets:
            try:
                total_bytes += max(0, int(inodes[ino].logical_size))
            except Exception:
                pass
        done = 0
        last_pct = -1
        for rel, ino in targets:
            out = dest_dir / Path(rel)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("wb") as o:
                for chunk in pfs.iter_inode_logical_blocks(fh, header, inodes[ino]):
                    o.write(chunk)
                    done += len(chunk)
                    if total_bytes:
                        pct = min(99, int(done * 100 / total_bytes))
                        if pct != last_pct:
                            last_pct = pct
                            print(f"[####] {pct}% extract ({rel})", flush=True)
    print("[####] 100% extract", flush=True)
    print(f"[OK] Extracted {len(targets)} file(s) and {len(dir_targets)} folder(s) to {dest_dir}", flush=True)
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_title_id_from_name(name: str) -> str:
    match = re.search(r'\b([A-Z]{4}\d{5})\b', name, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    fallback = name
    for suffix in [".exfat", ".ffpkg", ".ffpfs", ".ffpfsc", "-app0", "-app", "-patch0", "-patch"]:
        if fallback.lower().endswith(suffix):
            fallback = fallback[:-len(suffix)]
    return fallback


def get_title_id(item_path: Path) -> str:
    if item_path.is_dir():
        param_path = item_path / "sce_sys" / "param.json"
        try:
            if param_path.is_file():
                with open(param_path, encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("titleId") or data.get("title_id") or ""
        except Exception as e:
            print(f"[WARN] Could not parse param.json for title ID: {e}")
    return get_title_id_from_name(item_path.name)


_DISK_IMAGE_SUFFIXES = {'.exfat', '.ffpkg'}
_PFS_IMAGE_SUFFIXES = {'.ffpfs', '.ffpfsc'}


def find_game_items(path: Path, batch: bool = False) -> list[Path]:
    if path.is_file():
        # Disk images (.exfat/.ffpkg) AND a bare uncompressed PFS image (.ffpfs) are valid
        # pack sources: a .ffpfs is just re-wrapped into a compressed .ffpfsc (its native
        # nested layout), so an already-built image can be (re)packed without a folder
        # round-trip. .ffpfsc is NOT packable here (it's already the compressed deliverable).
        if path.suffix.lower() in _DISK_IMAGE_SUFFIXES or path.suffix.lower() == ".ffpfs":
            return [path]
        print(f"[ERROR] Unsupported file type: {path.name}. Supported: .exfat, .ffpkg, .ffpfs, or a game folder.")
        sys.exit(1)

    print(f"[INFO] Scanning for game folder(s) and disk image(s) (.exfat / .ffpkg) in {path}...")

    image_items: list[Path] = []
    for dirpath, _, filenames in os.walk(path):
        curr = Path(dirpath)
        for f in filenames:
            if Path(f).suffix.lower() in _DISK_IMAGE_SUFFIXES:
                image_items.append(curr / f)

    folder_items: list[Path] = []
    for dirpath, _, _ in os.walk(path):
        curr = Path(dirpath)
        if (curr / "eboot.bin").is_file() and (curr / "sce_sys" / "param.json").is_file():
            folder_items.append(curr)

    # A disk image that sits *inside* a detected game folder is a by-product of
    # that game, not a separate item - drop it so one game isn't counted twice
    # (which would otherwise trip the "multiple items, use --batch" error, or
    # pack the same game twice in batch mode).
    folder_resolved = [f.resolve() for f in folder_items]
    def _inside_a_game_folder(p: Path) -> bool:
        rp = p.resolve()
        return any(fr in rp.parents for fr in folder_resolved)

    valid_items: list[Path] = folder_items + [
        img for img in image_items if not _inside_a_game_folder(img)
    ]

    seen: set[Path] = set()
    deduped: list[Path] = []
    for item in valid_items:
        r = item.resolve()
        if r not in seen:
            seen.add(r)
            deduped.append(item)
    valid_items = deduped

    if not valid_items:
        print(f"[ERROR] Could not find any valid game folders or disk images (.exfat / .ffpkg) in {path}.")
        sys.exit(1)

    if not batch and len(valid_items) > 1:
        print(f"[ERROR] Multiple game folders/files found in {path}:")
        for item in valid_items:
            print(f" - {item}")
        print("Use --batch to process all.")
        sys.exit(1)

    if not batch:
        print(f"[OK] Found game source at {valid_items[0]}")
    else:
        print(f"[OK] Found {len(valid_items)} game item(s) for batch processing.")
    return valid_items


def find_pfs_images(path: Path, batch: bool = False) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() in _PFS_IMAGE_SUFFIXES:
            return [path]
        print(f"[ERROR] Unsupported file type for unpack: {path.name}. Supported: .ffpfs or .ffpfsc.")
        sys.exit(1)

    print(f"[INFO] Scanning for PFS image(s) (.ffpfs / .ffpfsc) in {path}...")
    images: list[Path] = []
    for dirpath, _, filenames in os.walk(path):
        curr = Path(dirpath)
        for f in filenames:
            if Path(f).suffix.lower() in _PFS_IMAGE_SUFFIXES:
                images.append(curr / f)

    images = sorted({p.resolve(): p for p in images}.values(), key=lambda p: str(p).lower())

    if not images:
        print(f"[ERROR] Could not find any .ffpfs or .ffpfsc images in {path}.")
        sys.exit(1)

    if not batch and len(images) > 1:
        print(f"[ERROR] Multiple PFS images found in {path}:")
        for image in images:
            print(f" - {image}")
        print("Use --batch to process all.")
        sys.exit(1)

    if not batch:
        print(f"[OK] Found PFS image at {images[0]}")
    else:
        print(f"[OK] Found {len(images)} PFS image(s) for batch extraction.")
    return images


def _mkpfs_error_hint(exc: subprocess.CalledProcessError, output_path: Path) -> None:
    """Print a clear [ERROR] summary when mkpfs returns a non-zero exit code.
    Advice is platform-aware - Windows talks NTFS/drive letters, macOS/Linux do not."""
    print(f"[ERROR] mkpfs failed with exit code {exc.returncode}.", flush=True)
    if os.name == "nt":
        fs_label = ""
        try:
            import ctypes as _ct
            drive = str(output_path.resolve())[:3]
            buf = _ct.create_unicode_buffer(64)
            _ct.windll.kernel32.GetVolumeInformationW(drive, None, 0, None, None, None, buf, _ct.sizeof(buf))
            fs_label = buf.value.strip()
        except Exception:
            pass
        if fs_label in ("exFAT", "FAT32", "FAT"):
            print(
                f"[ERROR] OUTPUT DRIVE IS {fs_label} - 4 GB per-file limit exceeded.\n"
                f"[ERROR] PS5 .ffpfsc files are almost always larger than 4 GB.\n"
                f"[ERROR]   OUTPUT folder  →  change to an NTFS drive (e.g. C:\\ or D:\\)\n"
                f"[ERROR]   TEMP folder    →  also move to NTFS if it is on the same drive",
                flush=True,
            )
        else:
            print(
                f"[ERROR] Output path: {output_path}\n"
                f"[ERROR]   OUTPUT folder  →  ensure the drive is NTFS (not exFAT/FAT32) with enough space\n"
                f"[ERROR]   TEMP folder    →  needs ~1.5x the game size of free space during compression\n"
                f"[ERROR]   CPU cores      →  try lowering to 2 or 1 if RAM could be the cause\n"
                f"[ERROR]   Level          →  try 5 if the default (7) runs out of memory",
                flush=True,
            )
        return
    # macOS / Linux - no NTFS / drive-letter advice; exFAT 4 GB limit is the
    # common culprit on external PS5 transfer drives.
    print(
        f"[ERROR] Output path: {output_path}\n"
        f"[ERROR] Common causes & fixes:\n"
        f"[ERROR]   exFAT/FAT drive →  4 GB per-file limit; PS5 .ffpfsc files are usually larger.\n"
        f"[ERROR]                      Use an APFS or HFS+ drive (Disk Utility → Erase → APFS),\n"
        f"[ERROR]                      or choose a different OUTPUT drive.\n"
        f"[ERROR]   Free space      →  TEMP folder needs ~1.5x the game size free during compression\n"
        f"[ERROR]   Memory          →  lower CPU cores to 2/1, or compression Level to 5, if RAM runs out",
        flush=True,
    )


def _locate_mkpfs() -> tuple[list[str], str | None]:
    """Return (cmd_base, cwd) for invoking mkpfs."""
    # Frozen EXE - use internal bundle
    if getattr(sys, "frozen", False):
        print("[INFO] Running in packaged/frozen environment. Using internal MkPFS bundle.")
        return [sys.executable, "--mkpfs-internal"], None

    # Bundled package next to this script (backend/mkpfs/). It always ships with the
    # app; there is deliberately no fallback to a sibling checkout, the PATH or a pip
    # install - those could adopt a foreign mkpfs (different image bytes) or mutate
    # the interpreter at runtime.
    if os.path.isfile(_BUNDLED_MKPFS):
        print(f"[INFO] Using bundled MkPFS package at {_CLI_DIR}")
        return [sys.executable, "-m", "mkpfs"], _CLI_DIR

    print(f"[ERROR] Bundled MkPFS package not found at {_BUNDLED_MKPFS} - the installation is incomplete.",
          flush=True)
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# MkPFS wrappers
# ─────────────────────────────────────────────────────────────────────────────

def _unlink_quiet(path) -> None:
    """Remove *path* if it is a file; never raises."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _discard_stale_pass1_output(temp_pfs: Path) -> None:
    """Drop a leftover pass-1 image (plus mkpfs's '.tmp' for it) right before pass 1
    regenerates it. mkpfs `pack folder` asks "Overwrite? [Y/n]" on stdin when its
    output already exists; the GUI runs the backend with stdin closed, so an image a
    crash left behind turned that prompt into an EOFError and made every retry of the
    same title fail. This only ever touches the pass-1 OUTPUT we are about to write - never a user-supplied image (an OOM resume hands the inner .ffpfs in as the SOURCE
    and takes the single-file route, which does not come through here)."""
    _unlink_quiet(temp_pfs)
    _unlink_quiet(str(temp_pfs) + ".tmp")


def _stage_build_output(final_path: Path, replace_existing: bool) -> Path:
    """Where mkpfs should write an image whose final name is *final_path*.

    When the destination already exists and the caller wants it replaced, mkpfs builds
    into a sibling '<name>.partial' (leftovers of an earlier attempt removed first) and
    `_commit_build_output` swaps it onto the final name only after mkpfs returned 0. A
    failed build - ENOSPC, OOM kill, cancel, unplugged drive - therefore never costs
    the user the previous file, which is exactly what unlinking the old image up front
    used to do. (That unlink existed because `mkpfs pack` has no --overwrite and
    prompts interactively when its output exists; the sibling name sidesteps the
    prompt instead.) Returns *final_path* itself when nothing needs replacing."""
    if replace_existing and final_path.exists():
        partial = final_path.with_name(final_path.name + ".partial")
        _unlink_quiet(partial)
        _unlink_quiet(str(partial) + ".tmp")
        return partial
    return final_path


def _commit_build_output(build_path: Path, final_path: Path) -> None:
    """Move a finished '.partial' build onto its final name (atomic, same directory).
    No-op when mkpfs wrote the final name directly."""
    if build_path != final_path:
        os.replace(build_path, final_path)


def _discard_build_output(build_path: Path, final_path: Path) -> None:
    """After a failed build: remove the '.partial' (and mkpfs's '.tmp' for it) so only
    the untouched previous file remains under the final name."""
    if build_path != final_path:
        _unlink_quiet(build_path)
        _unlink_quiet(str(build_path) + ".tmp")


def _free_sibling_name(path: Path) -> Path:
    """*path* if nothing sits there yet, else the first free '<stem> (N)<suffix>'
    next to it (N = 2, 3, ...)."""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _is_same_file(a: Path, b: Path) -> bool:
    """True when *a* and *b* name the same file: by inode when both exist (this also
    catches a differently-cased spelling on a case-insensitive macOS volume), else by
    resolved path."""
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _pass2_needs_spool(block_size) -> bool:
    """Whether MkPFS's `pack file` will spool the image into its temp folder at all.

    MkPFS streams single-file packs (no spool, only the output's own .tmp) unless the
    image is signed, uses 64-bit inodes or asks for an auto-fit block size - its own
    `_stream_fallback_reason` decides. This backend never signs, always passes
    --inode-bits 32 and normalises the block size to 64 KiB, so pass 2 normally
    streams. Asking MkPFS itself keeps this in step if that rule ever changes; any
    doubt keeps the conservative answer (spool)."""
    try:
        import argparse as _ap
        from mkpfs.cli import _stream_fallback_reason
        ns = _ap.Namespace(signed=False, inode_bits=32, block_size=str(block_size or "65536"))
        return _stream_fallback_reason(args=ns) is not None
    except Exception:
        return True


def _assert_pass2_spool_space(image_path, temp_dir, block_size="65536") -> None:
    """Before pass-2 PFSC compression - when it spools roughly the image size into
    *temp_dir* - make sure the temp drive can hold it. If not, exit non-zero with a
    distinct, parseable message BEFORE mkpfs starts. Call this INSIDE the enclosing
    TemporaryDirectory block so its unwind reclaims the inner image, instead of letting
    mkpfs crash mid-write and strand a ~150 GB image. A streaming pass 2 needs no spool,
    so there is nothing to check (see _pass2_needs_spool)."""
    if not _pass2_needs_spool(block_size):
        return
    try:
        need = int(Path(image_path).stat().st_size * 1.10)
        free = shutil.disk_usage(str(temp_dir)).free
    except Exception:
        return
    if free < need:
        g = 1024 ** 3
        print(f"[ERROR] Insufficient temp space for pass-2 spool: need ~{need // g} GiB, "
              f"have {free // g} GiB in {temp_dir}. Point --temp-dir at a drive with more "
              f"free space (e.g. the output drive).", flush=True)
        sys.exit(1)


def _open_pass2_spool_dir(image_path, default_temp_dir, output_path, spill_base=None,
                          block_size="65536"):
    """Pick where the pass-2 PFSC spool lives - the key to using a fast SSD temp even when
    it can't hold image+spool together.

    The inner image (pass 1) stays on *default_temp_dir* (the --temp-dir the GUI placed on
    the SSD). The transient spool (~ the image size) goes there too WHEN it still fits
    beside the image; otherwise it spills onto the OUTPUT drive (a different, usually much
    larger volume). That keeps the image on the fast drive for the compression read instead
    of forcing the whole build onto the slow drive. The spool is pure scratch - where it
    lives does NOT change the resulting .ffpfsc bytes.

    *spill_base* (the GUI's output-root, via --spool-fallback-dir) is the preferred spill
    location (under <spill_base>/_ffpfsc_temp) so the GUI's startup sweep and failure
    cleanup find it; it falls back to the output file's own folder.

    Returns (spool_dir, cleanup_ctx): cleanup_ctx is a TemporaryDirectory to .cleanup()
    (spool spilled to the output drive) or None (spool on default_temp_dir, reclaimed by the
    caller's own temp dir). Never raises - on any doubt it returns default_temp_dir and the
    pre-pass-2 assert remains the backstop."""
    default_temp_dir = Path(default_temp_dir) if default_temp_dir else Path(tempfile.gettempdir())
    if not _pass2_needs_spool(block_size):
        return default_temp_dir, None          # streaming pass 2: no spool to place
    try:
        need = int(Path(image_path).stat().st_size * 1.10)
    except Exception:
        return default_temp_dir, None
    try:
        if shutil.disk_usage(str(default_temp_dir)).free >= need:
            return default_temp_dir, None          # fits beside the image - fastest path
    except Exception:
        return default_temp_dir, None
    # default_temp_dir can't hold image + spool. Spill the spool onto the output drive if it
    # is a DIFFERENT volume with room (the image keeps reading from the fast temp drive).
    try:
        base = Path(spill_base) if spill_base else Path(output_path).parent
        probe = base if base.exists() else base.parent
        same = os.stat(str(default_temp_dir)).st_dev == os.stat(str(probe)).st_dev
    except Exception:
        return default_temp_dir, None
    if not same:
        try:
            if shutil.disk_usage(str(probe)).free >= need:
                spill = base / "_ffpfsc_temp"
                spill.mkdir(parents=True, exist_ok=True)
                ctx = tempfile.TemporaryDirectory(prefix="ffpfsc_spool_", dir=str(spill))
                print(f"[INFO] Pass-2 spool routed to the output drive ({spill}) - temp can't "
                      f"hold image+spool; the inner image stays on temp for fast reads.", flush=True)
                return Path(ctx.name), ctx
        except Exception as e:
            print(f"[WARN] Could not place the pass-2 spool on the output drive: {e}", flush=True)
    return default_temp_dir, None   # nothing better; the assert below errors cleanly if needed


def _build_exfat_image(folder: Path, outdir: Path, title_id: str):
    """Build a raw exFAT filesystem image of *folder* so it can be compressed straight into
    a .ffpfsc - PSBrew's most-stable 'exfat -> ffpfsc' workflow, which wraps a real exFAT
    volume (read natively by the PS5) instead of going through the folder PFS builder. Uses
    MkPFS's native, CROSS-PLATFORM exFAT writer (no hdiutil) with 64 KiB clusters - the
    SMP/LVD fast-path allocation unit the PS5 loader expects. Returns the .exfat path, or
    None on failure - the caller then falls back to the two-pass folder image."""
    # Strip any pre-existing OS junk from the source first (._*, .DS_Store, __MACOSX, …) so
    # none of it lands in the exFAT volume.
    _stripped = _strip_junk_files(folder)
    if _stripped:
        print(f"[INFO] Removed {_stripped} macOS/Windows junk file(s)/folder(s) before building exFAT.", flush=True)
    out = outdir / f"{title_id or 'game'}.exfat"
    print("[INFO] Building exFAT image from the game folder (MkPFS native writer, 64 KiB clusters)...", flush=True)
    try:
        from mkpfs.exfat_writer import write_exfat_image
        # 64 KiB clusters = the SMP/LVD fast-path allocation unit (MkPFS's default policy).
        written = Path(write_exfat_image(folder, out, cluster_size=65536))
    except Exception as e:
        print(f"[WARN] exFAT build failed ({e}); using the two-pass folder image instead.", flush=True)
        return None
    if not written.exists():
        print("[WARN] exFAT build produced no image; using the two-pass folder image instead.", flush=True)
        return None
    print(f"[OK] exFAT image built: {written.name} ({written.stat().st_size // (1024**2)} MiB)", flush=True)
    return written


def _extract_exfat_to(exfat_path: Path, dest: Path) -> bool:
    """Extract a RAW exFAT image's contents into *dest* using MkPFS's native, CROSS-PLATFORM
    exFAT reader (no hdiutil/mount). Skips OS junk; deletes the .exfat on success. Returns
    True on success. Used to turn a nested exFAT (from a --via-exfat .ffpfsc) into a folder."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
        from mkpfs.pfs import extract_exfat_image
        result = extract_exfat_image(Path(exfat_path), Path(dest))
    except Exception as e:
        print(f"[WARN] Could not extract the nested exFAT image: {e}", flush=True)
        return False
    errs = list(getattr(result, "errors", None) or [])
    if errs:
        print(f"[WARN] exFAT extraction reported errors: {'; '.join(str(x) for x in errs[:3])}", flush=True)
        return False
    # Drop any OS junk a legacy (hdiutil-built) exFAT may still carry, then remove the .exfat.
    try:
        _strip_junk_files(dest)
    except Exception:
        pass
    try:
        exfat_path.unlink()
    except Exception:
        pass
    return True


def _unwrap_pfs_one_pass(image: Path, dest: Path) -> bool:
    """Write the game files of a .ffpfs, or of the PFS nested in a .ffpfsc, straight into
    *dest*, decoding every block once: no intermediate inner image on the disk (the two-pass
    unpack wrote the whole inner .ffpfs first, one more copy of the game). False when the
    container is not a plain or PFS-nested one (an exFAT wrapper): the caller then takes
    the two-pass road. Prints the '[####] N% extract' bars the GUI reads."""
    image, dest = Path(image), Path(dest)
    try:
        listing = list_pfs_image(image)
    except ValueError as e:
        print(f"[INFO] {image.name}: {e}; unpacking in two passes.", flush=True)
        return False
    except Exception as e:
        print(f"[WARN] Could not read {image.name} directly ({e}); unpacking in two passes.", flush=True)
        return False
    if listing.get("errors"):
        print(f"[WARN] {image.name}: {listing['errors'][0]}; unpacking in two passes.", flush=True)
        return False
    top = sorted({str(e["path"]).strip("/").split("/")[0] for e in listing.get("entries", []) if str(e["path"]).strip("/")})
    if not top:
        return False
    print(f"[INFO] Unpacking {image.name} in one pass ({listing.get('file_count', 0)} files, no intermediate image)...",
          flush=True)
    dest.mkdir(parents=True, exist_ok=True)
    rc = extract_pfs_members(image, top, dest)
    if rc != 0:
        raise RuntimeError(f"direct unpack of {image.name} failed (rc={rc})")
    return True


def _fully_unwrap(out_dir: Path, mkpfs_cmd_base, mkpfs_cwd) -> None:
    """Turn a freshly-unpacked image directory into the actual game FOLDER: keep
    unwrapping a SINGLE nested image - .ffpfs/.ffpfsc via another PFS unpack, .exfat/
    .ffpkg via a native exFAT read - until real files/folders remain. So 'unpack a
    .ffpfsc' yields a folder in ONE action, whether it was packed folder->ffpfsc (the
    nested inner .ffpfs) or via exFAT (the nested .exfat)."""
    for _ in range(8):
        try:
            entries = [p for p in out_dir.iterdir()
                       if not (p.name.startswith("._") or p.name == ".DS_Store"
                               or p.name == "_exfat_mnt" or p.name.startswith("ffpfsc_exfatmnt_"))]
        except Exception:
            return
        pfs = [p for p in entries if p.is_file() and p.suffix.lower() in (".ffpfs", ".ffpfsc")]
        exf = [p for p in entries if p.is_file() and p.suffix.lower() in (".exfat", ".ffpkg")]
        others = [p for p in entries if p not in pfs and p not in exf]
        if others:
            return   # real content present → this IS the folder
        if len(pfs) == 1 and not exf:
            img = pfs[0]
            print(f"[INFO] Unwrapping nested image {img.name} -> folder...", flush=True)
            with tempfile.TemporaryDirectory(dir=out_dir) as td:
                sub = Path(td)
                unpack_pfs_image(img, sub, mkpfs_cmd_base, mkpfs_cwd, overwrite=True)
                try:
                    img.unlink()
                except Exception:
                    pass
                for child in list(sub.iterdir()):
                    if _is_junk_name(child.name):
                        continue      # OS clutter; an exFAT sidecar may even vanish meanwhile
                    try:
                        shutil.move(str(child), str(out_dir / child.name))
                    except FileNotFoundError:
                        continue
            continue
        if len(exf) == 1 and not pfs:
            print(f"[INFO] Unwrapping nested exFAT {exf[0].name} -> folder...", flush=True)
            if _extract_exfat_to(exf[0], out_dir):
                continue
            return   # couldn't extract (non-macOS) - leave the .exfat for the user
        if pfs or exf:
            print(f"[WARN] Stopped unwrapping - nested image(s) left in place: "
                  f"{[p.name for p in (pfs + exf)]}. The output is NOT a plain folder.", flush=True)
        return   # nothing single to unwrap (empty / multiple images)


def _fmt_rate(bytes_per_s: float) -> str:
    """'812.40 MB/s' / '1.02 GB/s', the way mkpfs writes its bars (the GUI reads them)."""
    mb = bytes_per_s / (1024 * 1024)
    return f"{mb / 1024:.2f} GB/s" if mb >= 1024 else f"{mb:.2f} MB/s"


def _fmt_eta(seconds: float) -> str:
    """mkpfs's ETA: whole seconds under an hour, decimal minutes from there ('84.2m')."""
    return f"{int(seconds)}s" if seconds < 3600 else f"{seconds / 60:.1f}m"


def _tree_bytes(folder: Path) -> int:
    """Bytes in the files under *folder* so far (files still being written count too)."""
    total = 0
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _progress_line(done: int, total: int, elapsed: float, label: str) -> str:
    """One mkpfs-style bar line: '[####----]  42% extract … @ 812.40 MB/s ETA 57s'. The
    label names what happens; '{done}' and '{total}' in it become gigabytes."""
    pct = min(99, int(done * 100 / total)) if total else 0
    width = 32
    fill = min(width, int(width * pct / 100))
    label = label() if callable(label) else label
    text = label.replace("{done}", f"{min(done, total or done) / 1024 ** 3:.1f}").replace(
        "{total}", f"{total / 1024 ** 3:.1f}")
    line = f"[{'#' * fill}{'-' * (width - fill)}] {pct:3d}% {text}"
    if elapsed > 0.5 and done > 0:
        rate = done / elapsed
        line += f" @ {_fmt_rate(rate)}"
        if total > done:
            line += f" ETA {_fmt_eta((total - done) / rate)}"
    return line


class _RUsageV4(__import__("ctypes").Structure):
    """rusage_info_v4 from <sys/resource.h>, as far as the write counter."""
    _fields_ = [("ri_uuid", __import__("ctypes").c_uint8 * 16)] + [(n, __import__("ctypes").c_uint64) for n in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins",
        "ri_wired_size", "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime",
        "ri_proc_exit_abstime", "ri_child_user_time", "ri_child_system_time", "ri_child_pkg_idle_wkups",
        "ri_child_interrupt_wkups", "ri_child_pageins", "ri_child_elapsed_abstime", "ri_diskio_bytesread",
        "ri_diskio_byteswritten", "ri_cpu_time_qos_default", "ri_cpu_time_qos_maintenance",
        "ri_cpu_time_qos_background", "ri_cpu_time_qos_utility", "ri_cpu_time_qos_legacy",
        "ri_cpu_time_qos_user_initiated", "ri_cpu_time_qos_user_interactive", "ri_billed_system_time",
        "ri_serviced_system_time", "ri_logical_writes", "ri_lifetime_max_phys_footprint",
        "ri_instructions", "ri_cycles", "ri_billed_energy", "ri_serviced_energy",
        "ri_interval_max_phys_footprint", "ri_runnable_time")]


def _proc_bytes_written(pid: int) -> int | None:
    """Bytes process *pid* has written, from the kernel (macOS proc_pid_rusage; psutil
    elsewhere). None when the system does not tell. Unlike the size of the files it
    writes, this also moves while a tool fills a file it created at full size first,
    which is what the .pkg extractor does (measured on exFAT/FSKit and APFS)."""
    if sys.platform == "darwin":
        try:
            import ctypes
            lib = ctypes.CDLL("/usr/lib/libproc.dylib")
            info = _RUsageV4()
            if lib.proc_pid_rusage(int(pid), 4, ctypes.byref(info)) == 0:
                return int(info.ri_diskio_byteswritten)
        except Exception:
            return None
        return None
    try:
        import psutil
        return int(psutil.Process(int(pid)).io_counters().write_bytes)
    except Exception:
        return None


def _meter_process(proc, total: int, label, interval: float = 1.0) -> None:
    """Print progress bars for the running *proc* from its write counter until it exits.
    For a tool whose own bar is too coarse: MkPFS pass 1 counts whole files, so a game
    with one 57 GB file sits at "0 % write" for ten minutes while the data flows. Nothing
    is printed before 1 % is written, so the tool's earlier steps (scan, read) keep their
    own bars; nothing at all when the system does not tell."""
    import time as _time
    started = _time.monotonic()
    while True:
        try:
            proc.wait(timeout=interval)
            return
        except subprocess.TimeoutExpired:
            pass
        done = _proc_bytes_written(proc.pid)
        if done is None or not total:
            return
        if done * 100 >= total:
            # The tool writes its own bars to stderr as "\r<bar>" without a newline; the
            # leading "\r" ends that partial line, or a reader that splits on \r and \n
            # (the GUI) would see "0% write[####] 42% write ..." and take the 0 %.
            print("\r" + _progress_line(done, total, _time.monotonic() - started, label), flush=True)


def _run_with_folder_progress(work, folder: Path, total: int, label: str, interval: float = 1.0,
                              measure=None):
    """Run *work()* (which fills *folder*) and print a progress bar line every *interval*
    seconds, compared with the expected *total*. *measure()* returns the bytes done (the
    writing tool's own write counter); without it, or when it cannot tell, the bytes
    already on disk count. For steps whose tool prints nothing until it is done (a full
    .pkg extract): without this the window sits at 0 % for minutes. Returns what *work*
    returns."""
    import threading
    import time as _time
    result, error = {}, {}

    def _target():
        try:
            result["value"] = work()
        except BaseException as exc:          # re-raised in the caller's thread
            error["exc"] = exc

    runner = threading.Thread(target=_target, daemon=True)
    started = _time.monotonic()
    runner.start()
    while runner.is_alive():
        runner.join(interval)
        if runner.is_alive() and total:
            done = measure() if measure is not None else None
            if done is None:
                done = _tree_bytes(folder)
            print(_progress_line(done, total, _time.monotonic() - started, label), flush=True)
    if "exc" in error:
        raise error["exc"]
    if total:
        width = 32
        print(f"[{'#' * width}] 100% {label}", flush=True)
    return result.get("value")


def _pkg_content_size(pkg: Path) -> int:
    """Bytes of the files a full extract of *pkg* writes (its inner /app0 tree and the
    sce_sys files of the CNT), read from the package's directory, not by decoding it.
    0 when the listing fails; the extract then runs without a bar."""
    try:
        import fpkg as _fpkg
        doc = _fpkg.list_inner(pkg)
        return sum(int(e.get("size") or 0) for e in doc.get("entries", []) if e.get("type") == "file")
    except Exception:
        return 0


def _pkg_extract_plan(pkg: Path, content: int) -> int:
    """Bytes a full .pkg extract writes, all on the drive it unpacks to. The package tool
    (LibProsperoPkg ExtractInnerFiles) works in three steps: it decrypts the outer image
    into a temp file (about the package size), decodes the inner image from it into a
    second temp file (about the size of the game files) and then writes the files. The
    decoded image and the files sit side by side at the end, so the drive needs about
    twice the game's size for a moment."""
    try:
        size = Path(pkg).stat().st_size
    except OSError:
        size = 0
    return size + 2 * content if content else 0


def _pkg_extract_step(folder: Path) -> int:
    """Which of the package tool's three extract steps runs, from its temp files in
    *folder*: 1 decrypt (outer temp only), 2 decode (the inner image is being written
    under a second temp name), 3 write the files (the finished inner image)."""
    try:
        names = [n for n in os.listdir(folder) if n.startswith(".")]
    except OSError:
        return 0
    if any(n.startswith("..libprospero-inner-") for n in names):
        return 2
    if any(n.startswith(".libprospero-inner-") for n in names):
        return 3
    if any(n.startswith(".libprospero-outer-") for n in names):
        return 1
    return 0


_PKG_EXTRACT_STEPS = {1: "decrypt the package", 2: "unpack the game image", 3: "write the game files"}


def _pkg_extract_label(folder: Path) -> str:
    """Progress label for a full .pkg extract: where it writes and which step runs."""
    step = _pkg_extract_step(folder)
    what = f", step {step} of 3: {_PKG_EXTRACT_STEPS[step]}" if step else ""
    return f"extract the .pkg on {_drive_label(folder)}{what} ({{done}} of {{total}} GB written)"


def _drive_label(path: Path) -> str:
    """The drive's volume name ('SAMSUNG'), or 'the system drive'."""
    parts = Path(path).resolve().parts
    return parts[2] if len(parts) > 2 and parts[1] == "Volumes" else "the system drive"


def _describe_drive(path: Path) -> str:
    """'SAMSUNG, 225.30 GB free' for the drive that holds *path*."""
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return str(path)
    parts = Path(path).resolve().parts
    name = parts[2] if len(parts) > 2 and parts[1] == "Volumes" else "the system drive"
    return f"{name}, {free / 1024 ** 3:.2f} GB free"


def _phase(name: str) -> None:
    """Emit a machine-readable phase marker the GUI maps directly to its stage
    tracker (with force=True, so multi-phase jobs like patch/convert - which
    extract THEN repack - advance correctly instead of latching on 'Extracting').
    The *name* must be a canonical GUI stage name."""
    print(f"[PHASE] {name}", flush=True)


class FpkgProgress:
    """Translate the package tool's log into the GUI's phase markers and progress bars.

    The tool prints LibProsperoPkg's own log: "[stage 3/5] ... 59% (55.77 GiB / 94.25 GiB;
    71.4 MiB/s)", "[inner] data 17% (25/265): /path -> N bytes (Kraken, ratio 68.4 %)",
    "[inner] Kraken level -4: 70% of /path", "[finalize] ...: 60% (1,234 / 4,567 blocks)".
    The GUI understands "[PHASE] <Stage>" markers and "[####----] NN% <label>" bars, and
    reads speed and time left from a label of the form "... @ 71.4 MB/s ETA 1234s".

    The Kraken pass is metered in bytes: the planning line gives the total, every large
    file announces its size, and the per-file "NN% of" lines move inside it; smaller files
    are sized from their output and ratio. The library's own percentage is the floor.
    Phases only move forward: the tool's late "Source scan" (it scans after staging) is
    not echoed as a bar once the job is past scanning, which used to pin "Temp PFS" at 99 %.
    """

    _STAGE_PHASE = {1: "Creating Temp PFS", 2: "Compressing", 3: "Compressing",
                    4: "Writing Final Image", 5: "Writing Final Image"}
    _ORDER = ["Scanning Files", "Reading Game", "Creating Temp PFS", "Compressing",
              "Writing Final Image", "Verifying Output"]
    # validate's milestones ("[validate] <name>") and roughly where each sits in its run
    _VALIDATE_STEPS = {"file": 5, "header": 15, "cnt": 30, "entries": 55, "inner": 70, "report": 95}
    _UNIT = {"b": 1, "bytes": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
             "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12}

    def __init__(self, phase_cb, out=None, clock=None):
        self._phase_cb = phase_cb
        self._out = out or (lambda text: print(text, flush=True))
        self._clock = clock or time.monotonic
        self.cur = None
        # Kraken pass meter
        self.total = 0          # uncompressed bytes the inner image will hold
        self.done = 0           # bytes of files the image already holds
        self.file = None        # the large file being encoded, and its size / fraction done
        self.file_size = 0
        self.file_frac = 0.0
        self.t0 = None
        self.lib_pct = 0

    # ── helpers ───────────────────────────────────────────────────────────
    def _bar(self, pct, label):
        pct = max(0, min(100, int(pct)))
        filled = pct // 5
        self._out(f"[{'#' * filled}{'-' * (20 - filled)}] {pct}% {label}")

    def _set_phase(self, name):
        if self.cur != name:
            self.cur = name
            self._phase_cb(name)

    def _past(self, name):
        """True when the job already moved beyond *name*."""
        return (self.cur in self._ORDER and name in self._ORDER
                and self._ORDER.index(self.cur) > self._ORDER.index(name))

    @classmethod
    def _bytes(cls, number, unit):
        return float(number.replace(",", "")) * cls._UNIT.get(unit.lower(), 1)

    @staticmethod
    def _rate_eta(done_bytes, total_bytes, seconds):
        """'@ 71.4 MB/s ETA 1234s' from bytes done, total and elapsed seconds; '' if unknown."""
        if seconds is None or seconds <= 0 or done_bytes <= 0:
            return ""
        rate = done_bytes / seconds
        left = max(0.0, total_bytes - done_bytes) / rate if total_bytes > done_bytes else 0.0
        return f" @ {rate / 1e6:.1f} MB/s ETA {int(round(left))}s"

    def _kraken_bar(self):
        in_flight = self.file_size * self.file_frac if self.file else 0
        got = self.done + in_flight
        pct = int(got * 100 / self.total) if self.total else 0
        pct = max(pct, self.lib_pct)
        elapsed = (self._clock() - self.t0) if self.t0 is not None else None
        self._bar(pct, "inner image (Kraken)" + self._rate_eta(got, self.total, elapsed))

    # ── one line of tool output ───────────────────────────────────────────
    def line(self, line: str) -> None:
        self._out(line)
        low = line.lower()
        if "source scan:" in low:
            if not self._past("Scanning Files"):
                self._set_phase("Scanning Files"); self._bar(100, "source scan")
            return
        m = re.search(r"planning nwonly inner image: ([\d,]+) files, ([\d,]+) uncompressed bytes", low)
        if m:
            self.total = int(m.group(2).replace(",", ""))   # the Kraken meter's denominator
            return
        if "[inner] preparing" in low or "prepared inner tree" in low:
            if not self._past("Reading Game"):
                self._set_phase("Reading Game"); self._bar(100 if "prepared" in low else 50, "inner files prepared")
            return
        if "writing afid-ordered inner data" in low:
            self._set_phase("Creating Temp PFS"); self.t0 = self._clock()
            self._bar(0, "inner image (Kraken)"); return
        m = re.search(r"\[stage\] copy (\d+)%", line)
        if m:
            self._set_phase("Reading Game"); self._bar(int(m.group(1)), "staging copy"); return
        if "[stage] staged in place" in low:
            self._set_phase("Reading Game"); self._bar(100, "staged in place"); return
        if self.cur == "Creating Temp PFS":
            m = re.search(r"processing large file: (/\S+) \(([\d,]+) bytes\)", line)
            if m:
                self.file, self.file_size, self.file_frac = m.group(1), int(m.group(2).replace(",", "")), 0.0
                return
            m = re.search(r"kraken level -?\d+:\s*(\d+)% of (/\S+)", low)
            if m:
                if self.file and m.group(2) == self.file.lower():
                    self.file_frac = int(m.group(1)) / 100.0
                self._kraken_bar(); return
            m = re.search(r"\bdata\s+(\d+)%\s*\(\d+/\d+\): (/\S+) -> ([\d,]+) bytes(?: \((?:kraken|raw)[^)]*?ratio ([\d.]+) %\))?", line, re.I)
            if m:
                self.lib_pct = int(m.group(1))
                path = m.group(2)
                if self.file and path == self.file:
                    self.done += self.file_size
                else:
                    out_bytes = int(m.group(3).replace(",", ""))
                    ratio = float(m.group(4)) if m.group(4) else 100.0
                    self.done += int(out_bytes * 100.0 / ratio) if ratio > 0 else out_bytes
                self.file, self.file_size, self.file_frac = None, 0, 0.0
                self._kraken_bar(); return
        m = re.search(r"\[stage (\d)/5\]", line)
        if m:
            st = int(m.group(1))
            if st == 1:
                # stage 1 opens with file preparation (Reading Game) and closes with
                # "Inner image complete" (Creating Temp PFS at 100 %) - no back-jump.
                if "complete" in low:
                    self._set_phase("Creating Temp PFS"); self._bar(100, "inner image (Kraken)")
                elif not self._past("Reading Game"):
                    self._set_phase("Reading Game")
                return
            self._set_phase(self._STAGE_PHASE.get(st, self.cur or "Compressing"))
            if st == 2:
                self._bar(100 if "complete" in low else 0, "NAPS tables"); return
            if st == 3:
                m2 = re.search(r"(\d+)% \(([\d.,]+) (\w+) / ([\d.,]+) (\w+); ([\d.,]+) (\w+)/s\)", line)
                if m2:
                    done_b = self._bytes(m2.group(2), m2.group(3)); total_b = self._bytes(m2.group(4), m2.group(5))
                    rate = self._bytes(m2.group(6), m2.group(7)) / 1e6
                    left = (total_b - done_b) / (rate * 1e6) if rate > 0 else 0
                    self._bar(int(m2.group(1)), f"outer PFS @ {rate:.1f} MB/s ETA {int(round(left))}s"); return
                if "outer pfs complete" in low:
                    self._bar(100, "outer PFS"); return
                if "started" in low or "building" in low:
                    self._bar(0, "outer PFS"); return
                return
            if st == 4:
                self._bar(40 if "complete" in low else 0, "CNT image"); return
            if st == 5:
                self._bar(50, "finalize"); return
            return
        m = re.search(r"\[finalize\].*?:\s*(\d+)%\s*\(([\d,]+) / ([\d,]+) blocks\)", line)
        if m:
            self._set_phase("Writing Final Image")
            self._bar(50 + int(m.group(1)) // 2, "finalize (FIH digests)"); return
        if "build finished" in low:
            self._set_phase("Writing Final Image"); self._bar(100, "finalized .pkg written"); return
        if line.startswith("[INFO] Auto-validating"):
            self._set_phase("Verifying Output"); self._bar(0, "validate checklist"); return
        m = re.match(r"\[validate\] (\w+)$", line.strip())
        if m and self.cur == "Verifying Output":
            self._bar(self._VALIDATE_STEPS.get(m.group(1), 0), "validate checklist"); return
        if low.startswith("summary:") and "failed" in low:
            self._bar(100, "validate checklist"); return


# macOS / Windows metadata sidecars that must never be packed into a PFS image.
# All are OS-generated junk, never game data - safe to delete unconditionally.
# Lower-case, compared case-insensitively (exFAT and Windows volumes keep no case). One
# list for the whole app: mkpfs.utils.IGNORED_NAMES, ultra_core.FS_JUNK_NAMES,
# after_job._JUNK_NAMES and the fPKG tool's FsJunk.cs name the same entries; a test keeps
# them equal. Nothing on it is ever extracted, copied, moved along or packed.
_JUNK_FILE_NAMES = frozenset({
    ".ds_store", ".localized", ".lsoverride", ".apdisk", ".volumeicon.icns", "icon\r",
    "thumbs.db", "ehthumbs.db", "desktop.ini",
})
_JUNK_DIR_NAMES = frozenset({
    "__macosx", ".spotlight-v100", ".trashes", ".fseventsd", ".temporaryitems",
    ".documentrevisions-v100", ".appledouble", "$recycle.bin", "system volume information",
})
_JUNK_NAMES = _JUNK_FILE_NAMES | _JUNK_DIR_NAMES
# The same rule for shutil.ignore_patterns (fnmatch is case-sensitive here: the usual spellings).
_JUNK_GLOBS = ("._*", ".DS_Store", ".localized", ".LSOverride", ".apdisk", ".VolumeIcon.icns", "Icon\r",
               "Thumbs.db", "ehthumbs.db", "desktop.ini", "__MACOSX", ".Spotlight-V100", ".Trashes",
               ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100", ".AppleDouble",
               "$RECYCLE.BIN", "System Volume Information")


def _is_junk_name(name: str) -> bool:
    """OS or archiver clutter by base name: an AppleDouble sidecar (._*) or a known name."""
    return name.startswith("._") or name.lower() in _JUNK_NAMES

def _strip_junk_files(root: Path) -> int:
    """Recursively remove macOS/Windows metadata junk from *root* so it never
    lands in the PFS image: AppleDouble sidecars (``._*``), ``.DS_Store``,
    Spotlight/Trash/fseventsd folders, ``Thumbs.db``/``desktop.ini``, etc. These
    are OS-generated, never game files. Returns the number of entries removed.
    (Also why a build can fail with structure-verify on: a stray .DS_Store.)
    Third-party extras are handled separately by _evacuate_non_game_extras - they are MOVED out (preserved beside the output), not deleted."""
    root = Path(root)
    removed = 0
    # topdown=False so we can rmtree junk dirs after their contents are handled.
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            if _is_junk_name(name):
                try:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    pass
        for name in list(dirnames):
            if _is_junk_name(name):
                p = os.path.join(dirpath, name)
                if os.path.islink(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                else:
                    shutil.rmtree(p, ignore_errors=True)
                removed += 1
    return removed


# ── fPKG identity ────────────────────────────────────────────────────────────
_FPKG_CID_RE = re.compile(r"^[A-Z]{2}[0-9]{4}-[A-Z]{4}[0-9]{5}_00-[A-Z0-9]{16}$")
_FPKG_TID_RE = re.compile(r"^[A-Z]{4}[0-9]{5}$")
_FPKG_VER_RE = re.compile(r"^\d{2}\.\d{2,3}(\.\d{3})?$")


def _content_version(v: str) -> str:
    """A version in the package's xx.xxx.xxx form: "01.03" (a masterVersion) and "01.003"
    become "01.003.000"; anything already xx.xxx.xxx, or empty, is returned as is."""
    m = re.match(r"^(\d{2})\.(\d{2,3})(?:\.(\d{3}))?$", v or "")
    if not m:
        return v
    return f"{m.group(1)}.{int(m.group(2)):03d}.{m.group(3) or '000'}"


def _clean_title(t: str) -> str:
    """A titleName the console shows cleanly: no BOM or control characters, no leading or
    trailing spaces, inner runs of whitespace collapsed."""
    t = "".join(" " if ch.isspace() else ch for ch in (t or "")
                if ch != "\ufeff" and (ch.isspace() or not unicodedata.category(ch).startswith("C")))
    return " ".join(t.split())


def _fpkg_param_title(d: dict) -> str:
    """The display title from a param.json dict (Sony layout: localizedParameters →
    defaultLanguage block → titleName; any language as fallback; legacy top-level)."""
    try:
        lp = d.get("localizedParameters") or {}
        if isinstance(lp, dict):
            lang = lp.get("defaultLanguage")
            block = lp.get(lang) if isinstance(lang, str) else None
            if isinstance(block, dict) and block.get("titleName"):
                return str(block["titleName"])
            for v in lp.values():
                if isinstance(v, dict) and v.get("titleName"):
                    return str(v["titleName"])
        return str(d.get("titleName") or "")
    except Exception:
        return ""


def _resolve_fpkg_identity(build_src: Path, args) -> dict:
    """Decide the identity an fPKG build is stamped with.

    sce_sys/param.json is the source of truth. The console checks that the package
    header agrees with it, and so does the validate checklist - so a value the GUI
    passed (a placeholder guessed from a file name, a stale field) must never win over
    what the game itself declares. --content-id / --title-id / --fpkg-version /
    --fpkg-title are fallbacks for fields param.json lacks, and the whole identity for a
    source without a param.json (the builder then generates one from them).

    Returns {"content_id", "title_id", "version", "title", "source"} where source names
    where the content id came from ('param.json' or 'arguments'). Fields may be empty - the caller decides whether that is fatal."""
    pj = Path(build_src) / "sce_sys" / "param.json"
    d: dict = {}
    if pj.is_file():
        try:
            loaded = json.loads(pj.read_text(encoding="utf-8-sig", errors="replace"))
            d = loaded if isinstance(loaded, dict) else {}
        except Exception as e:
            print(f"[WARN] Could not read {pj}: {e} - using the passed identity.", flush=True)

    def _pick(label: str, passed: str | None, from_pj: str, valid) -> tuple[str, str]:
        passed = (passed or "").strip()
        from_pj = (from_pj or "").strip()
        if from_pj and valid(from_pj):
            if passed and passed != from_pj:
                print(f"[WARN] {label} {passed!r} differs from param.json {from_pj!r} - using param.json "
                      f"(the console checks that the header and param.json agree).", flush=True)
            return from_pj, "param.json"
        if from_pj:
            print(f"[WARN] param.json {label} {from_pj!r} is malformed - using the passed value.", flush=True)
        return passed, "arguments"

    cid, cid_src = _pick("content id", args.content_id,
                         str(d.get("contentId") or "").upper(), _FPKG_CID_RE.match)
    tid, _ = _pick("title id", args.title_id,
                   str(d.get("titleId") or "").upper(), _FPKG_TID_RE.match)
    # The argparse default is indistinguishable from an explicit "01.000.000"; treat the
    # default as "not passed" so it never triggers a mismatch warning.
    passed_ver = args.fpkg_version if args.fpkg_version != "01.000.000" else ""
    ver, _ = _pick("version", passed_ver,
                   str(d.get("contentVersion") or d.get("masterVersion") or ""), _FPKG_VER_RE.match)
    ver = _content_version(ver)
    title = _clean_title(_fpkg_param_title(d) or (args.fpkg_title or ""))
    if cid and not tid:
        tid = cid[7:16]          # UP9000-PPSA99099_00-… → PPSA99099
    if cid and tid and tid not in cid:
        print(f"[WARN] title id {tid} does not appear inside the content id {cid}.", flush=True)
    return {"content_id": cid, "title_id": tid, "version": ver or "01.000.000",
            "title": title, "source": cid_src}


# Loose side-car metadata file extensions (never game data). These, plus any top-level
# entry whose name starts with '_', are treated as NON-GAME and pulled out of the dump
# by _evacuate_non_game_extras so they don't enter the image - but PRESERVED next to the
# output .ffpfsc, since the user still wants them (.nfo, group tools, …).
_EXTRA_FILE_EXTS = frozenset({".nfo", ".sfv", ".diz", ".par2"})


def _is_os_junk_name(name: str) -> bool:
    """OS-generated metadata that _strip_junk_files DELETES (never preserved)."""
    return _is_junk_name(name)


def _evacuate_non_game_extras(game_folder: Path, dest_dir: Path) -> int:
    """Move NON-GAME extras OUT of *game_folder* (so they're never packed into the
    image) and INTO *dest_dir* (next to the final .ffpfsc), preserving them for the
    user instead of deleting them.

    Non-game, at the TOP LEVEL of the dump only:
      • any folder OR file whose name starts with '_' - the tooling convention
        (``_bundle_``, ``_bundle_``, ``_update``, …). A real PS5 dump never uses a
        leading underscore at the game root (its entries are sce_sys, sce_module,
        eboot.bin, Data, …), and our own injected fakelib/ + ampr_emu.index don't
        either, so they are safe.
      • loose side-car metadata files (.nfo/.sfv/.diz/.par2).
    OS metadata (.DS_Store, ._*, __MACOSX, …) is NOT moved - _strip_junk_files deletes
    it. Only the top level is scanned, so a '_'-named folder DEEP inside real game data
    is left alone. Moving (not deleting) makes even a wrong guess recoverable - it just
    lands beside the .ffpfsc. Returns the number of entries moved."""
    game_folder = Path(game_folder)
    try:
        entries = sorted(game_folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return 0
    to_move = [
        p for p in entries
        if not _is_os_junk_name(p.name)
        and (p.name.startswith("_")
             or (p.is_file() and p.suffix.lower() in _EXTRA_FILE_EXTS))
    ]
    if not to_move:
        return 0
    dest_dir = Path(dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[WARN] Could not create a place for non-game extras ({e}) - leaving "
              f"them in the game (they will be packed).", flush=True)
        return 0
    moved = 0
    for p in to_move:
        target = dest_dir / p.name
        if target.exists():
            # Never overwrite something already beside the output - pick a free name.
            stem = target.stem if target.is_file() else target.name
            suf = target.suffix if target.is_file() else ""
            i = 2
            while (dest_dir / f"{stem} ({i}){suf}").exists():
                i += 1
            target = dest_dir / f"{stem} ({i}){suf}"
        try:
            shutil.move(str(p), str(target))
            moved += 1
            kind = "folder" if target.is_dir() else "file"
            print(f"[INFO] Non-game {kind} '{p.name}' moved next to the output "
                  f"(kept out of the image, preserved for you).", flush=True)
        except Exception as e:
            print(f"[WARN] Could not move '{p.name}' out of the game ({e}) - it will be packed.", flush=True)
    return moved


def pack_folder_uncompressed(
    game_folder: Path,
    pfs_path: Path,
    mkpfs_cmd_base: list[str],
    mkpfs_cwd: str | None,
    *,
    verify_enabled: bool = False,
    compression_level: int = 7,
    cpu_count: int = 0,
    threshold_gain: int = 5,
    block_size: str = "auto",
    verbose: bool = False,
    temp_folder: Path | None = None,
    replace_existing: bool = False,
) -> None:
    # Never pack OS metadata junk (._*, .DS_Store, Spotlight/Trash dirs, …) into the
    # image. Done here so EVERY folder-pack path (normal pack + patch repack) is covered.
    _stripped = _strip_junk_files(game_folder)
    if _stripped:
        print(f"[INFO] Removed {_stripped} macOS/Windows junk file(s)/folder(s) before packing.", flush=True)
    print(f"[INFO] Packing folder {game_folder.name} to uncompressed PFS image {pfs_path.name}...")
    # An existing output is replaced only by a FINISHED build (see _stage_build_output).
    build_path = _stage_build_output(pfs_path, replace_existing)
    cmd = mkpfs_cmd_base + [
        "pack", "folder",
        # MkPFS 1.0.0: `pack folder` now DEFAULTS to wrapping the folder in an exFAT image
        # and compressing it in one pass; --raw restores the 0.0.8 behaviour pass 1 relies
        # on - a DIRECT, uncompressed folder -> PFS image (the required inner .ffpfs).
        "--raw",
        # MkPFS 1.0.0 auto-builds the AMPR emulation index during packing; the app builds
        # its own index GUI-side (authoritative - signed before indexing), so suppress it.
        "--no-ampr-index",
        "--no-compress",
        "--no-adjust-output-file-extension",
        "--version", "PS5",
        "--inode-bits", "32",
        "--block-size", str(block_size),
    ]
    if temp_folder:
        cmd += ["--temp-folder", str(temp_folder)]
    if verbose:
        cmd.append("--verbose")
    if verify_enabled:
        print("[INFO] Post-pack verify is ENABLED (full check against the source folder - slower, more RAM).", flush=True)
        cmd.append("--verify")
    else:
        # "Verify Output" off → skip the post-pack verify entirely. Without this,
        # mkpfs runs its DEFAULT structure verify, which still compares the image's
        # file list against the source folder and fails the whole build on a single
        # discrepancy (a stray .DS_Store, an empty file, an extraction artifact) - 
        # verification the user never asked for. The final .ffpfsc still gets a cheap
        # internal structure check in the compress pass.
        print("[INFO] Post-pack verify is off (enable 'Verify Output' to check against the source). Skipping it.", flush=True)
        cmd.append("--no-verify-structure")
    cmd += [str(game_folder), str(build_path)]
    print(f"[INFO] Running: {shlex.join(cmd)}", flush=True)
    total = _tree_bytes(game_folder)
    try:
        proc = subprocess.Popen(cmd, cwd=mkpfs_cwd)
        try:
            _meter_process(proc, total, f"write the PFS image on {_drive_label(build_path.parent)}: "
                                        f"{{done}} of {{total}} GB")
        finally:
            rc = proc.wait()
        if rc:
            raise subprocess.CalledProcessError(rc, cmd)
    except subprocess.CalledProcessError as e:
        _discard_build_output(build_path, pfs_path)
        _mkpfs_error_hint(e, pfs_path)
        sys.exit(1)
    try:
        _commit_build_output(build_path, pfs_path)
    except OSError as e:
        _discard_build_output(build_path, pfs_path)
        print(f"[ERROR] Could not replace the existing output file {pfs_path}: {e}", flush=True)
        sys.exit(1)
    print(f"[OK] Uncompressed PFS creation complete: {pfs_path}")


def _looks_incompressible(path: Path, *, samples: int = 24, chunk: int = 1 << 20,
                          min_gain_pct: float = 2.0) -> bool:
    """Cheap heuristic: sample ~`samples` x `chunk` bytes spread across `path`,
    zlib-compress them, and return True if the aggregate gain is below
    `min_gain_pct`%.

    Used to skip the expensive deflate on already-compressed games: when the inner
    image barely shrinks, the .ffpfsc would store every block raw anyway (the per-
    block threshold rejects non-shrinking blocks), so a level-0 pass produces the
    same container far faster. Conservative by design - on any error, a small file,
    or genuine compressibility it returns False (i.e. compress normally), so the
    worst case is a slightly-larger-but-correct file, never a broken one."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < 64 * (1 << 20):   # small images: just compress normally
        return False
    raw_total = 0
    comp_total = 0
    try:
        with path.open("rb") as fh:
            step = max(chunk, size // max(1, samples))
            offset = 0
            taken = 0
            while offset < size and taken < samples:
                fh.seek(offset)
                buf = fh.read(chunk)
                if not buf:
                    break
                raw_total += len(buf)
                comp_total += len(zlib.compress(buf, 6))
                taken += 1
                offset += step
    except OSError:
        return False
    if raw_total == 0:
        return False
    gain_pct = (1.0 - comp_total / raw_total) * 100.0
    return gain_pct < min_gain_pct


_PATCH_TITLE_RE = re.compile(r'\b(PPSA\d{5}|CUSA\d{5})\b')
def _is_wrapper_noise(name: str) -> bool:
    """A file beside a wrapper folder that is not content: an AppleDouble `._*` sidecar (an
    exFAT temp drive gets one for every folder that carries an xattr, right after an archive
    is unpacked there), Finder/Explorer leftovers, our Spotlight marker, or the notes a
    release ships beside its files."""
    return _is_junk_name(name) or name == ".metadata_never_index" or bool(_PATCH_NOTES_RE.match(name))


def _patch_descend_wrapper(root: Path) -> Path:
    """Descend folders that hold exactly one subdir and no content of their own, so a patch
    or game wrapped in an extra folder (e.g. <CUSA...>/eboot.bin) resolves to its real root.
    OS and archiver leftovers beside the wrapper (see _is_wrapper_noise, __MACOSX) do not
    count: with them counted, a patch unpacked on an exFAT drive was copied into the game
    as a whole folder, which then held a second eboot.bin and sce_sys."""
    cur = root
    for _ in range(8):
        try:
            entries = list(cur.iterdir())
        except Exception:
            break
        files = [p for p in entries if p.is_file() and not _is_wrapper_noise(p.name)]
        dirs = [p for p in entries if p.is_dir() and not _is_junk_name(p.name)]
        if not files and len(dirs) == 1:
            cur = dirs[0]
        else:
            break
    return cur


def _patch_find_game_root(folder: Path) -> Path:
    """Locate the game root (the dir holding sce_sys/eboot.bin) inside an unpacked tree."""
    cand = _patch_descend_wrapper(folder)
    if (cand / "sce_sys").exists() or (cand / "eboot.bin").exists():
        return cand
    for p in sorted(folder.rglob("sce_sys")):
        if p.is_dir():
            return p.parent
    return cand


def _patch_dir_title_id(d: Path) -> str:
    """Best-effort title id for a game/patch directory (param.json, else folder name)."""
    try:
        pj = d / "sce_sys" / "param.json"
        if pj.is_file():
            m = _PATCH_TITLE_RE.search(pj.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return m.group(1).upper()
    except Exception:
        pass
    m = _PATCH_TITLE_RE.search(d.name)
    return m.group(1).upper() if m else ""


# Notes a release puts beside a patch's files (top level only): not part of the game.
_PATCH_NOTES_RE = re.compile(r"^(readme.*|sha\d*sums?(\..*)?|.*\.(nfo|sfv|md5|sha1|sha256|diz|url))$", re.I)


def _param_json_of(root: Path) -> dict:
    try:
        return json.loads((root / "sce_sys" / "param.json").read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}


def _param_int(value) -> int | None:
    try:
        text = str(value).strip()
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except Exception:
        return None


def patch_brings_ampr(patch_dir: Path) -> bool:
    """True when the patch ships its own AMPR emulator (fakelib/libSceAmpr.sprx). That
    emulator builds its index itself; an index from the game's dump does not belong to it."""
    return (_patch_descend_wrapper(patch_dir) / "fakelib" / "libSceAmpr.sprx").is_file()


def check_patch_fits(game_root: Path, patch_root: Path) -> None:
    """Refuse a patch for another game (title id). A backport (a patch whose SDK is lower
    than the game's) only fits the version it was made for: warn when the versions differ."""
    gp, pp = _param_json_of(game_root), _param_json_of(patch_root)
    gtid = str(gp.get("titleId") or _patch_dir_title_id(game_root) or "").upper()
    ptid = str(pp.get("titleId") or "").upper()
    if gtid and ptid and gtid != ptid:
        raise RuntimeError(f"the patch is for {ptid}, the game is {gtid}: not applied")
    gsdk, psdk = _param_int(gp.get("sdkVersion")), _param_int(pp.get("sdkVersion"))
    gver, pver = str(gp.get("contentVersion") or ""), str(pp.get("contentVersion") or "")
    if gsdk and psdk and psdk < gsdk and gver and pver and gver != pver:
        print(f"[WARN] This patch is a backport made for version {pver}; the game is {gver}. A backport "
              f"usually works only with the version it was made for.", flush=True)


def new_patch_backup(scratch_root: Path, patch_name: str) -> Path:
    """A folder for the game's files a patch replaces or removes, named after the patch.
    Staged on the temp drive; the app moves it beside the output once the job is done."""
    stage = Path(tempfile.mkdtemp(prefix="patch-backup-", dir=str(scratch_root)))
    stem = re.sub(r"\.(zip|rar|7z)$", "", Path(patch_name).name, flags=re.I)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", f"Original files - {stem}").strip(" .") or "Original files"
    return stage / name[:200]


def _write_patch_backup_readme(backup_dir: Path, patch_name: str, game: dict, done: dict) -> None:
    title = (game.get("localizedParameters") or {}).get((game.get("localizedParameters") or {}).get(
        "defaultLanguage", "en-US"), {}).get("titleName", "") if isinstance(game.get("localizedParameters"), dict) else ""
    lines = [
        f"Original files of {title or 'the game'} [{game.get('titleId', '?')}] v{game.get('contentVersion', '?')},",
        f"from before this patch was integrated: {patch_name}",
        f"Built by PS5 UltraPack on {time.strftime('%Y-%m-%d %H:%M')}.",
        "",
        "The folder app0/ holds every file of the game that the patch replaced or removed, at its",
        "path inside the game. To get the unpatched game back: unpack the built image or package to",
        "a folder, copy everything in app0/ back over it, delete the files listed under \"Added by",
        "the patch\", and pack it again.",
        "",
    ]
    for key, head in (("replaced", "Replaced by the patch"), ("removed", "Removed for the patch"),
                      ("added", "Added by the patch")):
        items = done.get(key) or []
        lines.append(f"{head} ({len(items)}):")
        lines += [f"  {r}" for r in items] or ["  (none)"]
        lines.append("")
    (backup_dir / "README.txt").write_text("\n".join(lines), encoding="utf-8")


def overlay_patch(game_root: Path, patch_dir: Path, backup_dir: Path | None = None, patch_name: str = "") -> int:
    """Copy every file from the patch onto the game at matching relative paths,
    overwriting existing files and adding new ones. Skips OS/archiver junk and the notes a
    release puts beside the files. Refuses a patch for another game. When the patch brings
    its own AMPR emulator, the game's old ampr_emu.index is removed (the emulator builds a
    fresh one). With *backup_dir*, every file it replaces or removes is kept there first
    (under app0/), with a README naming the patch. Returns the number of files applied."""
    src_root = _patch_descend_wrapper(patch_dir)
    if not (src_root / "sce_sys" / "param.json").is_file():
        # A game-shaped folder below the root means the wrapper was not resolved; copying it
        # as is would put a second game inside the game. Stop here instead.
        nested = [p for p in sorted(src_root.iterdir()) if p.is_dir() and (p / "sce_sys" / "param.json").is_file()]
        if nested:
            raise RuntimeError(f"the patch's files sit in the folder '{nested[0].name}', beside other things the "
                               f"app could not sort out ({', '.join(sorted(q.name for q in src_root.iterdir() if q != nested[0])[:5])}): "
                               f"not applied. Unpack the patch so that its files are at the top, or report this.")
    check_patch_fits(game_root, src_root)
    game_param = _param_json_of(game_root)
    done = {"replaced": [], "removed": [], "added": []}
    count = 0
    for src in sorted(src_root.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_root)
        if any(_is_junk_name(part) for part in rel.parts):
            continue
        if len(rel.parts) == 1 and _PATCH_NOTES_RE.match(rel.name):
            print(f"[INFO] Left out {rel.name}: release notes, not part of the game.", flush=True)
            continue
        dst = game_root / rel
        try:
            if dst.is_file():
                if backup_dir is not None:
                    keep = backup_dir / "app0" / rel
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, keep)
                done["replaced"].append(rel.as_posix())
            else:
                done["added"].append(rel.as_posix())
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
        except Exception as e:
            print(f"[WARN] Could not apply patch file {rel}: {e}", flush=True)
    if patch_brings_ampr(patch_dir):
        for name in ("ampr_emu.index", "ampr_emu.index.tmp"):
            stale = game_root / name
            if stale.is_file():
                if backup_dir is not None:
                    (backup_dir / "app0").mkdir(parents=True, exist_ok=True)
                    shutil.move(str(stale), str(backup_dir / "app0" / name))
                else:
                    stale.unlink()
                done["removed"].append(name)
                print(f"[INFO] Removed the game's {name}: the patch brings its own AMPR emulator, "
                      f"which builds a fresh index.", flush=True)
    if backup_dir is not None and (done["replaced"] or done["removed"]):
        _write_patch_backup_readme(backup_dir, patch_name or Path(patch_dir).name, game_param, done)
        print(f"[INFO] Kept {len(done['replaced']) + len(done['removed'])} original file(s) the patch replaced "
              f"or removed; they go beside the output when the job is done.", flush=True)
        print(f"[PATCH-BACKUP] {backup_dir}", flush=True)
    elif backup_dir is not None:
        shutil.rmtree(backup_dir.parent if backup_dir.parent.name.startswith("patch-backup-") else backup_dir,
                      ignore_errors=True)
    return count


def compress_file_to_ffpfsc(
    source_file: Path,
    ffpfsc_path: Path,
    mkpfs_cmd_base: list[str],
    mkpfs_cwd: str | None,
    *,
    compression_level: int = 7,
    cpu_count: int = 0,
    threshold_gain: int = 5,
    block_size: str = "auto",
    verbose: bool = False,
    temp_folder: Path | None = None,
    replace_existing: bool = False,
) -> None:
    print(f"[INFO] Compressing {source_file.name} to outer container {ffpfsc_path.name} using MkPFS...")
    # An existing output is replaced only by a FINISHED build (see _stage_build_output).
    build_path = _stage_build_output(ffpfsc_path, replace_existing)
    cmd = mkpfs_cmd_base + [
        "pack", "file",
        "--compress",
        # MkPFS 1.0.0 defaults the compressor to "auto" (isal/zlib-ng when installed); pin
        # stdlib zlib so the compressed PFSC bytes stay IDENTICAL to 0.0.8's output. A
        # bundled faster backend would change the DEFLATE bytes and force a fresh on-console
        # boot test; identical bytes mean the existing 0.0.8 console verification still holds.
        "--compression-backend", "zlib",
        "--version", "PS5",
        "--inode-bits", "32",
        "--compression-level", str(compression_level),
        "--cpu-count", str(cpu_count),
        "--threshold-gain", str(threshold_gain),
        "--block-size", str(block_size),
    ]
    if temp_folder:
        cmd += ["--temp-folder", str(temp_folder)]
    if verbose:
        cmd.append("--verbose")
    if build_path != ffpfsc_path:
        # mkpfs would otherwise "fix" the staging name's suffix (.partial -> .ffpfsc)
        # and write straight over the file we are protecting.
        cmd.append("--no-adjust-output-file-extension")
    cmd += [str(source_file), str(build_path)]
    print(f"[INFO] Running: {shlex.join(cmd)}", flush=True)
    try:
        subprocess.run(cmd, cwd=mkpfs_cwd, check=True)
    except subprocess.CalledProcessError as e:
        _discard_build_output(build_path, ffpfsc_path)
        _mkpfs_error_hint(e, ffpfsc_path)
        sys.exit(1)
    try:
        _commit_build_output(build_path, ffpfsc_path)
    except OSError as e:
        _discard_build_output(build_path, ffpfsc_path)
        print(f"[ERROR] Could not replace the existing output file {ffpfsc_path}: {e}", flush=True)
        sys.exit(1)
    print(f"[OK] Compression complete: {ffpfsc_path}")


def _dir_size(path: Path) -> int:
    """Sum of file sizes under *path* (cheap stat walk; PS5 games have modest file counts)."""
    total = 0
    try:
        for dirpath, _d, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except Exception:
        pass
    return total


def unpack_pfs_image(
    image_file: Path,
    output_dir: Path,
    mkpfs_cmd_base: list[str],
    mkpfs_cwd: str | None,
    *,
    overwrite: bool = False,
) -> None:
    print(f"[INFO] Extracting {image_file.name} to {output_dir} using MkPFS...")
    cmd = mkpfs_cmd_base + ["unpack", str(image_file), str(output_dir)]
    if overwrite:
        cmd.append("--overwrite")
    print(f"[INFO] Running: {shlex.join(cmd)}", flush=True)
    # mkpfs unpack emits no incremental progress, so a big extraction (e.g. the inner image
    # of a 100+ GB game) looks frozen in the GUI for many minutes. Run it via Popen and,
    # while it works, poll the destination size and emit a GUI-parsable progress bar
    # ("[####----] NN% extract (X.X GB)") every ~12 s so the user sees live movement. The
    # source image size is a good size estimate for an inner-.ffpfs → folder extraction;
    # for an outer .ffpfsc (compressed) the % saturates early but the GB readout keeps moving.
    try:
        expected = image_file.stat().st_size if image_file.is_file() else 0
    except OSError:
        expected = 0
    try:
        proc = subprocess.Popen(cmd, cwd=mkpfs_cwd)
    except Exception as e:
        print(f"[ERROR] Could not start mkpfs unpack: {e}", flush=True)
        sys.exit(1)
    _last_pct = 0
    while True:
        try:
            rc = proc.wait(timeout=12)
            break
        except subprocess.TimeoutExpired:
            done = _dir_size(output_dir)
            gb = done / (1024 ** 3)
            if expected > 0:
                pct = max(1, min(99, int(done * 100 / expected)))
            else:
                pct = min(99, _last_pct + 2)
            _last_pct = pct
            filled = pct // 5
            bar = "#" * filled + "-" * (20 - filled)
            print(f"[{bar}] {pct}% extract ({gb:.1f} GB)", flush=True)
    if rc != 0:
        print(f"[ERROR] mkpfs unpack failed with exit code {rc}.", flush=True)
        print(f"[ERROR] Source image: {image_file}", flush=True)
        print(f"[ERROR] Output folder: {output_dir}", flush=True)
        sys.exit(1)
    print(f"[OK] Extraction complete: {output_dir}")


def resolve_unpack_output_dir(image_file: Path, requested_output: Path, *, batch: bool = False) -> Path:
    if batch or requested_output == Path(".").resolve():
        return requested_output / f"{image_file.stem}_extracted"
    return requested_output


# ─────────────────────────────────────────────────────────────────────────────
# CPU auto-cap (OOM safety)
# ─────────────────────────────────────────────────────────────────────────────

def _auto_cap_cpu(requested: int, source: Path) -> int:
    """Effective mkpfs worker count. When the user left CPU cores on AUTO (0), cap the
    worker count for large sources: mkpfs spawns one worker per core and each buffers
    compressed blocks in RAM, so a big game can backlog memory and get OOM-killed
    (a silent SIGKILL seen on a large title). >30 GB -> 2 workers, >10 GB -> 4; smaller -> mkpfs default.
    An explicit non-zero count is always honoured untouched."""
    requested = max(0, int(requested or 0))
    if requested:
        return requested
    try:
        src = Path(source)
        if src.is_file():
            size = src.stat().st_size
        else:
            size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        GB = 1024 ** 3
        if size > 30 * GB:
            cap = 2
        elif size > 10 * GB:
            cap = 4
        else:
            return 0   # small source - let mkpfs use its default (all cores)
        cap = min(cap, max(1, os.cpu_count() or 4))
        print(f"[INFO] Source is {size / GB:.1f} GB - auto-capping mkpfs workers to {cap} "
              f"to prevent out-of-memory (override with the CPU cores setting).", flush=True)
        return cap
    except Exception:
        return 0   # stat failed - leave at mkpfs default


def _fake_sign_tree(folder) -> dict:
    """Recursively fake-sign every executable under *folder*, in place.

    Thin wrapper around backend/fake_sign.py (which imports the vendored
    make_fself). Routes the signer's per-file lines through print(..., flush=True)
    so the GUI's stdout scraper shows them live. Returns the counts dict."""
    # Strip OS junk here too, so EVERY folder operation (pack, patch, via-exfat AND
    # the standalone Fake Sign tool) cleans macOS/Windows metadata - never any junk
    # left behind regardless of which action touched the folder.
    _stripped = _strip_junk_files(Path(folder))
    if _stripped:
        print(f"[INFO] Removed {_stripped} macOS/Windows junk file(s)/folder(s).", flush=True)
    if _CLI_DIR not in sys.path:
        sys.path.insert(0, _CLI_DIR)
    from fake_sign import fake_sign_tree
    return fake_sign_tree(str(folder), log=lambda m: print(m, flush=True))


_EXECUTABLE_SUFFIXES = (".prx", ".sprx", ".elf", ".self")


def _is_executable_name(rel: str) -> bool:
    """eboot.bin and every module: the files a backport reads and changes."""
    name = rel.rsplit("/", 1)[-1].lower()
    return name == "eboot.bin" or name.endswith(_EXECUTABLE_SUFFIXES)


def _pull_executables(src: Path, dest: Path) -> bool:
    """Copy only the executables of the game in a .pkg, .ffpfs or .ffpfsc into *dest*,
    in their folders, without unpacking the rest (the package tool and the PFS reader
    both extract single members). A backport check needs nothing else, so it can run
    in seconds before a job unpacks the whole game. False for other sources."""
    src, dest = Path(src), Path(dest)
    suf = src.suffix.lower()
    if suf == ".pkg":
        import fpkg as _fpkg
        listing = _fpkg.list_inner(src)
    elif suf in (".ffpfs", ".ffpfsc"):
        listing = list_pfs_image(src)
    else:
        return False
    names = [str(e.get("path", "")).strip("/") for e in listing.get("entries", [])
             if e.get("type") == "file" and _is_executable_name(str(e.get("path", "")))]
    if not names:
        return False
    dest.mkdir(parents=True, exist_ok=True)
    if suf == ".pkg":
        members = dest.parent / f".{dest.name}.members.txt"
        members.write_text("\n".join(names) + "\n", encoding="utf-8")
        try:
            rc = _fpkg.extract_members(src, dest, members, on_line=lambda _l: None)
        finally:
            members.unlink(missing_ok=True)
    else:
        rc = extract_pfs_members(src, names, dest)
    if rc:
        raise RuntimeError(f"could not read the executables out of {src.name} (rc={rc})")
    return True


def _backport_precheck(folder, target: str, libs_root=None, fw_root=None):
    """Everything that can stop a backport, decided on the executables under *folder*
    before a byte changes. Returns (sdk words, patched libraries folder or None);
    raises with the reason when the backport cannot work."""
    import backport as _bp
    folder = Path(folder)
    if libs_root is not None and not Path(libs_root).is_dir():
        raise FileNotFoundError(f"--backport-libs is not a folder: {libs_root}")
    misuse = _bp.patched_libs_problem(libs_root, fw_root)
    if misuse:
        raise RuntimeError(misuse[0].upper() + misuse[1:] + ".")
    words = _bp.target_words(target, fw_root)
    locked = _bp.encrypted_selfs(folder)
    if locked:
        names = ", ".join(str(p.relative_to(folder)) for p in locked[:4]) + (", …" if len(locked) > 4 else "")
        raise RuntimeError(f"{len(locked)} executable(s) are encrypted ({names}). A backport needs "
                           f"decrypted or fake-signed executables.")
    libs = _bp.patched_libs_folder(libs_root, target) if libs_root else None
    if libs_root and libs is None:
        print(f"[WARN] Backport: {libs_root} holds no patched libraries for {target}.", flush=True)
    if fw_root:
        check = _bp.analyse_backport(folder, target, fw_libs_root=fw_root, backport_libs_root=libs_root)
        missing = check.unresolved_count()
        if check.firmware_note or not check.firmware_checked:
            print(f"[WARN] Backport: {check.verdict()}", flush=True)
        elif missing and libs is None:
            raise RuntimeError(check.verdict(patched=False))
        else:
            print(f"[{'WARN' if missing else 'INFO'}] Backport: {check.verdict(patched=libs is not None)}",
                  flush=True)
    else:
        print(f"[WARN] Backport: no firmware libraries folder given, so the functions the game uses "
              f"were not checked against {target}.", flush=True)
    return words, libs


def _apply_backport(folder, target: str, libs_root=None, fw_root=None) -> None:
    """Backport pass on *folder*, IN PLACE. Runs before fake-sign in the same
    invocation. Everything that can stop the job is decided before a byte changes:

    1. The target's SDK words: the public table for 7.61/6.02/10.xx, otherwise read
       from that firmware's own libraries under *fw_root* (one subfolder per firmware).
    2. Encrypted executables are refused: nothing in them can be read or changed.
    3. With *fw_root*, the check: which functions does the game use that the target
       firmware lacks? None: lowering the SDK is enough. Some: patched libraries for
       the target (*libs_root*, or its <target> subfolder) are required.
    4. Lower the SDK words in every eboot/prx/sprx, raw or fake-signed (in place).
    5. Copy the patched libraries into <folder>/fakelib/ (never overwrites files
       already there - the user's own copies win).

    The staging mirror is expected to be a copy already; the caller decides whether
    this modifies the user's own folder (fake-sign-first path) or the staging one
    (queue path)."""
    from pathlib import Path
    import backport as _bp
    folder = Path(folder)
    words, libs = _backport_precheck(folder, target, libs_root, fw_root)
    print(f"[INFO] Backport: lowering SDK to {target} (ps5 {words[0]:#010x}, ps4 {words[1]:#010x})",
          flush=True)
    report = _bp.lower_sdk_in_folder(folder, target, words)
    for path, changes in report.written:
        effective = [c for c in changes if not c.unchanged()]
        if effective:
            fields = ", ".join(f"{c.field}={c.before:#010x}->{c.after:#010x}" for c in effective)
            print(f"  [sdk] {path.relative_to(folder)}: {fields}", flush=True)
    if report.skipped_no_param:
        print(f"  [sdk] {len(report.skipped_no_param)} ELF(s) had no SCE param segment; "
              f"kept as-is (helper modules).", flush=True)
    if report.skipped_not_elf:
        print(f"  [sdk] {len(report.skipped_not_elf)} file(s) named like executables are not; "
              f"kept as-is.", flush=True)
    print(f"[INFO] Backport: {report.summary()}", flush=True)

    if libs is None:
        if libs_root is None:
            print("[INFO] Backport: no patched libraries given; nothing copied into fakelib/.", flush=True)
        return
    fakelib = folder / "fakelib"
    fakelib.mkdir(exist_ok=True)
    copied, kept = 0, 0
    for src in sorted(libs.iterdir()):
        if not src.is_file() or src.name.startswith("."):
            continue
        dst = fakelib / src.name
        if dst.exists():
            kept += 1
            continue
        dst.write_bytes(src.read_bytes())
        copied += 1
    print(f"[INFO] Backport: copied {copied} lib(s) into fakelib/; "
          f"kept {kept} existing file(s) as-is.", flush=True)


def _extract_archive_into(archive: Path, dest: Path, password: str | None = None) -> None:
    """Extract a .zip or a .rar (any volume of a multi-part set) into *dest*.
    Zip members are checked against Zip-Slip; the bundled rarfile guards that itself.
    Raises on failure with a readable message; the caller decides how to report it."""
    dest.mkdir(parents=True, exist_ok=True)
    suf = archive.suffix.lower()
    if suf == ".zip":
        with zipfile.ZipFile(archive) as zf:
            root = dest.resolve()
            wanted = []
            for member in zf.infolist():
                try:
                    (dest / member.filename).resolve().relative_to(root)
                except ValueError:
                    raise RuntimeError(f"ZIP path traversal blocked: {member.filename}")
                # OS and archiver clutter (__MACOSX/, ._*, .DS_Store, …) is not written at all.
                if any(_is_junk_name(part) for part in member.filename.replace("\\", "/").split("/") if part):
                    continue
                wanted.append(member)
            zf.extractall(dest, members=wanted, pwd=password.encode() if password else None)
        return
    first = archive
    m = re.match(r"^(?P<b>.*\.part)(?P<n>\d+)(?P<e>\.rar)$", archive.name, re.I)
    if m:
        cand = archive.with_name(f"{m.group('b')}{'1'.zfill(len(m.group('n')))}{m.group('e')}")
        if cand.exists():
            first = cand
    elif re.match(r"^.+\.r\d{2,}$", archive.name, re.I):
        cand = archive.with_suffix(".rar")
        if cand.exists():
            first = cand
    from unrar import rarfile  # bundled extractall already guards traversal
    with rarfile.RarFile(first, pwd=password or None) as rf:
        rf.extractall(str(dest))
    # The RAR reader extracts whole: clutter it wrote goes now, before anything reads the tree.
    _strip_junk_files(dest)


def _resolve_patch_dir(patch_arg: Path, td: Path, password: str | None = None) -> Path:
    """The folder holding a patch's files: *patch_arg* itself when it is a folder, else
    the archive extracted under *td*. A 7z patch is left to the GUI to pre-extract."""
    if patch_arg.is_file() and patch_arg.suffix.lower() in (".zip", ".rar"):
        patch_dir = td / "_patch"
        print(f"[INFO] Extracting patch archive '{patch_arg.name}'...", flush=True)
        _extract_archive_into(patch_arg, patch_dir, password)
        return patch_dir
    return patch_arg


# ── CHAIN MODE helpers: any source → [patch → backport → sign] → any output ──────
_CHAIN_KINDS = {".ffpfs": "ffpfs", ".ffpfsc": "ffpfsc", ".pkg": "pkg", ".exfat": "exfat",
                ".ffpkg": "ffpkg", ".zip": "zip", ".rar": "rar", ".r00": "rar"}


def _chain_source_kind(src: Path) -> str:
    if src.is_dir():
        return "folder"
    suf = src.suffix.lower()
    if re.match(r"^\.r\d{2,}$", suf):
        return "rar"
    return _CHAIN_KINDS.get(suf, suf.lstrip(".") or "file")


def _chain_materialize(src: Path, scratch_root: Path, args) -> tuple[Path, Path | None]:
    """Resolve *src* to a game folder. A folder is returned as is (owned = None). Every
    container is unpacked into a fresh scratch folder under *scratch_root* and the game
    root inside it is returned together with that scratch folder, which the caller
    removes when the job is done (or moves, for a folder output)."""
    if src.is_dir():
        return src, None
    scratch = Path(tempfile.mkdtemp(prefix="chain-", dir=str(scratch_root)))
    kind = _chain_source_kind(src)
    _phase("Extracting")
    print(f"[INFO] Unpacking {src.name} ({kind}) into {scratch} ({_describe_drive(scratch)}) before "
          f"the next step...", flush=True)
    try:
        if kind in ("exfat", "ffpkg"):
            if not _extract_exfat_to(src, scratch):
                raise RuntimeError("could not read the exFAT/UFS image on this platform")
        elif kind in ("ffpfs", "ffpfsc"):
            if not _unwrap_pfs_one_pass(src, scratch):
                cmd, cwd = _locate_mkpfs()
                unpack_pfs_image(src, scratch, cmd, cwd, overwrite=True)
                _fully_unwrap(scratch, cmd, cwd)
        elif kind == "pkg":
            import fpkg as _fpkg
            content = _pkg_content_size(src)
            total = _pkg_extract_plan(src, content)
            if content:
                print(f"[INFO] The package holds {content / 1024 ** 3:.2f} GB of files. Unpacking it writes "
                      f"about {total / 1024 ** 3:.2f} GB on {_drive_label(scratch)} in three steps (decrypt, "
                      f"unpack the game image, write the files) and needs about "
                      f"{2 * content / 1024 ** 3:.2f} GB there at the end.", flush=True)
            tool = {}
            rc = _run_with_folder_progress(
                lambda: _fpkg.extract(src, scratch, passcode=args.fpkg_passcode,
                                      on_line=lambda l: print(l, flush=True),
                                      on_start=lambda proc: tool.update(pid=proc.pid)),
                scratch, total, lambda: _pkg_extract_label(scratch),
                measure=lambda: _proc_bytes_written(tool["pid"]) if "pid" in tool else 0)
            if rc != 0:
                raise RuntimeError(f"fPKG extract failed (rc={rc})")
        elif kind in ("zip", "rar"):
            _extract_archive_into(src, scratch, args.password)
        else:
            raise RuntimeError(f"unsupported source type '{src.suffix}' - expected a folder, "
                               ".zip/.rar, .exfat/.ffpkg, .ffpfs, .ffpfsc or .pkg")
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    _strip_junk_files(scratch)
    return _patch_find_game_root(scratch), scratch


def _chain_transforms(root: Path, args, scratch_root: Path) -> list[str]:
    """Apply the requested content changes to *root*, in place, in the order patch →
    backport → sign. The order matters: a patch may add executables that must be
    lowered too, and fake-signing turns raw ELFs into SELFs the backport pass skips.
    Returns the list of changes applied (for the log)."""
    done: list[str] = []
    if args.patch or args.backport_target or getattr(args, "chain_sign", False):
        _phase("Reading Game")
    if args.patch:
        patch_arg = Path(args.patch).resolve()
        if not patch_arg.exists():
            raise FileNotFoundError(f"patch source not found: {patch_arg}")
        td = Path(tempfile.mkdtemp(prefix="chain-patch-", dir=str(scratch_root)))
        try:
            patch_dir = _resolve_patch_dir(patch_arg, td, args.password)
            applied = overlay_patch(root, patch_dir, new_patch_backup(scratch_root, patch_arg.name), patch_arg.name)
            if patch_brings_ampr(patch_dir):
                args._no_ampr_index = True          # its emulator builds the index itself
        finally:
            shutil.rmtree(td, ignore_errors=True)
        if applied == 0:
            raise RuntimeError("the patch contained no files to overlay")
        print(f"[OK] Applied {applied} patch file(s).", flush=True)
        done.append("patch")
    if args.backport_target:
        _apply_backport(root, args.backport_target, *_backport_dirs(args))
        done.append(f"backport {args.backport_target}")
    if getattr(args, "chain_sign", False):
        counts = _fake_sign_tree(root)
        if counts.get("failed"):
            raise RuntimeError(f"fake-sign reported {counts['failed']} failure(s)")
        done.append("sign")
    return done


@contextlib.contextmanager
def _extracted_zip_source(path: Path, *, temp_root=None, password: str | None = None):
    """Extract a ZIP source into a temporary folder and yield that folder.

    Only the extraction sits inside the try: the body that runs while the folder is
    yielded (the whole pack pipeline) must never have its own RuntimeError reported
    as "ZIP extraction failed"."""
    with tempfile.TemporaryDirectory(dir=temp_root) as tmpdir:
        try:
            with zipfile.ZipFile(path) as zf:
                wanted = []
                for member in zf.infolist():
                    dest = Path(tmpdir) / member.filename
                    try:
                        dest.resolve().relative_to(Path(tmpdir).resolve())
                    except ValueError:
                        print(f"[ERROR] ZIP path traversal detected: {member.filename}")
                        sys.exit(1)
                    # OS and archiver clutter (__MACOSX/, ._*, .DS_Store, …) is not written at all.
                    if any(_is_junk_name(part) for part in member.filename.replace("\\", "/").split("/") if part):
                        continue
                    wanted.append(member)
                zf.extractall(tmpdir, members=wanted, pwd=password.encode() if password else None)
        except (zipfile.BadZipFile, RuntimeError) as exc:
            print(f"[ERROR] ZIP extraction failed: {exc}")
            sys.exit(1)
        yield Path(tmpdir)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    _exit_with_parent()
    parser = argparse.ArgumentParser(
        description="PS5 UltraPack backend - create .ffpfsc containers or extract .ffpfs/.ffpfsc images."
    )
    parser.add_argument("game_folder", nargs='?', help="Source game folder, .exfat/.ffpkg file, or .ffpfs/.ffpfsc image")
    parser.add_argument("output", nargs='?', default=".", help="Output .ffpfsc file/directory, or extraction directory")
    parser.add_argument("--pack", dest="operation", action="store_const", const="pack", help="Force pack/compress mode")
    parser.add_argument("--unpack", dest="operation", action="store_const", const="unpack", help="Extract .ffpfs/.ffpfsc image(s)")
    parser.add_argument("--keep-pfs",     action="store_true", help="Keep intermediate pfs_image.dat")
    parser.add_argument("--no-compress",  dest="no_compress", action="store_true",
                        help="Emit the UNCOMPRESSED inner PFS image (.ffpfs) instead of wrapping "
                             "it in a compressed .ffpfsc. Faster to build (no pass 2) and to mount "
                             "(no decompression), at full size. Applies to game folders and .ffpfs "
                             "sources; .exfat/.ffpkg inputs are still compressed.")
    parser.add_argument("--via-exfat",    action="store_true",
                        help="Build an exFAT image of the game folder and compress THAT into "
                             ".ffpfsc (PSBrew's most-stable exfat->ffpfsc path; native "
                             "cross-platform writer, falls back to the two-pass folder image on failure)")
    parser.add_argument("--no-unwrap", dest="unwrap", action="store_false", default=True,
                        help="When unpacking, stop at the inner image instead of unwrapping "
                             "all the way to a game folder (default: unwrap to a folder)")
    parser.add_argument("--verify",       action="store_true", help="Run MkPFS post-build verification (slower, more RAM)")
    parser.add_argument("--batch",        action="store_true", help="Process all supported items found under source")
    parser.add_argument("-f", "--force", "--overwrite", dest="overwrite", action="store_true", help="Overwrite existing files")
    parser.add_argument("--password",     type=str, help="Password for ZIP/RAR archives")
    # MkPFS tuning flags (forwarded to mkpfs pack file)
    parser.add_argument("--compression-level", type=int, default=7,  metavar="0-9",
                        help="Zlib compression level (0=store, 9=max, default: 7)")
    parser.add_argument("--cpu-count",    type=int, default=0,  metavar="N",
                        help="CPU cores for compression (0=auto, default: 0)")
    parser.add_argument("--threshold-gain", type=int, default=5, metavar="PCT",
                        help="Minimum per-block compression gain %% to keep compressed (default: 5)")
    parser.add_argument("--block-size",   type=str, default="auto",
                        help="PFS block size in bytes, 'auto' (65536), or 'auto-fit' (default: auto)")
    parser.add_argument("--verbose",      action="store_true", help="Verbose per-file mkpfs output")
    parser.add_argument("--temp-dir",     type=str, default=None,
                        help="Temp folder for intermediate files (default: system temp). "
                             "Use a fast NVMe drive for best performance.")
    parser.add_argument("--spool-fallback-dir", type=str, default=None, metavar="DIR",
                        help="Output-drive root to spill the pass-2 spool into (under "
                             "<DIR>/_ffpfsc_temp) when --temp-dir can't hold image+spool. "
                             "Keeps the inner image on the fast temp drive for big games.")
    parser.add_argument("--patch",        type=str, default=None, metavar="DIR",
                        help="PATCH MODE: overlay the loose files in DIR onto the game "
                             "(a folder or an existing .ffpfsc), then (re)pack to OUTPUT.")
    parser.add_argument("--patch-inplace", action="store_true",
                        help="The game folder is a throwaway temp extract - overlay in "
                             "place instead of copying it first.")
    parser.add_argument("--fake-sign", type=str, default=None, metavar="DIR",
                        help="FAKE-SIGN MODE: recursively fake-sign every executable "
                             "(eboot.bin/.elf/.prx/.sprx) under DIR in place, then exit. "
                             "Already-signed files are skipped (idempotent). No pack/unpack.")
    parser.add_argument("--copy", type=str, default=None, metavar="SRC",
                        help="COPY MODE: transport SRC (.ffpfsc/.ffpfs/.pkg) to OUTPUT (a "
                             "folder) unchanged; --copy-mode says what happens to SRC. Used "
                             "for same-format queue items and the Organize flow.")
    parser.add_argument("--ps4-sort", type=str, default=None, metavar="SRC",
                        help="PS4 MODE: sort the PS4 packages in SRC (a .pkg or a folder) into "
                             "OUTPUT as '<Title> [CUSA…] [vX]/…' (UPDATE, DLC, 'DLC Pack' from four "
                             "DLCs on; a title folder already in OUTPUT is joined). --copy-mode says "
                             "what happens to each source package.")
    parser.add_argument("--if-exists", choices=("skip", "ask", "overwrite", "keep"), default="skip",
                        help="For --ps4-sort and --organize-into: what happens when an item is already in "
                             "the library (ask behaves like skip inside a job).")
    parser.add_argument("--title-name", type=str, default=None, metavar="ID",
                        help="Print the name the public title list gives a title id (PPSA… / CUSA…); "
                             "fetches the list into the app folder when it is missing or a week old.")
    parser.add_argument("--organize-scan", type=str, default=None, metavar="ROOT",
                        help="ORGANIZE: read every game folder, image, package and archive under ROOT and "
                             "plan the library layout in place. Prints 'ORGANIZE_PROGRESS: n/total name' "
                             "lines and writes the plan as JSON to --organize-plan (else prints "
                             "'ORGANIZE_PLAN: <json>'). Nothing is moved.")
    parser.add_argument("--organize-plan", type=str, default=None, metavar="FILE",
                        help="The plan file --organize-scan writes and --organize-apply reads.")
    parser.add_argument("--organize-apply", action="store_true",
                        help="ORGANIZE: carry out --organize-plan (renames on the same drive only) and "
                             "write --organize-journal.")
    parser.add_argument("--organize-only", type=str, default=None, metavar="I,J,...",
                        help="With --organize-apply: only these moves of the plan (0-based).")
    parser.add_argument("--organize-journal", type=str, default=None, metavar="FILE",
                        help="The journal --organize-apply writes and --organize-undo reads.")
    parser.add_argument("--organize-undo", action="store_true",
                        help="ORGANIZE: move back what --organize-journal records.")
    parser.add_argument("--organize-into", type=str, default=None, metavar="SRC",
                        help="ORGANIZE OUTPUT: write SRC (a game folder, image, package or a folder "
                             "holding them) into the library OUTPUT in its own format and under its "
                             "library name. --copy-mode keep copies, move moves; --if-exists applies.")
    parser.add_argument("--copy-name", type=str, default=None, metavar="NAME",
                        help="Destination filename for --copy (defaults to SRC's basename). "
                             "Auto-organize passes the library name here.")
    parser.add_argument("--stage-in-place", action="store_true",
                        help="The source folder is the caller's own working copy (an archive the app unpacked): "
                             "a .pkg is built right in it and its files are taken in as the package grows.")
    parser.add_argument("--copy-mode", choices=("keep", "organize", "move"), default="keep",
                        help="For --copy and a chain job that ends as a copy: keep (default) "
                             "leaves SRC in place (an APFS clone on the same drive); organize "
                             "renames on the same drive and copies across drives; move renames "
                             "on the same drive and deletes SRC after a checked cross-drive copy.")
    parser.add_argument("--fpkg-extract", type=str, default=None, metavar="PKG",
                        help="fPKG MODE: extract the /app0 inner files from a finalized "
                             "PS5 fake package (.pkg) into OUTPUT (a folder). No mkpfs "
                             "pipeline runs; requires the bundled ffpfsc-pkg-tool.")
    parser.add_argument("--fpkg-build", type=str, default=None, metavar="SRC",
                        help="fPKG MODE: build a debug PS5 fake package (.pkg) into OUTPUT "
                             "(a folder). SRC is a prepared /app0 folder OR a packed image "
                             "(.ffpfsc/.ffpfs/.exfat/.ffpkg - unwrapped to --temp-dir first, "
                             "the one-click image→fPKG conversion). Uses drakmor's "
                             "LibProsperoPkg 1.2.0. The identity comes from the source's "
                             "sce_sys/param.json; --content-id/--title-id/--fpkg-version/"
                             "--fpkg-title only fill what it lacks. --compression-level sets "
                             "the Kraken/zlib level, --temp-dir the staging drive.")
    parser.add_argument("--content-id", type=str, default=None,
                        help="fPKG build: 36-char content id (e.g. UP9000-PPSA00000_00-...). "
                             "Fallback only - sce_sys/param.json wins when it has one.")
    parser.add_argument("--title-id", type=str, default=None,
                        help="fPKG build: 9-char title id (e.g. PPSA00000). Fallback only.")
    parser.add_argument("--fpkg-title", type=str, default="",
                        help="fPKG build: human-readable title used when generating param.json. Fallback only.")
    parser.add_argument("--fpkg-version", type=str, default="01.000.000",
                        help="fPKG build: content version NN.NNN.NNN (default 01.000.000). Fallback only.")
    parser.add_argument("--fpkg-passcode", type=str, default="0"*32,
                        help="fPKG build/extract: 32-char passcode (default 32 zeroes).")
    parser.add_argument("--fpkg-inner", type=str, default="kraken",
                        choices=("none", "zlib", "kraken"),
                        help="fPKG build: extra codec layer over the whole inner image (every "
                             "file is Kraken-packed regardless): 'kraken' (block-level Kraken "
                             "layer; the configuration verified to launch on a retail PS5, "
                             "default), 'none' (no extra layer, console-untested), 'zlib' "
                             "(legacy PFSC layer).")
    parser.add_argument("--fpkg-kraken-backend", type=str, default="builtin",
                        choices=("automatic", "builtin", "publishingtools", "uncompressed"),
                        help="fPKG build: Kraken encoder policy. Default 'builtin' (pure "
                             "managed, no external DLL) - the only one whose output launches "
                             "on a console; 'uncompressed'/'automatic' fail with CE-100096-6. "
                             "'publishingtools' requires the Sony libScePubTools.dll AND "
                             "64-bit Windows - on any other OS LibProsperoPkg throws and the "
                             "build fails (no fallback).")
    parser.add_argument("--fpkg-deterministic", action="store_true",
                        help="fPKG build: produce byte-reproducible output (fixed seeds "
                             "and RSA wrapping; the timestamp still comes from --fpkg-version).")
    parser.add_argument("--fpkg-no-retail-normalize", action="store_true",
                        help="fPKG build: do NOT apply the retail fixes to a 'standard'-DRM "
                             "source (valid license entries, retail SELF flavour, Sony-style "
                             "param.json fields). Default is to apply them.")
    parser.add_argument("--fpkg-hdr-flag", type=str, default="auto", choices=("auto", "on", "off"),
                        help="fPKG build: param.json attribute bit 29 (HDR support). 'auto' "
                             "(default) keeps what the source declares; 'on' sets it, 'off' "
                             "clears it. A console on 'HDR when supported' switches to HDR "
                             "output for the title only when the bit is set.")
    parser.add_argument("--fpkg-regen-playgo", action="store_true",
                        help="fPKG build: discard the source's sce_sys/playgo-*.dat even when "
                             "they look valid and let the builder regenerate them. A corrupt "
                             "set is always regenerated.")
    parser.add_argument("--fpkg-no-fake-sign", action="store_true",
                        help="fPKG build: do not fake-sign raw ELFs found in the source.")
    parser.add_argument("--fpkg-no-ampr-index", action="store_true",
                        help="fPKG: keep the source's ampr_emu.index as it is (default: when "
                             "fakelib/libSceAmpr.sprx is shipped, rebuild it over the packed files)")
    parser.add_argument("--fpkg-pubtools-dll", type=str, default=None,
                        help="fPKG build: path to Sony libScePubTools.dll for the "
                             "'publishingtools' backend. Overrides the LIBPROSPERO_PUBTOOLS_DLL "
                             "env variable.")
    parser.add_argument("--fpkg-validate", type=str, default=None, metavar="PKG",
                        help="fPKG MODE: run a diagnostic checklist against PKG (magic, "
                             "header fields, CNT signature wrap, required sce_sys entries, "
                             "param.json coherence, eboot fake-self magic). Exits with a "
                             "non-zero status if any check fails.")
    parser.add_argument("--fake-sign-first", action="store_true",
                        help="Before packing a game FOLDER, fake-sign its executables in "
                             "place first (ignored for .exfat/.ffpkg/.ffpfs sources).")
    parser.add_argument("--backport-target", type=_backport_target_arg, default=None, metavar="FW",
                        help="BACKPORT: before fake-signing, lower the SDK version in the "
                             "game's eboot.bin and every prx/sprx (raw or fake-signed) to this "
                             "target's SDK words: 7.61 (public library patches), 6.02 "
                             "(small public library set), 10.xx (SDK only), or any firmware whose original "
                             "libraries are in --fw-libs-root (the words are read from them). "
                             "With --fw-libs-root the game's functions are checked first; if the "
                             "target lacks some, --backport-libs is required.")
    parser.add_argument("--to", dest="chain_to", choices=("folder", "ffpfs", "ffpfsc", "pkg"), default=None,
                        help="CHAIN: turn the SOURCE positional (folder, .zip/.rar, .exfat/.ffpkg, .ffpfs, "
                             ".ffpfsc or .pkg) into this output, applying --patch, --backport-target and "
                             "--sign on the way (in that order). One job for e.g. "
                             "'.ffpfsc → backport 7.61 → .ffpfsc'. Same format without changes = copy.")
    parser.add_argument("--sign", dest="chain_sign", action="store_true",
                        help="CHAIN: fake-sign eboot/prx/sprx after --patch and --backport-target.")
    parser.add_argument("--backport-libs", type=str, default=None, metavar="DIR",
                        help="BACKPORT: folder holding user-supplied, PATCHED Sony system "
                             "libraries for the selected --backport-target (never bundled "
                             "with this app). Each file is copied into <source>/fakelib/ "
                             "before packing. Nothing is done to files already there.")
    parser.add_argument("--param-report", type=str, default=None, metavar="PATH",
                        help="List every game under PATH (folders, .ffpfsc/.ffpfs, .pkg) with its title "
                             "id, version and whether param.json declares HDR support, then exit. Read-only.")
    parser.add_argument("--prepare-backport-libs", type=str, default=None,
                        choices=("7.61", "6.02"),
                        help="BACKPORT: download BestPig's BackPork BPS patches for TARGET, "
                             "apply them to your 10.01 libraries (--fw-libs-root), and write "
                             "the patched libraries into --backport-libs/<TARGET>/. Runs and "
                             "exits; the app's job dialog then finds them under Patched libraries.")
    parser.add_argument("--backport-analyze", type=str, default=None, metavar="SRC",
                        help="BACKPORT: read every eboot/prx/sprx under SRC, list the libraries and "
                             "NIDs it imports, and (with --fw-libs-root) compare them to the target "
                             "firmware and your --backport-libs. Read-only; nothing is built.")
    parser.add_argument("--fw-libs-root", type=str, default=None, metavar="DIR",
                        help="BACKPORT: folder with your ORIGINAL firmware libraries, one subfolder "
                             "per firmware named by its version (7.61/, 10.01/, ...; never bundled "
                             "with this app). The check reads <DIR>/<target>, --prepare-backport-libs "
                             "reads <DIR>/10.01, and every subfolder is a backport target.")
    parser.add_argument("--sdk-of", type=str, default=None, metavar="SRC",
                        help="Print the firmware the game in SRC needs (its eboot.bin's SDK version) as "
                             "JSON, then exit. SRC: a game folder, a .ffpfs/.ffpfsc or a .pkg. Read-only.")
    parser.add_argument("--list-image", type=str, default=None, metavar="IMG",
                        help="PFS BROWSE: print the directory tree of a .ffpfs/.ffpfsc as JSON "
                             "(only metadata blocks are decompressed), then exit.")
    parser.add_argument("--extract-from", type=str, default=None, metavar="IMG",
                        help="PFS BROWSE: extract selected members from a .ffpfs/.ffpfsc "
                             "(decompresses only their blocks). Needs --dest and --members-file.")
    parser.add_argument("--dest", type=str, default=None, metavar="DIR",
                        help="Destination folder for --extract-from.")
    parser.add_argument("--members-file", type=str, default=None, metavar="FILE",
                        help="Newline-separated relative paths (files or directories) to "
                             "extract, for --extract-from.")

    args = parser.parse_args()

    # ── PFS BROWSE MODE (list / extract; standalone, no temp, no mkpfs pack) ─────
    # A .pkg (fPKG) browses the same way: the bundled tool lists / extracts the inner
    # image block-wise (list-inner / extract-inner --members) and answers in the same
    # JSON shape and progress-line format, so the GUI dialog needs no second path.
    if args.param_report:
        sys.exit(param_report(Path(args.param_report).expanduser().resolve()))

    if args.prepare_backport_libs:
        target = args.prepare_backport_libs
        fw = Path(args.fw_libs_root).expanduser().resolve() if args.fw_libs_root else None
        out = Path(args.backport_libs).expanduser().resolve() if args.backport_libs else None
        if fw is None or out is None:
            print("[ERROR] --prepare-backport-libs needs --fw-libs-root (your 10.01 libraries) "
                  "and --backport-libs (where the patched files land)", flush=True)
            sys.exit(2)
        import backport as _bp
        import backport_libs as _bpl
        fw10 = _bp.firmware_folder(fw, "10.01")
        if fw10 is None:
            print(f"[ERROR] no 10.01 folder in {fw}: the BackPork patches apply to 10.01 libraries only",
                  flush=True)
            sys.exit(1)
        fw = fw10
        # Same profile folder as the GUI (tests point PS5_FFPFSC_APP_DIR at a scratch one).
        _profile = os.environ.get("PS5_FFPFSC_APP_DIR", "").strip()
        cache = (Path(_profile) if _profile else
                 Path.home() / "Library" / "Application Support" / "PS5_UltraPack") / "backport-patches"
        try:
            r = _bpl.prepare_target(target, fw, out, cache_dir=cache,
                                    log=lambda m: print(m, flush=True))
        except _bpl.BackportLibsError as e:
            print(f"[ERROR] {e}", flush=True); sys.exit(1)
        print(f"[prepare] {r.summary()}", flush=True)
        sys.exit(0 if not r.failed else 1)

    if args.backport_analyze:
        if not args.backport_target:
            print("[ERROR] --backport-analyze needs --backport-target (7.61, 6.02, 10.xx or a firmware)",
                  flush=True)
            sys.exit(2)
        src = Path(args.backport_analyze).expanduser().resolve()
        _analyze_tmp = None
        if src.is_file() and src.suffix.lower() in (".pkg", ".ffpfs", ".ffpfsc"):
            # the executables alone answer the question; the rest stays packed
            _analyze_tmp = tempfile.TemporaryDirectory(prefix="analyze-")
            try:
                if not _pull_executables(src, Path(_analyze_tmp.name) / "game"):
                    print(f"[ERROR] --backport-analyze: no executables in {src.name}", flush=True)
                    sys.exit(1)
            except SystemExit:
                raise
            except Exception as e:
                print(f"[ERROR] --backport-analyze: {e}", flush=True)
                sys.exit(1)
            src = Path(_analyze_tmp.name) / "game"
        if not src.is_dir():
            print(f"[ERROR] --backport-analyze: not a folder, .pkg, .ffpfs or .ffpfsc: {src}", flush=True)
            sys.exit(1)
        import backport as _bp
        fw = Path(args.fw_libs_root).expanduser().resolve() if args.fw_libs_root else None
        fk = Path(args.backport_libs).expanduser().resolve() if args.backport_libs else None
        report = _bp.analyse_backport(src, args.backport_target, fw_libs_root=fw, backport_libs_root=fk)
        # human-readable, one line per library
        print(f"[analyse] {report.summary()}", flush=True)
        print(f"{'STATUS':<8} {'LIBRARY':<32} {'USED':>5} {'FW':>5} {'FAKE':>5} {'GAME':>5} {'MISS':>5}")
        for lr in report.per_library:
            print(f"{lr.status:<8} {lr.library:<32} "
                  f"{len(lr.used_nids):>5} {len(lr.firmware_covers):>5} "
                  f"{len(lr.fakelib_covers):>5} {len(lr.game_covers):>5} {len(lr.unresolved):>5}")
        if report.unnamed_imports:
            print(f"[warn] {report.unnamed_imports} import(s) had no resolvable library name")
        for name in report.unreadable[:20]:
            print(f"[warn] encrypted, not read: {name}")
        patched = bool(fk and not _bp.patched_libs_problem(fk, fw)
                       and _bp.patched_libs_folder(fk, args.backport_target))
        print(f"[verdict] {report.verdict(patched=patched)}", flush=True)
        # exit 0 when every function is covered, 1 otherwise - the shell can chain
        ok = report.firmware_checked and not report.firmware_note and not report.unreadable \
            and not report.unresolved_count()
        sys.exit(0 if ok else 1)

    if args.sdk_of:
        try:
            info = required_firmware(Path(args.sdk_of).expanduser().resolve())
        except Exception as e:
            print(f"[ERROR] --sdk-of: {e}", flush=True)
            sys.exit(1)
        print("SDK_JSON: " + json.dumps(info), flush=True)
        sys.exit(0 if info else 1)

    if args.list_image:
        img = Path(args.list_image).resolve()
        try:
            if img.suffix.lower() == ".pkg":
                import fpkg as _fpkg
                result = _fpkg.list_inner(img)
            else:
                result = list_pfs_image(img)
        except Exception as e:
            print(f"PFSBROWSE_ERROR: {e}", flush=True)
            sys.exit(1)
        print("PFSBROWSE_JSON: " + json.dumps(result), flush=True)
        return
    if args.extract_from:
        if not args.dest or not args.members_file:
            print("[ERROR] --extract-from requires --dest and --members-file.", flush=True)
            sys.exit(1)
        try:
            members = Path(args.members_file).read_text(encoding="utf-8").splitlines()
        except Exception as e:
            print(f"[ERROR] Could not read members file: {e}", flush=True)
            sys.exit(1)
        img = Path(args.extract_from).resolve()
        try:
            if img.suffix.lower() == ".pkg":
                import fpkg as _fpkg
                rc = _fpkg.extract_members(img, Path(args.dest).resolve(),
                                           Path(args.members_file).resolve(),
                                           on_line=lambda l: print(l, flush=True))
            else:
                rc = extract_pfs_members(img, members, Path(args.dest).resolve())
        except Exception as e:
            print(f"[ERROR] Extraction failed: {e}", flush=True)
            sys.exit(1)
        sys.exit(rc or 0)

    # ── BACKPORT MODE (standalone) ───────────────────────────────────────────────
    # Lower the SDK version of every eboot/prx/sprx under a folder, in place. Runs
    # before --fake-sign in the same invocation. Same standalone shape as fake-sign;
    # no positional needed. Applied to whatever is under --backport-target's PATH.
    _backport_dir = getattr(args, "fake_sign", None) or getattr(args, "fake_sign_first", None)
    if args.backport_target and not (args.batch or _backport_dir is False):
        # `--backport-target` alone (no --fake-sign, no game_folder positional).
        # We only run the standalone lowering if the user did NOT ask for anything
        # else - the pipeline paths call _apply_backport() themselves.
        pass
    if args.backport_target and args.fake_sign:
        try:
            _apply_backport(Path(args.fake_sign).resolve(), args.backport_target, *_backport_dirs(args))
        except Exception as e:
            print(f"[ERROR] Backport failed: {e}", flush=True)
            sys.exit(1)

    # ── FAKE-SIGN MODE (standalone) ──────────────────────────────────────────────
    # Pure folder operation: needs no game_folder positional, no mkpfs, no temp dirs.
    # Handle it before everything else and exit (matching how PATCH/unpack end).
    if args.fake_sign:
        fs_dir = Path(args.fake_sign).resolve()
        if not fs_dir.exists() or not fs_dir.is_dir():
            print(f"[ERROR] Fake-sign target not found or not a folder: {fs_dir}", flush=True)
            sys.exit(1)
        print(f"[INFO] Fake-sign mode: {fs_dir}", flush=True)
        try:
            counts = _fake_sign_tree(fs_dir)
        except Exception as e:
            print(f"[ERROR] Fake-sign failed: {e}", flush=True)
            sys.exit(1)
        if counts.get("failed"):
            print(f"\n[ERROR] Fake-sign finished with {counts['failed']} failure(s) - "
                  f"see the lines above.", flush=True)
            sys.exit(1)
        print(f"\n[SUCCESS] Fake-signed {counts.get('signed', 0)} file(s); "
              f"skipped {counts.get('skipped', 0)} non-ELF.", flush=True)
        return

    # ── COPY MODE (same-format transport, no re-encode) ──────────────────────────
    # Runs before every mkpfs-shaped path: --copy needs no game_folder positional
    # (SRC is on --copy), the OUTPUT positional is the destination folder.
    if args.title_name:
        import title_db
        import ultra_core
        name = title_db.lookup(args.title_name, Path(ultra_core.APP_DIR) / "titles")
        print(f"TITLE_NAME: {name}" if name else
              f"[ERROR] not in the title list" + (f" (fetch failed: {title_db.LAST_ERROR})" if title_db.LAST_ERROR else ""),
              flush=True)
        sys.exit(0 if name else 1)

    if (args.organize_scan is not None or args.organize_apply or args.organize_undo
            or args.organize_into is not None):
        sys.exit(_organize_main(args))

    if args.ps4_sort:
        import ps4_sort as _ps4_sort
        sys.exit(_ps4_sort.sort_packages(Path(args.ps4_sort).resolve(), Path(args.output).resolve(),
                                         mode=args.copy_mode, if_exists=args.if_exists))

    if args.copy:
        try:
            import copy_job as _copy_job
        except Exception as e:
            print(f"[ERROR] copy support unavailable ({e}). Rebuild backend/copy_job.py.", flush=True)
            sys.exit(1)
        src = Path(args.copy).resolve()
        dst_dir = Path(args.output).resolve()
        rc = _copy_job.run_copy(src, dst_dir,
                                dst_name=args.copy_name or None,
                                mode=args.copy_mode,
                                on_line=lambda l: print(l, flush=True))
        sys.exit(rc)

    # ── CHAIN MODE: any source → [patch → backport → sign] → any output ─────────
    # The one job shape the GUI's job dialog produces. Resolves the source to a game
    # folder (a container is unpacked into scratch), applies the requested changes,
    # then hands over to the path that already knows how to produce the output:
    # the mkpfs pack path (.ffpfs/.ffpfsc), the fPKG build (.pkg), a move (folder),
    # or the copy job (same format, nothing to change). Sources the target path takes
    # natively are passed straight through when there is nothing to change.
    if args.chain_to:
        if not args.game_folder or not args.output:
            print("[ERROR] --to needs SOURCE and OUTPUT positionals.", flush=True); sys.exit(2)
        src = Path(args.game_folder).resolve()
        out = Path(args.output).resolve()
        if not src.exists():
            print(f"[ERROR] Source path does not exist: {src}", flush=True); sys.exit(1)
        to = args.chain_to
        kind = _chain_source_kind(src)
        wanted = [n for flag, n in ((args.patch, "patch"),
                                    (args.backport_target, f"backport {args.backport_target}"),
                                    (args.chain_sign, "sign")) if flag]
        temp_root = Path(args.temp_dir).resolve() if args.temp_dir else Path(tempfile.gettempdir())
        scratch_root = temp_root / "_ffpfsc_temp"
        scratch_root.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] CHAIN: {src.name} ({kind}) → {', '.join(wanted) or 'no changes'} → {to}", flush=True)

        if not wanted and kind == to == "folder":
            print("[ERROR] Nothing to do: a folder to a folder with no changes.", flush=True); sys.exit(1)
        if not wanted and kind == to:
            # Same container format, nothing to change: the copy job (rename on the same
            # drive, chunked copy across drives). Same switches as --copy.
            import copy_job as _copy_job
            out.mkdir(parents=True, exist_ok=True)
            sys.exit(_copy_job.run_copy(src, out, dst_name=args.copy_name or None,
                                        mode=args.copy_mode,
                                        on_line=lambda l: print(l, flush=True)))

        native = {"ffpfs": {"folder", "exfat", "ffpkg", "ffpfs", "zip", "rar"},
                  "ffpfsc": {"folder", "exfat", "ffpkg", "ffpfs", "zip", "rar"},
                  "pkg": {"folder", "ffpfs", "ffpfsc", "exfat", "ffpkg"}}
        root, owned = src, None
        if args.backport_target and src.is_file() and kind in ("pkg", "ffpfs", "ffpfsc"):
            # Check the backport on the executables alone before the whole game is
            # unpacked: a refusal comes after seconds, not after the unpack.
            probe = Path(tempfile.mkdtemp(prefix="chain-check-", dir=str(scratch_root)))
            try:
                print(f"[INFO] Checking the backport to {args.backport_target} on the executables of "
                      f"{src.name} before unpacking it...", flush=True)
                if _pull_executables(src, probe / "game"):
                    _backport_precheck(probe / "game", args.backport_target, *_backport_dirs(args))
            except Exception as e:
                print(f"[ERROR] {e}", flush=True); sys.exit(1)
            finally:
                shutil.rmtree(probe, ignore_errors=True)
        if wanted or kind not in native.get(to, set()):
            try:
                root, owned = _chain_materialize(src, scratch_root, args)
                if owned is None and wanted:
                    print("[INFO] Applying the changes to the source folder in place.", flush=True)
                if wanted:
                    _chain_transforms(root, args, scratch_root)
            except SystemExit:
                if owned: shutil.rmtree(owned, ignore_errors=True)
                raise
            except Exception as e:
                if owned: shutil.rmtree(owned, ignore_errors=True)
                print(f"[ERROR] {e}", flush=True); sys.exit(1)

        if to == "folder":
            if owned is None:
                print(f"\n[SUCCESS] Changed in place: {root}", flush=True); return
            dest = out / f"{src.stem}_extracted" if out.is_dir() else out
            if dest.exists():
                if not args.overwrite:
                    shutil.rmtree(owned, ignore_errors=True)
                    print(f"[ERROR] Output folder already exists: {dest}  (use --overwrite)", flush=True); sys.exit(1)
                shutil.rmtree(dest, ignore_errors=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root), str(dest))
            shutil.rmtree(owned, ignore_errors=True)
            print(f"[OK] Folder ready: {dest}", flush=True)
            print("\n[SUCCESS] All operations completed successfully!", flush=True); return

        # Hand over. The changes are done, so the flags that would redo them are cleared;
        # an owned scratch is removed when the process ends (the startup sweep reclaims
        # a leftover under _ffpfsc_temp after a crash).
        if owned is not None:
            import atexit
            atexit.register(shutil.rmtree, str(owned), ignore_errors=True)
        args.patch = None; args.backport_target = None; args.backport_libs = None
        args.chain_sign = False; args.fake_sign_first = False; args.chain_to = None
        if to == "pkg":
            args._fpkg_in_place = owned is not None   # the tool may build right in our scratch
            args.fpkg_build = str(root)
        else:
            args.game_folder = str(root); args.operation = "pack"; args.no_compress = (to == "ffpfs")

    # ── fPKG MODE (extract / build / validate via bundled ffpfsc-pkg-tool) ──────
    if args.fpkg_extract or args.fpkg_build or args.fpkg_validate:
        try:
            import fpkg as _fpkg
        except Exception as e:
            print(f"[ERROR] fPKG support unavailable ({e}). Rebuild with backend/fpkg.py.", flush=True)
            sys.exit(1)
        if not _fpkg.is_available():
            print("[ERROR] Bundled ffpfsc-pkg-tool binary was not found in backend/native/. "
                  "Rebuild the app (see README).", flush=True)
            sys.exit(1)
        out_dir = Path(args.output).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        # Scratch for the fPKG builder (inner image, CNT, outer image staging) - the app's
        # fast temp drive when given, else the system temp. Never the source folder.
        fpkg_temp = Path(args.temp_dir).resolve() if args.temp_dir else Path(tempfile.gettempdir())
        fpkg_temp.mkdir(parents=True, exist_ok=True)

        # GUI progress translation: see FpkgProgress. Every tool line is echoed verbatim and,
        # where it carries progress, mirrored into the "[PHASE] <Stage>" markers and
        # "[####----] NN% <label>" bars the GUI's stage tracker understands.
        _fpkg_progress = FpkgProgress(_phase)
        _gui_line = _fpkg_progress.line

        if args.fpkg_extract:
            src = Path(args.fpkg_extract).resolve()
            if not src.is_file():
                print(f"[ERROR] fPKG not found: {src}", flush=True); sys.exit(1)
            print(f"[INFO] fPKG extract: {src.name} -> {out_dir} ({_describe_drive(out_dir.parent if not out_dir.exists() else out_dir)})",
                  flush=True)
            _phase("Extracting")
            _fpkg_progress._bar(0, "extract inner PFS + CNT metadata")
            out_dir.mkdir(parents=True, exist_ok=True)
            # a PS4 package writes its files directly (no temp images on the way); a PS5 one
            # decodes two temp images first, see _pkg_extract_plan
            _total = (_pkg_content_size(src) if _fpkg.is_ps4(src)
                      else _pkg_extract_plan(src, _pkg_content_size(src)))
            _tool = {}
            rc = _run_with_folder_progress(
                lambda: _fpkg.extract(src, out_dir, passcode=args.fpkg_passcode,
                                      on_line=lambda l: print(l, flush=True),
                                      on_start=lambda proc: _tool.update(pid=proc.pid)),
                out_dir, _total, lambda: _pkg_extract_label(out_dir),
                measure=lambda: _proc_bytes_written(_tool["pid"]) if "pid" in _tool else 0)
            if rc != 0:
                print(f"\n[ERROR] fPKG extract failed (rc={rc}).", flush=True); sys.exit(1)
            _fpkg_progress._bar(100, "extract inner PFS + CNT metadata")
            _strip_junk_files(out_dir)   # clutter inside a foreign package stays out of the folder
            print(f"[OK] Extraction complete: {out_dir}", flush=True)
            print("\n[SUCCESS] fPKG extracted.", flush=True)
            return

        if args.fpkg_build:
            src = Path(args.fpkg_build).resolve()
            if not src.exists():
                print(f"[ERROR] fPKG source not found: {src}", flush=True); sys.exit(1)

            # Source may be a packed image (.ffpfsc/.ffpfs/.exfat/.ffpkg): unwrap it to a
            # scratch folder first - the ONE-CLICK ".ffpfsc → fPKG" conversion. The scratch
            # lives on the fPKG temp drive and is removed after the build.
            staged = None
            if src.is_file() and src.suffix.lower() in _PFS_IMAGE_SUFFIXES | {".exfat", ".ffpkg"}:
                _phase("Extracting")
                staged = Path(tempfile.mkdtemp(prefix="tmp", dir=str(fpkg_temp)))
                print(f"[INFO] Unwrapping {src.name} -> {staged} before building the fPKG...", flush=True)
                try:
                    if src.suffix.lower() in (".exfat", ".ffpkg"):
                        if not _extract_exfat_to(src, staged):
                            print("[ERROR] Could not read the exFAT/UFS image on this platform.", flush=True)
                            sys.exit(1)
                    elif not _unwrap_pfs_one_pass(src, staged):
                        mk_cmd, mk_cwd = _locate_mkpfs()
                        unpack_pfs_image(src, staged, mk_cmd, mk_cwd, overwrite=True)
                        _fully_unwrap(staged, mk_cmd, mk_cwd)
                    _strip_junk_files(staged)
                except SystemExit:
                    shutil.rmtree(staged, ignore_errors=True); raise
                except Exception as e:
                    shutil.rmtree(staged, ignore_errors=True)
                    print(f"[ERROR] Unwrapping the image failed: {e}", flush=True); sys.exit(1)
                # A nested game root (e.g. image contains one "PPSA12345" folder) → descend.
                try:
                    kids = [k for k in staged.iterdir() if k.is_dir() and not k.name.startswith(".")]
                    if not (staged / "sce_sys").is_dir() and len(kids) == 1 and (kids[0] / "sce_sys").is_dir():
                        print(f"[INFO] Game root is {kids[0].name}/ inside the image.", flush=True)
                        build_src = kids[0]
                    else:
                        build_src = staged
                except Exception:
                    build_src = staged
            elif src.is_dir():
                build_src = src
                # The same rule as the folder pack: the tool stages the folder as it is, so OS
                # clutter goes before it is staged (and never into the package).
                _n = _strip_junk_files(build_src)
                if _n:
                    print(f"[INFO] Removed {_n} macOS/Windows junk file(s)/folder(s) before building the package.",
                          flush=True)
            else:
                print(f"[ERROR] fPKG source must be a game folder or a .ffpfsc/.ffpfs/.exfat/.ffpkg image: {src}",
                      flush=True); sys.exit(1)

            # BACKPORT: lower the SDK words in eboot/prx/sprx and (optionally) copy the
            # user's patched Sony libs into fakelib/. Runs on build_src - that is either
            # the folder passed on the command line (in-place, same semantics as
            # --fake-sign-first) or the scratch we unwrapped an image into (self-contained,
            # never touches the original archive). Everything else in the fPKG build path
            # is the same as before; the C# tool's own staging then signs+packs whatever
            # is here.
            if getattr(args, "backport_target", None):
                try:
                    _apply_backport(build_src, args.backport_target, *_backport_dirs(args))
                except Exception as e:
                    if staged is not None:
                        shutil.rmtree(staged, ignore_errors=True)
                    print(f"[ERROR] Backport before fPKG build failed: {e}", flush=True)
                    sys.exit(1)

            # Identity: the game's own sce_sys/param.json decides; the passed values only
            # fill what it lacks. This is what lets ANY source (folder, archive, image)
            # become a .pkg without the GUI knowing the content id up front.
            try:
                ident = _resolve_fpkg_identity(build_src, args)
            except Exception as e:
                if staged is not None:
                    shutil.rmtree(staged, ignore_errors=True)
                print(f"[ERROR] Could not resolve the fPKG identity: {e}", flush=True); sys.exit(1)
            _cid, _tid = ident["content_id"], ident["title_id"]
            _ident_err = None
            if not _cid or not _tid:
                _ident_err = ("fPKG build needs a content id and a title id - none found in "
                              "sce_sys/param.json and none passed (--content-id / --title-id).")
            elif not _FPKG_CID_RE.match(_cid):
                _ident_err = (f"Content ID {_cid!r} must look like UP9000-PPSA12345_00-GAMENAME00000000 "
                              "(2 letters + 4 digits, dash, 4 letters + 5 digits, _00-, 16 upper-case alphanumerics).")
            elif not _FPKG_TID_RE.match(_tid):
                _ident_err = f"Title ID {_tid!r} must look like PPSA12345 (4 letters + 5 digits)."
            if _ident_err:
                if staged is not None:
                    shutil.rmtree(staged, ignore_errors=True)
                print(f"[ERROR] {_ident_err}", flush=True); sys.exit(1)
            print(f"[INFO] fPKG identity: {_cid}  title id {_tid}  version {ident['version']}"
                  f"  title {ident['title']!r}   (from {ident['source']})", flush=True)

            _opts = (f"retail-normalize {'off' if args.fpkg_no_retail_normalize else 'on'}, "
                     f"HDR flag {args.fpkg_hdr_flag}, "
                     f"PlayGo {'regenerate' if args.fpkg_regen_playgo else 'auto'}, "
                     f"fake-sign {'off' if args.fpkg_no_fake_sign else 'on'}")
            print(f"[INFO] fPKG build ({args.fpkg_inner}, {args.fpkg_kraken_backend}, level {args.compression_level}; {_opts}): "
                  f"{build_src} -> {out_dir}   [temp: {fpkg_temp}]", flush=True)
            _fpkg_progress._set_phase("Scanning Files")
            # The package tool writes its intermediates (the inner pfs_image.dat, about the
            # size of the package, plus CNT and outer-image files) straight into the folder it
            # is given. Give it a run-owned "tmpXXXXXXXX" subfolder of the temp drive - the
            # same shape mkpfs runs use - so nothing lands loose next to the user's temp
            # contents, the folder is removed when the build ends, and a leftover after a
            # crash is reclaimed by the GUI's startup sweep (_is_app_tmp_dir).
            build_temp = Path(tempfile.mkdtemp(prefix="tmp", dir=str(fpkg_temp)))
            # The app's own working copy (an image unwrapped here, a chain's scratch, an archive
            # the app unpacked) is built in place, and its files are taken in as the package
            # grows: no copy, and the unpacked game shrinks while the image grows.
            _in_place = (staged is not None or bool(getattr(args, "_fpkg_in_place", False))
                         or bool(getattr(args, "stage_in_place", False)))
            if _in_place:
                print("[INFO] Building in the unpacked copy itself: each file is removed as soon as the package "
                      "holds it. A build that fails is unpacked again on Retry.", flush=True)
            try:
                rc = _fpkg.build(build_src, out_dir,
                                 content_id=_cid,
                                 title_id=_tid,
                                 title=ident["title"],
                                 version=ident["version"],
                                 passcode=args.fpkg_passcode,
                                 inner_mode=args.fpkg_inner,
                                 kraken_backend=args.fpkg_kraken_backend,
                                 publishing_tools_dll=args.fpkg_pubtools_dll,
                                 deterministic=bool(args.fpkg_deterministic),
                                 temp_dir=str(build_temp),
                                 stage_in_place=_in_place,
                                 consume_source=_in_place,
                                 parallelism=max(0, int(args.cpu_count or 0)),
                                 level=int(args.compression_level),
                                 retail_normalize=not args.fpkg_no_retail_normalize,
                                 hdr_flag=args.fpkg_hdr_flag,
                                 regen_playgo=bool(args.fpkg_regen_playgo),
                                 fake_sign=not args.fpkg_no_fake_sign,
                                 ampr_index=not (args.fpkg_no_ampr_index or getattr(args, "_no_ampr_index", False)),
                                 on_line=_gui_line)
            finally:
                shutil.rmtree(build_temp, ignore_errors=True)
                if staged is not None:
                    shutil.rmtree(staged, ignore_errors=True)
                    print(f"[INFO] Removed unwrap scratch {staged}", flush=True)
            if rc != 0:
                print(f"\n[ERROR] fPKG build failed (rc={rc}).", flush=True); sys.exit(1)
            built = sorted(out_dir.glob("*.pkg"), key=lambda x: x.stat().st_mtime)
            # Run the diagnostic checklist BEFORE announcing the result so a bad build is
            # visible right in the log (not only after a console-install failure) and its
            # verdict lands next to the success line. The exit code stays 0 either way:
            # the package was built; the checklist is advice about installing it.
            validate_rc = 0
            try:
                if built:
                    print(f"\n[INFO] Auto-validating: {built[-1].name}", flush=True)
                    _fpkg_progress._set_phase("Verifying Output")
                    validate_rc = _fpkg.validate(built[-1], temp_dir=str(fpkg_temp), on_line=_gui_line)
            except Exception as ve:
                print(f"[warn] Auto-validate skipped: {ve}", flush=True)
            print("\n[SUCCESS] fPKG built.", flush=True)
            if built:
                print(f"[OK] fPKG complete: {built[-1]}", flush=True)
            if validate_rc:
                print("[WARN] Validation reported failures - check the checklist above before installing.",
                      flush=True)
            return

    # fpkg-validate (a diagnostic - no build/extract)
    if args.fpkg_validate:
        src = Path(args.fpkg_validate).resolve()
        if not src.is_file():
            print(f"[ERROR] fPKG not found: {src}", flush=True); sys.exit(1)
        print(f"[INFO] fPKG validate: {src}", flush=True)
        rc = _fpkg.validate(src, on_line=lambda l: print(l, flush=True))
        if rc != 0:
            print(f"\n[FAIL] validation reported failures (rc={rc}).", flush=True); sys.exit(rc)
        print("\n[SUCCESS] validation OK.", flush=True)
        return

    # ── PS5 console compatibility: force a 64 KiB PFS block size ─────────────────
    # The PS5 reads PFS filesystems with the native 64 KiB (0x10000) logical block.
    # A smaller block - which "auto-fit" picks for many-file games (it chose 4 KiB for
    # one 153 GB title, saving ~5 MB) and which 16384/32768 select
    # explicitly - builds an image that verifies fine locally but the console MISREADS,
    # crashing on launch. Everything we pack targets PS5 (--version PS5 is hardcoded in
    # the mkpfs invocations), so normalise any sub-64K / auto-fit request to 64 KiB here.
    _bs = str(getattr(args, "block_size", "auto")).strip().lower()
    _sub64k = _bs in ("auto-fit", "auto_small_files", "auto-small-files") or (_bs.isdigit() and int(_bs) < 0x10000)
    if _sub64k:
        print(f"[INFO] Forcing 64 KiB PFS block size for PS5 console compatibility "
              f"(requested '{args.block_size}'; sub-64K images crash the console).", flush=True)
        args.block_size = "65536"

    if not args.game_folder:
        parser.print_help()
        sys.exit(1)

    game_folder = Path(args.game_folder).resolve()
    ffpfs_path  = Path(args.output).resolve()

    if not game_folder.exists():
        print(f"[ERROR] Source path does not exist: {game_folder}")
        sys.exit(1)

    operation = args.operation
    if operation is None:
        operation = "unpack" if game_folder.is_file() and game_folder.suffix.lower() in _PFS_IMAGE_SUFFIXES else "pack"

    # Resolve temp dir - use user-specified fast drive if provided
    user_temp: Path | None = Path(args.temp_dir).resolve() if args.temp_dir else None
    if user_temp:
        user_temp.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Using user-specified temp folder: {user_temp}", flush=True)
    # Output-drive root the pass-2 spool spills into when --temp-dir can't hold image+spool
    # (the GUI passes its output root; the spill lands under <root>/_ffpfsc_temp).
    user_spill: Path | None = Path(args.spool_fallback_dir).resolve() if args.spool_fallback_dir else None

    _is_zip = lambda p: p.suffix.lower() == ".zip"
    _is_rar = lambda p: p.suffix.lower() in (".rar", ".r00")

    @contextlib.contextmanager
    def prepare_source_path(path: Path):
        if _is_zip(path):
            with _extracted_zip_source(path, temp_root=user_temp, password=args.password) as src_dir:
                yield src_dir
        elif _is_rar(path):
            # Multi-volume sets must be opened on the FIRST volume. The bundled
            # rarfile.extractall() already guards against path traversal, and the
            # native binding takes the password as a str.
            first = path
            m = re.match(r"^(?P<b>.*\.part)(?P<n>\d+)(?P<e>\.rar)$", path.name, re.I)
            if m:
                cand = path.with_name(f"{m.group('b')}{'1'.zfill(len(m.group('n')))}{m.group('e')}")
                if cand.exists():
                    first = cand
            elif re.match(r"^.+\.r\d{2,}$", path.name, re.I):
                cand = path.with_suffix(".rar")
                if cand.exists():
                    first = cand
            with tempfile.TemporaryDirectory(dir=user_temp) as tmpdir:
                try:
                    from unrar import rarfile
                    with rarfile.RarFile(first, pwd=args.password or None) as rf:
                        rf.extractall(tmpdir)
                except rarfile.RarWrongPassword as exc:
                    print(f"[ERROR] RAR extraction failed: wrong or missing password ({exc})")
                    sys.exit(1)
                except Exception as exc:
                    print(f"[ERROR] RAR extraction failed: {exc} "
                          "(for a multi-part RAR, ensure all .partN.rar / .rNN files are present)")
                    sys.exit(1)
                # Yield OUTSIDE the try: an exception from the pack body is not an
                # extraction failure and must surface as itself.
                yield Path(tmpdir)
        else:
            yield path

    mkpfs_cmd_base, mkpfs_cwd = _locate_mkpfs()

    # Print MkPFS version
    try:
        ver = subprocess.run(
            mkpfs_cmd_base + ["-V"],
            capture_output=True, text=True,
            cwd=mkpfs_cwd,
        )
        print(f"[INFO] MkPFS: {ver.stdout.strip() or ver.stderr.strip()}", flush=True)
    except Exception:
        pass

    # ── PATCH MODE: overlay loose patch files onto a game, then (re)pack ──────────
    if args.patch:
        patch_arg = Path(args.patch).resolve()
        if not patch_arg.exists():
            print(f"[ERROR] Patch source not found: {patch_arg}")
            sys.exit(1)
        if ffpfs_path.exists() and not args.overwrite:
            print(f"[ERROR] Output already exists: {ffpfs_path}  (use --overwrite)")
            sys.exit(1)
        patch_pack_kwargs = dict(
            compression_level=max(0, min(9, args.compression_level)),
            cpu_count=_auto_cap_cpu(args.cpu_count, game_folder),
            threshold_gain=max(0, args.threshold_gain),
            block_size=args.block_size,
            verbose=args.verbose,
        )
        print(f"[INFO] PATCH MODE: overlay '{patch_arg.name}' onto '{game_folder.name}'", flush=True)
        with tempfile.TemporaryDirectory(dir=user_temp) as td:
            td = Path(td)
            # The patch may arrive as an archive (auto-patch hands a sibling RAR/ZIP
            # straight through). Extract zip/rar here; a folder is used as-is. A 7z
            # patch is left to the GUI to pre-extract.
            try:
                patch_dir = _resolve_patch_dir(patch_arg, td, args.password)
            except Exception as exc:
                print(f"[ERROR] Patch archive extraction failed ('{patch_arg.name}'): {exc} "
                      "(corrupt archive, or wrong/missing password)")
                sys.exit(1)
            # Temp intermediates we create and may free mid-flow so temp doesn't grow to
            # ~3x the game size (only one of these layers is needed at a time).
            temp_game_dir = None   # the extracted/copied game folder we own (None = patch_inplace)
            if game_folder.is_file() and game_folder.suffix.lower() == ".ffpfsc":
                print("[INFO] Unpacking the existing .ffpfsc to patch it (outer → inner → files)...", flush=True)
                _phase("Extracting")
                outer = td / "_outer"
                unpack_pfs_image(game_folder, outer, mkpfs_cmd_base, mkpfs_cwd, overwrite=True)
                inner = next((p for p in sorted(outer.rglob("*"))
                              if p.is_file() and p.suffix.lower() == ".ffpfs"), None)
                if inner is None:
                    print("[ERROR] No inner .ffpfs image found inside the .ffpfsc.")
                    sys.exit(1)
                game_unpacked = td / "_game"
                unpack_pfs_image(inner, game_unpacked, mkpfs_cmd_base, mkpfs_cwd, overwrite=True)
                game_root = _patch_find_game_root(game_unpacked)
                temp_game_dir = game_unpacked
                # The inner .ffpfs (inside _outer) is now fully extracted into _game and is
                # no longer needed - drop it to reclaim ~1x the game size before we repack.
                shutil.rmtree(outer, ignore_errors=True)
                print("[INFO] Freed the intermediate outer image (no longer needed).", flush=True)
            elif game_folder.is_dir():
                if args.patch_inplace:
                    game_root = _patch_find_game_root(game_folder)
                else:
                    print("[INFO] Copying the game folder to temp before patching (source untouched)...", flush=True)
                    game_copy = td / "_game"
                    shutil.copytree(game_folder, game_copy, ignore=shutil.ignore_patterns(*_JUNK_GLOBS))
                    game_root = _patch_find_game_root(game_copy)
                    temp_game_dir = game_copy
            else:
                print(f"[ERROR] Patch game must be a folder or a .ffpfsc: {game_folder}")
                sys.exit(1)

            try:
                _bk_root = Path(user_temp) / "_ffpfsc_temp"
                _bk_root.mkdir(parents=True, exist_ok=True)
                applied = overlay_patch(game_root, patch_dir, new_patch_backup(_bk_root, patch_arg.name), patch_arg.name)
            except RuntimeError as e:
                print(f"[ERROR] {e}", flush=True)
                sys.exit(1)
            print(f"[OK] Applied {applied} patch file(s) onto the game.", flush=True)
            if applied == 0:
                print("[ERROR] The patch contained no files to overlay - nothing to do.")
                sys.exit(1)
            title_id = _patch_dir_title_id(game_root) or "patched"
            with tempfile.TemporaryDirectory(dir=user_temp) as td2:
                temp_pfs = Path(td2) / f"{title_id}.ffpfs"
                _discard_stale_pass1_output(temp_pfs)
                _phase("Creating Temp PFS")
                pack_folder_uncompressed(
                    game_root, temp_pfs, mkpfs_cmd_base, mkpfs_cwd,
                    verify_enabled=args.verify, temp_folder=Path(td2), **patch_pack_kwargs,
                )
                # Pass 1 has consumed the patched game folder into temp_pfs; pass 2 reads
                # only temp_pfs. Drop the (temp) game folder now to reclaim ~1x the game
                # size before compression. Never touch a patch_inplace source (temp_game_dir
                # is None then) - that's the user's own library folder.
                if temp_game_dir is not None:
                    shutil.rmtree(temp_game_dir, ignore_errors=True)
                    print("[INFO] Freed the extracted game folder (no longer needed for compression).", flush=True)
                pass2_kwargs = dict(patch_pack_kwargs)
                if pass2_kwargs.get("compression_level", 7) > 0 and _looks_incompressible(temp_pfs):
                    print("[INFO] Patched image sampled as incompressible - storing without compression.", flush=True)
                    pass2_kwargs["compression_level"] = 0
                # An existing output (--overwrite was verified above) is NOT removed here:
                # pass 2 builds beside it and swaps the finished file in, so a failed
                # compression (ENOSPC, OOM kill, cancel, unplugged drive) never loses the
                # previous image.
                _phase("Compressing")
                spool_dir, spool_ctx = _open_pass2_spool_dir(temp_pfs, td2, ffpfs_path, spill_base=user_spill, block_size=args.block_size)
                try:
                    _assert_pass2_spool_space(temp_pfs, spool_dir, block_size=args.block_size)
                    compress_file_to_ffpfsc(
                        temp_pfs, ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                        temp_folder=spool_dir, replace_existing=bool(args.overwrite), **pass2_kwargs,
                    )
                finally:
                    if spool_ctx is not None:
                        spool_ctx.cleanup()
        print("\n[SUCCESS] Patch integrated successfully!")
        return

    if operation == "unpack":
        images = find_pfs_images(game_folder, args.batch)
        if args.batch:
            ffpfs_path.mkdir(parents=True, exist_ok=True)
        for image in images:
            current_output_dir = resolve_unpack_output_dir(image, ffpfs_path, batch=args.batch)
            if current_output_dir.exists() and args.overwrite:
                print(f"[WARN] Output folder already exists. MkPFS will overwrite files in: {current_output_dir}")
            elif current_output_dir.exists() and not args.overwrite:
                print(f"[ERROR] Output folder already exists: {current_output_dir}")
                print("[ERROR] Use --overwrite to replace existing extracted files.")
                sys.exit(1)
            current_output_dir.parent.mkdir(parents=True, exist_ok=True)
            # One pass straight into files when the image is a plain or PFS-nested one;
            # otherwise unpack, then unwrap nested images all the way to a folder (ffpfsc ->
            # inner .exfat -> mount+copy), so one unpack action yields a folder regardless
            # of how the image was packed.
            if not (getattr(args, "unwrap", True) and _unwrap_pfs_one_pass(image, current_output_dir)):
                unpack_pfs_image(
                    image,
                    current_output_dir,
                    mkpfs_cmd_base,
                    mkpfs_cwd,
                    overwrite=args.overwrite,
                )
                if getattr(args, "unwrap", True):
                    _fully_unwrap(current_output_dir, mkpfs_cmd_base, mkpfs_cwd)
            _strip_junk_files(current_output_dir)   # clutter inside a foreign image stays out of the folder
        print("\n[SUCCESS] All operations completed successfully!")
        return

    # Pack options forwarded to mkpfs
    pack_kwargs = dict(
        compression_level=max(0, min(9, args.compression_level)),
        cpu_count=max(0, args.cpu_count),
        threshold_gain=max(0, args.threshold_gain),
        block_size=args.block_size,
        verbose=args.verbose,
    )

    with prepare_source_path(game_folder) as active_source_path:
        game_items = find_game_items(active_source_path, args.batch)

        # An explicit .ffpfsc output is a single-FILE target - never mkdir it into a
        # directory, even under --batch. (--batch is a backend folder-scan mode that
        # expects a directory output; the GUI hands a descriptive .ffpfsc file path.)
        explicit_file = ffpfs_path.suffix.lower() in (".ffpfsc", ".ffpfs")
        # Guard: --batch writes one file per game; a single explicit output FILE path would
        # make every game overwrite the SAME file (only the last survives). Refuse it.
        if args.batch and explicit_file and len(game_items) > 1:
            print(f"[ERROR] --batch with a single output file would overwrite all "
                  f"{len(game_items)} games into one file. Point the output at a DIRECTORY "
                  f"for batch mode.")
            sys.exit(1)
        if args.batch and not explicit_file:
            ffpfs_path.mkdir(parents=True, exist_ok=True)
        elif not ffpfs_path.is_dir() and not ffpfs_path.suffix:
            ffpfs_path.mkdir(parents=True, exist_ok=True)

        for item in game_items:
            title_id = get_title_id(item)
            # Per-item worker cap (auto only): size each game independently in a batch.
            pack_kwargs["cpu_count"] = _auto_cap_cpu(args.cpu_count, item)
            # Uncompressed output (.ffpfs) applies to the PFS family - a game folder or a
            # .ffpfs source; .exfat/.ffpkg are always compressed to .ffpfsc.
            src_pfs_family = item.is_dir() or item.suffix.lower() == ".ffpfs"
            uncompressed = getattr(args, "no_compress", False) and src_pfs_family
            ext = ".ffpfs" if uncompressed else ".ffpfsc"

            # Opt-in: fake-sign the game's executables in place before packing. Only
            # genuine game FOLDERS can be signed (we can't reach into an opaque disk
            # image / PFS); warn-and-skip for those.
            if getattr(args, "fake_sign_first", False) or getattr(args, "backport_target", None):
                if item.is_dir():
                    if args.backport_target:
                        try:
                            _apply_backport(item, args.backport_target, *_backport_dirs(args))
                        except Exception as e:
                            print(f"[ERROR] Backport before pack failed: {e}", flush=True)
                            sys.exit(1)
                    if getattr(args, "fake_sign_first", False):
                        print(f"[INFO] Fake-signing executables in {item.name} before packing…", flush=True)
                        try:
                            _fake_sign_tree(item)
                        except Exception as e:
                            print(f"[ERROR] Fake-sign before pack failed: {e}", flush=True)
                            sys.exit(1)
                else:
                    if getattr(args, "fake_sign_first", False):
                        print(f"[WARN] --fake-sign-first only applies to game folders; "
                              f"ignoring for image/PFS source {item.name}.", flush=True)
                    if args.backport_target:
                        print(f"[WARN] --backport-target only applies to game folders; "
                              f"ignoring for image/PFS source {item.name}.", flush=True)

            if (args.batch and not explicit_file) or ffpfs_path.is_dir():
                current_ffpfs_path = ffpfs_path / f"{title_id}{ext}"
            else:
                current_ffpfs_path = ffpfs_path.with_suffix(ext)

            if args.batch:
                print(f"\n[INFO] --- Processing batch item: {title_id} ({item.name}) ---")

            # Refuse to build onto the source itself - checked BEFORE anything below may
            # touch the destination. (With --overwrite the old flow removed the "existing
            # output" first, i.e. the user's own image, and then failed.)
            if _is_same_file(item, current_ffpfs_path):
                print(f"[ERROR] Output path is the source itself: {current_ffpfs_path}", flush=True)
                sys.exit(1)

            # An existing output is no longer removed up front. Every build route below
            # writes beside it and swaps the FINISHED file in (replace_existing), so a
            # failed pass 2 leaves the previous image untouched.
            replace_existing = bool(args.overwrite)
            if current_ffpfs_path.exists():
                if args.overwrite:
                    print(f"[WARN] Output file already exists. Overwriting: {current_ffpfs_path}")
                else:
                    print(f"[WARN] Output file already exists: {current_ffpfs_path}")
                    try:
                        if sys.stdin.isatty():
                            response = input("Overwrite existing file? [y/N]: ").strip().lower()
                        else:
                            print("[INFO] Non-interactive shell - skipping overwrite.")
                            response = 'n'
                    except (KeyboardInterrupt, EOFError):
                        print("\n[INFO] Cancelled.")
                        sys.exit(0)
                    if response not in ('y', 'yes'):
                        print(f"[INFO] Skipping: {current_ffpfs_path.name}")
                        continue
                    replace_existing = True

            # Pull third-party / non-game extras (a _bundle_-style group folder, loose
            # .nfo/.sfv, …) OUT of the dump so they are never packed into the image, and
            # drop them next to the output .ffpfsc so the user still has them. Runs before
            # EVERY folder-pack route below (via-exfat, two-pass, uncompressed).
            if item.is_dir():
                try:
                    current_ffpfs_path.parent.mkdir(parents=True, exist_ok=True)
                    _evacuate_non_game_extras(item, current_ffpfs_path.parent)
                except Exception as e:
                    print(f"[WARN] Could not evacuate non-game extras: {e}", flush=True)

            # OPT-IN exFAT path (--via-exfat): build an exFAT image of the game folder and
            # compress THAT into the .ffpfsc - PSBrew's most-stable workflow, wrapping a
            # real exFAT volume the PS5 reads natively instead of the folder PFS builder.
            # On non-macOS / hdiutil failure it returns None and we fall through to two-pass.
            if getattr(args, "via_exfat", False) and item.is_dir():
                with tempfile.TemporaryDirectory(dir=user_temp) as exdir:
                    exfat_img = _build_exfat_image(item, Path(exdir), title_id)
                    if exfat_img is not None:
                        spool_dir, spool_ctx = _open_pass2_spool_dir(exfat_img, exdir, current_ffpfs_path, spill_base=user_spill, block_size=args.block_size)
                        try:
                            _assert_pass2_spool_space(exfat_img, spool_dir, block_size=args.block_size)
                            print("[INFO] Compressing exFAT image -> .ffpfsc...", flush=True)
                            compress_file_to_ffpfsc(
                                exfat_img, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                                temp_folder=spool_dir, replace_existing=replace_existing, **pack_kwargs,
                            )
                        finally:
                            if spool_ctx is not None:
                                spool_ctx.cleanup()
                        continue   # done with this item; skip the two-pass folder build
                print("[WARN] Falling back to the two-pass folder image (exFAT path unavailable).", flush=True)

            if uncompressed and item.is_file() and item.suffix.lower() == '.ffpfs':
                # Uncompressed output + already a PFS image → emit the .ffpfs directly (copy;
                # no compression, no temp). Re-pack of a .ffpfs to a faster uncompressed copy.
                # (Source == output was refused above.) Copy beside an existing output and
                # swap on success - same rule as the mkpfs routes - so an interrupted copy
                # never truncates the previous file.
                print(f"[INFO] Uncompressed output - copying {item.name} -> {current_ffpfs_path.name}", flush=True)
                build_path = _stage_build_output(current_ffpfs_path, replace_existing)
                try:
                    current_ffpfs_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, build_path)
                    _commit_build_output(build_path, current_ffpfs_path)
                except OSError as e:
                    _discard_build_output(build_path, current_ffpfs_path)
                    print(f"[ERROR] Could not copy {item.name} to {current_ffpfs_path}: {e}", flush=True)
                    sys.exit(1)
            elif item.is_file() and item.suffix.lower() in ('.exfat', '.ffpkg', '.ffpfs'):
                # Direct image (.exfat / .ffpkg / .ffpfs) → .ffpfsc (single-file streaming
                # path). A .ffpfs is re-wrapped into its native compressed container. The
                # source is read in place; route the spool to the SSD temp if it fits, else
                # spill onto the output drive (same adaptive rule as the folder path).
                spool_dir, spool_ctx = _open_pass2_spool_dir(item, user_temp, current_ffpfs_path, spill_base=user_spill, block_size=args.block_size)
                try:
                    compress_file_to_ffpfsc(
                        item, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                        temp_folder=spool_dir, replace_existing=replace_existing,
                        **pack_kwargs,
                    )
                finally:
                    if spool_ctx is not None:
                        spool_ctx.cleanup()
            else:
                # Game folder: build an UNCOMPRESSED inner PFS image, then wrap it in a
                # compressed PFSC container (.ffpfsc). This two-pass "wrapper" flow is
                # REQUIRED, not an inefficiency: packing a game folder directly with
                # per-file PFSC compression (single-pass "pack folder --compress") builds
                # a valid-looking, locally-verifiable image that the PS5 console MISREADS
                # (upstream MkPFS issue #49 - see the warning in backend/mkpfs/cli.py). A
                # green local build/verify is NOT proof of console correctness, so never
                # "optimize" this into single-pass to save the temp intermediate.
                if uncompressed:
                    # Uncompressed deliverable: build the inner PFS image STRAIGHT to the
                    # output (.ffpfs) and stop - no pass 2, no compressed spool. Faster to
                    # build and (per ShadowMountPlus) far faster to mount; full size on disk.
                    print("[INFO] Uncompressed output - building the PFS image directly to "
                          f"{current_ffpfs_path.name} (skipping pass-2 compression).", flush=True)
                    current_ffpfs_path.parent.mkdir(parents=True, exist_ok=True)
                    pack_folder_uncompressed(
                        item, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                        verify_enabled=args.verify,
                        temp_folder=Path(user_temp) if user_temp else None,
                        replace_existing=replace_existing,
                        **pack_kwargs,
                    )
                    continue   # done with this item - no pass 2
                # Build the inner (pass-1) uncompressed PFS image in a STABLE dir (NOT an
                # auto-deleted TemporaryDirectory) so an OOM/restart can resume pass 2 from
                # it WITHOUT rebuilding pass 1 - and so the GUI can free the extracted source
                # the moment pass 1 is done (pass 2 reads only this image). The GUI keeps the
                # image across a crash (a retry resumes from it; the startup sweep reaps an
                # orphan) and removes it on terminal failure/cancel. On a clean pass-2 SUCCESS
                # we remove it right here; every failure path propagates past this and LEAVES
                # it for the resume.
                inner_dir = (Path(user_temp) if user_temp else Path(tempfile.gettempdir())) / "_ffpfsc_inner"
                inner_dir.mkdir(parents=True, exist_ok=True)
                temp_pfs = inner_dir / f"{title_id}.ffpfs"
                # We are rebuilding pass 1 from the source folder, so any inner image a
                # crashed run left under this name is stale (a resume never comes this
                # way - it passes that image in as the source). Drop it, or mkpfs stops
                # at its interactive overwrite prompt.
                _discard_stale_pass1_output(temp_pfs)

                pack_folder_uncompressed(
                    item, temp_pfs, mkpfs_cmd_base, mkpfs_cwd,
                    verify_enabled=args.verify,
                    temp_folder=inner_dir,
                    **pack_kwargs,
                )
                # Pass 1 done: tell the GUI so it frees the extracted source now (pass 2 needs
                # only the inner image) and records the image path for a resume-on-OOM.
                print(f"[PASS1-DONE] {temp_pfs}", flush=True)

                # Incompressible-image fast path: if the inner image barely shrinks
                # (already-compressed game assets - common; gain ~0%), pass 2 at
                # compression-level 0 stores every block raw, which is exactly what the
                # per-block threshold produces for incompressible data anyway. Same
                # .ffpfsc, but without spending CPU on millions of futile deflate
                # attempts over a ~150 GB image.
                pass2_kwargs = dict(pack_kwargs)
                if pass2_kwargs.get("compression_level", 7) > 0 and _looks_incompressible(temp_pfs):
                    print("[INFO] Inner image sampled as incompressible - storing without "
                          "compression (level 0) to skip wasted CPU; the .ffpfsc is the same "
                          "size either way.", flush=True)
                    pass2_kwargs["compression_level"] = 0
                # Adaptive pass-2 spool: keep the inner image on the SSD the GUI chose and
                # put the spool there too if it still fits beside it, else spill the spool
                # onto the output drive - so a big game still compresses off the fast drive
                # instead of falling entirely to the HDD.
                spool_dir, spool_ctx = _open_pass2_spool_dir(temp_pfs, str(inner_dir), current_ffpfs_path, spill_base=user_spill, block_size=args.block_size)
                try:
                    _assert_pass2_spool_space(temp_pfs, spool_dir, block_size=args.block_size)
                    compress_file_to_ffpfsc(
                        temp_pfs, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                        temp_folder=spool_dir, replace_existing=replace_existing,
                        **pass2_kwargs,
                    )
                finally:
                    if spool_ctx is not None:
                        spool_ctx.cleanup()

                if args.keep_pfs:
                    saved = current_ffpfs_path.parent / f"{title_id}.ffpfs"
                    if saved.exists():
                        # Never clobber what is already there (an earlier kept image or a
                        # user's own .ffpfs) - take the next free name instead.
                        saved = _free_sibling_name(saved)
                        print(f"[INFO] {title_id}.ffpfs already exists next to the output - "
                              f"keeping the intermediate image as {saved.name} instead.", flush=True)
                    print(f"[INFO] Saving intermediate PFS image to {saved}...")
                    # move (not copy): pass 2 already consumed temp_pfs; relocating frees the
                    # SSD copy instead of leaving it for the success cleanup below to wipe.
                    try:
                        shutil.move(str(temp_pfs), str(saved))
                    except Exception as e:
                        print(f"[WARN] Could not save intermediate PFS image: {e}")
                # Pass 2 succeeded - drop the inner image (and its dir). Only reached on
                # success: any earlier failure / sys.exit / -9 OOM kill propagates past here
                # and LEAVES the image so the GUI can resume pass 2 from it on retry.
                shutil.rmtree(inner_dir, ignore_errors=True)

    print("\n[SUCCESS] All operations completed successfully!")


if __name__ == "__main__":
    main()
