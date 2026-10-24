from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Callable, Optional

CHUNK = 4 * 1024 * 1024  # 4 MiB, like extract_members' write chunk
KEEP, ORGANIZE, MOVE = "keep", "organize", "move"
MODES = (KEEP, ORGANIZE, MOVE)

_ALLOWED_SUFFIXES = frozenset({".ffpfsc", ".ffpfs", ".pkg"})
# every in-flight copy ends in this; organize_lib.PARTIAL_SUFFIXES knows it
TEMP_SUFFIX = ".copy-tmp"
# exit code: finished, but items stayed in the source
KEPT = 4


def kept_line(names) -> str:
    names = list(names)
    more = f" and {len(names) - 5} more" if len(names) > 5 else ""
    return f"[KEPT] {len(names)} item(s) stay in the source: {', '.join(names[:5])}{more}"


def _temp_next_to(dst: Path, folder: bool = False) -> Path:
    """created exclusively, with normal permissions (mkstemp would make it private)"""
    # shorten long names so the temp name stays within 255 bytes
    stem = dst.name
    while stem and len(f".{stem}.copy-00000000{TEMP_SUFFIX}".encode("utf-8")) > 250:
        stem = stem[:-1]
    for _ in range(100):
        tmp = dst.with_name(f".{stem}.copy-{os.urandom(4).hex()}{TEMP_SUFFIX}")
        try:
            if folder:
                os.mkdir(tmp)
            else:
                os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
            return tmp
        except FileExistsError:
            continue
    raise FileExistsError(f"no free temp name beside {dst}")

def cross_device(e: OSError) -> bool:
    """EXDEV or WinError 17; two shares of one server can carry the same volume serial"""
    import errno
    return getattr(e, "winerror", None) == 17 or e.errno == errno.EXDEV


def rmtree_writable(path) -> None:
    """rmtree that also takes read-only files, which Windows refuses to delete"""
    if os.name != "nt":
        shutil.rmtree(path)
        return

    def retry(func, p, *_):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)

def _print(on_line: Optional[Callable[[str], None]], msg: str) -> None:
    if on_line is None:
        print(msg, flush=True)
    else:
        on_line(msg)


def progress_line(pct: int, done: int, total: int, t0: float, what: str = "copy") -> str:
    secs = time.monotonic() - t0
    rate = done / secs if secs > 0.5 else 0
    tail = f" @ {rate / 1e6:.2f} MB/s ETA {int((total - done) / rate)}s" if rate > 0 else ""
    return f"[####] {pct}% {what}{tail}"


def _same_device(a: Path, b: Path) -> bool:
    """checks the closest existing parent: a dest folder not created yet has no st_dev"""
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
    """macOS fsync stops at the drive cache; F_FULLFSYNC makes unplugging after a move safe"""
    if sys.platform == "darwin":
        try:
            import fcntl
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except (OSError, AttributeError, ImportError):
            pass
    os.fsync(fd)


def _fsync_dir(path: Path) -> None:
    """best effort: Windows can't fsync a directory and some filesystems refuse it"""
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
    """APFS clonefile: instant and free until one copy changes; False where unsupported"""
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
    """resolve() also catches macOS case-insensitive twins; a missing dst never matches"""
    try:
        return dst.exists() and src.resolve() == dst.resolve()
    except Exception:
        return False


def run_copy(src, dst_dir, *,
             dst_name: Optional[str] = None,
             mode: str = KEEP,
             on_line: Optional[Callable[[str], None]] = None) -> int:
    # first line, so even a skip or exit 2/3 is read as a copy by the app
    _print(on_line, "[JOB] copy")
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

    _print(on_line, "[PHASE] Writing Final Image")

    if same_drive and mode == KEEP and _clone(src, dst):
        _print(on_line, f"[INFO] copy: same-drive clone - {src.name} → {dst} (no extra space until one changes)")
        _print(on_line, f"[####] 100% copy")
        _print(on_line, f"[SUCCESS] Copied {src.name} → {dst}")
        return 0

    if same_drive and mode != KEEP:
        _print(on_line, f"[INFO] copy: same-drive move - {src.name} → {dst}")
        try:
            os.rename(src, dst)
        except OSError as e:
            if not cross_device(e):
                _print(on_line, f"[ERROR] copy: rename failed: {e}")
                return 1
            _print(on_line, "[INFO] copy: the drive could not rename across, copying instead")
        else:
            _print(on_line, f"[####] 100% move")
            # no [PHASE] Complete here: the worker owns stages, ours would race it (Complete: 0%)
            _print(on_line, f"[SUCCESS] Moved {src.name} → {dst}")
            return 0

    # chunked through a temp file, so a cut-off copy never shows up under the final name
    try:
        free = shutil.disk_usage(dst_dir).free
    except OSError:
        free = None
    if free is not None and free < total:
        _print(on_line, f"[ERROR] copy: not enough space on the destination: {total / 1e9:.2f} GB needed, "
                        f"{free / 1e9:.2f} GB free")
        return 1
    _print(on_line, f"[INFO] copy: {'cross-drive' if not same_drive else 'same-drive'} copy - {src.name} → {dst}")
    written = 0
    last_pct = -1
    t0 = time.monotonic()
    tmp = None
    try:
        tmp = _temp_next_to(dst)
        with open(src, "rb", buffering=0) as fin, open(tmp, "wb", buffering=0) as fout:
            while True:
                buf = fin.read(CHUNK)
                if not buf:
                    break
                # an unbuffered write may take fewer bytes than offered
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
                        _print(on_line, progress_line(pct, written, total, t0))
                        last_pct = pct
            # the source may only go once all of it is on disk: sync, then check count and size
            # a short copy (unplugged drive, source changed, silent ENOSPC) must fail here
            fout.flush()
            _fsync_file(fout.fileno())
            landed = os.fstat(fout.fileno()).st_size
            if written != total or landed != total:
                raise OSError(f"short copy: {landed} of {total} bytes reached {tmp.name}")
        os.replace(tmp, dst)
        _fsync_dir(dst.parent)
    except Exception as e:
        try:
            if tmp is not None and tmp.exists():
                tmp.unlink()                # ours alone
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
            # the copy is fine, so a failed source delete is only a warning
            _print(on_line, f"[WARN] copy: source could not be deleted: {e}")

    _print(on_line, f"[SUCCESS] {'Moved' if delete_source else 'Copied'} "
                    f"{src.name} → {dst}")
    return 0


def run_copy_tree(src, dst, *, mode: str = KEEP, on_line: Optional[Callable[[str], None]] = None) -> int:
    """copy or move an unpacked game folder; it only gets its name once complete"""
    _print(on_line, "[JOB] copy")
    if mode not in MODES:
        _print(on_line, f"[ERROR] copy: unknown mode {mode!r} (keep, organize or move)")
        return 1
    src, dst = Path(src), Path(dst)
    if not src.is_dir():
        _print(on_line, f"[ERROR] copy: source folder not found: {src}")
        return 1
    if _resolves_same(src, dst):
        _print(on_line, f"[WARN] copy: source and destination are the same folder, skipping: {src}")
        return 2
    if dst.exists():
        _print(on_line, f"[WARN] copy: destination already exists, skipping: {dst}")
        return 3
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _print(on_line, f"[ERROR] copy: cannot create destination folder {dst.parent}: {e}")
        return 1
    _print(on_line, "[PHASE] Writing Final Image")
    same_drive = _same_device(src, dst.parent)
    if same_drive and mode != KEEP:
        try:
            os.rename(src, dst)
        except OSError as e:
            if not cross_device(e):
                _print(on_line, f"[ERROR] copy: rename failed: {e}")
                return 1
            _print(on_line, "[INFO] copy: the drive could not rename across, copying instead")
        else:
            _print(on_line, "[####] 100% move")
            _print(on_line, f"[SUCCESS] Moved {src.name} → {dst}")
            return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # ultra_core next to the app
    from ultra_core import is_fs_junk_name as junk
    files, dirs, unreadable = [], [], []
    for dirpath, dirnames, filenames in os.walk(src, onerror=unreadable.append):
        rel = Path(dirpath).relative_to(src)
        dirnames[:] = [d for d in dirnames if not junk(d)]
        # folder links are copied as links too (os.walk lists them as dirs)
        files.extend(rel / d for d in dirnames if os.path.islink(os.path.join(dirpath, d)))
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        dirs.extend(rel / d for d in dirnames)
        files.extend(rel / f for f in filenames if not junk(f))
    try:
        total = sum(os.lstat(src / f).st_size for f in files)
    except OSError as e:
        unreadable.append(e)
    if unreadable:
        _print(on_line, f"[ERROR] copy: cannot read the source: {unreadable[0]}")
        return 1
    try:
        free = shutil.disk_usage(dst.parent).free
    except OSError:
        free = None
    if free is not None and free < total:
        _print(on_line, f"[ERROR] copy: not enough space on the destination: {total / 1e9:.2f} GB needed, "
                        f"{free / 1e9:.2f} GB free")
        return 1
    _print(on_line, f"[INFO] copy: folder copy - {src.name} → {dst}")
    written, last_pct, t0 = 0, -1, time.monotonic()
    tmp = None
    try:
        tmp = _temp_next_to(dst, folder=True)
        for d in dirs:
            (tmp / d).mkdir(parents=True, exist_ok=True)
        for f in files:
            s, t = src / f, tmp / f
            if s.is_symlink():
                os.symlink(os.readlink(s), t, target_is_directory=s.is_dir())  # Windows has separate file and dir symlinks
                continue
            with open(s, "rb", buffering=0) as fin, open(t, "wb", buffering=0) as fout:
                while True:
                    buf = fin.read(CHUNK)
                    if not buf:
                        break
                    view = memoryview(buf)
                    while view:
                        n = fout.write(view) or len(view)
                        written += n
                        view = view[n:]
                    if total:
                        pct = min(99, int(written * 100 / total))
                        if pct != last_pct:
                            _print(on_line, progress_line(pct, written, total, t0))
                            last_pct = pct
                _fsync_file(fout.fileno())
                if os.fstat(fout.fileno()).st_size != os.lstat(s).st_size:
                    raise OSError(f"short copy: {f}")
            shutil.copystat(s, t, follow_symlinks=False)
        os.rename(tmp, dst)
        _fsync_dir(dst.parent)
    except Exception as e:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)       # ours alone
        _print(on_line, f"[ERROR] copy: write failed: {e}")
        return 1
    _print(on_line, "[####] 100% copy")
    if mode == MOVE:
        _print(on_line, "[PHASE] Cleaning Up")
        try:
            rmtree_writable(src)
            _print(on_line, f"[INFO] copy: source removed after a complete copy: {src}")
        except Exception as e:
            _print(on_line, f"[WARN] copy: source could not be removed: {e}")
    _print(on_line, f"[SUCCESS] {'Moved' if mode == MOVE else 'Copied'} {src.name} → {dst}")
    return 0
