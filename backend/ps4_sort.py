"""Sort PS4 packages into the library layout (ultra_core.ps4_layout) and copy or move them there with copy_job."""
from __future__ import annotations

import os
import re
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # ultra_core next to the app
import copy_job
import ps4pkg
from organize_lib import case_blind
from ultra_core import _PS4_TID_TAG, is_fs_junk_name, ps4_layout, scan_ps4_library, strip_fs_junk

_BAR = re.compile(r"^\[#+\]\s*(\d+)%")
PART = ".ps4sort-part"           # a complete new package waiting to replace the old one (Overwrite)


def _say(on_line, text):
    (on_line or (lambda t: print(t, flush=True)))(text)


def find_packages(src) -> list[Path]:
    """*src* itself when it is a .pkg, else every .pkg below it (OS clutter skipped)."""
    src = Path(src)
    if src.is_file():
        return [src] if src.suffix.lower() == ".pkg" and not is_fs_junk_name(src.name) else []
    return sorted(p for p in src.rglob("*.pkg")
                  if p.is_file() and not any(is_fs_junk_name(part) for part in p.relative_to(src).parts))


def other_files(src) -> list[Path]:
    """Files below *src* that are no .pkg (OS clutter skipped): a sort leaves them where they
    are, so the run must say so."""
    src = Path(src)
    if not src.is_dir():
        return []
    return sorted(p for p in src.rglob("*")
                  if (p.is_file() or p.is_symlink()) and p.suffix.lower() != ".pkg"
                  and not any(is_fs_junk_name(part) for part in p.relative_to(src).parts))


def _free_name(dst_dir: Path, name: str, taken=(), key=str) -> str:
    stem, ext = os.path.splitext(name)
    n = 2
    while os.path.lexists(dst_dir / f"{stem} ({n}){ext}") or key(dst_dir / f"{stem} ({n}){ext}") in taken:
        n += 1
    return f"{stem} ({n}){ext}"


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _settle(part: Path, on_line) -> Path:
    """Give a complete package left under PART a name every run sees: its own when that is
    free, else a free '(n)' one beside it (both stay). Returns where it went."""
    dst_dir, name = part.parent, part.name[:-len(PART)]
    target = dst_dir / name
    if os.path.lexists(target):
        target = dst_dir / _free_name(dst_dir, name)
    os.rename(part, target)
    _say(on_line, f"[WARN] {part.name} was left by a sort that did not finish; it is now {target.name}")
    return target


def recover_parts(out_dir: Path, on_line=None) -> None:
    """Packages an interrupted Overwrite left under PART in the library (title folder, DLC Pack)."""
    for pattern in (f"*/*{PART}", f"*/*/*{PART}"):
        for part in sorted(Path(out_dir).glob(pattern)):
            if part.is_file() and not part.is_symlink() and not is_fs_junk_name(part.name):
                try:
                    _settle(part, on_line)
                except OSError as e:
                    _say(on_line, f"[WARN] could not restore {part.name}: {e}")


def sort_packages(src, out_dir, *, mode: str = copy_job.KEEP, if_exists: str = "skip", on_line=None) -> int:
    """0 when every PS4 package found is in the library, 1 on an error, copy_job.KEPT (4) when
    the run finished but packages stay in the source (not PS4, unreadable, or skipped)."""
    out_dir = Path(out_dir)
    _say(on_line, "[JOB] copy")
    _say(on_line, "[PHASE] Writing Final Image")
    if out_dir.is_dir():
        recover_parts(out_dir, on_line)
    items = []
    kept: list[str] = []
    for p in find_packages(src):
        if not ps4pkg.is_ps4_package(p):
            _say(on_line, f"[WARN] not a PS4 package, left where it is: {p.name}")
            kept.append(p.name)
            continue
        try:
            items.append((p, ps4pkg.read_identity(p)))
        except ps4pkg.Ps4PackageError as e:
            _say(on_line, f"[WARN] {p.name}: {e}; left where it is")
            kept.append(p.name)
    if not items:
        _say(on_line, "[ERROR] no PS4 package found")
        return 1
    for p in other_files(src):
        _say(on_line, f"[INFO] not a package, left where it is: {p.relative_to(src).as_posix()}")
        kept.append(p.name)
    _say(on_line, f"[PS4] {len(items)} package(s)")
    kinds = {p: i.kind for p, i in items}
    known = scan_ps4_library(out_dir)
    placements = ps4_layout(items, known=known)
    # A title folder already in the library whose version tag the set raises is renamed
    # (same drive, nothing copied); when that name is taken the set goes into the old folder.
    moved: dict[str, str] = {}
    for folder in dict.fromkeys(f for _, f, _, _ in placements):
        m = _PS4_TID_TAG.search(folder)
        lib = known.get(m.group(1).upper()) if m else None
        if not lib or lib.folder == folder:
            continue
        old, new = out_dir / lib.folder, out_dir / folder
        if new.exists():
            _say(on_line, f"[WARN] {folder} already exists; the packages go into {lib.folder}, its name stays")
            moved[folder] = lib.folder
            continue
        try:
            os.rename(old, new)
            try:
                (out_dir / ("._" + lib.folder)).unlink()   # a sidecar the rename left behind
            except OSError:
                pass
            _say(on_line, f"[PS4] renamed {lib.folder} -> {folder} (the newest version of the set)")
        except OSError as e:
            _say(on_line, f"[WARN] could not rename {lib.folder} ({e}); the packages go into it as it is")
            moved[folder] = lib.folder
    placements = [(src, moved.get(f, f), sub, name) for src, f, sub, name in placements]
    total = sum(max(1, p.stat().st_size) for p, *_ in placements)
    done = 0
    last_pct = [-1]
    t0 = time.monotonic()

    def bar(pct_of_file: int, size: int, name: str) -> None:
        moved = int(done + size * pct_of_file / 100)
        pct = int(moved * 100 / total)
        if pct > last_pct[0]:
            last_pct[0] = pct
            _say(on_line, copy_job.progress_line(pct, moved, total, t0, f"copy {name}"))

    def relay(size: int, name: str):
        def sink(line: str) -> None:
            m = _BAR.match(line)
            if m:
                bar(int(m.group(1)), size, name)
            elif not line.startswith(("[JOB]", "[PHASE]")):
                _say(on_line, line)
        return sink

    rc_all = 0
    folders = []
    out_dir.mkdir(parents=True, exist_ok=True)
    # names that differ only in case are one name on such a drive
    key = (lambda p: str(p).casefold()) if case_blind(out_dir) else str
    written: set[str] = set()                # key() of every target this run wrote
    for src_pkg, folder, sub, name in placements:
        size = max(1, src_pkg.stat().st_size)
        dst_dir = out_dir / folder / sub if sub else out_dir / folder
        if out_dir / folder not in folders:
            folders.append(out_dir / folder)
        rel = Path(folder, sub, name) if sub else Path(folder, name)
        target = dst_dir / name
        if key(target) in written:
            # another package of this run already took the name: never replace or skip it
            name = _free_name(dst_dir, name, written, key)
            _say(on_line, f"[WARN] {src_pkg.name} gets the same name as another package of this source; "
                          f"it goes in as {name}")
            rel, target = rel.with_name(name), dst_dir / name
        elif target.exists() and _same_file(target, src_pkg):
            _say(on_line, f"[INFO] already in place: {rel}")
            written.add(key(target))
            done += size
            continue
        elif target.exists():
            if if_exists == "keep":
                name = _free_name(dst_dir, name, written, key)
                rel, target = rel.with_name(name), dst_dir / name
            elif if_exists == "overwrite":
                # the old package keeps its name until the new one is complete beside it
                part = dst_dir / (name + PART)
                if os.path.lexists(part):
                    _settle(part, on_line)
                written.add(key(target))
                rc = copy_job.run_copy(src_pkg, dst_dir, dst_name=part.name, mode=mode, on_line=relay(size, name))
                if rc == 0:
                    try:
                        os.replace(part, target)
                        _say(on_line, f"[PS4] {kinds[src_pkg]} -> {rel} (replaced)")
                    except OSError as e:
                        _say(on_line, f"[ERROR] could not replace {rel}: {e}")
                        rc_all = 1
                        try:
                            if src_pkg.is_file():
                                part.unlink()              # the source is still there
                            else:
                                _settle(part, on_line)     # the only copy now: a name every run sees
                        except OSError as e2:
                            _say(on_line, f"[WARN] {part.name} stays as it is: {e2}")
                else:
                    rc_all = 1
                done += size
                continue
            else:
                note = " (rule Ask: a running job does not stop for each file)" if if_exists == "ask" else ""
                _say(on_line, f"[INFO] already there, skipped{note}: {rel}")
                kept.append(src_pkg.name)
                done += size
                bar(0, 0, name)
                continue
        written.add(key(target))
        rc = copy_job.run_copy(src_pkg, dst_dir, dst_name=name, mode=mode, on_line=relay(size, name))
        if rc in (0, 2):
            _say(on_line, f"[PS4] {kinds[src_pkg]} -> {rel}")
        else:
            rc_all = 1
        done += size
    bar(0, 0, "")
    # macOS tags every file it writes with a provenance attribute; on exFAT that becomes a
    # '._' sidecar, which the system drops after a few seconds but which stays for good when
    # the drive is ejected first. Remove them (and other clutter) from the folders this set
    # touched, and the sidecar beside each folder, so the library holds only packages.
    for f in folders:
        strip_fs_junk(f)
        try:
            (f.parent / ("._" + f.name)).unlink()
        except OSError:
            pass
    for f in folders:
        _say(on_line, f"[OK] PS4 sorted: {f}")
    if kept:
        _say(on_line, copy_job.kept_line(kept))
    return rc_all or (copy_job.KEPT if kept else 0)
