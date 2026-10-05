"""
Same-format copy/move for the queue's ``copy`` operation.

Used when the source format equals the target format (``.ffpfsc`` → ``.ffpfsc``,
``.ffpfs`` → ``.ffpfs``, ``.pkg`` → ``.pkg``): re-encoding is pure waste, so the
file is transported as-is.

Three modes decide what happens to the source:
  * ``keep``     — the source stays. Same drive: an APFS clone (instant, no extra
                   space) or, where the filesystem cannot clone, a real copy.
                   Across drives: a chunked copy.
  * ``organize`` — same drive: ``os.rename`` (a library is sorted in place);
                   across drives: a chunked copy, the source stays.
  * ``move``     — same drive: ``os.rename``; across drives: a chunked copy, then
                   the source is deleted once every byte is on the destination.

Emits the same ``[PHASE]`` and ``[####] NN%`` markers ``CLIWorker`` already
parses, so the queue's progress bar and stage indicators light up unchanged.

Exit codes:
  * ``0`` — success
  * ``1`` — I/O or unexpected error
  * ``2`` — source == destination (would rename onto itself) → skip
  * ``3`` — a different file already occupies the destination name → skip
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Optional

CHUNK = 4 * 1024 * 1024  # 4 MiB — same order as extract_members' write chunk
KEEP, ORGANIZE, MOVE = "keep", "organize", "move"
MODES = (KEEP, ORGANIZE, MOVE)

_ALLOWED_SUFFIXES = frozenset({".ffpfsc", ".ffpfs", ".pkg"})


def _print(on_line: Optional[Callable[[str], None]], msg: str) -> None:
    if on_line is None:
        print(msg, flush=True)
    else:
        on_line(msg)


def _same_device(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` live on the same filesystem, so ``os.rename``
    is a metadata-only operation instead of a copy. Compares the closest
    EXISTING parent for each — a not-yet-created destination folder has no
    st_dev on its own."""
    def dev(p: Path) -> int:
        q = p
        while not q.exists():
            q = q.parent
            if q == q.parent:
                break
        return os.stat(q).st_dev if q.exists() else -1

    da, db = dev(a), dev(b)
    return da != -1 and da == db


def _fsync_file(fd: int) -> None:
    """Push written data to stable storage before the source may be deleted. On
    macOS a plain fsync only reaches the drive's own cache; F_FULLFSYNC asks the drive
    to flush that too, which is what makes unplugging the destination after a move
    safe. Falls back to fsync where the filesystem does not support it."""
    if sys.platform == "darwin":
        try:
            import fcntl
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except (OSError, AttributeError, ImportError):
            pass
    os.fsync(fd)


def _fsync_dir(path: Path) -> None:
    """Flush the directory entry of a just-renamed file. Best effort: Windows cannot
    open a directory for fsync and some filesystems refuse it — neither is an error
    for the copy itself."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _clone(src: Path, dst: Path) -> bool:
    """An APFS clone of *src* at *dst* (macOS clonefile): instant, shares the blocks,
    takes no space until either copy changes. False where the filesystem cannot."""
    if sys.platform != "darwin":
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        return libc.clonefile(os.fsencode(str(src)), os.fsencode(str(dst)), 0) == 0
    except Exception:
        return False


def _resolves_same(src: Path, dst: Path) -> bool:
    """True when ``src`` and ``dst`` resolve to the same file — including the
    macOS case-insensitive equality that ``resolve()`` normalizes. A missing
    ``dst`` cannot equal an existing ``src`` (nothing to resolve to)."""
    try:
        return dst.exists() and src.resolve() == dst.resolve()
    except Exception:
        return False


def run_copy(src, dst_dir, *,
             dst_name: Optional[str] = None,
             mode: str = KEEP,
             on_line: Optional[Callable[[str], None]] = None) -> int:
    """
    Copy or move *src* into *dst_dir*.

    Args:
      src: source file path (must be an existing regular file with a supported
           suffix).
      dst_dir: destination directory (created if missing).
      dst_name: destination filename (defaults to ``src.name``).
      mode: ``keep`` (the default), ``organize`` or ``move``; see the module docstring.
      on_line: line sink (mirrors backend logging). ``None`` prints to stdout.

    Returns an exit code (see module docstring).
    """
    if mode not in MODES:
        _print(on_line, f"[ERROR] copy: unknown mode {mode!r} (keep, organize or move)")
        return 1
    src = Path(src)
    dst_dir = Path(dst_dir)
    if not src.is_file():
        _print(on_line, f"[ERROR] copy: source not found or not a file: {src}")
        return 1
    if src.suffix.lower() not in _ALLOWED_SUFFIXES:
        _print(on_line, f"[ERROR] copy: unsupported source type: {src.suffix} "
                        f"(need one of {sorted(_ALLOWED_SUFFIXES)})")
        return 1

    dst = dst_dir / (dst_name or src.name)

    if _resolves_same(src, dst):
        _print(on_line, f"[WARN] copy: source and destination are the same file, "
                        f"skipping: {src}")
        return 2

    if dst.exists() and not _resolves_same(src, dst):
        try:
            same_bytes = (dst.is_file() and dst.stat().st_size == src.stat().st_size
                          and dst.samefile(src))
        except Exception:
            same_bytes = False
        if not same_bytes:
            _print(on_line, f"[WARN] copy: destination already occupied by a "
                            f"different file, skipping: {dst}")
            return 3

    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _print(on_line, f"[ERROR] copy: cannot create destination folder {dst_dir}: {e}")
        return 1

    same_drive = _same_device(src, dst_dir)
    total = src.stat().st_size

    # Tells the GUI this job is a plain copy, whatever command started it (a --copy job,
    # or a chain job with nothing to change), so its bar follows the copy alone.
    _print(on_line, "[JOB] copy")
    _print(on_line, "[PHASE] Writing Final Image")

    if same_drive and mode == KEEP and _clone(src, dst):
        _print(on_line, f"[INFO] copy: same-drive clone — {src.name} → {dst} (no extra space until one changes)")
        _print(on_line, f"[####] 100% copy")
        _print(on_line, f"[SUCCESS] Copied {src.name} → {dst}")
        return 0

    if same_drive and mode != KEEP:
        # Metadata-only rename — no data movement. Feels instantaneous even on
        # a 100 GB game because we never touch the payload bytes.
        _print(on_line, f"[INFO] copy: same-drive move — {src.name} → {dst}")
        try:
            os.rename(src, dst)
        except OSError as e:
            _print(on_line, f"[ERROR] copy: rename failed: {e}")
            return 1
        _print(on_line, f"[####] 100% move")
        # No "[PHASE] Complete" — the worker owns stage transitions; emitting our own
        # would race the completion path and briefly show "Complete: 0%" in the log.
        _print(on_line, f"[SUCCESS] Moved {src.name} → {dst}")
        return 0

    # A real copy (across drives, or on a drive that cannot clone): chunked through a
    # *.copy-tmp file so an interrupted write never leaves a truncated target visible
    # under the final name. It needs the whole size free on the destination.
    try:
        free = shutil.disk_usage(dst_dir).free
    except OSError:
        free = None
    if free is not None and free < total:
        _print(on_line, f"[ERROR] copy: not enough space on the destination: {total / 1e9:.2f} GB needed, "
                        f"{free / 1e9:.2f} GB free")
        return 1
    tmp = dst.with_suffix(dst.suffix + ".copy-tmp")
    _print(on_line, f"[INFO] copy: {'cross-drive' if not same_drive else 'same-drive'} copy — {src.name} → {dst}")
    written = 0
    last_pct = -1
    t0 = time.monotonic()
    try:
        with open(src, "rb", buffering=0) as fin, open(tmp, "wb", buffering=0) as fout:
            while True:
                buf = fin.read(CHUNK)
                if not buf:
                    break
                # An unbuffered write may accept fewer bytes than offered; count what
                # actually went out and resubmit the rest.
                view = memoryview(buf)
                while view:
                    n = fout.write(view)
                    if n is None:            # a buffered writer took everything
                        n = len(view)
                    written += n
                    view = view[n:]
                if total > 0:
                    pct = min(99, int(written * 100 / total))
                    if pct != last_pct:
                        secs = time.monotonic() - t0
                        rate = written / secs if secs > 0.5 else 0
                        tail = (f" @ {rate / 1e6:.2f} MB/s ETA {int((total - written) / rate)}s"
                                if rate > 0 else "")
                        _print(on_line, f"[####] {pct}% copy{tail}")
                        last_pct = pct
            # Everything must be on the destination before the source may go: flush,
            # sync, then compare the byte count AND the on-disk size with the source
            # size, so a short read/write (drive unplugged, a source that changed under
            # us, a silent ENOSPC) fails here instead of surfacing later as a truncated
            # game on the destination drive.
            fout.flush()
            _fsync_file(fout.fileno())
            landed = os.fstat(fout.fileno()).st_size
            if written != total or landed != total:
                raise OSError(f"short copy: {landed} of {total} bytes reached {tmp.name}")
        os.replace(tmp, dst)
        _fsync_dir(dst.parent)
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        _print(on_line, f"[ERROR] copy: write failed: {e}")
        return 1

    _print(on_line, f"[####] 100% copy")

    delete_source = mode == MOVE and not same_drive
    if delete_source:
        _print(on_line, "[PHASE] Cleaning Up")
        try:
            src.unlink()
            _print(on_line, f"[INFO] copy: source deleted after successful copy: {src}")
        except Exception as e:
            # Copy succeeded — losing the source delete is a warning, not a fail:
            # the target is intact, the user can delete the source manually.
            _print(on_line, f"[WARN] copy: source could not be deleted: {e}")

    _print(on_line, f"[SUCCESS] {'Moved' if delete_source else 'Copied'} "
                    f"{src.name} → {dst}")
    return 0
