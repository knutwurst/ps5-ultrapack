"""What happens to a job's source once the job is done, and to the computer once the queue is.

A source is kept, moved to the Trash, moved to a folder, or deleted for good. Every action
first passes `refusal()`: the app never touches a path that is or holds the job's output,
the temp folder, a drive, the home folder, or a source another waiting job still needs.
Tk-free, so the tests drive it with scratch folders.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

KEEP, TRASH, MOVE, DELETE = "keep", "trash", "move", "delete"
ACTIONS = (KEEP, TRASH, MOVE, DELETE)
LABELS = {KEEP: "Keep", TRASH: "Move to Trash", MOVE: "Move to folder", DELETE: "Delete"}
DONE_TEXT = {KEEP: "keep the source", TRASH: "move the source to the Trash",
             MOVE: "move the source to {dest}", DELETE: "delete the source"}

QUEUE_ACTIONS = ("nothing", "sleep", "quit")
QUEUE_LABELS = {"nothing": "Nothing", "sleep": "Sleep", "quit": "Quit"}
NOTIFY = ("off", "job", "queue")
NOTIFY_LABELS = {"off": "Off", "job": "Each job", "queue": "Queue end"}

CHUNK = 8 * 1024 * 1024
_VOLUME_RE = re.compile(r"(\.part\d+\.rar|\.r\d{2,}|\.7z\.\d{3}|\.zip\.\d{3}|\.z\d{2}|\.\d{3}|\.rar|\.zip|\.7z)$",
                        re.I)


def _real(p) -> Path:
    return Path(os.path.realpath(str(p)))


def _within(a: Path, b: Path) -> bool:
    """a is b or lies inside b."""
    return a == b or b in a.parents


def size_of(paths: Iterable[Path]) -> int:
    """Bytes of data under *paths*; a symbolic link counts as none."""
    total = 0
    for p in paths:
        p = Path(p)
        try:
            if p.is_symlink():
                continue
            if p.is_file():
                total += p.stat().st_size
            elif p.is_dir():
                for dp, _dn, fns in os.walk(p):
                    for fn in fns:
                        fp = os.path.join(dp, fn)
                        try:
                            if not os.path.islink(fp):
                                total += os.stat(fp).st_size
                        except OSError:
                            pass
        except OSError:
            pass
    return total


def refusal(sources: list, *, output=None, dest=None, protected: Iterable = (),
            others: Iterable = ()) -> Optional[str]:
    """Why *sources* must stay where they are, or None when the action may run.

    *output* is the file or folder the job wrote (a source in the same folder is a
    library file and stays: a .ffpfsc next to the .pkg built from it), *dest* the folder a move goes to,
    *protected* the app's own folders (temp, profile), *others* the sources of jobs that
    have not run yet."""
    if not sources:
        return "the job has no source on disk"
    home = _real(Path.home())
    out = _real(output) if output else None
    dst = _real(dest) if dest else None
    prot = [_real(p) for p in protected if p]
    other = [_real(p) for p in others if p]
    for s in sources:
        r = _real(s)
        if not os.path.lexists(str(s)):
            return f"{Path(s).name} is no longer there"
        if os.path.ismount(str(r)) or r == Path(r.anchor):
            return f"{r} is a whole drive"
        if _within(home, r):
            return f"{r} holds the home folder"
        if out is not None and (_within(out, r) or _within(r, out)):
            return f"{r.name} holds the job's output" if _within(out, r) else f"{r.name} is inside the job's output"
        if out is not None and not out.is_dir() and r.parent == out.parent:
            return f"{r.name} sits in the folder the job wrote to, next to its output"
        if dst is not None and _within(dst, r):
            return f"the destination folder is inside {r.name}"
        for p in prot:
            if _within(p, r) or _within(r, p):
                return f"{r.name} overlaps the app's own folder {p}"
        for o in other:
            if _within(o, r) or _within(r, o):
                return f"another job in the queue still needs {r.name}"
    return None


# ── the four actions ──────────────────────────────────────────────────────────

def apply(action: str, sources: list, *, dest=None,
          on_progress: Optional[Callable[[int, int], None]] = None,
          trash: Optional[Callable[[Path], None]] = None,
          same_device: Optional[Callable[[Path, Path], bool]] = None) -> list:
    """Run *action* on every path in *sources*. Returns where each one went (the Trash
    or the folder) for the log. Raises OSError on the first failure; a path already
    handled stays handled, the rest stay where they are."""
    sources = [Path(s) for s in sources]
    if action == KEEP:
        return []
    if action == TRASH:
        put = trash or move_to_trash
        return [put(s) or "Trash" for s in sources]
    if action == DELETE:
        for s in sources:
            _delete(s)
        return ["deleted"] * len(sources)
    if action == MOVE:
        if not dest:
            raise OSError("no destination folder is set")
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        targets = _free_names(sources, dest)
        same = same_device or _same_device
        total = size_of(sources)
        done = [0]

        def tick(n):
            done[0] += n
            if on_progress:
                on_progress(done[0], total)
        out = []
        for s, t in zip(sources, targets):
            if same(s, dest):
                os.rename(s, t)
                tick(size_of([t]))
            else:
                _copy_then_remove(s, t, tick)
            out.append(str(t))
        return out
    raise ValueError(f"unknown action: {action}")


def _delete(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)


def _same_device(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _split(name: str, is_dir: bool):
    """(base, rest) so that base + " (2)" + rest is the numbered name; the volume
    suffix of a split archive stays at the end ("Game (2).part1.rar")."""
    if is_dir:
        return name, ""
    m = _VOLUME_RE.search(name)
    if m:
        return name[:m.start()], name[m.start():]
    stem, dot, ext = name.rpartition(".")
    return (stem, dot + ext) if stem else (name, "")


def _free_names(sources: list, dest: Path) -> list:
    """Target paths in *dest* that do not exist yet. The parts of one archive set get
    the same number, so the set still opens."""
    n = 1
    while True:
        names = []
        for s in sources:
            base, rest = _split(s.name, s.is_dir())
            names.append(dest / (s.name if n == 1 else f"{base} ({n}){rest}"))
        if not any(os.path.lexists(str(t)) for t in names):
            return names
        n += 1


def _copy_file(src: Path, dst: Path, tick) -> None:
    with open(src, "rb", buffering=0) as fin, open(dst, "wb", buffering=0) as fout:
        while True:
            buf = fin.read(CHUNK)
            if not buf:
                break
            view = memoryview(buf)
            while view:
                n = fout.write(view)
                n = len(view) if n is None else n
                view = view[n:]
                tick(n)
        fout.flush()
        os.fsync(fout.fileno())
    shutil.copystat(src, dst)
    if os.path.getsize(dst) != os.path.getsize(src):
        raise OSError(f"short copy: {dst.name}")


def _copy_then_remove(src: Path, dst: Path, tick) -> None:
    """Copy *src* to *dst* on another drive, check every file's size, then remove *src*.
    A failed copy removes what it wrote and leaves *src* untouched."""
    try:
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            _copy_file(src, dst, tick)
        else:
            dst.mkdir()
            for dp, dns, fns in os.walk(src):
                rel = Path(dp).relative_to(src)
                for d in dns:
                    sd, td = Path(dp) / d, dst / rel / d
                    if sd.is_symlink():
                        os.symlink(os.readlink(sd), td)
                    else:
                        td.mkdir()
                for fn in fns:
                    sf, tf = Path(dp) / fn, dst / rel / fn
                    if sf.is_symlink():
                        os.symlink(os.readlink(sf), tf)
                    else:
                        _copy_file(sf, tf, tick)
            if size_of([dst]) != size_of([src]):
                raise OSError(f"short copy: {dst.name}")
    except BaseException:
        try:
            if os.path.lexists(str(dst)):
                _delete(dst)
        except OSError:
            pass
        raise
    _delete(src)


# ── the Trash ─────────────────────────────────────────────────────────────────

def move_to_trash(path: Path) -> str:
    """Move *path* to the Trash of its drive, where Finder's Put Back can restore it.
    Returns where it went ("Trash" when the system does not say)."""
    path = Path(path)
    if sys.platform == "darwin":
        return _trash_mac(path)
    if sys.platform == "win32":
        _trash_windows(path)
        return "Recycle Bin"
    raise OSError("this system has no Trash the app can use")


def _trash_mac(path: Path) -> str:
    import ctypes
    import ctypes.util
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
    vp = ctypes.c_void_p
    objc.objc_getClass.restype, objc.objc_getClass.argtypes = vp, [ctypes.c_char_p]
    objc.sel_registerName.restype, objc.sel_registerName.argtypes = vp, [ctypes.c_char_p]
    objc.objc_autoreleasePoolPush.restype = vp
    objc.objc_autoreleasePoolPop.argtypes = [vp]
    send = objc.objc_msgSend

    def call(restype, obj, sel, *args):
        send.restype = restype
        send.argtypes = [vp, vp] + [type(a) if isinstance(a, ctypes._SimpleCData) else vp for a in args]
        return send(obj, objc.sel_registerName(sel), *args)

    pool = objc.objc_autoreleasePoolPush()
    try:
        cls = objc.objc_getClass
        s = call(vp, cls(b"NSString"), b"stringWithUTF8String:", ctypes.c_char_p(os.fsencode(str(path))))
        url = call(vp, cls(b"NSURL"), b"fileURLWithPath:", vp(s))
        fm = call(vp, cls(b"NSFileManager"), b"defaultManager")
        err, landed = vp(), vp()
        send.restype = ctypes.c_bool
        send.argtypes = [vp, vp, vp, ctypes.POINTER(vp), ctypes.POINTER(vp)]
        ok = send(fm, objc.sel_registerName(b"trashItemAtURL:resultingItemURL:error:"), url,
                  ctypes.byref(landed), ctypes.byref(err))
        if not ok:
            why = "the Trash refused it"
            if err.value:
                desc = call(vp, err, b"localizedDescription")
                raw = call(ctypes.c_char_p, vp(desc), b"UTF8String") if desc else None
                why = raw.decode("utf-8", "replace") if raw else why
            raise OSError(f"could not move {path.name} to the Trash: {why}")
        where = "Trash"
        if landed.value:
            lp = call(vp, landed, b"path")
            raw = call(ctypes.c_char_p, vp(lp), b"UTF8String") if lp else None
            where = raw.decode("utf-8", "replace") if raw else where
        return where
    finally:
        objc.objc_autoreleasePoolPop(pool)


def _trash_windows(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted:
        raise OSError(f"could not move {path.name} to the Recycle Bin (code {rc})")


# ── notifications and the end of the queue ────────────────────────────────────

def notify(title: str, message: str) -> None:
    """A banner in Notification Center. Best effort: nothing happens where it is not supported."""
    if sys.platform != "darwin":
        return
    script = ["on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run"]
    cmd = ["osascript"]
    for line in script:
        cmd += ["-e", line]
    try:
        subprocess.Popen(cmd + [title, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def sleep_now() -> None:
    """Put the computer to sleep."""
    if sys.platform == "darwin":
        subprocess.run(["pmset", "sleepnow"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    elif sys.platform == "win32":
        subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=True)
    else:
        subprocess.run(["systemctl", "suspend"], check=True)
