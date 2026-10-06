"""Sort PS4 packages into the library layout (ultra_core.ps4_layout) and copy or move them
there with copy_job.

A set joins its title folder when the output already has one (ultra_core.scan_ps4_library).
The conflict rule applies per file: skip (default), overwrite (the new copy replaces the old
one only once it is complete), keep (a ' (2)' suffix); 'ask' behaves like skip, since a
running job cannot stop for every file. The copy lines of every package are folded into
one rising progress bar for the set."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # ultra_core next to the app
import copy_job
import ps4pkg
from ultra_core import _PS4_TID_TAG, is_fs_junk_name, ps4_layout, scan_ps4_library, strip_fs_junk

_BAR = re.compile(r"^\[#+\]\s*(\d+)%")


def _say(on_line, text):
    (on_line or (lambda t: print(t, flush=True)))(text)


def find_packages(src) -> list[Path]:
    """*src* itself when it is a .pkg, else every .pkg below it (OS clutter skipped)."""
    src = Path(src)
    if src.is_file():
        return [src] if src.suffix.lower() == ".pkg" and not is_fs_junk_name(src.name) else []
    return sorted(p for p in src.rglob("*.pkg")
                  if p.is_file() and not any(is_fs_junk_name(part) for part in p.relative_to(src).parts))


def _free_name(dst_dir: Path, name: str) -> str:
    stem, ext = os.path.splitext(name)
    n = 2
    while (dst_dir / f"{stem} ({n}){ext}").exists():
        n += 1
    return f"{stem} ({n}){ext}"


def sort_packages(src, out_dir, *, mode: str = copy_job.KEEP, if_exists: str = "skip", on_line=None) -> int:
    """0 when every PS4 package found is in the library (or was already there), 1 otherwise."""
    out_dir = Path(out_dir)
    _say(on_line, "[JOB] copy")
    _say(on_line, "[PHASE] Writing Final Image")
    items = []
    for p in find_packages(src):
        if not ps4pkg.is_ps4_package(p):
            _say(on_line, f"[WARN] not a PS4 package, left where it is: {p.name}")
            continue
        try:
            items.append((p, ps4pkg.read_identity(p)))
        except ps4pkg.Ps4PackageError as e:
            _say(on_line, f"[WARN] {p.name}: {e}; left where it is")
    if not items:
        _say(on_line, "[ERROR] no PS4 package found")
        return 1
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

    def bar(pct_of_file: int, size: int, name: str) -> None:
        pct = int((done + size * pct_of_file / 100) * 100 / total)
        if pct > last_pct[0]:
            last_pct[0] = pct
            _say(on_line, f"[{'#' * max(1, pct // 5)}] {pct}% copy {name}")

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
    for src_pkg, folder, sub, name in placements:
        size = max(1, src_pkg.stat().st_size)
        dst_dir = out_dir / folder / sub if sub else out_dir / folder
        if out_dir / folder not in folders:
            folders.append(out_dir / folder)
        rel = Path(folder, sub, name) if sub else Path(folder, name)
        target = dst_dir / name
        if target.exists():
            if if_exists == "keep":
                name = _free_name(dst_dir, name)
                rel = rel.with_name(name)
            elif if_exists == "overwrite":
                part = name + ".ps4sort-part"
                rc = copy_job.run_copy(src_pkg, dst_dir, dst_name=part, mode=mode, on_line=relay(size, name))
                if rc == 0:
                    os.replace(dst_dir / part, target)
                    _say(on_line, f"[PS4] {kinds[src_pkg]} -> {rel} (replaced)")
                else:
                    rc_all = 1
                done += size
                continue
            else:
                note = " (rule Ask: a running job does not stop for each file)" if if_exists == "ask" else ""
                _say(on_line, f"[INFO] already there, skipped{note}: {rel}")
                done += size
                bar(0, 0, name)
                continue
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
    return rc_all
