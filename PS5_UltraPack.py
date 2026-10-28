
from __future__ import annotations

import os
import re
import sys
import time
import json
import queue
import zipfile
import copy
import threading
import subprocess
import tempfile
import signal
import shutil
import struct
import types
import webbrowser
import filecmp
import multiprocessing
from pathlib import Path


def _ensure_std_streams() -> None:
    """a windowed Windows build may start with no stdout/stderr; writes must not kill the job"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _prepare_multiprocessing_runtime() -> None:
    """Keep frozen multiprocessing children from starting the GUI."""
    multiprocessing.freeze_support()


def _bundled_backend_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend"
    return Path(__file__).resolve().parent / "backend"


def _run_packaged_cli_mode() -> None:
    """Run bundled backend entry points without requiring an external Python."""
    if len(sys.argv) <= 1 or sys.argv[1] not in {"--backend-internal", "--mkpfs-internal"}:
        return

    if sys.argv[1] == "--mkpfs-internal" and sys.platform == "darwin":
        # fork, not spawn: spawn re-execs the frozen app and every worker opens the GUI.
        # only safe while this branch imports no Tk/CTk/PIL: fork after Cocoa init is unsafe
        try:
            multiprocessing.set_start_method("fork", force=True)
        except RuntimeError:
            pass

    backend_dir = _bundled_backend_dir()
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    for _stream in (sys.stdout, sys.stderr):     # paths beyond the ANSI code page
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        if sys.argv[1] == "--mkpfs-internal":
            from mkpfs.cli import cli_mkpfs_main

            sys.exit(cli_mkpfs_main(sys.argv[2:]))

        import runpy

        cli_py = backend_dir / "cli.py"
        sys.argv = [str(cli_py)] + sys.argv[2:]
        runpy.run_path(str(cli_py), run_name="__main__")
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:
        # into the job log, or the windowed exe pops a crash window of its own
        import traceback
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        sys.exit(1)
    sys.exit(0)


_ensure_std_streams()
_prepare_multiprocessing_runtime()
_run_packaged_cli_mode()

from tkinter import filedialog, messagebox, ttk
import tkinter as tk

try:
    import customtkinter as ctk
except ImportError:
    raise SystemExit("Missing customtkinter. Run: py -m pip install customtkinter")

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

try:
    import winsound
except Exception:
    winsound = None

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    TkinterDnD = None
    DND_FILES = None
    _HAS_DND = False

APP_NAME = "PS5 UltraPack"
APP_VERSION = "2.3.3"
APP_EDITION = " (Windows preview)" if sys.platform == "win32" else ""
# archive unpack fills the first slice of overall progress so it never goes back
ARCHIVE_EXTRACT_OVERALL_PCT = 25
# copy jobs unpack on the output drive, so a rename follows (or a copy across drives)
ARCHIVE_EXTRACT_RENAME_PCT = 97
ARCHIVE_EXTRACT_COPY_PCT = 50


def unpack_rate(pct: float, secs: float, total_bytes: int) -> tuple[str, str]:
    """(speed, time left), ' - ' until 2% and 3 s are in or without a known size"""
    if pct < 2 or secs < 3 or pct >= 100:
        return " - ", " - "
    left = humanize_eta(f"{int(secs * (100 - pct) / pct)}s")
    if total_bytes <= 0:
        return " - ", left
    rate = total_bytes * pct / 100.0 / secs
    return (f"{rate / 1e9:.2f} GB/s" if rate >= 1e9 else f"{rate / 1e6:.1f} MB/s"), left
BACKEND_NAME = "bizkut/ps5-ffpfs-cli"
MKPFS_NAME    = "MkPFS"
MKPFS_VERSION = "1.0.0"

# ultra_core sits next to us; tests exec this file from other directories
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import ultra_core  # noqa: E402
from ultra_core import *  # noqa: E402,F401,F403
import i18n  # noqa: E402
from i18n import t as tr, plural  # noqa: E402
# language is read once at start; a change needs a restart
i18n.use(str((load_settings() or {}).get("language", "en")))
i18n.install()


class TextVar(tk.StringVar):
    """translated on set; display only, values the code compares stay plain StringVars"""

    def set(self, value):
        return super().set(tr(value) if isinstance(value, str) else value)

    initialize = set
import ui_kit  # noqa: E402
from ui_kit import (Kit, IconButton, IconView, ArtView, ProgressBar, QueueList, TransportControl, unpack_touchpad_delta,  # noqa: E402
                    Chips, StepStrip, LogText, Tile, RoundBox, ctk_pair, apply_ctk_theme, PALETTE)


def _backport_module():
    """read-only helpers in-process; jobs still run backport.py through the CLI"""
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import backport
    return backport


def _title_db_module():
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import title_db
    return title_db


def _ps4pkg_module():
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import ps4pkg
    return ps4pkg


def version_note(vp: dict) -> str:
    """why the console will offer the official update for this PS4 package"""
    if vp["named"]:
        return tr("An update (v{ver}) is merged into this full game, but its app version is still {app}: the "
                  "console will offer the official update.", ver=vp["version"], app=vp["app_ver"])
    return tr("Its master version {ver} is above its app version {app}: if an update is merged in, the console "
              "will offer the official one.", ver=vp["version"], app=vp["app_ver"])


def _ks_job_module():
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import ps4_keystone_job
    return ps4_keystone_job


def _is_ps4_source(p) -> bool:
    """a PS4 .pkg or a PS4-only folder; anything PS5 beside them makes it mixed"""
    try:
        p = Path(p)
        m = _ps4pkg_module()
        if p.is_file():
            return p.suffix.lower() == ".pkg" and m.is_ps4_package(p)
        if not p.is_dir() or is_game_folder(p):
            return False
        sources = find_job_sources(p)
        return bool(sources) and all(s.is_file() and s.suffix.lower() == ".pkg" and m.is_ps4_package(s)
                                     for s in sources)
    except Exception:
        return False


def _organize_module():
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import organize_lib
    return organize_lib


def _patch_backup_module():
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import patch_backup
    return patch_backup


def _after_job_module():
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import after_job
    return after_job


def _remove_scratch(p: Path) -> int:
    """our own scratch only; the int is what it freed"""
    try:
        if p.is_dir() and not p.is_symlink():
            size = get_folder_size(p)
            shutil.rmtree(str(p), ignore_errors=True)
            return size
        if p.is_symlink() or p.exists():
            size = p.stat().st_size if p.is_file() else 0
            p.unlink()
            return size
    except OSError:
        pass
    return 0


def _drive_name(path: Path) -> str:
    """'SAMSUNG', 'D: (Games)' or 'the system drive'"""
    return ultra_core.drive_display_name(path)


def _drive_colon(path: Path) -> str:
    """'C:' stays 'C:', not 'C::'"""
    name = _drive_name(path)
    return name if name.endswith(":") else name + ":"


def _sane_patched_libs(libs: str, fw_root: str) -> str:
    """'' when it points into the firmware folder: originals never go into fakelib/"""
    try:
        return "" if (libs and _backport_module().patched_libs_problem(libs, fw_root)) else libs
    except Exception:
        return libs


def _firmware_folder_status(raw: str) -> str:
    if not raw:
        return "Not set: backports use the three public targets, and Check has nothing to compare with."
    root = Path(raw)
    if not root.is_dir():
        return "This folder does not exist."
    try:
        folders = _backport_module().firmware_folders(root)
    except Exception as e:
        return tr("Could not read the folder: {e}", e=e)
    if folders:
        names = list(folders)
        span = names[0] if len(names) == 1 else f"{names[0]} … {names[-1]}"
        return tr("{names} firmware folder(s) found: {span}.", names=len(names), span=span)
    if any(root.rglob("*.sprx")):
        return "No firmware subfolders: the libraries here count for every target. Name a subfolder per firmware."
    return "No firmware subfolders and no .sprx libraries found."


def same_content(a: Path, b: Path) -> bool:
    """byte for byte up to 256 MB; above that size, head, tail and 16 samples (no hashing of GBs)"""
    try:
        size = a.stat().st_size
        if not b.is_file() or b.stat().st_size != size:
            return False
        if size <= 256 * 1024 * 1024:
            return filecmp.cmp(a, b, shallow=False)
        edge, block = 8 * 1024 * 1024, 1024 * 1024
        spots = [(0, edge), (size - edge, edge)] + [(size * k // 17, block) for k in range(1, 17)]
        with open(a, "rb") as fa, open(b, "rb") as fb:
            for off, n in spots:
                fa.seek(off); fb.seek(off)
                if fa.read(n) != fb.read(n):
                    return False
        return True
    except OSError:
        return False


def fit_notes(view, notes, beside=None) -> None:
    """room = view width less indent and margin, in logical px since CTk scales wraplength"""
    try:
        s = notes[0]._get_widget_scaling()
        room = view.winfo_width() / s - 146 - 6
        if room <= 0:
            return
        for i, lbl in enumerate(notes):
            w = room - (beside.winfo_reqwidth() / s + 10 if i == 0 and beside is not None
                        and beside.winfo_manager() else 0)
            lbl.configure(wraplength=max(160, round(w)))
    except (tk.TclError, AttributeError):
        pass


def wrap_beside(row, label, *others, pad=16) -> None:
    """others pack first, so a narrow window wraps the label instead of hiding them"""
    def _fit(e):
        try:
            s = label._get_widget_scaling()
            room = (e.width - sum(o.winfo_reqwidth() for o in others)) / s - pad
            label.configure(wraplength=max(120, round(room)))
        except (tk.TclError, AttributeError):
            pass
    row.bind("<Configure>", _fit, add="+")


def fit_columns(tree) -> None:
    """Tk won't shrink stretching columns on its own, the last ones just slide out of sight"""
    def _fit(e):
        try:
            cols = ["#0", *tree["columns"]]
            over = sum(int(tree.column(c, "width")) for c in cols) - e.width
            spare = {c: int(tree.column(c, "width")) - int(tree.column(c, "minwidth")) for c in cols
                     if tree.tk.getboolean(tree.column(c, "stretch"))}
            room = sum(v for v in spare.values() if v > 0)
            if over <= 0 or room <= 0:
                return
            for c, v in spare.items():
                if v > 0:
                    tree.column(c, width=int(tree.column(c, "width")) - min(v, -(-over * v // room)))
        except tk.TclError:
            pass
    tree.bind("<Configure>", _fit, add="+")


_WORD_WIDTHS: dict = {}


def reading_note(after: float = 2.0) -> str:
    """' · reading <archive> · m:ss' while a RAR listing has run for a while, else ''"""
    now = ArchiveExtractor.reading_now()
    if not now or now[1] < after:
        return ""
    secs = int(now[1])
    return "  ·  " + tr("reading {name} · {time}", name=now[0], time=f"{secs // 60}:{secs % 60:02d}")


def _wrapped_line_count(font, text: str, width: int) -> int:
    """Tk-style word wrap; widths cached since every measure is a round trip to Tk"""
    try:
        fkey = tuple(sorted(font.actual().items()))   # a page makes new font objects each time it opens
    except (tk.TclError, AttributeError, TypeError):
        fkey = (id(font),)
    widths = _WORD_WIDTHS.setdefault(fkey, {})
    if len(widths) > 5000:
        widths.clear()

    def measure(word):
        w = widths.get(word)
        if w is None:
            w = widths[word] = font.measure(word)
        return w

    lines = 0
    for paragraph in str(text).split("\n"):
        lines += 1
        used, space = 0, measure(" ")
        for word in paragraph.split():
            w = measure(word)
            if used and used + space + w > width:
                lines += 1
                used = w
            else:
                used = used + (space if used else 0) + w
    return max(1, lines)


def _augment_path_for_gui() -> None:
    """Finder hands GUI apps a bare PATH, so Homebrew 7z/unrar would go missing"""
    if os.name == "nt":
        return
    extra = [
        "/opt/homebrew/bin",   # Homebrew (Apple Silicon)
        "/usr/local/bin",      # Homebrew (Intel) / common
        "/opt/local/bin",      # MacPorts
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]
    parts = os.environ.get("PATH", "").split(os.pathsep)
    parts = [p for p in parts if p]
    for d in extra:
        if d not in parts and os.path.isdir(d):
            parts.append(d)
    os.environ["PATH"] = os.pathsep.join(parts)


_augment_path_for_gui()

# (light, dark) pairs from the ui_kit palette, shared with the main window
BLACK   = ctk_pair("surface")         # dialog background
PANEL   = ctk_pair("surface2")
CARD    = ctk_pair("control")
CARD2   = ctk_pair("control")
BTN     = ctk_pair("btn")
BTN_HOVER = ctk_pair("btn_hover")
BTN_BORDER = ctk_pair("btn_border")   # a hairline in light mode, the fill colour in dark
BORDER  = ctk_pair("border")
BORDER2 = ctk_pair("border_strong")
ACCENT  = ctk_pair("accent_fill")  # the one primary action per window
ACCENT_HOVER = ctk_pair("accent_hover")
ON_ACCENT = "#ffffff"
HOVER   = ctk_pair("control_hover")
SUCCESS = ctk_pair("success")
YELLOW  = ctk_pair("warning")
RED     = ctk_pair("danger")
DANGER_HOVER = ctk_pair("danger_hover")
WHITE   = ctk_pair("text")            # primary text (dark text in light mode)
MUTED   = ctk_pair("muted")
MONO_FONT = "Consolas" if sys.platform == "win32" else "Menlo"   # each ships with its system
# menus get ACCEL (Tk on macOS maps Command-N to a real key), tooltips SHORTCUT
IS_MAC = sys.platform == "darwin"
TRASH_NAME = "Recycle Bin" if sys.platform == "win32" else "Trash"


def _without_sheet(fn):
    """macOS turns a parented dialog into a sheet, and AppKit aborts when it refuses one"""
    def call(*args, **kw):
        kw.pop("parent", None)
        return fn(*args, **kw)
    call.__wrapped__ = fn
    call.__name__ = getattr(fn, "__name__", "dialog")
    return call


if IS_MAC:
    for _mod, _names in ((filedialog, ("askdirectory", "askopenfilename", "askopenfilenames", "asksaveasfilename")),
                         (messagebox, ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel",
                                       "askyesnocancel", "askretrycancel", "askquestion"))):
        for _n in _names:
            setattr(_mod, _n, _without_sheet(getattr(_mod, _n)))

def native_path(p) -> str:
    """Windows wants backslashes; Tk dialogs and old settings hand out forward ones"""
    p = str(p or "")
    return os.path.normpath(p) if (p and sys.platform == "win32") else p


def _native_paths(fn):
    def call(*args, **kw):
        r = fn(*args, **kw)
        if isinstance(r, str):
            return native_path(r)
        if isinstance(r, (tuple, list)):
            return type(r)(native_path(x) for x in r)
        return r
    call.__wrapped__ = fn
    call.__name__ = getattr(fn, "__name__", "dialog")
    return call


if sys.platform == "win32":
    for _n in ("askdirectory", "askopenfilename", "askopenfilenames", "asksaveasfilename"):
        setattr(filedialog, _n, _native_paths(getattr(filedialog, _n)))

MOD = "Command" if IS_MAC else "Control"
_KEYS = {"add": "N", "open": "O", "start": "R", "stop": ".", "settings": ",", "queue": "1",
         "history": "2", "log": "3", "details": "I"}
if IS_MAC:
    ACCEL = {k: ("Command-Option-I" if k == "details" else f"Command-{v}") for k, v in _KEYS.items()}
    SHORTCUT = {k: ("⌥⌘I" if k == "details" else f"⌘{v}") for k, v in _KEYS.items()}
else:
    ACCEL = SHORTCUT = {k: ("Ctrl+Alt+I" if k == "details" else f"Ctrl+{v}") for k, v in _KEYS.items()}
DETAILS_SEQ = "<Command-Option-i>" if IS_MAC else "<Control-Alt-i>"
REPO_URL = "https://github.com/knutwurst/ps5-ultrapack"
DONATE_URL = "https://paypal.me/oliverkoester"
# set any CTk scaling before the first window, later it pins open windows' sizes


# panels and message windows

def set_window_appearance(win, mode: str) -> None:
    """macOS follows the system setting otherwise; MacWindowStyle needs Tk 8.6.11+"""
    try:
        win.tk.call("::tk::unsupported::MacWindowStyle", "appearance", win._w,
                    "aqua" if str(mode).lower() == "light" else "darkaqua")
    except tk.TclError:
        pass


class ThinScrollbar(tk.Canvas):
    """CTkScrollbar calls update_idletasks() on every set(): a full page relayout. no thanks"""

    def __init__(self, master, command, bg="transparent", thumb=None, hover=None):
        S = ui_kit.SCALE
        self._pad, self._min, self._lw = 6 * S, 30 * S, max(3, round(7 * S))
        super().__init__(master, width=round(12 * S), highlightthickness=0, bd=0)
        self.command = command
        self._colors = {"bg": bg,
                        "thumb": thumb or ctk.ThemeManager.theme["CTkScrollbar"]["button_color"],
                        "hover": hover or ctk.ThemeManager.theme["CTkScrollbar"]["button_hover_color"]}
        self.span = (0.0, 1.0)
        self._px = None                    # (top, bottom) of the thumb as drawn
        self._grab = None                  # pointer offset from the thumb's top while dragging
        self._hot = False
        self._wheel_px = 0.0
        x = round(12 * S) / 2
        self.thumb = self.create_line(x, 0, x, 0, width=self._lw, capstyle="round")
        self.bind("<Configure>", lambda e: self._draw(), add="+")
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_grab", None))
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda e: self.command("scroll", -1, "units"))
        self.bind("<Button-5>", lambda e: self.command("scroll", 1, "units"))
        try:
            self.bind("<TouchpadScroll>", self._touchpad)
        except tk.TclError:
            pass  # Tk 8.6: no such event
        ctk.AppearanceModeTracker.add(self._recolor, self)
        self._recolor()

    @staticmethod
    def _mode_color(c):
        if isinstance(c, (tuple, list)):
            return c[1] if ctk.get_appearance_mode() == "Dark" else c[0]
        return c

    def _background(self):
        c, w = self._colors["bg"], self.master
        while (c in (None, "transparent")) and w is not None:
            try:
                c = w.cget("fg_color")
            except (tk.TclError, ValueError, AttributeError):
                try:
                    c = w.cget("bg")
                except (tk.TclError, AttributeError):
                    c = None
            w = getattr(w, "master", None)
        return self._mode_color(c) if c not in (None, "transparent") else None

    def _recolor(self, _mode=None):
        try:
            bg = self._background()
            if bg:
                tk.Canvas.configure(self, bg=bg)
            self.itemconfigure(self.thumb, fill=self._mode_color(self._colors["hover" if self._hot else "thumb"]))
        except tk.TclError:
            pass

    def _hover(self, hot):
        self._hot = hot
        self._recolor()

    def configure(self, **kw):
        for key, slot in (("fg_color", "bg"), ("button_color", "thumb"), ("button_hover_color", "hover")):
            if key in kw:
                self._colors[slot] = kw.pop(key)
        if kw:
            super().configure(**kw)
        self._recolor()

    def cget(self, key):
        slot = {"fg_color": "bg", "button_color": "thumb", "button_hover_color": "hover"}.get(key)
        return self._colors[slot] if slot else super().cget(key)

    def set(self, lo, hi):
        lo, hi = float(lo), float(hi)
        if (lo, hi) != self.span:
            self.span = (lo, hi)
            self._draw()

    def get(self):
        return self.span

    def _geometry(self):
        """(track top, track length, thumb length) in pixels."""
        h = self.winfo_height()
        lo, hi = self.span
        track = max(1.0, h - 2 * self._pad)
        return self._pad, track, min(track, max(self._min, (hi - lo) * track))

    def _draw(self):
        lo, hi = self.span
        if hi - lo >= 0.999 or self.winfo_height() < 20:
            self._px = None
            self.itemconfigure(self.thumb, state="hidden")
            return
        top0, track, length = self._geometry()
        room = 1.0 - (hi - lo)
        top = top0 + (lo / room if room > 0 else 0.0) * (track - length)
        self._px = (top, top + length)
        x, cap = float(self.cget("width")) / 2, self._lw / 2      # round caps reach past the ends
        self.coords(self.thumb, x, top + cap, x, top + length - cap)
        self.itemconfigure(self.thumb, state="normal")

    def _move_thumb_to(self, top):
        top0, track, length = self._geometry()
        lo, hi = self.span
        room = 1.0 - (hi - lo)
        free = track - length
        frac = (top - top0) / free if free > 0 else 0.0
        self.command("moveto", max(0.0, min(room, frac * room)))

    def _press(self, event):
        if self._px is None:
            return
        top, bottom = self._px
        if top <= event.y <= bottom:
            self._grab = event.y - top
        else:
            self._grab = (bottom - top) / 2
            self._move_thumb_to(event.y - self._grab)

    def _drag(self, event):
        if self._grab is not None:
            self._move_thumb_to(event.y - self._grab)

    def _wheel(self, event):
        d = event.delta
        steps = -int(d / 40) if sys.platform == "win32" else -d
        if steps:
            self.command("scroll", steps, "units")
        return "break"

    def _touchpad(self, event):
        _dx, dy = unpack_touchpad_delta(event.delta)
        self._wheel_px -= dy
        steps = int(self._wheel_px / 20)
        if steps:
            self._wheel_px -= steps * 20
            self.command("scroll", steps, "units")
        return "break"

    def destroy(self):
        try:
            ctk.AppearanceModeTracker.remove(self._recolor)
        except Exception:
            pass
        super().destroy()


def _skip_layout_pass(cls):
    """CTkOptionMenu runs update_idletasks() after every draw; Tk paints at idle anyway"""
    draw = cls._draw

    def _draw(self, no_color_updates=False):
        cv = getattr(self, "_canvas", None)
        if cv is not None and "update_idletasks" not in vars(cv):
            cv.update_idletasks = lambda: None
        return draw(self, no_color_updates)
    cls._draw = _draw


_skip_layout_pass(ctk.CTkOptionMenu)


class _ScrollableFrameBar(ThinScrollbar):
    """a class, not a factory: CTk isinstance()-checks it for wheel scrolling"""

    def __new__(cls, master=None, orientation="vertical", command=None, fg_color=None, button_color=None,
                button_hover_color=None, **kw):
        if orientation != "vertical":
            return ctk.CTkScrollbar(master, orientation=orientation, command=command, fg_color=fg_color,
                                    button_color=button_color, button_hover_color=button_hover_color, **kw)
        return super().__new__(cls)

    def __init__(self, master=None, orientation="vertical", command=None, fg_color=None, button_color=None,
                 button_hover_color=None, **kw):
        super().__init__(master, command, bg=fg_color or "transparent", thumb=button_color, hover=button_hover_color)


import customtkinter.windows.widgets.ctk_scrollable_frame as _ctk_scrollable_frame  # noqa: E402
_ctk_scrollable_frame.CTkScrollbar = _ScrollableFrameBar        # every scrollable frame gets the thin bar


class ScrollFrame(ctk.CTkScrollableFrame):
    """CTk keeps the scrollbar up even with nothing to scroll; this one hides it"""

    SETTLE_MS = 60

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # CTk resets scrollregion on every <Configure>, and Tk relays out even if unchanged
        self._region = None
        self.bind("<Configure>", lambda e: self._fit_region())
        self._sb_shown = True
        self._sb_job = None
        self._parent_canvas.bind("<Configure>", lambda e: self._queue_sb_check(), add="+")
        self.bind("<Configure>", lambda e: self._queue_sb_check(), add="+")
        # CTk binds only <MouseWheel>; Tk 9 on macOS sends two-finger swipes as <TouchpadScroll>
        try:
            self.bind_all("<TouchpadScroll>", self._touchpad_all, add=True)
        except tk.TclError:
            pass                                   # Tk 8.6: no such event

    def _fit_region(self):
        try:
            region = self._parent_canvas.bbox("all")
            if region != self._region:
                self._region = region
                self._parent_canvas.configure(scrollregion=region)
        except tk.TclError:
            pass

    def _touchpad_all(self, event):
        try:
            if not self._check_if_valid_scroll(event.widget):
                return
            cv = self._parent_canvas
            lo, hi = cv.yview()
            if hi - lo >= 0.999:
                return
            dx, dy = unpack_touchpad_delta(event.delta)
            if not dy:
                return
            region = cv.cget("scrollregion")
            parts = [float(v) for v in str(region).split()] if region else []
            height = (parts[3] - parts[1]) if len(parts) == 4 else 0.0
            if height <= 0:
                return
            cv.yview_moveto(max(0.0, min(1.0, lo - dy / height)))   # Tk's rule: scroll -dy pixels
        except tk.TclError:
            pass

    def _queue_sb_check(self):
        if self._sb_job is None:
            self._sb_job = self.after(self.SETTLE_MS, self._sb_check)

    def _sb_check(self):
        self._sb_job = None
        try:
            need = self.winfo_reqheight() > self._parent_canvas.winfo_height() + 2
        except tk.TclError:
            return
        if need != self._sb_shown:
            self._sb_shown = need
            if need:
                self._scrollbar.grid()
            else:
                self._scrollbar.grid_remove()
                self._parent_canvas.yview_moveto(0.0)

    def destroy(self):
        if self._sb_job is not None:
            try:
                self.after_cancel(self._sb_job)
            except tk.TclError:
                pass
            self._sb_job = None
        super().destroy()


class PanelHost:
    """stacked panels over the content area; the sidebar stays but is disabled"""

    MARGIN = 24
    LARGE_MAX_W = 900

    def __init__(self, app, content):
        self.app, self.content = app, content
        self.stack: list = []
        self.frame = app.kit.frame(content, bg="bg")
        self.frame.bind("<Configure>", lambda e: self._layout(), add="+")
        for seq in ("<Escape>", "<Return>", "<KP_Enter>"):
            app.root.bind(seq, lambda e, s=seq: self._on_key(s, e), add="+")

    def push(self, panel):
        if self.stack:
            self.stack[-1]._slot.place_forget()
        self.stack.append(panel)
        if len(self.stack) == 1:
            self.frame.place(in_=self.content, x=0, y=0, relwidth=1, relheight=1)
            self.frame.lift()
        self.frame.after_idle(self._layout)

    def pop(self, panel):
        if panel not in self.stack:
            return
        was_top = self.stack[-1] is panel
        self.stack.remove(panel)
        if not self.stack:
            self.frame.place_forget()
        elif was_top:
            self._layout()

    def close_all(self) -> None:
        """sidebar navigation closes them all, no questions asked"""
        for panel in list(reversed(self.stack)):
            try:
                keep = getattr(panel, "keep_draft", None)
                if keep is not None:
                    keep()                # a half-filled Add job comes back next time
                panel.destroy()
            except Exception:
                pass

    def _layout(self):
        if not self.stack:
            return
        top = self.stack[-1]
        try:
            if not top.winfo_exists():
                return
        except tk.TclError:
            return
        W, H = self.frame.winfo_width(), self.frame.winfo_height()
        if W < 60 or H < 60:
            self.frame.after(40, self._layout)
            return
        S = ui_kit.SCALE                       # W and H are device pixels, the sizes logical
        m = round(self.MARGIN * S)
        slot = top._slot
        if top.LARGE:
            max_w = round(getattr(top, "MAX_W", self.LARGE_MAX_W) * S)
            slot.place(relx=0.5, y=m, anchor="n", width=min(max_w, W - 2 * m), height=H - 2 * m)
        else:
            pw, ph = (round(v * S) for v in top.preferred_size())
            slot.place(relx=0.5, rely=0.5, anchor="center", width=min(pw, W - 2 * m), height=min(ph, H - 2 * m))
        slot.lift()
        top.raise_close_button()

    def _on_key(self, seq, event):
        if not self.stack:
            return None
        top = self.stack[-1]
        fn = top._keys.get(seq)
        if fn is not None:
            return fn(event)
        if seq == "<Escape>":
            top.request_close()
            return "break"
        return None


class EmbeddedDialog(ctk.CTkFrame):
    LARGE = False  # large panels fill the content area
    host = None

    def __init__(self, master=None, **kw):
        host = EmbeddedDialog.host
        # own holder per panel: CTk hooks its master's configure(), stale hooks break recolouring
        self._slot = host.app.kit.frame(host.frame, bg="bg")
        super().__init__(self._slot, fg_color=BLACK, corner_radius=12, border_width=1, border_color=BORDER2)
        self.pack(fill="both", expand=True)
        self._title, self._pref, self._close_cb, self._keys, self._gone = "", (560, 420), None, {}, False
        self._close_btn = IconButton(self, host.app.kit, icon="x", command=self.request_close, variant="ghost",
                                     height=26, width=26, padx=0, icon_size=14, bg="surface", tooltip="Close  (Esc)")
        self._close_btn.place(relx=1.0, x=-round(12 * ui_kit.SCALE), y=round(12 * ui_kit.SCALE), anchor="ne")
        host.push(self)

    def raise_close_button(self):
        try:
            self._close_btn.tk.call("raise", self._close_btn._w)
        except tk.TclError:
            pass

    def title(self, text=None):
        if text is None:
            return self._title
        self._title = str(text)

    def geometry(self, spec=None):
        if spec is None:
            return f"{self.winfo_width()}x{self.winfo_height()}+0+0"
        m = re.match(r"\s*(\d+)x(\d+)", str(spec))
        if m:
            self._pref = (int(m.group(1)), int(m.group(2)))
            if EmbeddedDialog.host:
                EmbeddedDialog.host.frame.after_idle(EmbeddedDialog.host._layout)

    def preferred_size(self):
        return self._pref

    def protocol(self, name=None, func=None):
        if name == "WM_DELETE_WINDOW" and func is not None:
            self._close_cb = func

    def request_close(self):
        if self._close_cb:
            self._close_cb()
        else:
            self.destroy()

    def bind(self, sequence=None, command=None, add=True):
        if sequence in ("<Return>", "<KP_Enter>", "<Escape>"):
            self._keys[sequence] = command
            return None
        return super().bind(sequence, command, add)

    def destroy(self):
        if self._gone:
            return
        self._gone = True
        if EmbeddedDialog.host:
            EmbeddedDialog.host.pop(self)
        super().destroy()
        try:
            self._slot.destroy()
        except tk.TclError:
            pass

    # Toplevel API stubs, a panel has no window to manage
    def transient(self, *a, **k): pass
    def grab_set(self, *a, **k): pass
    def grab_release(self, *a, **k): pass
    def resizable(self, *a, **k): pass
    def minsize(self, *a, **k): pass
    def maxsize(self, *a, **k): pass
    def attributes(self, *a, **k): pass
    wm_attributes = attributes
    def focus_force(self, *a, **k): pass
    def overrideredirect(self, *a, **k): pass
    def deiconify(self, *a, **k): pass
    def iconify(self, *a, **k): pass


REVEAL_LABEL = ("Show in Finder" if sys.platform == "darwin" else
                "Show in Explorer" if sys.platform == "win32" else "Show in folder")


class MessageWindow(ctk.CTkToplevel):
    def __init__(self, parent=None, **kw):
        host = EmbeddedDialog.host
        self._owner = parent if parent is not None else (host.app.root if host else None)
        super().__init__(self._owner, fg_color=BLACK, **kw)
        self._title, self._size, self._close_cb = "", None, None
        self._want_grab = self._shown = False
        self.withdraw()                      # placed before it shows, so it never jumps
        if self._owner is not None:
            super().transient(self._owner.winfo_toplevel())
        super().title("")
        self.bind("<Escape>", lambda e: self._escape())
        self._show_job = self.after(1, self._show)

    def _show(self):
        self._show_job = None
        if self._shown:
            return
        try:
            if not self.winfo_exists():
                return
            owner = self._owner.winfo_toplevel() if self._owner is not None else None
            if owner is not None and not owner.winfo_viewable():
                self._show_job = self.after(250, self._show)  # main window hidden: wait for it
                return
            self.update_idletasks()          # the native window exists from here on
            set_window_appearance(self, ctk.get_appearance_mode())
            S = ui_kit.SCALE                 # placed in device pixels, sized in logical ones
            w, h = ((round(self._size[0] * S), round(self._size[1] * S)) if self._size
                    else (self.winfo_reqwidth(), self.winfo_reqheight()))
            x, y = self._position(owner, w, h)
            super().geometry(f"{round(w / S)}x{round(h / S)}+{x}+{y}")
            self._shown = True
            self.deiconify()
            self.lift()
            self.focus_force()
        except tk.TclError:
            return
        if self._want_grab:
            self._grab_now()

    def _position(self, owner, w, h):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if owner is not None:
            ox, oy = owner.winfo_rootx(), owner.winfo_rooty()
            x = ox + (owner.winfo_width() - w) // 2
            y = oy + max(32, (owner.winfo_height() - h) // 3)
        else:
            x, y = (sw - w) // 2, (sh - h) // 3
        return max(0, min(x, sw - w)), max(28, min(y, sh - h - 8))

    def title(self, text=None):
        if text is None:
            return self._title
        self._title = str(text)

    def geometry(self, spec=None):
        if spec is None:
            return super().geometry()
        m = re.match(r"\s*(\d+)x(\d+)\s*$", str(spec))
        if m and not self._shown:
            self._size = (int(m.group(1)), int(m.group(2)))
            return None
        return super().geometry(spec)

    def protocol(self, name=None, func=None):
        if name == "WM_DELETE_WINDOW" and func is not None:
            self._close_cb = func
        return super().protocol(name, func)

    def _escape(self):
        (self._close_cb or self.destroy)()
        return "break"

    def grab_set(self):
        self._want_grab = True
        if self._shown:
            self._grab_now()

    def _grab_now(self):
        try:
            if self.winfo_exists():
                super().grab_set()
        except tk.TclError:
            pass

    def destroy(self):
        if self._show_job:
            try:
                self.after_cancel(self._show_job)
            except tk.TclError:
                pass
            self._show_job = None
        super().destroy()


class ConfirmWindow(MessageWindow):
    """buttons say what they do; a destructive one is red and never the Return default"""

    def __init__(self, parent, title: str, text: str, action: str, cancel: str = "Cancel",
                 danger: bool = False, detail: str = ""):
        super().__init__(parent)
        self.ok = False
        self.resizable(True, False)
        self.title(title)
        ctk.CTkLabel(self, text=tr(title), font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=RED if danger else WHITE).pack(anchor="w", padx=20, pady=(16, 6))
        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(side="bottom", fill="x", padx=20, pady=(4, 14))
        ctk.CTkButton(btns, text=tr(action), fg_color=RED if danger else ACCENT,
                      hover_color=DANGER_HOVER if danger else ACCENT_HOVER, text_color=ON_ACCENT,
                      command=self._yes).pack(side="right")
        ctk.CTkButton(btns, text=tr(cancel), fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=self.destroy).pack(side="right", padx=(0, 8))
        ctk.CTkLabel(self, text=text, text_color=WHITE, wraplength=480, justify="left",
                     font=ctk.CTkFont(size=13)).pack(anchor="w", padx=20, pady=(0, 6 if detail else 12))
        if detail:
            ctk.CTkLabel(self, text=detail, text_color=MUTED, wraplength=480, justify="left",
                         font=ctk.CTkFont(size=12)).pack(anchor="w", padx=20, pady=(0, 12))
        for seq in ("<Return>", "<KP_Enter>"):
            self.bind(seq, lambda e: (self.destroy() if danger else self._yes()))
        self.update_idletasks()
        self.geometry(f"520x{max(150, self.winfo_reqheight())}")
        self.grab_set()

    def _yes(self):
        self.ok = True
        self.destroy()


class NoticeWindow(MessageWindow):
    def __init__(self, parent, title: str, text: str, warn: bool = False):
        super().__init__(parent)
        self.resizable(True, False)
        self.title(title)
        ctk.CTkLabel(self, text=tr(title), font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=YELLOW if warn else WHITE).pack(anchor="w", padx=20, pady=(16, 6))
        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(side="bottom", fill="x", padx=20, pady=(4, 14))
        ctk.CTkButton(btns, text="OK", width=90, fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT,
                      command=self.destroy).pack(side="right")
        ctk.CTkLabel(self, text=text, text_color=WHITE, wraplength=480, justify="left",
                     font=ctk.CTkFont(size=13)).pack(anchor="w", padx=20, pady=(0, 12))
        for seq in ("<Return>", "<KP_Enter>"):
            self.bind(seq, lambda e: self.destroy())
        self.update_idletasks()
        self.geometry(f"520x{max(130, self.winfo_reqheight())}")


_TREES: list = []  # (tree, style, kit, tags), restyled on theme change


def style_tree(widget, name: str, kit) -> dict:
    """on scaled Windows displays Tk's own fonts still think it's 96 dpi, so ours go in"""
    style = ttk.Style(widget)
    try:
        style.theme_use("default")              # the Aqua theme ignores the colours below
    except tk.TclError:
        pass
    pal = PALETTE["light" if ctk.get_appearance_mode().lower() == "light" else "dark"]
    style.configure(name, background=pal["surface2"], fieldbackground=pal["surface2"],
                    foreground=pal["text"], rowheight=round(24 * ui_kit.SCALE), borderwidth=0)
    style.configure(name + ".Heading", background=pal["surface"], foreground=pal["muted"], borderwidth=0)
    if ui_kit.SCALE != 1:
        style.configure(name, font=kit.fonts.body)
        style.configure(name + ".Heading", font=kit.fonts.small)
    style.map(name, background=[("selected", pal["select"])], foreground=[("selected", pal["text"])])
    return pal


def watch_tree_theme(tree, name: str, kit, tags=None) -> None:
    _TREES.append((tree, name, kit, tags))


def restyle_trees() -> None:
    for tree, name, kit, tags in list(_TREES):
        try:
            if not tree.winfo_exists():
                _TREES.remove((tree, name, kit, tags))
                continue
            pal = style_tree(tree, name, kit)
            if tags is not None:
                tags(pal)
        except tk.TclError:
            pass


def ask_confirm(title: str, text: str, action: str, cancel: str = "Cancel", danger: bool = False,
                detail: str = "", parent=None) -> bool:
    """with the main window hidden (GUI test driver) a plain yes/no stands in"""
    owner = parent or (EmbeddedDialog.host.app.root if EmbeddedDialog.host else None)
    try:
        visible = owner is not None and bool(owner.winfo_toplevel().winfo_viewable())
    except tk.TclError:
        visible = False
    if not visible:
        return bool(messagebox.askyesno(title, text + (f"\n\n{detail}" if detail else "")))
    win = ConfirmWindow(owner, title, text, action, cancel=cancel, danger=danger, detail=detail)
    try:
        owner.wait_window(win)
    except tk.TclError:
        return False
    return win.ok


class FirstRunWizard(EmbeddedDialog):
    LARGE = True
    MAX_W = 700  # a readable column, not the whole window

    def __init__(self, parent):
        super().__init__(parent)
        self.title(f"{APP_NAME} - First Run Setup")
        self.geometry("620x400")
        self.resizable(False, False)
        self.grab_set()
        self.configure(fg_color=BLACK)

        self.step = 0
        self.temp_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.result = {}

        self._build()
        for v in (self.temp_path, self.output_path):
            v.trace_add("write", self._gate_next)
        self._show_step(0)

    def _build(self):
        self.header = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=17, weight="bold"), text_color=WHITE)
        self.header.pack(pady=(24, 6), padx=30, anchor="w")

        self.sub = ctk.CTkLabel(self, text="", text_color=MUTED, wraplength=580, justify="left")
        self.sub.pack(padx=30, anchor="w")

        nav = ctk.CTkFrame(self, fg_color=BLACK)
        nav.pack(side="bottom", fill="x", padx=30, pady=(0, 20))
        self.body = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        self.body.pack(fill="x", padx=30, pady=18)
        nav.grid_columnconfigure(1, weight=1)
        self.back_btn = ctk.CTkButton(nav, text="← Back", width=100, fg_color=BTN, text_color=WHITE,
                                       hover_color=BTN_HOVER, command=self._back, border_width=1, border_color=BTN_BORDER)
        self.back_btn.grid(row=0, column=0, padx=(0, 8))
        self.next_btn = ctk.CTkButton(nav, text="Next →", width=100, fg_color=ACCENT,
                                       text_color=ON_ACCENT, hover_color=ACCENT_HOVER, command=self._next)
        self.next_btn.grid(row=0, column=2)

        self.step_var = TextVar(value="Step 1 of 4")
        ctk.CTkLabel(nav, textvariable=self.step_var, text_color=MUTED).grid(row=0, column=1)

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _show_step(self, n):
        self.step = n
        self.step_var.set(tr("Step {n} of 4", n=n + 1))
        self.back_btn.configure(state="normal" if n > 0 else "disabled")
        self.next_btn.configure(text="Finish" if n == 3 else "Next →")
        self._gate_next()
        self._clear_body()
        ctk.CTkFrame(self.body, height=8, fg_color=PANEL).pack(fill="x")

        if n == 0:
            self.header.configure(text="Choose a temp folder")
            self.sub.configure(text="Put it on a fast SSD or NVMe drive: a mechanical HDD slows large games down.")
            ctk.CTkLabel(self.body, text="Temp folder", text_color=WHITE).pack(anchor="w", padx=14, pady=(6, 4))
            row = ctk.CTkFrame(self.body, fg_color=PANEL)
            row.pack(fill="x", padx=14)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkEntry(row, textvariable=self.temp_path, fg_color=CARD, border_color=BORDER2, text_color=WHITE).grid(row=0, column=0, sticky="ew", padx=(0, 8))
            ctk.CTkButton(row, text="Folder…", width=80, fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                           command=self._browse_temp, border_width=1, border_color=BTN_BORDER).grid(row=0, column=1)

        elif n == 1:
            self.header.configure(text="Choose an output folder")
            self.sub.configure(text="Finished games go here (.ffpfsc, .pkg, .exfat or folders). An external drive "
                                    "works, and so does the temp drive.")
            ctk.CTkLabel(self.body, text="Output folder", text_color=WHITE).pack(anchor="w", padx=14, pady=(6, 4))
            row = ctk.CTkFrame(self.body, fg_color=PANEL)
            row.pack(fill="x", padx=14)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkEntry(row, textvariable=self.output_path, fg_color=CARD, border_color=BORDER2, text_color=WHITE).grid(row=0, column=0, sticky="ew", padx=(0, 8))
            ctk.CTkButton(row, text="Folder…", width=80, fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                           command=self._browse_output, border_width=1, border_color=BTN_BORDER).grid(row=0, column=1)

        elif n == 2:
            self.header.configure(text="Check the drives")
            self.sub.configure(text="Free space and drive type of the folders you picked.")
            lines = []
            tp = self.temp_path.get().strip()
            op = self.output_path.get().strip()
            temp_label = None
            tfree = 0
            if tp:
                tpath = Path(tp)
                tfree = get_free_space(tpath)
                # the SSD/HDD probe can take seconds (diskutil/PowerShell), so it fills this in later
                lines.append(tr("Temp drive:    {tfree} free  ·  type: detecting…", tfree=format_size(tfree)))
            if op:
                opath = Path(op)
                ofree = get_free_space(opath)
                lines.append(tr("Output drive:  {ofree} free", ofree=format_size(ofree)))
            if not lines:
                lines.append("No paths selected. Go back and select folders.")
            for line in lines:
                color = YELLOW if "⚠" in line else WHITE
                lbl = ctk.CTkLabel(self.body, text=line, text_color=color, anchor="w",
                                   font=ctk.CTkFont(family=MONO_FONT, size=11),
                                   justify="left")
                lbl.pack(anchor="w", padx=14, pady=3)
                if temp_label is None and line.startswith("Temp drive:"):
                    temp_label = lbl
            if tp and temp_label is not None:
                self._probe_temp_drive_type(Path(tp), temp_label, tfree)

        elif n == 3:
            self.header.configure(text="Ready")
            self.sub.configure(text="Setup is complete. You can change both folders later in Settings.")
            summary = []
            if self.temp_path.get():
                summary.append(tr("Temp folder:    {v}", v=self.temp_path.get()))
            if self.output_path.get():
                summary.append(tr("Output folder:  {v}", v=self.output_path.get()))
            summary.append("")
            summary.append("")
            summary.append(tr("Next: drop a game folder, an archive or a .pkg onto the window, or press Add job "
                              "({add}).", add=SHORTCUT["add"]))
            for line in summary:
                ctk.CTkLabel(self.body, text=line, text_color=WHITE, anchor="w",
                              font=ctk.CTkFont(family=MONO_FONT, size=11)).pack(anchor="w", padx=14, pady=2)
        self._bottom_pad = ctk.CTkFrame(self.body, height=14, fg_color=PANEL)
        self._bottom_pad.pack(fill="x")

    def _probe_temp_drive_type(self, tpath: Path, label, tfree: int) -> None:
        """worker thread, get_drive_type may block; the result is dropped once the step is gone"""
        def _apply(dt: str) -> None:
            try:
                if not (self.winfo_exists() and label.winfo_exists()):
                    return
                label.configure(text=tr("Temp drive:    {tfree} free  ·  type: {dt}", tfree=format_size(tfree), dt=dt))
                if dt == "HDD":
                    ctk.CTkLabel(self.body,
                                 text="  Temp folder is on a mechanical HDD.\n  Large games may "
                                      "process significantly slower.\n  SSD/NVMe recommended.",
                                 text_color=YELLOW, anchor="w",
                                 font=ctk.CTkFont(family=MONO_FONT, size=11),
                                 justify="left").pack(anchor="w", padx=14, pady=3, after=label)
            except Exception:
                pass

        def _detect() -> None:
            dt = get_drive_type(tpath)
            try:
                self.after(0, lambda: _apply(dt))
            except Exception:
                pass

        threading.Thread(target=_detect, daemon=True).start()

    def _browse_temp(self):
        p = filedialog.askdirectory(title="Choose the temp folder")
        if p:
            self.temp_path.set(str(Path(p) / "_ffpfsc_temp"))

    def _browse_output(self):
        p = filedialog.askdirectory(title="Choose the output folder")
        if p:
            self.output_path.set(p)

    def _back(self):
        if self.step > 0:
            self._show_step(self.step - 1)

    def _gate_next(self, *_):
        """Next waits for a folder on the two folder steps."""
        need = {0: self.temp_path, 1: self.output_path}.get(getattr(self, "step", 0))
        ok = need is None or bool((need.get() or "").strip())
        try:
            self.next_btn.configure(state="normal" if ok else "disabled")
        except tk.TclError:
            pass

    def _next(self):
        need = {0: self.temp_path, 1: self.output_path}.get(self.step)
        if need is not None and not (need.get() or "").strip():
            return
        if self.step < 3:
            self._show_step(self.step + 1)
        else:
            self.result = {
                "temp_folder": self.temp_path.get(),
                "output_folder": self.output_path.get(),
                "first_run_done": True,
            }
            save_settings(self.result)
            self.destroy()


# error dialog

class ErrorDialog(MessageWindow):
    _TITLES = {
        "pack":         "Pack failed",
        "copy":         "Copy failed",
        "unpack":       "Extraction failed",
        "fpkg-extract": "fPKG extraction failed",
        "fpkg-build":   "fPKG build failed",
        "fake-sign":    "Fake-signing failed",
        "ps4-keystone": "PS4 PKG Tools job failed",
        "chain":        "Job failed",
    }

    def __init__(self, parent, msg: str, last_cmd: str = "", log_lines: str = "",
                 operation: str = "pack", on_edit=None, on_retry=None):
        super().__init__(parent)
        self._on_edit, self._on_retry = on_edit, on_retry
        self._op = operation or "pack"
        self._heading = self._TITLES.get(self._op, "Job failed")
        self.title(self._heading)
        self.geometry("660x470")
        self.minsize(520, 380)
        self.resizable(True, True)
        self.grab_set()
        self.configure(fg_color=BLACK)
        self._msg = (msg or "").strip() or "The job stopped before it finished."
        self._cmd = last_cmd
        self._log = log_lines or ""
        self._build()
        for seq in ("<Return>", "<KP_Enter>"):
            self.bind(seq, lambda e: self.destroy())

    @staticmethod
    def _diagnose_windows(msg: str, t: str) -> str | None:
        import re as _re
        m = _re.search(r"['\"]([a-z]:)\\", msg, _re.I)
        drive = m.group(1).upper() if m else "the drive"
        if any(f"[winerror {n}]" in t for n in (21, 433, 1117, 1167)):
            return (tr("Windows lost {drive} during the job: it was disconnected, went to sleep or failed to "
                      "respond. Check the cable, wake the drive and rerun the job.", drive=drive))
        if "[winerror 206]" in t:
            return ("A path got longer than Windows allows (260 characters). Pick a shorter temp or output "
                    "folder, or have an administrator turn on long paths in Windows.")
        if "[winerror 112]" in t or "there is not enough space on the disk" in t:
            return ("The drive ran out of free space while writing. Free space, move the output or "
                    "the temp folder to a larger drive, or lower the compression level.")
        if "[winerror 32]" in t:
            return ("Another program had a file open (an antivirus scan, Explorer's preview, a player). "
                    "Close it or wait a moment, then retry the job.")
        if "[winerror 5]" in t or "permission denied" in t:
            m = _re.search(r"(?:\[winerror 5\]|permission denied)[^\n]*?:\s*['\"]([^'\"\n]+)", msg, _re.I)
            path = m.group(1).strip().replace("\\\\", "\\") if m else ""     # repr() doubles them
            return ("Windows refused access" + (f" to “{path}”" if path else "") +
                    ". The file is read-only or open in another program, or the folder needs administrator "
                    "rights. Choose a writable folder or close the other program.")
        return None

    @staticmethod
    def _diagnose(msg: str, log: str) -> str | None:
        t = (msg + "\n" + log).lower()
        # a USB drive that wanders off mid-run leaves EACCES on its mount point
        import re as _re
        if sys.platform == "win32":
            win = ErrorDialog._diagnose_windows(msg, t)
            if win:
                return win
        m = _re.search(r"permission denied[^\n]*['\"](/volumes/[^'\"]+)", msg, _re.I)
        if m:
            vol = m.group(1)
            return (tr("macOS refuses to read or write “{vol}”. The drive most likely disconnected or went to "
                      "sleep during the job. Check the cable, wake the drive and rerun the job.", vol=vol))
        if "permission denied" in t:
            m = _re.search(r"permission denied[^\n]*(?::|')\s*['\"]?([^\"'\n]+)", msg, _re.I)
            path = m.group(1).strip() if m else ""
            return ("The system refused write access" + (f" to “{path}”" if path else "") +
                    ". The folder is read-only, on a locked disk, or restricted by macOS privacy "
                    "settings. Grant access in System Settings → Privacy & Security, or choose a "
                    "writable folder.")
        if "no space left on device" in t or "enospc" in t:
            return ("The drive ran out of free space while writing. Free space, move the output or "
                    "the temp folder to a larger drive, or lower the compression level.")
        if "read-only file system" in t or "erofs" in t:
            return "The target folder is on a read-only volume. Choose a writable folder."
        if "wrong or missing password" in t or "rarwrongpassword" in t:
            return "The archive's password is wrong or missing. Set it in Settings › Archives."
        if "extraction failed" in t and ("is damaged" in t or "of the set is missing" in t
                                         or "of the set are missing" in t or "cannot be opened" in t):
            return ("The archive itself is broken: a part is damaged or missing. A password does not "
                    "help here. Download the part the message names (or the whole set) again and retry.")
        if "operation cancelled" in t or "cancelled by user" in t:
            return "You cancelled this job. Nothing was written to the output."
        if "sigkill" in t or "killed" in t or "-9" in t:
            if sys.platform == "win32":
                return ("The backend was stopped by the system, usually for lack of memory or by an "
                        "antivirus. Try a lower CPU count in Settings.")
            return ("The backend was killed by the system. On macOS that is usually memory pressure "
                    "or a Gatekeeper block - try a lower CPU count in Settings, or run the app once "
                    "from a Finder open.")
        if "no eboot" in t or "not a ps5 game" in t:
            return "The source has no eboot.bin - it is not a PS5 game folder."
        if "function(s) that" in t and "lacks" in t:
            return ("The game calls functions that this firmware does not have, so lowering the SDK alone "
                    "would not start it. Edit job lets you pick another backport target (Check tells you "
                    "which one works), or put patched libraries for this target into the folder set in "
                    "Settings › Backport & libraries and Retry.")
        return None

    def _build(self):
        ctk.CTkLabel(self, text=self._heading, font=ctk.CTkFont(size=15, weight="bold"),
                      text_color=RED).pack(anchor="w", padx=20, pady=(16, 2))
        # buttons pack first so a short window never hides them
        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(side="bottom", fill="x", padx=20, pady=(0, 14))
        ctk.CTkButton(btns, text="Close", width=100, fg_color=ACCENT, text_color=ON_ACCENT,
                       hover_color=ACCENT_HOVER, command=self.destroy).pack(side="right")
        ctk.CTkButton(btns, text="Open log folder", width=132, fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER, command=self._open_folder, border_width=1, border_color=BTN_BORDER).pack(side="right", padx=(0, 8))
        ctk.CTkButton(btns, text="Copy details", width=110, fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER, command=self._copy, border_width=1, border_color=BTN_BORDER).pack(side="right", padx=(0, 8))
        for text, cb, w in (("Edit job", self._on_edit, 90), ("Retry", self._on_retry, 76)):
            if cb is not None:
                ctk.CTkButton(btns, text=text, width=w, fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                              border_width=1, border_color=BTN_BORDER,
                              command=lambda cb=cb: (self.destroy(), cb())).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(self, text=self._msg, text_color=WHITE, wraplength=560, justify="left",
                      font=ctk.CTkFont(size=13)).pack(anchor="w", padx=20, pady=(0, 8))

        note = self._diagnose(self._msg, self._log)
        if note:
            box = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=8)
            box.pack(fill="x", padx=20, pady=(0, 10))
            ctk.CTkLabel(box, text="What this means", text_color=YELLOW,
                          font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=14, pady=(10, 2))
            ctk.CTkLabel(box, text=note, text_color=WHITE, wraplength=530, justify="left",
                          font=ctk.CTkFont(size=12)).pack(anchor="w", padx=14, pady=(0, 10))

        ctk.CTkLabel(self, text="Backend log (last lines)", text_color=MUTED,
                      font=ctk.CTkFont(size=12)).pack(anchor="w", padx=20, pady=(0, 2))
        log = ctk.CTkTextbox(self, fg_color=PANEL, text_color=ctk_pair("log"),
                              border_width=1, border_color=BORDER,
                              font=ctk.CTkFont(family=MONO_FONT, size=11), wrap="word")
        log.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        log.insert("end", self._tail(self._log) or "(no log available)")
        log.configure(state="disabled")


    @staticmethod
    def _tail(text: str, n: int = 20) -> str:
        """last n non-empty lines, minus the header row and the [LAST 10 MB] banner"""
        if not text:
            return ""
        lines = [ln for ln in text.splitlines()
                 if ln.strip() and not ln.startswith(("[LAST 10 MB", "[COMMAND] "))]
        return "\n".join(lines[-n:])

    def _copy(self):
        text = f"Error: {self._msg}\n\nLast Command: {self._cmd}\n\nLog:\n{self._log}"
        self.clipboard_clear()
        self.clipboard_append(text)

    def _export_log(self):
        ensure_app_dir()
        if RAW_LOG_FILE.exists():
            open_path(RAW_LOG_FILE)

    def _open_folder(self):
        ensure_app_dir()
        open_path(APP_DIR)


# summary dialog

class SummaryDialog(MessageWindow):
    def __init__(self, parent, report: str, on_show=None):
        super().__init__(parent)
        self.title("Job complete")
        self.configure(fg_color=BLACK)
        self.resizable(True, True)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text="Job complete",
                      text_color=WHITE, font=ctk.CTkFont(size=17, weight="bold"),
                      anchor="w").grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 6))

        # window height follows the report (4 to 24 lines), then the box scrolls
        mono = ctk.CTkFont(family=MONO_FONT, size=11)
        per_row = max(40, 490 // max(1, mono.measure("0")))
        rows = sum(max(1, -(-len(ln) // per_row)) for ln in (report.splitlines() or [""]))
        box = ctk.CTkTextbox(self, fg_color=CARD, border_width=1, border_color=BORDER2,
                              text_color=WHITE, font=mono, wrap="word", width=508,
                              height=min(24, max(4, rows)) * mono.metrics("linespace") + 14)
        box.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        box.insert("1.0", report)
        box.configure(state="disabled")

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))
        btn_row.grid_columnconfigure(0, weight=1)
        btn_row.grid_columnconfigure(1, weight=1)
        btn_row.grid_columnconfigure(2, weight=1)
        if on_show is not None:
            ctk.CTkButton(btn_row, text=REVEAL_LABEL, command=on_show, fg_color=BTN, hover_color=BTN_HOVER,
                          text_color=WHITE, border_width=1, border_color=BTN_BORDER).grid(
                row=0, column=1, sticky="ew", padx=(0, 6))

        def _copy():
            self.clipboard_clear()
            self.clipboard_append(report)
            copy_btn.configure(text="Copied")
            self.after(2000, lambda: copy_btn.winfo_exists() and copy_btn.configure(text="Copy result"))

        copy_btn = ctk.CTkButton(btn_row, text="Copy result", command=_copy,
                                  fg_color=BTN, hover_color=BTN_HOVER,
                                  text_color=WHITE, border_width=1, border_color=BTN_BORDER)
        copy_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(btn_row, text="Close", command=self.destroy,
                       fg_color=ACCENT, hover_color=ACCENT_HOVER,
                       text_color=ON_ACCENT).grid(row=0, column=2, sticky="ew")
        for seq in ("<Return>", "<KP_Enter>"):
            self.bind(seq, lambda e: self.destroy())


class QueueReportWindow(MessageWindow):
    def __init__(self, parent, done: int, total: int, left: list, retry=None):
        super().__init__(parent)
        self.resizable(True, False)
        failed = [it for it in left if getattr(it, "status", "") == "Failed"]
        head = tr("Queue finished") if not left else tr("Queue finished with problems")
        self.title(head)
        ctk.CTkLabel(self, text=head, font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=SUCCESS if not left else YELLOW).pack(anchor="w", padx=20, pady=(16, 4))
        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(side="bottom", fill="x", padx=20, pady=(4, 14))
        ctk.CTkButton(btns, text="Close", width=100, fg_color=ACCENT, text_color=ON_ACCENT,
                      hover_color=ACCENT_HOVER, command=self.destroy).pack(side="right")
        if failed and retry is not None:
            ctk.CTkButton(btns, text="Retry failed", width=110, fg_color=BTN, hover_color=BTN_HOVER,
                          text_color=WHITE, border_width=1, border_color=BTN_BORDER,
                          command=lambda: (self.destroy(), retry())).pack(side="right", padx=(0, 8))
        ctk.CTkLabel(self, text=plural(total, "{done} of 1 job done.", "{done} of {n} jobs done.", done=done),
                     text_color=WHITE, font=ctk.CTkFont(size=13)).pack(anchor="w", padx=20, pady=(0, 8))
        status_text = {"Failed": tr("failed"), "Skipped": tr("skipped"), "Cancelled": tr("stopped")}
        lines = []
        for it in left[:12]:
            name = getattr(it, "display_name", None) or it.name
            note = " ".join(str(getattr(it, "status_note", "") or "").split())
            lines.append(f"•  {name}: {status_text.get(it.status, it.status)}" + (f". {note}" if note else ""))
        if len(left) > 12:
            lines.append(plural(len(left) - 12, "… and 1 more", "… and {n} more"))
        if lines:
            ctk.CTkLabel(self, text="\n".join(lines), text_color=MUTED, wraplength=500, justify="left",
                         font=ctk.CTkFont(size=12)).pack(anchor="w", padx=20, pady=(0, 10))
        for seq in ("<Return>", "<KP_Enter>"):
            self.bind(seq, lambda e: self.destroy())
        self.update_idletasks()
        self.geometry(f"560x{max(150, self.winfo_reqheight())}")


class SpaceSkipWindow(MessageWindow):
    _HINTS = {"output": "Free up space there, or pick another output with Edit, then Retry.",
              "temp": "Free up space there, or add a temp drive in Settings, then Retry."}

    def __init__(self, parent):
        super().__init__(parent)
        self.configure(fg_color=BLACK)
        self.resizable(True, False)
        self._lines, self._kinds = [], []
        self._head = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=15, weight="bold"), text_color=YELLOW)
        self._head.pack(anchor="w", padx=20, pady=(16, 6))
        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(side="bottom", fill="x", padx=20, pady=(0, 14))
        ctk.CTkButton(btns, text="Close", width=100, fg_color=ACCENT, text_color=ON_ACCENT,
                      hover_color=ACCENT_HOVER, command=self.destroy).pack(side="right")
        self._body = ctk.CTkLabel(self, text="", text_color=WHITE, wraplength=500, justify="left",
                                  font=ctk.CTkFont(size=13))
        self._body.pack(anchor="w", padx=20, pady=(0, 8))
        self._hint = ctk.CTkLabel(self, text="", text_color=MUTED, wraplength=500, justify="left",
                                  font=ctk.CTkFont(size=12))
        self._hint.pack(anchor="w", padx=20, pady=(0, 12))
        for seq in ("<Return>", "<KP_Enter>"):
            self.bind(seq, lambda e: self.destroy())

    def add(self, line: str, kind: str) -> None:
        self._lines.append(line)
        self._kinds.append(kind)
        kinds = set(self._kinds)
        head = ("Output drive full" if kinds == {"output"} else
                "Temp drive full" if kinds == {"temp"} else "Not enough space")
        self.title(head)
        self._head.configure(text=head)
        self._body.configure(text="\n\n".join(self._lines))
        self._hint.configure(text=" ".join(self._HINTS[k] for k in ("output", "temp") if k in kinds))
        self.update_idletasks()
        self.geometry(f"540x{max(180, self.winfo_reqheight())}")

    def text(self) -> str:
        return "\n\n".join(self._lines)


# space check dialog

class SpaceDiagnosticsDialog(MessageWindow):
    """opens at once; the drive type is probed in the background"""

    def __init__(self, parent, item, temp_dir: Path, out_dir: Path):
        super().__init__(parent)
        self.title("Drive space check")
        self.geometry("520x500")
        self.resizable(False, False)
        self.configure(fg_color=BLACK)
        self.proceed = False
        self.choice = "cancel"          # proceed | skip | cancel
        self._auto_timer = None
        self._countdown  = 0
        self._proceed_btn = None
        self._skip_btn = None
        self._low = not _space_preflight_ok(item, temp_dir, out_dir)
        self._build(item, temp_dir, out_dir)
        self.geometry(f"520x{300 + 28 * getattr(self, '_row_count', 8)}")
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        for seq in ("<Return>", "<KP_Enter>"):
            self.bind(seq, lambda e: self._skip() if self._low else self._ok())
        self.after(50, self.grab_set)
        # 4 s to start with room, a minute to skip without, so an overnight queue goes on
        self._countdown = 60 if self._low else 4
        self.after(1000, self._tick_countdown)

    def _build(self, item, temp_dir: Path, out_dir: Path):
        ctk.CTkLabel(self, text="Drive space check",
                      font=ctk.CTkFont(size=17, weight="bold"),
                      text_color=WHITE).pack(anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(self, text="Pre-flight check before compression starts.",
                      text_color=MUTED).pack(anchor="w", padx=20, pady=(0, 10))

        panel = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        panel.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        # same numbers the pre-flight gate decides on
        temp_fs = get_filesystem_type(temp_dir)   # fast ctypes call
        out_fs  = get_filesystem_type(out_dir)

        def _color(status):
            if status == "ok":   return SUCCESS
            if status == "warn": return YELLOW
            return WHITE

        static_rows, space_ok, result_text = _space_report(item, temp_dir, out_dir, temp_fs, out_fs)

        self._row_count = len(static_rows)
        for lbl, val, st in static_rows:
            row = ctk.CTkFrame(panel, fg_color=PANEL)
            row.pack(fill="x", padx=14, pady=2)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text=lbl + ":", text_color=MUTED,
                          anchor="w", width=200).grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(row, text=val, text_color=_color(st),
                          anchor="e").grid(row=0, column=1, sticky="e")

        dt_row = ctk.CTkFrame(panel, fg_color=PANEL)
        dt_row.pack(fill="x", padx=14, pady=2)
        dt_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dt_row, text="Temp drive type:", text_color=MUTED,
                      anchor="w", width=200).grid(row=0, column=0, sticky="w")
        self._dt_label = ctk.CTkLabel(dt_row, text="Detecting…", text_color=MUTED,
                                       anchor="e")
        self._dt_label.grid(row=0, column=1, sticky="e")

        result_color = SUCCESS if space_ok else YELLOW
        ctk.CTkLabel(panel, text=result_text, text_color=result_color,
                      font=ctk.CTkFont(size=13, weight="bold")
                     ).pack(anchor="w", padx=14, pady=(8, 2))

        if temp_fs in ("exFAT", "FAT32", "FAT"):
            ctk.CTkLabel(panel,
                          text=tr("Temp drive is {temp_fs} - no hardlink support. Slower copy mode will be used.", temp_fs=temp_fs),
                          text_color=YELLOW, justify="left", wraplength=460
                         ).pack(anchor="w", padx=14, pady=(0, 2))
        if out_fs in ("exFAT", "FAT32", "FAT"):
            ctk.CTkLabel(panel,
                          text=tr("Output drive is {out_fs}. NTFS recommended.", out_fs=out_fs),
                          text_color=YELLOW, justify="left", wraplength=460
                         ).pack(anchor="w", padx=14, pady=(0, 2))

        self._hdd_warn = ctk.CTkLabel(panel,
                                       text="Temp folder is on a mechanical HDD - will be significantly slower.",
                                       text_color=YELLOW, justify="left", wraplength=460)
        # packed only when the probe says HDD

        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(fill="x", padx=20, pady=(0, 16))

        if self._low:
            self._skip_btn = ctk.CTkButton(btns, text="Skip this job", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                           text_color=ON_ACCENT, font=ctk.CTkFont(size=13), height=38,
                                           command=self._skip)
            self._skip_btn.pack(side="right", padx=(8, 0))
            self._proceed_btn = ctk.CTkButton(btns, text="Start anyway", fg_color=BTN, hover_color=BTN_HOVER,
                                              text_color=WHITE, height=38, border_width=1,
                                              border_color=BTN_BORDER, command=self._ok)
        else:
            self._proceed_btn = ctk.CTkButton(btns, text="Start now",
                           fg_color=ACCENT, hover_color=ACCENT_HOVER,
                           text_color=ON_ACCENT,
                           font=ctk.CTkFont(size=13),
                           height=38,
                           command=self._ok
                          )
        self._proceed_btn.pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Stop the queue",
                       fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER,
                       command=self._cancel, border_width=1, border_color=BTN_BORDER
                      ).pack(side="right")

        def _detect():
            dt = get_drive_type(temp_dir)   # may block up to 6 s
            try:
                self.after(0, lambda: self._apply_drive_type(dt))
            except Exception:
                pass

        threading.Thread(target=_detect, daemon=True).start()

    def _apply_drive_type(self, dt: str):
        """Called on the main thread when background detection finishes."""
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if dt == "SSD":
            self._dt_label.configure(text="SSD / NVMe", text_color=SUCCESS)
        elif dt == "HDD":
            self._dt_label.configure(text="HDD", text_color=YELLOW)
            self._hdd_warn.pack(anchor="w", padx=14, pady=(0, 2))
        else:
            self._dt_label.configure(text="Unknown", text_color=MUTED)

    def _tick_countdown(self):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self._countdown > 0:
            if self._low and self._skip_btn:
                self._skip_btn.configure(text=tr("Skip this job  (auto in {countdown} s)", countdown=self._countdown))
            elif self._proceed_btn:
                self._proceed_btn.configure(
                    text=tr("Start now  (auto in {countdown} s)", countdown=self._countdown))
            self._countdown -= 1
            self._auto_timer = self.after(1000, self._tick_countdown)
        else:
            self._skip() if self._low else self._ok()

    def _close(self, choice: str):
        if self._auto_timer:
            try:
                self.after_cancel(self._auto_timer)
            except Exception:
                pass
        self.choice = choice
        self.proceed = choice == "proceed"
        self.destroy()

    def _ok(self):
        self._close("proceed")

    def _skip(self):
        self._close("skip")

    def _cancel(self):
        self._close("cancel")


# diagnostic export

def _redact_settings_for_export(data):
    """drops the password list; other password keys become '<redacted>' if they were set"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if isinstance(k, str) and "password" in k.lower():
                if k == "archive_passwords" or isinstance(v, (list, tuple, dict)):
                    continue
                out[k] = "<redacted>" if v else ""
            else:
                out[k] = _redact_settings_for_export(v)
        return out
    if isinstance(data, list):
        return [_redact_settings_for_export(v) for v in data]
    return data


def export_diagnostic_zip(last_cmd: str = "", extra_info: str = "") -> Path | None:
    ensure_app_dir()
    zip_path = APP_DIR / f"diagnostic_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if RAW_LOG_FILE.exists():
                zf.write(RAW_LOG_FILE, "raw.log")
            if SETTINGS_FILE.exists():
                # Never the raw file: it holds the saved archive passwords in plain text.
                try:
                    redacted = _redact_settings_for_export(
                        json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
                except Exception as e:
                    redacted = {"_note": f"settings.json could not be parsed for redaction: {e}"}
                zf.writestr("settings.json", json.dumps(redacted, indent=2))
            if FINAL_REPORT_FILE.exists():
                zf.write(FINAL_REPORT_FILE, "last_result_report.txt")
            def _drive_info(p: str) -> str:
                if not p:
                    return " - "
                try:
                    pp = Path(p)
                    # cached probe only: this is the UI thread and get_drive_type() can take seconds
                    return (f"{get_filesystem_type(pp)} | "
                            f"{drive_type_cached(pp)} | "
                            f"Free: {format_size(get_free_space(pp))}")
                except Exception:
                    return " - "

            temp_p  = ""
            out_p   = ""
            try:
                s = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
                temp_p = s.get("temp_folder", "")
                out_p  = s.get("output_folder", "")
            except Exception:
                pass

            session_info = "\n".join([
                f"{APP_NAME} {APP_VERSION}{APP_EDITION}",
                f"Generated:      {now_datetime()}",
                f"Python:         {sys.version}",
                f"OS:             {sys.platform} {os.name}",
                "",
                f"Last Command:   {last_cmd}",
                "",
                f"Temp Folder:    {temp_p or ' - '}",
                f"Temp Drive:     {_drive_info(temp_p)}",
                f"Output Folder:  {out_p or ' - '}",
                f"Output Drive:   {_drive_info(out_p)}",
                "",
                extra_info,
            ])
            zf.writestr("session_info.txt", session_info)
        return zip_path
    except Exception:
        return None


# settings

class SettingsView:
    """pages are built on first show; every change applies at once"""

    PAGES = [
        ("general", "General", "settings",
         "Where new jobs write, and how the app tells you it is done. Changes apply immediately."),
        ("compression", "Compression", "package", "How games are packed, and what a new job starts with."),
        ("archives", "Archives", "key", "Passwords for protected archives."),
        ("drives", "Drives & space", "drive",
         "Where archives are unpacked, how much free space a job needs, and how drives are kept awake."),
        ("backport", "Backport & libraries", "layers",
         "Libraries for backports and PlayGo titles" + (", and the optional fPKG backend" if sys.platform == "win32" else "")
         + ". You supply all of them; none ship with the app."),
        ("about", "About", "info", ""),
    ]

    def __init__(self, parent, app):
        self.app = app
        self._page_frames = {}
        kit = app.kit
        self.frame = kit.frame(parent)
        self.frame.grid_columnconfigure(2, weight=1)
        self.frame.grid_rowconfigure(0, weight=1)
        nav = kit.frame(self.frame, bg="inspector", width=208)
        nav.grid(row=0, column=0, sticky="nsw")
        nav.pack_propagate(False)
        kit.label(nav, bg="inspector", text="Settings", font=kit.fonts.view).pack(anchor="w", padx=18, pady=(18, 12))
        self._nav = {}
        for key, title, icon, _sub in self.PAGES:
            b = IconButton(nav, kit, text=title, icon=icon, command=lambda k=key: self.show(k), variant="nav",
                           height=28, bg="inspector", icon_size=15, padx=10)
            b.pack(fill="x", padx=8, pady=1)
            self._nav[key] = b
        kit.fit_width(nav, 208, *(b.natural_width() + 16 for b in self._nav.values()),
                      kit.fonts.view.measure("Settings") + 36)
        kit.rule(self.frame, bg_token="border_strong", horizontal=False).grid(row=0, column=1, sticky="ns")
        self._body = kit.frame(self.frame)
        self._body.grid(row=0, column=2, sticky="nsew")
        self.show("general")

    def show(self, key):
        for f in self._page_frames.values():
            f.pack_forget()
        f = self._page_frames.get(key)
        if f is None:
            _k, title, _icon, sub = next(pg for pg in self.PAGES if pg[0] == key)
            f = ScrollFrame(self._body, fg_color=BLACK, corner_radius=0)
            ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=15, weight="bold"), text_color=WHITE).pack(
                anchor="w", padx=4, pady=(22, 2))
            if sub:
                ctk.CTkLabel(f, text=sub, text_color=MUTED, font=ctk.CTkFont(size=13), wraplength=620,
                             justify="left").pack(anchor="w", padx=4, pady=(0, 12))
            getattr(self, "_page_" + key)(f)
            self._page_frames[key] = f
        f.pack(fill="both", expand=True, padx=(24, 12))
        for k, b in self._nav.items():
            b.configure(selected=(k == key))

    def _page_general(self, scroll):
        self._section_label(scroll, "Folders")
        fold = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        fold.pack(fill="x", pady=(4, 12))
        fold.grid_columnconfigure(1, weight=1)
        for row_i, (lbl, var, key, title) in enumerate([
            ("Default output folder", self.app.output_var, "output_folder", "Choose the default output folder"),
            ("Default temp folder",   self.app.temp_var,   "temp_folder",   "Choose the default temp folder"),
        ]):
            ctk.CTkLabel(fold, text=tr(lbl) + ":", text_color=MUTED, anchor="w", width=170).grid(
                row=row_i, column=0, padx=14, pady=8, sticky="w")
            ctk.CTkEntry(fold, textvariable=var, fg_color=CARD, border_color=BORDER2,
                          text_color=WHITE).grid(row=row_i, column=1, sticky="ew", padx=(0, 8), pady=8)
            ctk.CTkButton(fold, text="Folder…", width=80, fg_color=BTN, text_color=WHITE,
                           hover_color=BTN_HOVER,
                           command=lambda v=var, k=key, t=title: self._browse_folder(v, k, t), border_width=1, border_color=BTN_BORDER).grid(
                row=row_i, column=2, padx=(0, 14), pady=8)
        _ct = ctk.CTkFrame(fold, fg_color="transparent")
        _ct.grid(row=2, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 10))
        ctk.CTkLabel(_ct, text="Leftovers from cancelled or failed jobs stay in the temp folder until cleaned.",
                     text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left")
        ctk.CTkButton(_ct, text="Clean temp now", width=120, fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                      border_width=1, border_color=BTN_BORDER, command=self.app.clear_temp_files).pack(side="right")
        ctk.CTkCheckBox(fold, text="Clean the temp folder after every job that succeeds", variable=self.app.auto_clear_temp_var,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18,
                        checkbox_height=18).grid(row=3, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 10))

        aj = _after_job_module()
        def seg_row(parent, label, var, keys, labels, command=None, pady=(4, 4)):
            row = ctk.CTkFrame(parent, fg_color=PANEL)
            row.pack(fill="x", padx=14, pady=pady)
            ctk.CTkLabel(row, text=label, text_color=WHITE, width=70, anchor="w").pack(side="left", padx=(0, 12))
            seg = ctk.CTkSegmentedButton(row, values=[labels[k] for k in keys],
                                         command=command or (lambda v: var.set(
                                             next(k for k in keys if labels[k] == v))))
            seg.set(labels.get(var.get(), labels[keys[0]]))
            seg.pack(side="left")
            return seg

        self._section_label(scroll, "When a job is done")
        jd = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        jd.pack(fill="x", pady=(4, 12))
        self._after_seg = seg_row(jd, "Source", self.app.after_source_var, aj.ACTIONS, aj.LABELS,
                                  command=self._on_after_source, pady=(10, 2))
        self._after_extra = ctk.CTkFrame(jd, fg_color=PANEL, height=1)
        self._after_extra.pack(fill="x", padx=14)
        self._after_dir_row = ctk.CTkFrame(self._after_extra, fg_color=PANEL)
        ctk.CTkLabel(self._after_dir_row, text="Folder", text_color=MUTED, width=70, anchor="w").pack(
            side="left", padx=(0, 12))
        ctk.CTkEntry(self._after_dir_row, textvariable=self.app.after_move_dir_var, fg_color=CARD,
                     border_color=BORDER2, text_color=WHITE).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(self._after_dir_row, text="Folder…", width=80, fg_color=BTN, text_color=WHITE,
                      hover_color=BTN_HOVER, border_width=1, border_color=BTN_BORDER,
                      command=lambda: self._browse_folder(self.app.after_move_dir_var, "after_move_dir",
                                                          "Choose the folder finished sources move to")).pack(side="left")
        self._after_note = ctk.CTkLabel(self._after_extra, text=tr("Deleted for good; it does not go to the {TRASH_NAME}.", TRASH_NAME=TRASH_NAME),
                                        text_color=RED, font=ctk.CTkFont(size=12), anchor="w")
        ctk.CTkLabel(jd, text="The game folder, the archive with all its parts, or the container, once the "
                              "job is Done. New jobs start with this; each job keeps its own choice "
                              "(Add job › Output).",
                     text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=600, justify="left").pack(
            anchor="w", padx=14 + 82, pady=(2, 6))
        self._refresh_after_rows()
        seg_row(jd, "Notify", self.app.notify_var, aj.NOTIFY, aj.NOTIFY_LABELS, pady=(4, 6))
        for text, var in [
            ("Show the result", self.app.summary_popup_var),
            ("Remove the job from the queue (failed jobs stay)", self.app.auto_remove_done_var),
            ("Open the output folder", self.app.open_output_var),
            ("Play a sound when it is done", self.app.sound_complete_var),
            ("Play a sound when it fails", self.app.sound_error_var),
        ]:
            ctk.CTkCheckBox(jd, text=text, variable=var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                            text_color=WHITE, checkbox_width=18, checkbox_height=18).pack(anchor="w", padx=14, pady=5)
        ctk.CTkCheckBox(jd, text="Bring a game's add-ons along", variable=self.app.copy_siblings_var,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18,
                        checkbox_height=18,
                        command=lambda: save_settings({"copy_bundle_siblings": self.app.copy_siblings_var.get()})).pack(
            anchor="w", padx=14, pady=(5, 2))
        ctk.CTkLabel(jd, text="When a download folder holds one game and its add-ons, the DLC and update packages "
                              "go into the game's title folder in the library (archives are unpacked first) and "
                              "the other files are copied next to the output. Off: they stay in the download "
                              "folder.",
                     text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=600, justify="left").pack(
            anchor="w", padx=14 + 28, pady=(0, 6))
        ctk.CTkFrame(jd, fg_color=PANEL, height=6).pack()

        self._section_label(scroll, "When the queue is done")
        qd = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        qd.pack(fill="x", pady=(4, 12))
        seg_row(qd, "Then", self.app.after_queue_var, aj.QUEUE_ACTIONS, aj.QUEUE_LABELS, pady=(10, 2))
        ctk.CTkLabel(qd, text="Only after the last job of a run, not after Stop. Sleep and Quit wait 30 seconds "
                              "in a small window you can cancel.",
                     text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=600, justify="left").pack(
            anchor="w", padx=14 + 82, pady=(2, 10))

        self._section_label(scroll, "Interface")
        ui = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        ui.pack(fill="x", pady=(4, 12))
        oe_row = ctk.CTkFrame(ui, fg_color=PANEL)
        oe_row.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(oe_row, text="When the output is already there", text_color=WHITE).pack(side="left", padx=(0, 12))
        _labels = self.app.OUTPUT_EXISTS_LABELS
        _oe = ctk.CTkSegmentedButton(oe_row, values=[_labels[k] for k in self.app.OUTPUT_EXISTS_CHOICES],
                                     command=lambda v: self.app.output_exists_var.set(
                                         next(k for k, lbl in _labels.items() if lbl == v)))
        _oe.set(_labels.get(self.app.output_exists_var.get(), "Skip"))
        _oe.pack(side="left")
        theme_row = ctk.CTkFrame(ui, fg_color=PANEL)
        theme_row.pack(fill="x", padx=14, pady=(4, 10))
        ctk.CTkLabel(theme_row, text="Appearance", text_color=WHITE).pack(side="left", padx=(0, 12))
        _mode = ctk.CTkSegmentedButton(theme_row, values=["Dark", "Light"],
                                       command=lambda v: self.app._set_theme(v.lower()))
        _mode.set("Light" if self.app._theme == "light" else "Dark")
        _mode.pack(side="left")
        lang_row = ctk.CTkFrame(ui, fg_color=PANEL)
        lang_row.pack(fill="x", padx=14, pady=(0, 10))
        ctk.CTkLabel(lang_row, text="Language", text_color=WHITE).pack(side="left", padx=(0, 12))
        _names = dict(i18n.LANGUAGES)
        _saved = str((load_settings() or {}).get("language", i18n.language()))
        _lang_note = tk.StringVar(value="")

        def _pick_language(name):
            key = next(k for k, v in i18n.LANGUAGES if v == name)
            save_settings({"language": key})
            _lang_note.set(tr("Takes effect after a restart.") if key != i18n.language() else "")
        _lang = ctk.CTkSegmentedButton(lang_row, values=list(_names.values()), command=_pick_language)
        _lang.set(_names.get(_saved, "English"))
        _lang.pack(side="left")
        ctk.CTkLabel(lang_row, textvariable=_lang_note, text_color=MUTED, font=ctk.CTkFont(size=12)).pack(
            side="left", padx=(12, 0))
        if _saved != i18n.language():
            _lang_note.set(tr("Takes effect after a restart."))

        self._section_label(scroll, "Game names")
        gn = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        gn.pack(fill="x", pady=(4, 12))
        ctk.CTkCheckBox(gn, text="Look up a game's name by its title id", variable=self.app.online_names_var,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18,
                        checkbox_height=18).pack(anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(gn, text="For an archive whose game cannot be read before it is unpacked (an image or "
                              "a solid 7z or RAR). Uses the public PlayStation title lists "
                              "(github.com/andshrew/PlayStation-Titles), downloaded when first needed and "
                              "again once a week. The unpacked game's own name replaces it.",
                     text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=600, justify="left").pack(
            anchor="w", padx=14 + 28, pady=(0, 10))


    def _advanced(self, scroll, title: str = "Advanced"):
        head = ctk.CTkFrame(scroll, fg_color="transparent")
        head.pack(fill="x", pady=(6, 0))
        box = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        label = tk.StringVar(value="▸  " + tr(title))

        def toggle():
            if box.winfo_manager():
                box.pack_forget()
                label.set("▸  " + tr(title))
            else:
                box.pack(fill="x", pady=(4, 12), after=head)
                label.set("▾  " + tr(title))
        ctk.CTkButton(head, textvariable=label, fg_color="transparent", hover_color=CARD2, text_color=WHITE,
                      anchor="w", width=160, height=26, font=ctk.CTkFont(size=13, weight="bold"),
                      command=toggle).pack(side="left", padx=(0, 0))
        return box

    def _check(self, parent, text, var, command=None, pady=6):
        cb = ctk.CTkCheckBox(parent, text=text, variable=var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                             text_color=WHITE, checkbox_width=18, checkbox_height=18, command=command)
        cb.pack(anchor="w", padx=14, pady=pady)
        return cb

    def _help(self, parent, text, indent=14):
        ctk.CTkLabel(parent, text=text, text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                     wraplength=640).pack(anchor="w", padx=(indent, 14), pady=(0, 8))

    def _page_compression(self, scroll):
        self._section_label(scroll, "Packing")
        pk = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        pk.pack(fill="x", pady=(4, 12))
        self._check(pk, "Verify the output after packing (slower, uses more RAM)", self.app.verify_output_var,
                    pady=(10, 6))
        self._check(pk, "Integrate a patch found in the release folder", self.app.auto_integrate_patch_var)

        def _confirm_fake_sign():
            # ask only when turning it on: from then on every pack signs the source in place
            if not self.app.fake_sign_before_pack_var.get():
                return
            if not ask_confirm("Fake-sign before packing?",
                               tr("With this on, every pack of a game folder fake-signs its executables in "
                                  "place before packing: your source files change (files already signed are "
                                  "skipped). Disk-image sources are not touched."), "Turn on"):
                self.app.fake_sign_before_pack_var.set(False)
        self._check(pk, "Fake-sign executables before packing (game folders, in place)",
                    self.app.fake_sign_before_pack_var, command=_confirm_fake_sign)
        self._check(pk, "Build through an exFAT volume", self.app.build_via_exfat_var, pady=(6, 2))
        self._help(pk, "Slower than the folder builder; worth a try when a folder-built .ffpfsc crashes the "
                       "console.", indent=14 + 28)

        self._section_label(scroll, "Compression for new jobs")
        comp = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        comp.pack(fill="x", pady=(4, 12))
        _st = ctk.CTkFrame(comp, fg_color="transparent")
        _st.pack(fill="x", padx=14, pady=(10, 4))

        def slider_row(r, label, frm, to, var, text_of):
            ctk.CTkLabel(_st, text=label, text_color=WHITE, width=150, anchor="w",
                         font=ctk.CTkFont(size=12)).grid(row=r, column=0, sticky="w", pady=4)
            ctk.CTkSlider(_st, from_=frm, to=to, number_of_steps=to - frm, variable=var, width=240,
                          fg_color=BORDER2, progress_color=ACCENT, button_color=ACCENT,
                          button_hover_color=ACCENT_HOVER).grid(row=r, column=1, sticky="w", padx=8, pady=4)
            lbl = ctk.CTkLabel(_st, text="", text_color=MUTED, anchor="w", font=ctk.CTkFont(size=12))
            lbl.grid(row=r, column=2, sticky="w")

            def show(*_):
                if lbl.winfo_exists():
                    try:
                        lbl.configure(text=text_of(int(var.get())))
                    except (TypeError, ValueError, tk.TclError):
                        pass
            var.trace_add("write", show)
            show()

        slider_row(0, ".ffpfsc level", 1, 9, self.app.compression_level_var,
                   lambda v: tr("level {n}", n=v) + "  ·  " + tr("higher: smaller file, slower build"))
        _pkg_level = tk.IntVar(value=self.app._pkg_level_default())

        def _pkg_cb(*_):
            try:
                v = max(-4, min(9, int(_pkg_level.get())))
            except (TypeError, ValueError):
                return
            self.app.fpkg_defaults["level"] = v
            save_settings({"fpkg_defaults": self.app.fpkg_defaults})
        _pkg_level.trace_add("write", _pkg_cb)
        slider_row(1, ".pkg Kraken level", -4, 9, _pkg_level,
                   lambda v: tr("level {n}", n=v) + "  ·  " + tr(self.app._pkg_level_text(v)))
        slider_row(2, "CPU cores", 0, 16, self.app.cpu_count_var,
                   lambda v: tr("auto: by game size and output drive") if v == 0 else str(v))
        self._help(comp, "Each job keeps its own compression (Add job › Output); a new job starts from these "
                         "values, and the editor remembers the last one you used. Blocks are always 64 KiB, "
                         "the size the PS5 needs.")
        self._pkg_level_var = _pkg_level

        self._section_label(scroll, "Auto output")
        auto = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        auto.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(auto, text="With Auto as the output, every source gets the output set here for its kind. "
                                "An archive is opened briefly and counts as what is inside it.",
                     text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                     wraplength=600).pack(anchor="w", padx=14, pady=(10, 4))
        labels = JobDialog._TARGET_LABEL
        names = {"folder": "Game folder", "exfat": ".exfat image", "ffpkg": ".ffpkg image",
                 "ffpfs": ".ffpfs image", "ffpfsc": ".ffpfsc image", "pkg": ".pkg package"}
        acts = auto_actions(load_settings().get("auto_actions"))

        def _pick(kind, disp, rev):
            table = auto_actions(load_settings().get("auto_actions"))
            table[kind] = rev.get(disp, table[kind])
            save_settings({"auto_actions": table})
        for kind in AUTO_SOURCE_KINDS:
            row = ctk.CTkFrame(auto, fg_color=PANEL)
            row.pack(fill="x", padx=14, pady=4)
            ctk.CTkLabel(row, text=names[kind], text_color=WHITE, width=180, anchor="w").pack(side="left")
            # a folder into a folder has nothing to do
            choices = [c for c in AUTO_CHOICES if not (kind == "folder" and c == "folder")]
            disp = {labels[c]: c for c in choices}
            menu = ctk.CTkOptionMenu(row, values=list(disp), width=200,
                                     command=lambda d, k=kind, r=disp: _pick(k, d, r))
            menu.set(labels[acts[kind]])
            menu.pack(side="left")
        ctk.CTkFrame(auto, fg_color=PANEL, height=6).pack()


        adv = self._advanced(scroll)
        self._check(adv, "Keep the intermediate PFS image", self.app.keep_pfs_var, pady=(10, 2))
        self._help(adv, "The uncompressed image pass 1 builds stays in the temp folder (as big as the game).",
                   indent=14 + 28)
        self._check(adv, "Verbose MkPFS output in the log (debugging)", self.app.verbose_var, pady=(2, 10))
        rs = ctk.CTkFrame(scroll, fg_color="transparent")
        rs.pack(fill="x", pady=(8, 12))
        ctk.CTkButton(rs, text="Reset this page", width=130, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=self._reset_compression).pack(side="left")

    def _reset_compression(self):
        if not ask_confirm("Reset this page?", tr("Packing, the compression for new jobs, Auto output and the "
                                                   "advanced options go back to their defaults."), "Reset"):
            return
        a = self.app
        for var, value in ((a.verify_output_var, False), (a.auto_integrate_patch_var, False),
                           (a.fake_sign_before_pack_var, False), (a.build_via_exfat_var, False),
                           (a.keep_pfs_var, False), (a.verbose_var, False)):
            var.set(value)
        a.compression_level_var.set(7)
        a.cpu_count_var.set(0)
        if getattr(self, "_pkg_level_var", None) is not None:
            self._pkg_level_var.set(0)
        save_settings({"auto_actions": dict(DEFAULT_AUTO_ACTIONS)})
        self._rebuild("compression")

    def _rebuild(self, key):
        f = self._page_frames.pop(key, None)
        if f is not None:
            f.destroy()
        self.show(key)

    def _page_archives(self, scroll):
        comp = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        comp.pack(fill="x", pady=(4, 12))
        ctk.CTkLabel(comp, text="Password to try first (until you quit):", text_color=MUTED, anchor="w").pack(
            anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(comp, text="Tried before the saved list for every archive in this session; not stored.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w").pack(
            anchor="w", padx=14)
        ctk.CTkEntry(comp, textvariable=self.app.password_var, show="*",
                      fg_color=CARD, border_color=BORDER2, text_color=WHITE).pack(
            fill="x", padx=14, pady=(4, 10))

        ctk.CTkLabel(comp, text="Saved archive passwords (tried in order, one per line):",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(comp, text="Every password here is tried automatically, in order - handy for a "
                                "queue of differently-protected archives. The list is empty until "
                                "you add passwords.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left").pack(
            anchor="w", padx=14)
        self._pw_list_box = ctk.CTkTextbox(comp, height=110, fg_color=CARD,
                                            border_width=1, border_color=BORDER2, text_color=WHITE,
                                            font=ctk.CTkFont(size=12))
        self._pw_list_box.pack(fill="x", padx=14, pady=(4, 4))
        self._pw_list_box.insert("1.0", "\n".join(self.app.archive_passwords))

        def _save_pw_list(*_):
            raw = self._pw_list_box.get("1.0", "end")
            seen, pwds = set(), []
            for ln in raw.splitlines():
                ln = ln.strip()
                if ln and ln not in seen:
                    seen.add(ln)
                    pwds.append(ln)
            self.app.archive_passwords = pwds
            save_settings({"archive_passwords": pwds})

        self._pw_list_box.bind("<FocusOut>", _save_pw_list, add="+")
        ctk.CTkButton(comp, text="Save passwords", fg_color=ACCENT, text_color=ON_ACCENT,
                       hover_color=ACCENT_HOVER, width=150, command=_save_pw_list).pack(
            anchor="w", padx=14, pady=(0, 10))


    def _page_drives(self, scroll):
        ds = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        ds.pack(fill="x", pady=(4, 12))
        s = load_settings()

        def _ds_row(label):
            row = ctk.CTkFrame(ds, fg_color=PANEL)
            row.pack(fill="x", padx=14, pady=6)
            ctk.CTkLabel(row, text=label, text_color=WHITE, width=180, anchor="w").pack(side="left")
            return row

        dm_labels = {"auto": "Auto (smart)", "temp": "Temp drive only", "spread": "Spread across drives"}
        dm_rev = {v: k for k, v in dm_labels.items()}
        dm_menu = ctk.CTkOptionMenu(
            _ds_row("Drive usage:"), values=list(dm_labels.values()), width=200,
            command=lambda disp: self.app.drive_mode_var.set(dm_rev.get(disp, "auto")))
        dm_menu.set(dm_labels.get(self.app.drive_mode_var.get(), "Auto (smart)"))
        dm_menu.pack(side="left")

        # scales only the temp headroom
        sf_labels = {0.7: "70% (risky)", 0.85: "85%", 1.0: "100% (recommended)",
                     1.2: "120%", 1.5: "150% (cautious)"}
        sf_rev = {v: k for k, v in sf_labels.items()}
        sf_menu = ctk.CTkOptionMenu(
            _ds_row("Temp space safety:"), values=list(sf_labels.values()), width=200,
            command=lambda disp: save_settings({"space_safety_factor": sf_rev.get(disp, 1.0)}))
        try:
            _cur_sf = min(sf_labels, key=lambda k: abs(k - float(s.get("space_safety_factor", 1.0))))
        except Exception:
            _cur_sf = 1.0
        sf_menu.set(sf_labels[_cur_sf])
        sf_menu.pack(side="left")

        lp_labels = {"ask": "Ask me", "auto": "Proceed anyway", "skip": "Skip the game"}
        lp_rev = {v: k for k, v in lp_labels.items()}
        lp_menu = ctk.CTkOptionMenu(
            _ds_row("When space is low:"), values=list(lp_labels.values()), width=200,
            command=lambda disp: save_settings({"low_space_policy": lp_rev.get(disp, "ask")}))
        lp_menu.set(lp_labels.get(s.get("low_space_policy", "ask"), "Ask me"))
        lp_menu.pack(side="left")

        ctk.CTkCheckBox(ds, text="Show the drive space check before each pack",
                         variable=self.app.show_space_dialog_var, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18, checkbox_height=18).pack(anchor="w", padx=14, pady=(6, 4))
        ctk.CTkCheckBox(ds, text="Keep external drives spun up during a run (bridges gaps between games)",
                         variable=self.app.keep_drives_awake_var, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18, checkbox_height=18).pack(anchor="w", padx=14, pady=(0, 4))

        ctk.CTkLabel(ds, text="Extra temp drives (pool, one path per line):",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(ds, text="Add more fast scratch drives here (e.g. external SSDs). For a big archive "
                              "game that won't fit one drive, the source is extracted to one and the inner "
                              "image built on another - so pass 1 stays SSD↔SSD instead of reading off the "
                              "HDD. The main Temp folder is the first pool drive; these are added to it.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                      wraplength=640).pack(anchor="w", padx=14)
        self._pool_box = ctk.CTkTextbox(ds, height=70, fg_color=CARD, border_width=1,
                                         border_color=BORDER2, text_color=WHITE,
                                         font=ctk.CTkFont(size=12))
        self._pool_box.pack(fill="x", padx=14, pady=(4, 4))
        self._pool_box.insert("1.0", "\n".join(self.app.temp_pool))

        def _save_pool(*_):
            raw = self._pool_box.get("1.0", "end")
            seen, dirs = set(), []
            for ln in raw.splitlines():
                ln = ln.strip()
                if ln and ln not in seen:
                    seen.add(ln)
                    dirs.append(ln)
            self.app.temp_pool = dirs
            save_settings({"temp_pool": dirs})
            self.app._warm_drive_types()

        self._pool_box.bind("<FocusOut>", _save_pool, add="+")
        _pool_btns = ctk.CTkFrame(ds, fg_color=PANEL)
        _pool_btns.pack(anchor="w", fill="x", padx=14, pady=(0, 6))
        def _add_pool_dir():
            p = filedialog.askdirectory(title="Select an extra temp/scratch drive")
            if p:
                cur = self._pool_box.get("1.0", "end").rstrip("\n")
                self._pool_box.delete("1.0", "end")
                self._pool_box.insert("1.0", (cur + "\n" + p).strip("\n"))
                _save_pool()
        ctk.CTkButton(_pool_btns, text="Add drive…", fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER, width=120,
                       command=_add_pool_dir, border_width=1, border_color=BTN_BORDER).pack(side="left")
        ctk.CTkButton(_pool_btns, text="Save pool", fg_color=ACCENT, text_color=ON_ACCENT,
                       hover_color=ACCENT_HOVER, width=120, command=_save_pool).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(ds, text="The safety factor scales only the temp headroom; the output drive always has to "
                              "fit the finished .ffpfsc. With Ask me, a job that does not fit is skipped after a "
                              "minute unless you decide, so a queue left alone goes on.\n\n"
                              "Keeping drives spun up runs only while a job packs: a tiny write every few seconds "
                              "keeps bus-powered HDDs from parking between games.",
                      text_color=MUTED, justify="left", wraplength=640).pack(anchor="w", padx=14, pady=(0, 10))

        adv = self._advanced(scroll)
        def _adv_row(label):
            row = ctk.CTkFrame(adv, fg_color=PANEL)
            row.pack(fill="x", padx=14, pady=6)
            ctk.CTkLabel(row, text=label, text_color=WHITE, width=180, anchor="w").pack(side="left")
            return row
        # same-drive read+write: free on SSD, seek thrashing on HDD. Auto = SSD only
        sd_labels = {"auto": "Auto (allow on SSD)", "always": "Always allow", "never": "Never (always split)"}
        sd_rev = {v: k for k, v in sd_labels.items()}
        sd_menu = ctk.CTkOptionMenu(
            _adv_row("Same-drive read+write:"), values=list(sd_labels.values()), width=200,
            command=lambda disp: save_settings({"same_drive_rw": sd_rev.get(disp, "auto")}))
        sd_menu.set(sd_labels.get(s.get("same_drive_rw", "auto"), sd_labels["auto"]))
        sd_menu.pack(side="left")
        ka_labels = {5: "every 5 s", 8: "every 8 s (recommended)", 10: "every 10 s", 15: "every 15 s"}
        ka_rev = {v: k for k, v in ka_labels.items()}
        ka_menu = ctk.CTkOptionMenu(
            _adv_row("Keep-awake interval:"), values=list(ka_labels.values()), width=200,
            command=lambda disp: save_settings({"keep_awake_interval": ka_rev.get(disp, 8)}))
        try:
            _cur_ka = int(s.get("keep_awake_interval", 8))
        except (TypeError, ValueError):
            _cur_ka = 8
        ka_menu.set(ka_labels.get(_cur_ka, "every 8 s (recommended)"))
        ka_menu.pack(side="left")
        self._help(adv, "Reading and writing on one drive costs nothing on an SSD and is slow on an HDD. Keep the "
                        "keep-awake interval under about 8 s, the park timer of most drives.")
        rs = ctk.CTkFrame(scroll, fg_color="transparent")
        rs.pack(fill="x", pady=(8, 12))
        ctk.CTkButton(rs, text="Reset this page", width=130, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=self._reset_drives).pack(side="left")

    def _reset_drives(self):
        if not ask_confirm("Reset this page?", tr("Drive usage, the space options and the advanced options go "
                                                   "back to their defaults. The extra temp drives stay."), "Reset"):
            return
        self.app.drive_mode_var.set("auto")
        self.app.show_space_dialog_var.set(True)
        self.app.keep_drives_awake_var.set(False)
        save_settings({"space_safety_factor": 1.0, "low_space_policy": "ask", "same_drive_rw": "auto",
                       "keep_awake_interval": 8})
        self._rebuild("drives")


    def _page_backport(self, scroll):
        ds = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        ds.pack(fill="x", pady=(4, 12))
        ctk.CTkLabel(ds, text="AMPR / APR (PlayGo) - emu files folder:",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(ds, text="Folder holding libSceAmpr.sprx + libScePlayGo.sprx (you supply these). "
                              "PlayGo/APR games are auto-detected (sce_sys/playgo-chunk.dat); the two "
                              "files are injected into a fakelib/ folder and an ampr_emu.index is built "
                              "before packing, so the game boots from the compressed container.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                      wraplength=640).pack(anchor="w", padx=14)
        _ampr_row = ctk.CTkFrame(ds, fg_color="transparent")
        _ampr_row.pack(fill="x", padx=14, pady=(4, 4))
        _ampr_entry = ctk.CTkEntry(_ampr_row, textvariable=self.app.ampr_var,
                                   placeholder_text="Folder with the two .sprx files…")
        _ampr_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        def _save_ampr(*_):
            save_settings({"ampr_folder": self.app.ampr_var.get().strip()})
        def _browse_ampr():
            from tkinter import filedialog
            c = filedialog.askdirectory(title="Select AMPR Emu Folder")
            if c:
                self.app.ampr_var.set(c)
                _save_ampr()
        _ampr_entry.bind("<FocusOut>", lambda e: _save_ampr())
        ctk.CTkButton(_ampr_row, text="Folder…", width=80, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=_browse_ampr).pack(side="left")

        # firmware folder first: Prepare reads it, Check compares with it
        ctk.CTkLabel(ds, text="Backport - firmware libraries:",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(ds, text="Your original system libraries, one subfolder per firmware named by its version "
                              "(7.61, 10.01, …). Each subfolder becomes a backport target with the SDK values "
                              "read from its libraries. Check compares a game with the target's subfolder, "
                              "Prepare reads 10.01. Nothing ships with this app.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                      wraplength=640).pack(anchor="w", padx=14)
        _fw_row = ctk.CTkFrame(ds, fg_color="transparent")
        _fw_row.pack(fill="x", padx=14, pady=(4, 0))
        _fw_entry = ctk.CTkEntry(_fw_row, textvariable=self.app.fw_libs_var,
                                 placeholder_text="Folder with one subfolder per firmware…")
        _fw_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        _fw_status = TextVar(value="")

        def _show_fw_status():
            _fw_status.set(_firmware_folder_status((self.app.fw_libs_var.get() or "").strip()))

        def _save_fw(*_):
            save_settings({"fw_libs_root": self.app.fw_libs_var.get().strip()})
            _show_fw_status()
        def _browse_fw():
            from tkinter import filedialog
            c = filedialog.askdirectory(title="Select the folder with one subfolder per firmware")
            if c:
                self.app.fw_libs_var.set(c)
                _save_fw()
        _fw_entry.bind("<FocusOut>", lambda e: _save_fw())
        ctk.CTkButton(_fw_row, text="Folder…", width=80, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=_browse_fw).pack(side="left")
        ctk.CTkLabel(ds, textvariable=_fw_status, text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w",
                      justify="left", wraplength=640).pack(anchor="w", padx=14, pady=(2, 4))
        _show_fw_status()

        ctk.CTkLabel(ds, text="Backport - patched libraries (default for new jobs):",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(ds, text="Patched system libraries, one subfolder per target (Prepare below makes 7.61 and "
                              "6.02). A job copies the set for its target into the game's fakelib/. Needed only "
                              "when the target lacks functions the game uses.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                      wraplength=640).pack(anchor="w", padx=14)
        _bl_row = ctk.CTkFrame(ds, fg_color="transparent")
        _bl_row.pack(fill="x", padx=14, pady=(4, 4))
        _bl_entry = ctk.CTkEntry(_bl_row, textvariable=self.app.backport_libs_var,
                                 placeholder_text="Folder for the patched libraries…")
        _bl_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        def _save_bl(*_):
            save_settings({"backport_libs_root": self.app.backport_libs_var.get().strip()})
        def _browse_bl():
            from tkinter import filedialog
            c = filedialog.askdirectory(title="Select the folder for your patched PS5 libraries")
            if c:
                self.app.backport_libs_var.set(c)
                _save_bl()
        _bl_entry.bind("<FocusOut>", lambda e: _save_bl())
        ctk.CTkButton(_bl_row, text="Folder…", width=80, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=_browse_bl).pack(side="left")

        # BackPork patches (~1 KB per target) applied to the user's 10.01 libs, in a thread
        _prep_row = ctk.CTkFrame(ds, fg_color="transparent")
        _prep_row.pack(fill="x", padx=14, pady=(6, 0))
        ctk.CTkLabel(_prep_row, text="Prepare from BestPig BackPork patches:",
                      text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left")
        for _t in ("7.61", "6.02"):
            ctk.CTkButton(_prep_row, text=tr("Prepare {target}", target=_t), width=100, height=26,
                           fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                           command=lambda t=_t: self.app.prepare_backport_libs(t), border_width=1, border_color=BTN_BORDER).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(ds, text="Downloads BackPork's current .bps patches for the target from its public GitHub "
                              "repo (small, cached), applies them to the libraries in your 10.01 subfolder and "
                              "writes the result into the patched libraries folder, in a subfolder named after "
                              "the target. The libraries never leave your machine.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                      wraplength=640).pack(anchor="w", padx=14, pady=(4, 4))

        if sys.platform == "win32":     # LibProsperoPkg refuses this backend elsewhere
            ctk.CTkLabel(ds, text="fPKG - Publishing Tools DLL (optional):",
                          text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
            ctk.CTkLabel(ds, text="Path to your own libScePubTools.dll (Sony SDK, not bundled). Used when an fPKG "
                                  "job picks the 'publishingtools' Kraken backend.",
                          text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                          wraplength=640).pack(anchor="w", padx=14)
            _pt_row = ctk.CTkFrame(ds, fg_color="transparent")
            _pt_row.pack(fill="x", padx=14, pady=(4, 4))
            _pt_entry = ctk.CTkEntry(_pt_row, textvariable=self.app.pubtools_dll_var,
                                     placeholder_text="…/libScePubTools.dll")
            _pt_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            def _save_pt(*_):
                save_settings({"pubtools_dll": self.app.pubtools_dll_var.get().strip()})
            def _browse_pt():
                from tkinter import filedialog
                c = filedialog.askopenfilename(title="Select libScePubTools.dll",
                                               filetypes=[("DLL", "*.dll"), ("All files", "*.*")])
                if c:
                    self.app.pubtools_dll_var.set(c)
                    _save_pt()
            _pt_entry.bind("<FocusOut>", lambda e: _save_pt())
            ctk.CTkButton(_pt_row, text="Choose…", width=80, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                          border_width=1, border_color=BTN_BORDER, command=_browse_pt).pack(side="left")


    def _page_about(self, scroll):
        about = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        about.pack(fill="x", pady=(4, 12))
        rows = [("Version", f"{APP_VERSION}{tr(APP_EDITION)}"), ("MkPFS", f"{MKPFS_NAME} {MKPFS_VERSION}"),
                ("Config", SETTINGS_FILE), ("History", HISTORY_FILE), ("Log", RAW_LOG_FILE)]
        pad = max(len(tr(k)) for k, _v in rows) + 3
        for line in [f"{tr(k):<{pad}}{v}" for k, v in rows]:
            ctk.CTkLabel(about, text=line, text_color=MUTED, anchor="w",
                         font=ctk.CTkFont(family=MONO_FONT, size=11)).pack(anchor="w", padx=14, pady=3)
        ctk.CTkLabel(about, text="By Knutwurst. The backend grew out of Bizkut's ps5-ffpfs-cli; images are built "
                                 "with PSBrew's MkPFS and packages with LibProsperoPkg. See NOTICES.md.",
                     text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=520, justify="left").pack(
            anchor="w", padx=14, pady=(8, 12))
        ctk.CTkLabel(about, text="If you like what I do, you can buy me a coffee.", text_color=WHITE,
                     font=ctk.CTkFont(size=12), anchor="w").pack(anchor="w", padx=14)
        self._donate_btn = ctk.CTkButton(about, text="Donate with PayPal", fg_color=BTN, text_color=WHITE,
                                         hover_color=BTN_HOVER, border_width=1, border_color=BTN_BORDER, width=160,
                                         command=lambda: webbrowser.open(DONATE_URL))
        self._donate_btn.pack(anchor="w", padx=14, pady=(6, 4))
        ctk.CTkLabel(about, text="It doesn't have to be a big donation. A few cents show me who uses the tool and "
                                 "cares about where it goes next. That keeps me motivated, and I know I'm not "
                                 "coding for the bin ;)",
                     text_color=MUTED, font=ctk.CTkFont(size=11), wraplength=520, justify="left").pack(
            anchor="w", padx=14, pady=(0, 12))
        ctk.CTkButton(scroll, text="Open config folder", fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                      border_width=1, border_color=BTN_BORDER, width=160,
                      command=lambda: open_path(APP_DIR)).pack(anchor="w", pady=(0, 12))

    def _refresh_after_rows(self):
        aj = _after_job_module()
        v = self.app.after_source_var.get()
        for w in (self._after_dir_row, self._after_note):
            w.pack_forget()
        if v == aj.MOVE:
            self._after_dir_row.pack(fill="x", pady=(4, 2))
        elif v == aj.DELETE:
            self._after_note.pack(anchor="w", padx=82, pady=(4, 0))

    def _on_after_source(self, label):
        aj = _after_job_module()
        key = next(k for k in aj.ACTIONS if aj.LABELS[k] == label)
        prev = self.app.after_source_var.get()
        if key == aj.DELETE and prev != aj.DELETE:
            if not ask_confirm(
                    "Delete sources?",
                    tr("From now on, a new job deletes its source for good once it is Done: the game folder, "
                      "the archive with all its parts, or the container, and its whole download folder when "
                      "the game's add-ons went along. It does not go to the "
                      "{TRASH_NAME}.\n\nJobs already in the queue keep their own choice.", TRASH_NAME=TRASH_NAME),
                    "Use Delete", danger=True):
                self._after_seg.set(aj.LABELS.get(prev, aj.LABELS[aj.KEEP]))
                return
        self.app.after_source_var.set(key)
        if key == aj.MOVE and not self.app.after_move_dir_var.get().strip():
            self._browse_folder(self.app.after_move_dir_var, "after_move_dir",
                                "Choose the folder finished sources move to")
        self._refresh_after_rows()

    def _section_label(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=WHITE).pack(anchor="w", padx=4, pady=(10, 4))

    def _browse_folder(self, var, settings_key, title):
        current = (var.get() or "").strip()
        kw = {"initialdir": current} if current and Path(current).is_dir() else {}
        p = filedialog.askdirectory(title=title, **kw)
        if p:
            var.set(p)
            save_settings({settings_key: p})


# CLI worker

def _package_platform(p) -> str | None:
    """ps4 or ps5 by the package magic, else None"""
    try:
        with open(p, "rb") as f:
            head = f.read(4)
    except OSError:
        return None
    return {b"\x7fCNT": "ps4", b"\x7fFIH": "ps5"}.get(head)


def _is_inner_image_path(p) -> bool:
    """pass-1 image: <temp>/_ffpfsc_inner/<id>.ffpfs"""
    try:
        q = Path(str(p or ""))
        return (q.is_absolute() and q.suffix.lower() == ".ffpfs" and q.parent.name == "_ffpfsc_inner"
                and ".." not in q.parts)
    except (TypeError, ValueError):
        return False


class CLIWorker(threading.Thread):
    WEIGHTS = {
        "Scanning Files":      (0,    8),
        "Reading Game":        (8,   18),
        "Creating Temp PFS":   (18,  40),
        "Compressing":         (40,  80),
        "Extracting":          (18,  93),
        "Writing Final Image": (80,  93),
        "Verifying Output":    (93,  97),
        "Cleaning Up":         (97, 100),
        "Complete":            (100, 100),
    }

    # patch mode unpacks first, then repacks: own bands so the bar never jumps back
    PATCH_WEIGHTS = {
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   40),
        "Reading Game":        (40,  42),
        "Creating Temp PFS":   (42,  64),   # pass-1: build patched PFS image
        "Compressing":         (64,  92),   # pass-2: compress to .ffpfsc
        "Writing Final Image": (92,  98),
        "Verifying Output":    (98,  99),
        "Cleaning Up":         (99, 100),
        "Complete":            (100, 100),
    }

    # band split is a guess from one 160 GB run; each band's bar is metered in bytes
    FPKG_BUILD_WEIGHTS = {
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   34),   # only when the source is a .ffpfsc/.exfat image
        "Reading Game":        (34,  38),  # staging copy of a folder source, if needed
        "Creating Temp PFS":   (38,  74),   # inner image incl. Kraken
        "Compressing":         (74,  90),   # NAPS tables + outer-PFS write and hash
        "Writing Final Image": (90,  96),   # CNT + FIH finalize
        "Verifying Output":    (96,  98),   # validate checklist
        "Cleaning Up":         (98, 100),
        "Complete":            (100, 100),
    }
    FPKG_EXTRACT_WEIGHTS = {
        "Scanning Files":      (0,    3),
        "Extracting":          (3,   96),
        "Cleaning Up":         (96, 100),
        "Complete":            (100, 100),
    }

    # chain from a container: unpack, change, build. the shares are a guess
    CHAIN_WEIGHTS = {
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   30),
        "Reading Game":        (30,  34),
        "Creating Temp PFS":   (34,  58),
        "Compressing":         (58,  92),
        "Writing Final Image": (92,  97),
        "Verifying Output":    (97,  99),
        "Cleaning Up":         (99, 100),
        "Complete":            (100, 100),
    }
    RESUME_WEIGHTS = {            # a retry that compresses the image pass 1 left
        "Scanning Files":      (0,    2),
        "Compressing":         (2,   92),
        "Writing Final Image": (92,  97),
        "Verifying Output":    (97,  99),
        "Cleaning Up":         (99, 100),
        "Complete":            (100, 100),
    }
    CHAIN_FFPFS_WEIGHTS = {       # uncompressed output: pass 1 writes the output itself
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   40),
        "Reading Game":        (40,  45),
        "Creating Temp PFS":   (45,  96),
        "Verifying Output":    (96,  99),
        "Cleaning Up":         (99, 100),
        "Complete":            (100, 100),
    }
    CHAIN_EXFAT_WEIGHTS = {  # LZ4 packs show up as Compressing
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   25),
        "Reading Game":        (25,  35),
        "Compressing":         (35,  80),
        "Writing Final Image": (80,  98),
        "Cleaning Up":         (98, 100),
        "Complete":            (100, 100),
    }
    CHAIN_EXFAT_PLAIN_WEIGHTS = {
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   45),
        "Reading Game":        (45,  50),
        "Writing Final Image": (50,  98),
        "Cleaning Up":         (98, 100),
        "Complete":            (100, 100),
    }
    CHAIN_FOLDER_WEIGHTS = {
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   90),
        "Reading Game":        (90,  98),
        "Cleaning Up":         (98, 100),
        "Complete":            (100, 100),
    }
    # plain copy or move, the write is the whole job; switched to on [JOB] copy
    COPY_WEIGHTS = {
        "Writing Final Image": (0,   97),
        "Cleaning Up":         (97, 100),
        "Complete":            (100, 100),
    }
    _COPY_ORDER = ["Writing Final Image", "Cleaning Up", "Complete"]
    # for jobs that unpack first; the plain order ranks Extracting late
    _EXTRACT_FIRST_ORDER = [
        "Scanning Files", "Extracting", "Reading Game", "Creating Temp PFS",
        "Compressing", "Writing Final Image", "Verifying Output", "Cleaning Up", "Complete",
    ]
    # suffixes a chain target reads without unpacking (native in cli.py)
    _CHAIN_NATIVE = {"exfat": set(),
                     "ffpfs": {".exfat", ".ffpkg", ".ffpfs", ".zip", ".rar"},
                     "ffpfsc": {".exfat", ".ffpkg", ".ffpfs", ".zip", ".rar"},
                     "pkg": {".ffpfs", ".ffpfsc", ".exfat", ".ffpkg"}}

    @staticmethod
    def _without_extract(weights: dict) -> dict:
        """later bands stretch over the freed share so a folder source doesn't skip ahead"""
        if "Extracting" not in weights:
            return dict(weights)
        a, b = weights["Extracting"]
        if b >= 100:
            return dict(weights)
        def at(x):
            return x if x <= a else a + (x - b) * (100 - a) / (100 - b)
        return {st: (at(s), at(e)) for st, (s, e) in weights.items() if st != "Extracting"}

    @staticmethod
    def _cmd_value(cmd, flag):
        try:
            return cmd[cmd.index(flag) + 1]
        except (ValueError, IndexError):
            return None

    def _pick_weights(self, cmd):
        cmd = cmd or []
        if self._is_fpkg_build:
            src = self._cmd_value(cmd, "--fpkg-build")
            unpacks = bool(src) and Path(src).is_file()
            w = self.FPKG_BUILD_WEIGHTS
            return (w if unpacks else self._without_extract(w)), self._EXTRACT_FIRST_ORDER
        if self._is_fpkg_extract:
            return self.FPKG_EXTRACT_WEIGHTS, self._EXTRACT_FIRST_ORDER
        to = self._cmd_value(cmd, "--to")
        _ri = getattr(self.item, "_resume_inner", None)
        if (to == "ffpfsc" or (not to and self.operation == "pack")) and _ri and Path(str(_ri)).is_file():
            return self.RESUME_WEIGHTS, self._EXTRACT_FIRST_ORDER     # compression only
        if to:
            src = Path(str(getattr(self.item, "path", "") or ""))
            changes = any(f in cmd for f in ("--patch", "--backport-target", "--sign", "--lz4", "--lz4-unpack"))
            unpacks = src.is_file() and (changes or src.suffix.lower() not in self._CHAIN_NATIVE.get(to, set()))
            if to == "pkg":
                # the fPKG builder unwraps an image source itself, behind the same marker
                unpacks = src.is_file()
                w = self.FPKG_BUILD_WEIGHTS
            elif to == "folder":
                w = self.CHAIN_FOLDER_WEIGHTS
            elif to == "ffpfs":
                w = self.CHAIN_FFPFS_WEIGHTS
            elif to == "exfat":
                w = self.CHAIN_EXFAT_WEIGHTS if "--lz4" in cmd else self.CHAIN_EXFAT_PLAIN_WEIGHTS
            else:
                w = self.CHAIN_WEIGHTS
            return (w if unpacks else self._without_extract(w)), self._EXTRACT_FIRST_ORDER
        if self._is_patch:
            return self.PATCH_WEIGHTS, self._EXTRACT_FIRST_ORDER
        return self.WEIGHTS, self._STAGE_ORDER

    def __init__(self, app, item, cmd, cwd, output_dir, temp_dir):
        super().__init__(daemon=True)
        self.app = app
        self.item = item
        self.cmd = cmd
        self.cwd = cwd
        self.output_dir = output_dir
        self.temp_dir = temp_dir
        self.proc = None
        self.start_time = 0
        self.last_heartbeat = 0
        self.last_log = 0
        self.last_status_ui = 0
        self.last_log_ui = 0
        self.phase = "Starting"
        self.operation = getattr(item, "operation", "pack")
        # patch mode unpacks then repacks: forward-only weights, [PHASE] markers move it on
        self._is_patch = "--patch" in (cmd or []) and "--to" not in (cmd or [])
        self._is_fpkg_build   = "--fpkg-build"   in (cmd or [])
        self._is_fpkg_extract = "--fpkg-extract" in (cmd or [])
        # a chain to .pkg ends in the fPKG builder: free-text bar labels, no ShadowMount check
        _chain_to = self._cmd_value(cmd or [], "--to")
        self._is_fpkg = self._is_fpkg_build or self._is_fpkg_extract or _chain_to == "pkg"
        self._weights, self._stage_order = self._pick_weights(cmd)
        self._is_copy = False
        self.patch_backup = ""        # the originals a patch replaced, staged by the backend
        self.extras_left: list[str] = []  # extras that never reached the output
        self.validate_failed = False   # the .pkg checklist reported failures: the source stays
        self.consumed = False  # backend ate the unpacked files while packing
        self._written: list[str] = []  # extra outputs: bundle extras, PS4 title folders
        # Highest whole-job progress sent so far: the queue bar never moves backward.
        self._overall_sent = 0.0
        # read Tk vars here on the main thread; the worker thread must not touch them
        try:
            self.copy_siblings = bool(app.copy_siblings_var.get())
        except Exception:
            self.copy_siblings = True
        try:  # add-on unpacking, see _place_bundle_siblings
            self._addon_passwords = app._candidate_passwords(item)
            self._if_exists = app.output_exists_var.get() or "skip"
        except Exception:
            self._addon_passwords, self._if_exists = [], "skip"
        self.output_path = ""
        self.final_size = 0
        self.speed = " - "
        self.temp_start_size = get_folder_size(self.temp_dir)
        self.temp_peak_size = self.temp_start_size
        self.last_cmd_str = " ".join(cmd)
        self.stage_progress = {
            "Scanning Files": 0,
            "Reading Game": 0,
            "Creating Temp PFS": 0,
            "Compressing": 0,
            "Extracting": 0,
            "Writing Final Image": 0,
            "Verifying Output": 0,
            "Cleaning Up": 0,
            "Complete": 0,
        }
        self.last_stage_bucket = {}
        self._mem_error_shown = False

    def run(self):
        ensure_app_dir()
        self.start_time = time.time()
        self.last_heartbeat = self.start_time
        _ticker_stop = threading.Event()

        # a bundle writes into a recreated subfolder that has to exist first
        if self.operation != "unpack":
            try:
                self.output_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

        # raw log: the last 10 MB plus every error line, which is never dropped
        RAW_LOG_MAX_BYTES = 10 * 1024 * 1024
        from collections import deque as _deque
        _raw_lines: _deque[str] = _deque()
        _raw_errors: list[str]  = []
        _raw_buf_bytes          = 0

        def _raw_append(text: str):
            nonlocal _raw_buf_bytes
            encoded = text.encode("utf-8", errors="replace")
            _raw_lines.append(text)
            _raw_buf_bytes += len(encoded)
            while _raw_buf_bytes > RAW_LOG_MAX_BYTES and _raw_lines:
                dropped = _raw_lines.popleft()
                _raw_buf_bytes -= len(dropped.encode("utf-8", errors="replace"))

        self.app.log("INFO", f"{APP_NAME} {APP_VERSION}{APP_EDITION} started")
        self.app.log("INFO", f"Backend: {BACKEND_NAME}")
        self.app.log("INFO", f"MkPFS: {MKPFS_NAME} v{MKPFS_VERSION}")
        _op_label = {"unpack": "Unpack", "patch": "Integrate patch",
                     "fake-sign": "Fake sign", "fpkg-extract": "Extract fPKG",
                     "fpkg-build": "Build fPKG", "ps4-keystone": "PS4 PKG Tools"}.get(self.operation, "Pack")
        self.app.log("INFO", f"Operation: {_op_label}")
        self.app.log("INFO", f"Game: {self.item.title_id} | {self.item.name}")
        self.app.log("INFO", f"Original: {format_size(self.item.size)} | Files: {self.item.files}")
        self.app.log("INFO", f"Backend Python: {' '.join(get_backend_python_command()) or 'NOT FOUND'}")
        self.app.log("CMD", self.last_cmd_str)
        _raw_append("[COMMAND] " + self.last_cmd_str + "\n")

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            env["TEMP"] = str(self.temp_dir)
            env["TMP"] = str(self.temp_dir)
            env["TMPDIR"] = str(self.temp_dir)

            self.proc = subprocess.Popen(
                self.cmd,
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",     # a non-UTF-8 file name must not kill the reader
                bufsize=1,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                # own process group, so cancel kills the whole tree incl. the mkpfs pool workers
                start_new_session=(os.name != "nt"),
            )
            self.app.current_process = self.proc

            # heartbeat on a wall clock: silent phases like the final write looked hung
            def _ticker():
                _last_sample = 0.0
                while not _ticker_stop.wait(5):
                    t = time.time()
                    # sizing the temp tree is a stat storm on mkpfs's drive; every 30 s will do
                    if t - _last_sample >= 30:
                        _last_sample = t
                        try:
                            self.temp_peak_size = max(self.temp_peak_size, get_folder_size(self.temp_dir))
                        except Exception:
                            pass
                    if t - self.last_heartbeat >= 30:
                        self.last_heartbeat = t
                        elapsed = format_duration(t - self.start_time)
                        try:
                            # keep the card ticking, not the log; reuse the last detail so the text doesn't flicker
                            self.app.status_update("Still Working",
                                                    getattr(self, "_detail", "") or "Backend is active. Do not close the app.",
                                                    self.phase, self.stage_progress.get(self.phase, 0),
                                                    self._job_overall(), elapsed, self.speed, " - ",
                                                    job=self.item)
                        except Exception:
                            pass
            threading.Thread(target=_ticker, daemon=True).start()

            _last_flush_t = time.time()
            FLUSH_INTERVAL = 30

            def _flush_raw_log():
                try:
                    RAW_LOG_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    with RAW_LOG_FILE.open("w", encoding="utf-8", errors="replace") as f:
                        if _raw_errors:
                            f.write("=" * 60 + "\n")
                            f.write("ERRORS / FAILURES ENCOUNTERED DURING THIS JOB\n")
                            f.write("=" * 60 + "\n")
                            f.writelines(_raw_errors)
                            f.write("=" * 60 + "\n\n")
                        f.write(f"[LAST {RAW_LOG_MAX_BYTES // (1024*1024)} MB OF BACKEND OUTPUT]\n\n")
                        f.writelines(_raw_lines)
                except Exception:
                    pass

            for line in self.proc.stdout or []:
                if self.app.cancel_requested:
                    self._terminate()
                    break

                clean = line.rstrip("\r\n")
                _raw_append(clean + "\n")
                upper_c = clean.upper()
                if re.search(r'\bERROR\b|\bFAILED\b', upper_c):
                    _raw_errors.append(clean + "\n")
                self._handle_line(clean)

                t = time.time()
                if t - _last_flush_t >= FLUSH_INTERVAL:
                    _flush_raw_log()
                    _last_flush_t = t

            code = self.proc.wait() if self.proc else 1

            _flush_raw_log()

            if self.app.cancel_requested:
                self.app.finish(False, "Cancelled by user.", self.last_cmd_str)
                return
            # copy exit 2 = same file, 3 = name taken by another file: a skip, not a failure
            if (self.operation == "copy" or self._is_copy) and code in (2, 3):   # a chain that became a copy too
                reason = ("source and destination are the same file"
                          if code == 2 else
                          "a different file already uses that name in the output folder")
                self._write_report(True)
                self.app.finish(True, tr("Copy skipped - {reason}. Nothing was changed.", reason=reason),
                                self.last_cmd_str)
                return
            if code == 3 and self._is_fpkg and not self._is_copy:
                where = getattr(self, "invalid_pkg", "") or "the output folder"
                self.app.finish(False, tr("The package was built but failed its checks (see the log): {where}. It is kept so you "
                                         "can look at it; it is not marked as done.", where=where),
                                self.last_cmd_str)
                return
            if code == 4 and self.operation == "copy":
                # exit 4: some items stayed in the source ([KEPT]), still a success
                self.extras_left.append("items the backend left in the source (see the [KEPT] line)")
                code = 0
            if code != 0:
                smart = smart_error_from_log()
                msg = smart if smart else tr("Backend exited with code {code}.", code=code)
                # OOM: a MemoryError, or SIGKILL (-9/137) since the OS kill gives mkpfs no time to say so
                if (self.operation not in ("unpack", "fpkg-extract", "fpkg-build", "copy")
                        and (getattr(self, "_mem_error_shown", False) or code in (-9, 137))):
                    self.oom_killed = True
                self.app.finish(False, msg, self.last_cmd_str)
                return

            self._set_stage("Cleaning Up", 100, "Temporary cleanup finished.")
            try:
                self.temp_peak_size = max(self.temp_peak_size, get_folder_size(self.temp_dir))
            except Exception:
                pass

            # fake-sign rewrites in place: no output to look for, exit 0 is success
            if self.operation == "fake-sign":
                self._write_report(True)
                self.app.finish(True, "Fake-signing completed successfully.", self.last_cmd_str)
                return
            if self.operation == "ps4-keystone":      # changes the packages in place, writes nothing new
                self._write_report(True)
                self.app.finish(True, "PS4 packages repaired.", self.last_cmd_str)
                return

            # copy output lands at the name the backend picked: no scan, no ShadowMount sniff
            if self.operation == "copy":
                if getattr(self.item, "bundle_release", None) and getattr(self.item, "content_kind", "") in (ORGANIZE_TARGET, "ps4"):
                    try:
                        self._place_bundle_siblings()     # the add-ons of its download folder
                    except Exception as e:
                        self.app.log("WARN", f"Bundle copy failed: {e}")
                self._strip_written_clutter()
                self._write_report(True)
                self.app.finish(True, "Copy completed successfully.", self.last_cmd_str)
                return

            if not self._find_output():
                self._write_report(False)
                expected = {"unpack": "extracted output folder",
                            "fpkg-extract": "extracted /app0 folder",
                            "fpkg-build": "new .pkg output"}.get(self.operation, "new .ffpfsc output")
                if self._chain_to() == "exfat":
                    expected = "new .exfat output"
                self.app.finish(False, tr("Backend exited but no {expected} was created.", expected=expected), self.last_cmd_str)
                return

            # the output exists now: log bookkeeping errors, never fail the job,
            # or the failure cleanup runs and the next Start rebuilds everything
            try:
                if self.operation not in ("unpack", "fpkg-extract"):
                    self._place_bundle_siblings()
            except Exception as e:
                self.app.log("WARN", f"Bundle copy failed: {e}")
            self._strip_written_clutter()
            # a .pkg is installed, not mounted, and has its own checklist in the backend log
            try:
                if not self._is_fpkg and self.operation != "unpack":
                    for w in self._validate_shadowmount():
                        self.app.log("WARN", w)
            except Exception as e:
                self.app.log("WARN", f"Compatibility check skipped: {e}")
            try:
                self._write_report(True)
            except Exception as e:
                self.app.log("WARN", f"Could not write the result report: {e}")
            # history goes in on the main thread (done_q handler): add_history touches Tk
            success_msg = {"unpack": "Extraction completed successfully.",
                           "fpkg-extract": "fPKG extracted successfully.",
                           "fpkg-build": "fPKG built and validated - see the checklist above."
                           }.get(self.operation, "Compression completed successfully.")
            self.app.finish(True, success_msg, self.last_cmd_str)
        except Exception as e:
            try:
                _raw_append(f"[GUI ERROR] {e}\n")
                _flush_raw_log()
            except Exception:
                pass
            # the reader died, maybe not the backend: kill it before cleanup removes its scratch
            try:
                if self.proc is not None and self.proc.poll() is None:
                    self._terminate()
            except Exception:
                pass
            self.app.finish(False, str(e), self.last_cmd_str)
        finally:
            _ticker_stop.set()
            self.app.current_process = None

    def _stage_from_label(self, label: str, raw: str) -> str | None:
        """None when unsure: the caller then leaves the stage alone"""
        text = f"{label} {raw}".lower()

        # label is what follows the % (compress @ 290 MB/s ETA 14s); match its first word
        first_word = label.strip().lower().split()[0] if label.strip() else ""

        if self.operation == "unpack":
            if first_word in ("extract", "extracting", "unpack", "unpacking") or "extract" in text or "unpack" in text:
                return "Extracting"
            if first_word in ("read", "reading", "scan", "scanning") or "discover" in text:
                return "Reading Game"
            return "Extracting"

        # .ffpfsc checked before the generic write
        if ".ffpfsc" in text or "final image" in text or "final output" in text:
            return "Writing Final Image"

        # write shows up for the temp PFS and the final image: compression tells them apart
        if first_word in ("write", "writing"):
            if self.stage_progress.get("Compressing", 0) > 0:
                return "Writing Final Image"
            else:
                return "Creating Temp PFS"

        if first_word in ("scan", "scanning") or "discover" in text:
            return "Scanning Files"

        if first_word in ("read", "reading"):
            return "Reading Game"

        if first_word in ("compress", "compressing") or "compress" in text:
            return "Compressing"

        # verify only from a real bar, not from plain messages
        if first_word in ("verify", "verifying"):
            return "Verifying Output"

        if first_word in ("extract", "extracting", "unpack", "unpacking") or "extract" in text or "unpack" in text:
            return "Extracting"

        # looser fallbacks for odd backend messages
        if "scan" in text or "discover" in text:
            return "Scanning Files"
        if "read" in text and "write" not in text:
            return "Reading Game"
        if "clean" in text or "delete" in text or "removed" in text:
            return "Cleaning Up"

        # Complete is never returned here; run() sets it after exit
        return None

    def _overall_for_stage(self, stage: str, pct: float) -> float:
        start, end = self._weights.get(stage, (0, 100))
        return max(0, min(100, start + (max(0, min(100, pct)) / 100) * (end - start)))

    def _overall(self):
        return self._overall_for_stage(self.phase, self.stage_progress.get(self.phase, 0))

    def _job_overall(self) -> float:
        """one value for every status update: behind the GUI's unpack share, never going down"""
        overall = self._overall()
        if getattr(self.item, "_from_archive", False):
            share = self.app._archive_extract_pct(self.item)
            overall = share + overall * (100 - share) / 100.0
        self._overall_sent = max(self._overall_sent, overall)
        return self._overall_sent

    # a literal on purpose: _STAGE_DEFS is defined after CLIWorker
    _STAGE_ORDER = [
        "Scanning Files", "Reading Game", "Creating Temp PFS",
        "Compressing", "Extracting", "Writing Final Image", "Verifying Output",
        "Cleaning Up", "Complete",
    ]

    def _set_stage(self, stage, pct, label="", eta=" - ", force=False):
        # no going back: Writing PFS image after compression would snap back to Temp PFS
        order = getattr(self, "_stage_order", self._STAGE_ORDER)
        if not force and stage in order and self.phase in order:
            if order.index(stage) < order.index(self.phase):
                return
        self.phase = stage
        pct = max(0, min(100, pct))
        if stage == "Creating Temp PFS" and pct >= 100:
            pct = 99
        self.stage_progress[stage] = max(self.stage_progress.get(stage, 0), pct)

        # a later stage snaps earlier ones to 100%: some backends stop short of it
        def snap(*earlier):
            for s in earlier:
                if s in self._weights:
                    self.stage_progress[s] = 100
        if stage == "Compressing":
            snap("Creating Temp PFS", "Reading Game")
        elif stage == "Writing Final Image":
            snap("Creating Temp PFS", "Compressing")
        elif stage == "Verifying Output":
            snap("Writing Final Image", "Compressing")
        elif stage == "Extracting":
            snap("Scanning Files")
            if getattr(self, "_stage_order", self._STAGE_ORDER) is self._STAGE_ORDER:
                snap("Reading Game")   # extract-first jobs read after
        elif stage in ("Cleaning Up", "Complete"):
            for s in ("Scanning Files", "Reading Game", "Creating Temp PFS",
                      "Compressing", "Extracting", "Writing Final Image"):
                if self.stage_progress.get(s, 0) > 0:
                    self.stage_progress[s] = 100

        elapsed = format_duration(time.time() - self.start_time)
        overall = self._job_overall()

        detail = label or tr("{stage} is active.", stage=stage)
        self._detail = None
        if stage == "Creating Temp PFS" and not label:
            # the backend meters this step in bytes; the fixed text is only a fallback
            detail = ("Building temporary PFS image. "
                      "Large games may look frozen here - the backend is still working. "
                      "Do NOT close the app.")
        elif stage == "Cleaning Up":
            detail = "Cleaning up temporary files. Please wait before closing the app."
        elif stage == "Writing Final Image" and self._is_copy:
            detail = ("Moving the file to the output folder." if label == "move"
                      else "Copying the file to the output folder.")
        elif stage == "Writing Final Image" and not label:
            detail = "Writing the output file. Do not close the app."
        elif stage == "Compressing" and not label:
            detail = "Compressing game data."
        elif stage == "Extracting" and not label:
            detail = "Extracting PFS image contents."

        self._detail = detail
        now = time.time()
        bucket = (int(self.stage_progress[stage]) // 5) * 5
        should_update_ui = (force
                            or (now - self.last_status_ui >= 0.5)
                            or self.last_stage_bucket.get(stage, -1) != bucket
                            or int(self.stage_progress[stage]) == 100)
        if should_update_ui:
            self.last_status_ui = now
            self.app.status_update(stage, detail, stage, self.stage_progress[stage],
                                   overall, elapsed, self.speed, eta, job=self.item)

        # log at 5% steps and at 100% only, backends print dozens of 0% lines
        if self.last_stage_bucket.get(stage, -1) != bucket or int(self.stage_progress[stage]) == 100:
            self.last_stage_bucket[stage] = bucket
            self.app.log("PROGRESS", f"{stage}: {int(self.stage_progress[stage])}% {label}".strip())

    def _handle_line(self, line):
        if not line:
            return

        lower = line.lower()
        upper = line.upper()

        if line.startswith("[PATCH-BACKUP] "):
            self.patch_backup = line[len("[PATCH-BACKUP] "):].strip()
            return
        if line.startswith("[KEPT-BUILD] "):
            self.kept_build = line[len("[KEPT-BUILD] "):].strip()    # a finished image the rename refused
            return
        if line.startswith("[EXTRAS-LEFT] "):
            self.extras_left.append(line[len("[EXTRAS-LEFT] "):].strip())
            return
        if line.startswith("[EXTRAS-ASIDE] "):
            self.extras_aside = line[len("[EXTRAS-ASIDE] "):].strip()   # "<aside> -> <game folder>"
            return
        if line.startswith("[FPKG-INVALID] "):
            self.invalid_pkg = line[len("[FPKG-INVALID] "):].strip()   # built, but its checks failed
        if "[consume] freed" in line:
            self.consumed = True  # extraction no longer complete, so no retry from it
        if line == "[JOB] copy":
            self._is_copy = True
            self._weights, self._stage_order = self.COPY_WEIGHTS, self._COPY_ORDER
            return
        if line.startswith("[PHASE] "):
            stage = line[8:].strip()
            if stage in self._weights:
                # backward markers are dropped with their bars, or Temp PFS sits at 99% through Kraken
                order = getattr(self, "_stage_order", self._STAGE_ORDER)
                forced = self._stage_order is self._STAGE_ORDER
                self._phase_dropped = (not forced and stage in order and self.phase in order
                                       and order.index(stage) < order.index(self.phase))
                self._set_stage(stage, 0, force=forced)
            return

        # pass 2 reads only the inner image: free the extracted source now, halves peak temp
        if line.startswith("[PASS1-DONE] "):
            inner = line[len("[PASS1-DONE] "):].strip()
            if not _is_inner_image_path(inner):
                # a file name inside a package can carry a line break and fake this marker
                self.app.log("WARN", f"Ignored a pass-1 marker that does not name an inner image: {inner}")
                return
            try:
                self.item._inner_image = inner
            except Exception:
                pass
            try:
                self.app.root.after(0, lambda it=self.item: self.app._free_source_after_pass1(it))
            except Exception:
                pass
            return

        # raw MemoryError lines only, mkpfs [INFO] lines mention it too; shown once per job
        stripped = line.strip()
        if (stripped == "MemoryError"
                or ("memoryerror" in lower and not stripped.startswith("[") and "avoid" not in lower)):
            if not getattr(self, "_mem_error_shown", False):
                self._mem_error_shown = True
                self.app.log("ERROR",
                    "Out of RAM - mkpfs ran out of memory during parallel compression.\n"
                    "\n"
                    "  What happened:\n"
                    "    mkpfs spawns one worker process per CPU core. Each worker holds\n"
                    "    compressed data in RAM. Too many cores = not enough memory.\n"
                    "\n"
                    "  ╔═ Try these settings (Compression Tuning bar): ══════════════╗\n"
                    "  ║  CPU cores  →  set to 2 (or 1 for very large games)         ║\n"
                    "  ║  Level      →  try 5 instead of 7 (less RAM per worker)     ║\n"
                    "  ║  Block size →  try 16384 or 32768 (smaller per-block buffers) ║\n"
                    "  ╚═══════════════════════════════════════════════════════════════╝"
                )
            return

        # MemoryError fallout, not worth showing
        if "concurrent send_bytes" in lower or "maybeencodingerror" in lower:
            return

        # errno 22: exFAT/FAT32 and the 4 GB file limit, or a chunk mangled by an OOM crash
        if ("oserror" in lower or "ioerror" in lower) and (
            "errno 22" in lower or "invalid argument" in lower
        ):
            self.app.log("ERROR",
                "Write failed - OS error 22 (Invalid argument).\n"
                "\n"
                "  Most likely cause:  output drive is exFAT or FAT32\n"
                "    exFAT / FAT32 has a 4 GB per-file limit.\n"
                "    A large .ffpfsc will exceed this and fail mid-write.\n"
                "\n"
                "  ╔═ Settings to check / change: ═══════════════════════════════╗\n"
                "  ║  OUTPUT folder  →  move to an NTFS drive (e.g. C:\\  D:\\)   ║\n"
                "  ║  CPU cores      →  set to 1-2 if RAM could also be the cause ║\n"
                "  ╚══════════════════════════════════════════════════════════════╝"
            )
            return

        if ("errno 28" in lower or "no space left" in lower
                or "there is not enough space" in lower):
            self.app.log("ERROR",
                "Disk full - the output or temp drive ran out of space.\n"
                "\n"
                "  ╔═ Settings to check: ════════════════════════════════════════╗\n"
                "  ║  OUTPUT folder  →  point to a drive with more free space    ║\n"
                "  ║  TEMP folder    →  point to a drive with more free space    ║\n"
                "  ║                    (needs ~1.5× the game size during build) ║\n"
                "  ╚══════════════════════════════════════════════════════════════╝"
            )
            return

        if "calledprocesserror" in lower and "non-zero exit status" in lower:
            self.app.log("ERROR", "mkpfs exited with an error - see messages above.")
            if not getattr(self, "_mem_error_shown", False):
                self.app.log("ERROR",
                    "  Common causes & settings to try:\n"
                    "\n"
                    "  ╔═ Check these settings: ══════════════════════════════════════╗\n"
                    "  ║  OUTPUT folder  →  must be NTFS (not exFAT / FAT32)          ║\n"
                    "  ║  TEMP folder    →  needs ~1.5× game size of free space       ║\n"
                    "  ║  CPU cores      →  lower to 2 or 1 if you have limited RAM   ║\n"
                    "  ║  Level          →  try 5 if high level causes OOM            ║\n"
                    "  ╚═══════════════════════════════════════════════════════════════╝\n"
                    "  If mkpfs is missing:  pip install mkpfs\n"
                    "  Full error detail is in the raw log."
                )
            return

        # a crafted file name can fake these lines: only paths inside the job's folders count
        if line.startswith("[OK] Compression complete: "):
            self._take_output(line[len("[OK] Compression complete: "):])
        if line.startswith("[OK] fPKG complete: "):
            self._take_output(line[len("[OK] fPKG complete: "):])
        if line.startswith("[OK] exFAT image ready: "):
            self._take_output(line[len("[OK] exFAT image ready: "):])
        elif line.startswith("[OK] Folder ready: "):
            self._take_output(line[len("[OK] Folder ready: "):])
        if line.startswith("[OK] Organized: "):
            if self._take_output(line[len("[OK] Organized: "):].split(" (replaced)")[0]):
                self._written.append(self.output_path)
        if line.startswith("[OK] PS4 sorted: "):
            if self._take_output(line[len("[OK] PS4 sorted: "):]):           # a title folder of the set
                self._written.append(self.output_path)
        _cm = re.match(r"\[SUCCESS\] (?:Copied|Moved) .+? → (.+)$", line)
        if _cm and self.operation == "copy":
            self._take_output(_cm.group(1))
        if "Validation reported failures" in line:
            self.validate_failed = True
        if "Extraction complete:" in line:
            # patch mode repacks after the unpack: latching Extracting=100 would freeze the bar
            if self.operation in ("unpack", "fpkg-extract"):
                self._set_stage("Extracting", 100, "Extraction complete.")
            if line.startswith("[OK] Extraction complete: "):
                self._take_output(line[len("[OK] Extraction complete: "):])
        _um = re.match(r"\s+Output:\s+(.+)$", line) if self.operation == "unpack" else None
        if _um:
            self._take_output(_um.group(1))

        prog = PROGRESS_RE.search(line)
        if prog:
            pct = max(0, min(100, int(prog.group("pct"))))
            label = prog.group("label").strip()
            # fPKG and copy bars are free text: stick to the current [PHASE] instead of guessing
            if getattr(self, "_phase_dropped", False) and (self._is_fpkg or self._is_copy):
                return  # a bar of a refused [PHASE]
            stage = (self.phase if ((self._is_fpkg or self._is_copy) and self.phase in self._weights)
                     else self._stage_from_label(label, line))
            # two bars per step (MkPFS counts files, our meter bytes): drop the laggard or it flickers
            _now = time.time()
            _lead = getattr(self, "_lead_line", None)
            if (stage is not None and stage == self.phase and pct < self.stage_progress.get(stage, 0)
                    and _lead and _lead[0] == stage and _now - _lead[1] < 3):
                return
            if stage is not None and pct >= self.stage_progress.get(stage, 0):
                self._lead_line = (stage, _now)

            sp = re.search(r"@\s*([0-9.]+\s*(?:GB|MB)/s)", label, re.I)
            if sp:
                self.speed = sp.group(1)

            # pbar prints ETA 1695s below 1h but ETA 84.2m above, so take decimals and the unit
            eta_match = re.search(
                r"ETA\s*([0-9]+(?:\.[0-9]+)?\s*(?:h|hr|hrs|hours?|m|min|mins|minutes?|s|sec|secs|seconds?)?)",
                label, re.I)
            if eta_match:
                eta = humanize_eta(eta_match.group(1).strip())
                label = label[:eta_match.start()] + f"ETA {eta}" + label[eta_match.end():]
            else:
                eta = " - "

            if stage is None:
                return

            if stage == "Reading Game" and pct >= 100:
                self._set_stage("Reading Game", 100, label, eta)
                self._set_stage("Creating Temp PFS", 0, "Building temporary PFS image. Do NOT close the app.", " - ")
                return

            if stage == "Compressing" and pct >= 100:
                self._set_stage("Compressing", 100, label, eta)
                # a silent final write would leave the display stuck on Compressing
                self._set_stage("Writing Final Image", 0, "Writing final .ffpfsc output file…", " - ")
                return

            self._set_stage(stage, pct, label, eta)
            return

        if "unable to stage source file" in lower or "hard link and symlink both failed" in lower:
            self.app.log("WARN",
                "Temp drive does not support hardlinks/symlinks. "
                "Fallback to copy mode - compression will be slower and needs extra space.")

        if "renaming inner image" in lower or "inner image renamed" in lower:
            self.app.log("INFO",
                "ℹ  mkpfs renamed the inner image to match the outer filename. "
                "This is normal - the .ffpfsc will mount correctly.")

        # specific phrases only: temp paths show up in the parameter dump before the scan
        if "writing pfs image to" in lower:
            # start at 0 so the write bars own the percent, max() in _set_stage would pin 5
            self._set_stage("Creating Temp PFS", 0, "Building PFS image…")
        elif ".ffpfsc" in lower and self.stage_progress.get("Compressing", 0) > 0:
            self._set_stage("Writing Final Image",
                            max(5, self.stage_progress.get("Writing Final Image", 0)),
                            "Writing final .ffpfsc output file…")
        elif "successfully wrote" in lower or "pfs creation complete" in lower:
            if self.stage_progress.get("Compressing", 0) == 0:
                self._set_stage("Creating Temp PFS", 100, line)
        elif self.operation == "unpack" and ("extract" in lower or "files written" in lower or "dirs created" in lower):
            self._set_stage("Extracting", 100 if "complete" in lower else max(5, self.stage_progress.get("Extracting", 0)), line)
        # never Verifying from plain text, 'verify is disabled' would fire it at the start

        tag = "INFO"
        # word boundaries, or MemoryError would tag a line as ERROR
        if re.search(r'\bERROR\b|\bFAILED\b', upper):
            tag = "ERROR"
        elif re.search(r'\bWARN\b|\bWARNING\b', upper):
            tag = "WARN"
        elif "SUCCESS" in upper or "[OK]" in upper or "COMPLETE" in upper:
            tag = "OK"

        # errors and warnings are never throttled
        always_show = tag in ("ERROR", "WARN")
        important = always_show or tag != "INFO" or any(k in upper for k in [
            "BUILD SUMMARY", "TOTAL FILES", "TOTAL UNCOMPRESSED", "TOTAL STORED",
            "INPUT PATH", "OUTPUT PATH", "ELAPSED", "THROUGHPUT",
            "DISCOVERING", "COMPRESSING", "WRITING", "VERIFY", "INSPECT"
        ])
        t = time.time()
        if important or t - self.last_log >= 3:
            if not always_show:
                self.last_log = t
            self.app.log(tag, line)

    def _chain_to(self):
        """None unless a chain; lets result detection treat it like the matching plain job"""
        if self.operation != "chain":
            return None
        return getattr(self.item, "chain_to", None) or "ffpfsc"

    def _find_output(self):
        if self.output_path:
            p = Path(self.output_path.strip('"'))
            try:
                if (self.operation in ("unpack", "fpkg-extract") or self._chain_to() == "folder") and p.exists() and p.is_dir():
                    self.final_size = get_folder_size(p)
                    return True
                # a copy or a move into the library keeps the file's old date: no age check there
                if (self.operation == "copy" or self._is_copy) and p.exists() and (p.is_dir() or p.stat().st_size > 0):
                    self.final_size = get_folder_size(p) if p.is_dir() else p.stat().st_size
                    return True
                if p.exists() and p.is_file() and p.stat().st_size > 0 and p.stat().st_mtime >= self.start_time - 2:
                    if self.operation == "fpkg-build" or self._chain_to() == "pkg":
                        # the fPKG marker lands here, so the auto-organize rename has to happen here too
                        p = self.app._finalize_pkg_name(self.item, p)
                        self.output_path = str(p)
                    self.final_size = p.stat().st_size
                    return True
            except OSError:
                pass

        if self.operation == "fpkg-extract" or self._chain_to() == "folder":
            if self.output_dir.exists() and self.output_dir.is_dir() and any(self.output_dir.iterdir()):
                self.output_path = str(self.output_dir)
                self.final_size = get_folder_size(self.output_dir)
                return True
            return False

        if self.operation == "fpkg-build" or self._chain_to() == "pkg":
            try:
                cands = [q for q in self.output_dir.glob("*.pkg")
                         if q.is_file() and q.stat().st_size > 0 and q.stat().st_mtime >= self.start_time - 2]
            except OSError:
                cands = []
            if cands:
                best = max(cands, key=lambda q: q.stat().st_mtime)
                best = self.app._finalize_pkg_name(self.item, best)
                self.output_path = str(best)
                self.final_size = best.stat().st_size
                return True
            self.output_path = ""; self.final_size = 0
            return False

        if self.operation == "unpack":
            # build_command already points output_dir at "<stem>_extracted".
            expected = self.output_dir
            if expected.exists() and expected.is_dir():
                self.output_path = str(expected)
                self.final_size = get_folder_size(expected)
                return True
            if self.output_dir.exists() and self.output_dir.is_dir():
                self.output_path = str(self.output_dir)
                self.final_size = get_folder_size(self.output_dir)
                return True

        # expected name first: the newest file may be stale or another job's
        if self.operation != "unpack":
            tid = (getattr(self.item, "title_id", "") or "").strip()
            if tid and tid not in ("📦", "Unknown"):
                try:
                    # matches <tid>.ffpfsc as well as <name> [<tid>].ffpfsc
                    cands = [p for p in self.output_dir.glob(f"*{tid}*.ffpfs*")
                             if p.is_file() and p.stat().st_size > 0
                             and p.stat().st_mtime >= self.start_time - 2
                             and p.suffix.lower() in (".ffpfsc", ".ffpfs")]
                    if cands:
                        best = max(cands, key=lambda p: p.stat().st_mtime)
                        self.output_path = str(best)
                        self.final_size = best.stat().st_size
                        return True
                except OSError:
                    pass

        newest = find_newest_ffpfsc_after(self.output_dir, self.start_time)
        if newest:
            self.output_path = str(newest)
            self.final_size = newest.stat().st_size
            return True

        self.output_path = ""
        self.final_size = 0
        return False

    def _take_output(self, raw: str) -> bool:
        """only paths inside the output folder or own source count, the rest is logged"""
        raw = (raw or "").strip().strip('"')
        if not raw or not os.path.isabs(raw):
            return False
        roots = [(r, True) for r in (self.output_dir, getattr(self.item, "path", None),
                                     getattr(self.item, "archive_path", None)) if r]
        # plus the folders passed on the command line
        roots += [(a, False) for a in (self.cmd or []) if isinstance(a, str) and os.path.isabs(a) and os.path.isdir(a)]
        try:
            cand = Path(raw).resolve()
            home = Path.home().resolve()
            for r, own in roots:
                base = Path(str(r)).resolve()
                if base.is_file():
                    base = base.parent
                if not own and (base.parent == base or base == home
                                or str(base) in ("/Volumes", "/media", "/mnt", "/Users")):
                    continue                     # a drive root, the mount folders, the home folder
                if cand == base or cand.is_relative_to(base):
                    self.output_path = raw
                    return True
        except (OSError, ValueError):
            pass
        self.app.log("WARN", f"Ignored an output path outside the job's folders: {raw}")
        return False

    def _strip_written_clutter(self) -> None:
        """sweep '._' sidecars and other OS litter out of what this job wrote"""
        n = 0
        for p in [self.output_path, *self._written]:
            try:
                n += strip_written_clutter(str(p).strip('"')) if p else 0
            except Exception:
                pass
        if n:
            self.app.log("INFO", f"Removed {n} macOS clutter file(s) ('._' sidecars) from the output.")

    @staticmethod
    def _same_tree(a: Path, b: Path) -> bool:
        """same files, same bytes, OS clutter ignored"""
        def files(root):
            out = set()
            for dp, dn, fn in os.walk(root):
                dn[:] = [d for d in dn if not is_fs_junk_name(d)]
                out.update(Path(dp, f).relative_to(root) for f in fn if not is_fs_junk_name(f))
            return out
        fa = files(a)
        if fa != files(b):
            return False
        filecmp.clear_cache()  # filecmp caches by size and mtime
        return all(filecmp.cmp(a / f, b / f, shallow=False) for f in fa)

    _TITLE_FOLDER_RE = re.compile(r"\[(?:PPSA|CUSA)\d{5}\]", re.I)

    def _extras_dest(self) -> Path:
        """next to the output file (its title folder), else the output folder"""
        try:
            out = Path(str(self.output_path or "").strip('"'))
            if str(out) and out.is_file():
                return out.parent
            # a game folder Organize placed in its title folder: beside it, not inside it
            if str(out) and out.is_dir() and Path(self.output_dir) in out.parents:
                return out.parent
        except OSError:
            pass
        return self.output_dir

    def _place_bundle_siblings(self) -> None:
        """packages sorted into the library by role, the rest copied as is; nothing gets left out"""
        siblings = [Path(x) for x in (getattr(self.item, "bundle_siblings", None) or [])]
        if not siblings or not getattr(self, "copy_siblings", True):
            return self._copy_bundle_siblings()

        def sortable(q: Path) -> bool:
            return q.is_file() and (q.suffix.lower() == ".pkg" or is_archive_file(q))
        plain, mixed = [], []
        for sib in siblings:
            try:
                has = sortable(sib) or (sib.is_dir() and any(sortable(q) for q in sib.rglob("*")
                                                             if not is_fs_junk_name(q.name)))
            except OSError:
                has = False
            (mixed if has else plain).append(sib)
        self.item.bundle_siblings = plain
        try:
            self._copy_bundle_siblings()
        finally:
            self.item.bundle_siblings = siblings
        plain_ok = getattr(self, "extras_copied", False) or not plain
        dest_dir = self._extras_dest()
        library = dest_dir.parent if self._TITLE_FOLDER_RE.search(dest_dir.name) else dest_dir
        staging = Path(self.temp_dir) / f"_addons-{os.getpid()}-{id(self.item)}"
        _bd = str(backend_base_dir())
        if _bd not in sys.path:
            sys.path.insert(0, _bd)
        import organize_lib
        failed = 0
        try:
            for sib in mixed:
                if self.app.cancel_requested:
                    failed += 1                             # what is left stays in the source
                    break
                files = [sib] if sib.is_file() else sorted(q for q in sib.rglob("*")
                                                            if q.is_file() and not is_fs_junk_name(q.name))
                seen_sets: set = set()
                for f in files:
                    if self.app.cancel_requested:
                        failed += 1
                        break
                    dest_dir = self._extras_dest()          # Organize may have renamed the title folder
                    rel = Path(sib.name) / f.relative_to(sib) if sib.is_dir() else Path(f.name)
                    # a role folder (backport, updates) keeps its package: alone it would pass for the game
                    by_folder = sib.is_dir() and organize_lib.role_folder(f, sib.parent)
                    if f.suffix.lower() == ".pkg" and not by_folder:
                        ok = self._organize_addon(f, library, "keep")
                    elif is_archive_file(f) and by_folder:
                        first = ArchiveExtractor._first_volume(f)
                        if str(first) in seen_sets:
                            continue
                        seen_sets.add(str(first))
                        ok = all(self._copy_extra(q, dest_dir / rel.parent / q.name)
                                 for q in archive_set_parts(first))  # role comes from the folder: copy as is
                        failed += 0 if ok else 1
                        continue
                    elif is_archive_file(f):
                        first = ArchiveExtractor._first_volume(f)
                        if str(first) in seen_sets:
                            continue                       # a later part of a set already handled
                        seen_sets.add(str(first))
                        ok = self._sort_archive_addon(first, library, staging, rel.parent)
                        if not ok:                         # copied as it is, every part
                            ok = all(self._copy_extra(q, dest_dir / rel.parent / q.name)
                                     for q in archive_set_parts(first))
                            failed += 0 if ok else 1
                        continue
                    else:
                        ok = self._copy_extra(f, dest_dir / rel)
                    if not ok and f.suffix.lower() == ".pkg":
                        ok = self._copy_extra(f, dest_dir / rel)
                    failed += 0 if ok else 1
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.extras_copied = (plain_ok and failed == 0 and bool(siblings)
                              and not getattr(self, "_addons_skipped", 0))
        if getattr(self, "_addons_skipped", 0):
            self.extras_left.append(tr("{n} add-on(s) were already in the library and were skipped; "
                                       "the source keeps them", n=self._addons_skipped))
        if failed:
            self.extras_left.append(tr("{n} add-on item(s) could not be placed", n=failed))

    def _organize_addon(self, src: Path, library: Path, mode: str) -> bool:
        """through the backend's Organize, same as an Organize job"""
        pycmd = get_backend_python_command()
        if not pycmd:
            return False
        head = pycmd if getattr(sys, "frozen", False) else pycmd + ["-u", str(backend_base_dir() / "cli.py")]
        argv = head + ["placeholder", str(library), "--organize-into", str(src), "--copy-mode", mode,
                       "--if-exists", self._if_exists if self._if_exists in ("skip", "ask", "overwrite", "keep") else "skip"]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            library.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace", env=env,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                    start_new_session=(os.name != "nt"))
            self.proc = proc
            self.app.current_process = proc                 # Cancel stops it like the job itself
            placed = False
            for line in proc.stdout or ():
                line = line.rstrip()
                m_ren = re.match(r"\[ORGANIZE\] renamed (.+) -> (.+)$", line)
                if m_ren:
                    self._title_folder_renamed(library / m_ren.group(1), library / m_ren.group(2))
                if line.startswith(("[OK] Organized: ", "[OK] Came along: ", "[INFO] already there")):
                    placed = True
                    m_had = re.match(r"\[INFO\] already there, skipped(?: \(.*?\))?: (.+)$", line)
                    same = bool(m_had) and src.is_file() and same_content(src, library / m_had.group(1))
                    if m_had and not same:
                        # same name in the library may be another version: never delete the source over it
                        self._addons_skipped = getattr(self, "_addons_skipped", 0) + 1
                    self.app.log("INFO", f"Add-on: already in the library, the same file: {m_had.group(1)}" if same
                                 else f"Add-on: {line[5:] if line.startswith('[OK] ') else line[7:]}")
                    if line.startswith("[OK] Organized: "):
                        self._written.append(line[len("[OK] Organized: "):].split(" (replaced)")[0].strip())
                elif line.startswith(("[WARN]", "[ERROR]")):
                    self.app.log("WARN", f"Add-on {src.name}: {line.split('] ', 1)[-1]}")
            proc.wait()
            return proc.returncode in (0, 2) and placed
        except Exception as e:
            self.app.log("WARN", f"Could not sort the add-on {src.name}: {e}")
            return False

    def _title_folder_renamed(self, old: Path, new: Path) -> None:
        """Organize renamed the title folder to a newer version: follow our outputs there"""
        def moved(x: str) -> str:
            try:
                return str(new / Path(x).relative_to(old))
            except ValueError:
                return x
        self.output_path = moved(str(self.output_path or "").strip('"'))
        self._written = [moved(x) for x in self._written]
        if Path(self.output_dir) == old or old in Path(self.output_dir).parents:
            self.output_dir = Path(moved(str(self.output_dir)))
        self.app.log("INFO", f"The title folder took the newer version's name: {new.name}")

    def _sort_archive_addon(self, archive: Path, library: Path, staging: Path, rest_rel: Path) -> bool:
        """False when it can't be unpacked or holds no package; the caller then copies it as is"""
        names = ArchiveExtractor.member_entries(archive, self._addon_passwords)
        if names and not any(n.lower().endswith(".pkg") for n in names):
            return False                                   # nothing to sort: it goes as it is
        dest = staging / re.sub(r"[^\w.-]+", "_", archive.stem)[:60]
        try:
            shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True)
            ArchiveExtractor.extract_with_passwords(archive, dest, self._addon_passwords, log_fn=lambda *_a: None,
                                                    cancel_event=self.app.extract_cancel_event)
        except Exception as e:
            self.app.log("WARN", f"Could not unpack the add-on {archive.name}: {e}; it is copied as it is")
            shutil.rmtree(dest, ignore_errors=True)
            return False
        # the whole extraction, minus one wrapper folder if everything sits in it
        tops = [q for q in dest.iterdir() if not is_fs_junk_name(q.name)]
        base = tops[0] if len(tops) == 1 and tops[0].is_dir() else dest
        _bd = str(backend_base_dir())
        if _bd not in sys.path:
            sys.path.insert(0, _bd)
        import organize_lib
        pkgs = sorted(q for q in dest.rglob("*.pkg") if q.is_file() and not is_fs_junk_name(q.name))
        if not pkgs:
            shutil.rmtree(dest, ignore_errors=True)
            return False
        ok = True
        for pkg in pkgs:
            if self.app.cancel_requested:
                ok = False
                break
            # a folder in the archive that names the role keeps its package where it is
            if organize_lib.role_folder(pkg, dest) or not self._organize_addon(pkg, library, "move"):
                ok = self._copy_extra(pkg, self._extras_dest() / rest_rel / archive_label_stem(archive)
                                      / pkg.relative_to(base)) and ok
        left = [q for q in dest.rglob("*") if q.is_file() and not is_fs_junk_name(q.name)]
        rest_dir = self._extras_dest() / rest_rel / archive_label_stem(archive)   # after any rename
        for q in left:
            ok = self._copy_extra(q, rest_dir / q.relative_to(base)) and ok
        shutil.rmtree(dest, ignore_errors=True)
        return ok

    def _copy_extra(self, src: Path, target: Path) -> bool:
        """never overwrites: a different file of that name stays too"""
        try:
            if target.exists():
                if filecmp.cmp(src, target, shallow=False):
                    return True
                n = 2
                while (target.parent / f"{target.stem} ({n}){target.suffix}").exists():
                    n += 1
                target = target.parent / f"{target.stem} ({n}){target.suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            self._written.append(str(target))
            return True
        except Exception as e:
            self.app.log("WARN", f"Could not copy extra '{src.name}': {e}")
            return False

    def _copy_bundle_siblings(self) -> None:
        """copies DLC files and folders next to the output; the source is never touched"""
        siblings = getattr(self.item, "bundle_siblings", None)
        if not siblings:
            return
        if not getattr(self, "copy_siblings", True):
            self.app.log("INFO", "Copy extras is off - leaving DLC/extra files in the source folder.")
            self.extras_left += [Path(x).name for x in siblings if Path(x).exists()]
            return
        dest_dir = self._extras_dest()
        copied = 0
        for src in siblings:
            try:
                src = Path(src)
                if not src.exists():
                    continue
                target = dest_dir / src.name
                if src.is_dir():
                    # skip an identical copy already there, so a re-run doesn't copy GBs again
                    if target.resolve() == src.resolve():
                        continue  # source already sits there
                    if target.exists() and self._same_tree(src, target):
                        copied += 1
                        continue
                    if target.exists():
                        # a different folder of that name is there: both stay, nothing is merged over
                        n = 2
                        while (dest_dir / f"{src.name} ({n})").exists():
                            n += 1
                        target = dest_dir / f"{src.name} ({n})"
                    self.app.log("INFO", f"Copying extra folder next to output: {target.name} "
                                         f"({format_size(get_folder_size(src))})")
                    self._written.append(str(target))
                    shutil.copytree(src, target, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(*_COPYTREE_JUNK_GLOBS))
                    copied += 1
                elif src.is_file():
                    if target.exists() and filecmp.cmp(src, target, shallow=False):
                        copied += 1
                        continue
                    if target.exists():
                        # another file of that name is already there: both stay
                        n = 2
                        while (dest_dir / f"{src.stem} ({n}){src.suffix}").exists():
                            n += 1
                        target = dest_dir / f"{src.stem} ({n}){src.suffix}"
                    self.app.log("INFO", f"Copying extra next to output: {target.name} ({format_size(src.stat().st_size)})")
                    shutil.copy2(src, target)
                    self._written.append(str(target))
                    copied += 1
            except Exception as e:
                self.app.log("WARN", f"Could not copy extra '{Path(src).name}': {e}")
                self.extras_left.append(Path(src).name)
        if copied:
            self.app.log("OK", f"Copied {copied} extra item(s) next to the .ffpfsc.")
        self.extras_copied = copied == len([x for x in siblings if Path(x).exists()]) and copied > 0

    def _validate_shadowmount(self) -> list:
        warns = []
        if not self.output_path:
            return ["No output path recorded - cannot validate output."]
        p = Path(self.output_path)
        if not p.exists():
            warns.append(tr("Output file not found on disk: {name}", name=p.name))
            return warns
        name_lower = p.name.lower()
        if name_lower.endswith(".ffpfsc.ffpfsc"):
            warns.append(
                tr("Double extension detected: {name}\n   Rename the file - remove one '.ffpfsc' suffix "
                  "before mounting in ShadowMount.", name=p.name)
            )
        elif not name_lower.endswith(".ffpfsc"):
            warns.append(
                tr("Unexpected output extension '{suffix}' - expected .ffpfsc\n   ShadowMount may not "
                  "recognise this file.", suffix=p.suffix)
            )
        sz = p.stat().st_size
        if sz == 0:
            warns.append("Output file is 0 bytes - compression may have failed silently.")
        elif sz < 1 * 1024 * 1024:
            warns.append(
                tr("Output file is very small ({sz}) - the source dump may be incomplete or empty.", sz=format_size(sz))
            )
        return warns

    def _write_report(self, success=True):
        if success:
            self._find_output()
        elapsed = time.time() - self.start_time
        ok = tr("Success") if success else tr("Failed")
        if self.operation == "unpack":
            FINAL_REPORT_FILE.write_text(
                tr("{app} report", app=APP_NAME) + "\n\n"
                f"{tr('Status')}: {ok}\n"
                f"{tr('Operation')}: {tr('Extract PFS image')}\n"
                f"{tr('Source')}: {self.item.path}\n"
                f"{tr('Output')}: {self.output_path or tr('Unknown')}\n"
                f"{tr('Source size')}: {format_size(self.item.size)}\n"
                f"{tr('Extracted size')}: {format_size(self.final_size)}\n"
                f"{tr('Elapsed')}: {format_duration(elapsed)}\n"
                f"Backend: {BACKEND_NAME}\n"
                f"MkPFS: {MKPFS_NAME} v{MKPFS_VERSION}\n",
                encoding="utf-8",
                errors="replace",
            )
            return
        saved = self.item.size - self.final_size if self.item.size and self.final_size else 0
        pct = (saved / self.item.size * 100) if self.item.size else 0
        temp_removed = max(0, self.temp_peak_size - get_folder_size(self.temp_dir))
        # rate compression only where we compress; a copy or sort keeps the size anyway
        out_ext = Path(str(self.output_path or "")).suffix.lower()
        compressing = (self.operation not in ("copy", "ps4-keystone", "fake-sign")
                       and (out_ext in (".ffpfsc", ".pkg") or (not out_ext and self._chain_to() in ("ffpfsc", "pkg"))))
        lines = [tr("{app} report", app=APP_NAME), "",
                 f"{tr('Status')}: {ok}",
                 f"{tr('Game')}: {self.item.name}",
                 f"{tr('Title ID')}: {self.item.title_id}",
                 f"{tr('Source')}: {self.item.path}",
                 f"{tr('Output')}: {self.output_path or tr('Unknown')}",
                 f"{tr('Original size')}: {format_size(self.item.size)}",
                 f"{tr('Output size')}: {format_size(self.final_size)}"]
        if compressing:
            rating, recommendation = compression_rating(pct)
            lines += [f"{tr('Space saved')}: {format_size(saved)} ({pct:.2f}%)",
                      f"{tr('Compression rating')}: {tr(rating)}",
                      f"{tr('Recommendation')}: {tr(recommendation)}"]
        lines += [f"{tr('Peak temp use')}: {format_size(self.temp_peak_size)}",
                  f"{tr('Temporary files removed')}: {format_size(temp_removed)}",
                  f"{tr('Elapsed')}: {format_duration(elapsed)}",
                  f"Backend: {BACKEND_NAME}",
                  f"MkPFS: {MKPFS_NAME} v{MKPFS_VERSION}"]
        FINAL_REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8", errors="replace")

    def _terminate(self):
        _kill_process_tree(self.proc)


# (backend stage name, short label)
_STAGE_DEFS = [
    ("Scanning Files",      "Scan"),
    ("Extracting",          "Extract"),    # archives extract first; folders skip this station
    ("Reading Game",        "Read"),
    ("Creating Temp PFS",   "Temp PFS"),
    ("Compressing",         "Compress"),
    ("Writing Final Image", "Write"),
    ("Verifying Output",    "Verify"),
    ("Cleaning Up",         "Cleanup"),
    ("Complete",            "Done"),
]

def _kill_process_tree(proc) -> None:
    """kill the whole group: a bare terminate() orphans the mkpfs pool workers"""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    if os.name == "nt":
        _kill_process_tree_windows(proc)
        return
    try:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
    except Exception:
        pass


def _kill_process_tree_windows(proc) -> None:
    """no process groups on Windows: walk the tree, children first"""
    try:
        import psutil
        root = psutil.Process(proc.pid)
        procs = root.children(recursive=True) + [root]
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(procs, timeout=5)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
    except Exception:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        except Exception:
            pass


def _hdr_mode(v) -> str:
    """an old bool True was never a real choice, so it means auto; False means off"""
    if v is True:
        return "auto"
    if v is False:
        return "off"
    s = str(v or "auto").strip().lower()
    return s if s in ("auto", "on", "off") else "auto"




class JobDialog(EmbeddedDialog):
    LARGE = True

    _TARGET_LABEL = {"folder": "Folder", "ffpfs": ".ffpfs", "ffpfsc": ".ffpfsc", "exfat": ".exfat", "pkg": ".pkg",
                     "organize": "Organize", "auto": "Auto"}
    _TARGETS = ("folder", "ffpfs", "ffpfsc", "exfat", "pkg", "organize", "auto")
    # PS4 packages are sorted or unpacked, never converted: own labels, same keys
    _PS4_LABEL = {"pkg": "Into the library", "folder": "Unpack to folder"}
    _TARGET_KEY = {**{v: k for k, v in _PS4_LABEL.items()}, **{v: k for k, v in _TARGET_LABEL.items()}}
    _SOURCE_HINT = ("Game folder, parent folder of games, archive (.zip/.rar/.7z), disk image "
                    "(.exfat/.ffpkg), .ffpfs, .ffpfsc or .pkg")
    _BACKPORT_HINTS = {
        "7.61":  "7.61 - public library patches exist for this target",
        "6.02":  "6.02 - experimental: a smaller public library set, only some titles run",
        "10.xx": "10.xx - SDK only, no library bundle; for a game newer than the console",
    }
    _HELP_IDLE = "Hover an option to see what it does and when you need it."
    _HELP = {
        "source": "Drop a path here, or pick a file or a folder. A parent folder of games makes one job "
                  "per game. Archives are extracted when the job runs; a container is unpacked only if "
                  "something has to change in it.",
        "look":   "Opens the browser on this image or package: the tree, and single files pulled out "
                  "without unpacking the rest. A peek, not a job.",
        "patch":  "Merges an update's files over the game's own before packing - the result is the "
                  "updated game. A folder, a .zip or a .rar. Applied first, so patched executables are "
                  "backported and signed too.",
        "backport": "Lowers the SDK in eboot.bin and every prx/sprx so an older firmware loads them. A game that uses"
                    " functions the target lacks also needs patched libraries: the job copies them from the folder "
                    "set in Settings into fakelib/. Nothing is bundled with this app.",
        "target": "7.61: public library patches exist. 6.02: experimental, only some titles run. 10.xx: SDK only, "
                  "for a game newer than the console. The other entries come from your firmware libraries folder "
                  "(Settings); Check shows whether one has every function the game uses.",
        "check": "Compares the functions the game imports with what the target's libraries, your patched libraries"
                 " and the game's own modules export: is lowering the SDK enough, or which libraries are missing? "
                 "Needs a game folder; a container is checked when the job runs.",
        "sign":   "Fake-signs eboot.bin and every prx/sprx so a jailbroken console loads them. Needed "
                  "when a dump's executables are still plain ELFs. Already-signed files are skipped, "
                  "so leaving it on costs nothing. A .pkg is always signed by its builder.",
        "keystone": "Saves hold the keystone of the game that wrote them. A package with a default keystone shows "
                    "saves of the retail game as corrupted, and a merged update whose app version stayed the game's "
                    "makes the console offer the official update. PS4 PKG Tools… opens the tool that fixes both.",
        "ps5repair": "Checks the package the way the console's installer and loader look at it: the package "
                     "layout, param.json, PlayGo tables, images, license, executables, keystone and the firmware "
                     "it needs. PS5 PKG Tools… opens the tool that builds a fixed copy; the original stays "
                     "as it is.",
        "ps4out": "This folder holds PS4 packages too. They go to their own folder: sorted into the PS4 "
                  "library ('<Title> [CUSA…] [vX]', UPDATE and DLC named), or a package unpacked into a "
                  "folder. Each list remembers the folders last used for its platform.",
        "lz4": "Packs the game's data into LZ4 asset packs that ampr_emu reads while the game runs: about half "
               "the size, quicker from USB. Only for games that load through AMPR. Every packed file is checked "
               "against the original. Goes into an .exfat or a folder; keep the original until the game ran on "
               "the console.",
        "output": "Folder: a plain /app0 folder (a folder source is changed in place). .ffpfs: uncompressed image, "
                  "full size. .ffpfsc: compressed image for ShadowMount. .exfat: the format ShadowMountPlus reads "
                  "fastest from USB. .pkg: installable package.",
        "saveto": "Where the result lands. With auto-organize it gets its own folder there, named "
                  "from the game.",
        "organize": "Names the folder and the file from the game's own metadata: "
                    "<Title> [TID] [vX.Y.Z]/<Title> [TID] [vX.Y] [fwN.NN], with the firmware the "
                    "game needs read from its eboot.bin (a backport lowers it to the target). Long names are shortened to "
                    "ShadowMount's byte limit.",
        "after": tr("What happens to the source once the job is Done. What goes to the {TRASH_NAME} can be put back; "
                    "Delete cannot. Nothing happens when the job fails, is skipped or stopped, or while another job "
                    "still needs the same source. Settings › General sets the default.", TRASH_NAME=TRASH_NAME),
        "retail": "Keep on: drops placeholder licenses and issues a valid debug license, sets the retail DRM type "
                  "and flag, rebuilds a corrupt PlayGo set and repairs presentation images, as verified on a "
                  "console. Off only for a byte-exact repack.",
        "playgo": "Off (recommended): the game's PlayGo layout is kept and only the image ranges are recomputed. "
                  "On: a fresh one-chunk set is written. A corrupt set is rebuilt either way; switch this on only "
                  "for a title that installs but does not start (CE-100022-5).",
        "hdr":    "auto keeps what the game's param.json declares - the publisher's intent; a console on "
                  "'HDR when supported' follows this flag. on forces it, off clears it.",
        "identity": "The package identity comes from the game's sce_sys/param.json when it is built. Fill these in "
                    "for a game folder without one (Content ID and Title ID are then required), or to supply what "
                    "param.json lacks.",
        "codec":  "The codec layer of the image inside the package. kraken is the verified default; zlib and none "
                  "are for experiments.",
        "compression": "How hard this job compresses. .ffpfsc: zlib 1 to 9, 7 is the default. .pkg: Kraken -4 to 9, 0 is"
                       " the default; -4 to -1 are fastest and about 2 % larger, 7 to 9 take 4 to 10 times as long for "
                       "the last 0.3 %. A new job starts from Settings › Compression.",
    }
    _CID_RE = re.compile(r"^[A-Z]{2}[0-9]{4}-[A-Z]{4}[0-9]{5}_00-[A-Z0-9]{16}$")
    _TID_RE = re.compile(r"^[A-Z]{4}[0-9]{5}$")
    _VER_RE = re.compile(r"^\d{2}\.\d{2,3}(\.\d{3})?$")
    _INNER = ("kraken", "zlib", "none")
    _BACKEND = ("builtin", "publishingtools")
    _IDENT_OPTIONAL = ("Package identity (optional): the game's sce_sys/param.json decides at build time; a value "
                       "here only fills what param.json lacks.")
    _IDENT_REQUIRED = ("Package identity (required): this folder has no sce_sys/param.json, so the builder "
                       "generates one from these fields.")
    _KIND_LABEL = {"folder": "Game folder", "parent": "Parent folder", "archive": "Archive",
                   "exfat": "exFAT disk image", "ffpkg": "ffpkg disk image", "ffpfs": ".ffpfs image",
                   "ffpfsc": ".ffpfsc image", "pkg": "PS5 package (.pkg)", "ps4": "PS4 package"}

    def __init__(self, app, item=None, init_src: str | None = None, init_to: str | None = None,
                 init_sources: list | None = None):
        super().__init__(app.root)
        self.app = app
        self.edit_item = item
        self._dropped, self._dropped_root = [], None
        if init_sources and not item:
            self._dropped = [Path(x) for x in init_sources if Path(x).exists()]
            if len(self._dropped) > 1:
                try:
                    root = Path(os.path.commonpath([str(x) for x in self._dropped]))
                except ValueError:                 # different drives
                    root = self._dropped[0].parent
                self._dropped_root = root if root.is_dir() else self._dropped[0].parent
                init_src = str(self._dropped_root)
            elif self._dropped:
                init_src, self._dropped = str(self._dropped[0]), []
        self.title("Edit job" if item else "Add job")
        self.geometry("780x600")
        self.minsize(660, 420)
        self.configure(fg_color=BLACK)
        self.transient(app.root)
        self.lift()
        self.after(50, self.grab_set)

        settings = load_settings()
        self._defaults = dict(settings.get("job_dialog_defaults", {}) or {})
        self._defaults_kind = None
        self._kind = "none"
        self._games: list[Path] = []
        self._check_thread = None
        # 'folder' while walking it, 'archives' while checking them for PS4; Add waits for both
        self._reading, self._read_seq = "", 0

        src0 = init_src or ""
        if item is not None:
            src0 = str(getattr(item, "archive_path", None) or getattr(item, "path", "") or "")
        elif not src0:
            # pre-fill the last source: new downloads usually land in the same folder
            _last = str(settings.get("last_source", "") or "").strip()
            if _last and Path(_last).exists():
                src0 = _last
        self.src_var = tk.StringVar(value=src0)
        self._last_src_dir = native_path(str(settings.get("last_source_dir", "") or "").strip())
        self.detect_var = TextVar(value="Choose or drop a source.")
        self.help_var = TextVar(value=self._HELP_IDLE)
        self.sign_var = tk.BooleanVar(value=(bool(getattr(item, "chain_sign", False))
                                             or getattr(item, "operation", "") == "fake-sign") if item else False)
        self.lz4_var = tk.BooleanVar(value=bool(getattr(item, "chain_lz4", False) or getattr(item, "chain_lz4_unpack", False))
                                     if item else False)
        self.lz4_profile_var = tk.StringVar(value=str(getattr(item, "lz4_profile", "") or "") if item else "")
        self.lz4_note_var = TextVar(value="")
        self.lz4_mode_var = tk.StringVar(value="unpack" if (item and getattr(item, "chain_lz4_unpack", False)) else "pack")
        self._lz4_seen: dict[str, object] = {}  # folder: uses AMPR (True/False/None)
        self._archive_content: dict[str, str] = {}  # archive: what it holds
        self._detect_rest: tuple = ()
        self._auto_table = auto_actions(settings.get("auto_actions"))
        self.patch_on_var = tk.BooleanVar(value=bool(getattr(item, "patch_source", None)) if item else False)
        self.patch_var = tk.StringVar(value=str(getattr(item, "patch_source", "") or "") if item else "")
        self.ks_state_var = TextVar(value="")
        self.ps5_state_var = TextVar(value="")
        self._ps5_rows = None
        self._ks_default = None  # default keystone in the PS4 package (True/False/None)
        self.backport_on_var = tk.BooleanVar(value=bool(getattr(item, "backport_target", None)) if item else False)
        _bt0 = (getattr(item, "backport_target", None) if item else None) or settings.get("backport_target_default") or "7.61"
        self.backport_target_var = tk.StringVar(value=_bt0 if is_backport_target(_bt0) else "7.61")
        self._hint_cache: dict[str, str] = {}
        self.backport_hint_var = TextVar(value="")
        self.check_var = TextVar(value="")
        self.to_var = tk.StringVar(value=self._TARGET_LABEL.get(self._target_of(item) if item else "ffpfsc", ".ffpfsc"))
        out0 = str(getattr(item, "output_path", "") or "") if item else (app.output_var.get() or "").strip()
        self.out_var = tk.StringVar(value=out0)
        # PS4 and PS5 outputs are remembered per platform; a mixed folder shows both
        _r4 = app._recent_outputs("ps4")
        self._out_default = {"ps5": out0, "ps4": (_r4[0] if _r4 else out0)}
        self._platform = "ps5"
        self.ps4_out_var = tk.StringVar(value=self._out_default["ps4"])
        self.ps4_to_var = tk.StringVar(value="pkg")
        self._ps4_n = self._ps5_n = 0
        _org = getattr(item, "auto_organize", None) if item else None
        self.organize_var = tk.BooleanVar(value=bool(app.auto_organize_var.get()) if _org is None else bool(_org))
        _aj = _after_job_module()
        _af = getattr(item, "after_source", None) if item else app.after_source_var.get()
        self.after_var = tk.StringVar(value=_af if _af in _aj.ACTIONS else _aj.KEEP)
        _ad = (getattr(item, "after_move_to", None) if item else None) or app.after_move_dir_var.get()
        self.after_dir_var = tk.StringVar(value=str(_ad or "").strip())
        fp = app._fpkg_params_of(item) if item else dict(app.fpkg_defaults)
        self.retail_var = tk.BooleanVar(value=bool(fp.get("retail_normalize", True)))
        self.hdr_var = tk.StringVar(value=_hdr_mode(fp.get("hdr_flag", "auto")))
        self.regen_var = tk.BooleanVar(value=bool(fp.get("regen_playgo", False)))
        try:
            _lvl = int(fp.get("level", 7))
        except Exception:
            _lvl = 7
        self.pkg_level_var = tk.IntVar(value=max(-4, min(9, int(_lvl))))
        self._pkg_level_text = app._pkg_level_text
        _ff0 = getattr(item, "compression_level", None) if item else None
        try:
            _ff0 = int(_ff0) if _ff0 is not None else int(app.compression_level_var.get())
        except Exception:
            _ff0 = 7
        self.level_var = tk.IntVar(value=max(1, min(9, _ff0)))
        _ver0 = str(fp.get("version") or "")
        self.cid_var = tk.StringVar(value=str(fp.get("content_id") or ""))
        self.tid_var = tk.StringVar(value=str(fp.get("title_id") or ""))
        self.title_var = tk.StringVar(value=str(fp.get("title") or ""))
        self.ver_var = tk.StringVar(value="" if _ver0 == "01.000.000" else _ver0)
        self.inner_var = tk.StringVar(value=fp.get("inner") if fp.get("inner") in self._INNER else "kraken")
        self.back_var = tk.StringVar(value=fp.get("backend") if fp.get("backend") in self._BACKEND else "builtin")
        self._pkg_more = bool(item and (fp.get("content_id") or fp.get("title_id")))   # identity + codec rows
        self._ident_forced = False       # shown and required: a game folder without param.json
        self._auto_ident: dict = {}  # filled in from param.json, cleared on a new source
        self.summary_var = TextVar(value="")
        self.summary_dest_var = TextVar(value="")

        self._build()
        self.src_var.trace_add("write", lambda *_: self._on_source_changed())
        # LZ4 packs need an .exfat or folder: LZ4 on moves the output, other outputs turn it off
        self.lz4_var.trace_add("write", lambda *_: self._lz4_toggled())
        self.lz4_mode_var.trace_add("write", lambda *_: self._lz4_toggled())
        self.to_var.trace_add("write", lambda *_: self._lz4_output_changed())
        for v in (self.ps4_out_var, self.ps4_to_var, self.sign_var, self.patch_on_var, self.patch_var, self.backport_on_var,
                  self.backport_target_var, self.to_var, self.out_var,
                  self.organize_var, self.after_var, self.lz4_var, self.lz4_profile_var, self.lz4_mode_var):
            v.trace_add("write", lambda *_: self._refresh())
        self.bind("<Return>", lambda e: self._add())
        self.bind("<Escape>", lambda e: self.destroy())
        self._on_source_changed()
        if init_to in self._TARGET_LABEL:
            self.to_var.set(self._TARGET_LABEL[init_to])
        draft = getattr(app, "_job_draft", None)
        if draft and item is None and not init_src and not init_sources:
            self._restore_draft(draft)
        app._job_draft = None

    def _group(self, number: str, title: str, hint: str = ""):
        row = ctk.CTkFrame(self.body, fg_color=PANEL, corner_radius=8)
        row.pack(fill="x", padx=20, pady=5)
        head = ctk.CTkFrame(row, fg_color=PANEL); head.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(head, text=number, width=22, height=22, corner_radius=11, fg_color=ACCENT,
                      text_color=ON_ACCENT, font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkLabel(head, text=title, text_color=WHITE, font=ctk.CTkFont(size=13, weight="bold")
                      ).pack(side="left", padx=(8, 10))
        if hint:
            ctk.CTkLabel(head, text=hint, text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left")
        return row

    def _fit_to_content(self):
        """grows with its content until the user resizes it by hand"""
        try:
            if not self.winfo_exists():
                return
            self.update_idletasks()
            canvas = self.body._parent_canvas
            chrome = self.winfo_reqheight() - canvas.winfo_reqheight()
            need = chrome + self.body.winfo_reqheight() + 28
            limit = self.winfo_screenheight() - 120
            h = max(420, min(need, limit))
            last = getattr(self, "_fit_h", None)
            if last is not None and abs(self.winfo_height() - last) > 4:
                return                       # the user resized the window
            if last != h:
                self._fit_h = h
                self.geometry(f"{max(self.winfo_width(), 660)}x{h}")
        except Exception:
            pass

    def _build(self):
        head = "Edit job" if self.edit_item else "Add job"
        ctk.CTkLabel(self, text=head, font=ctk.CTkFont(size=17, weight="bold"), text_color=WHITE
                      ).pack(anchor="w", padx=20, pady=(16, 2))
        ctk.CTkLabel(self, text="Pick a source, what to change in it and what comes out.",
                      text_color=MUTED, wraplength=720, justify="left").pack(anchor="w", padx=20, pady=(0, 6))
        # pack last: filling an on-screen CTk scroll frame redraws the bar per widget (~0.4 s)
        self.body = ScrollFrame(self, fg_color=BLACK)
        if not self.LARGE:  # only a window fits its content
            self.after(60, self._fit_to_content)
            self.body.bind("<Configure>", lambda e: self.after_idle(self._fit_to_content), add="+")

        srow = self._group("1", "Source", "folder, parent folder, archive, disk image, .ffpfs, .ffpfsc or .pkg")
        sinner = ctk.CTkFrame(srow, fg_color=PANEL); sinner.pack(fill="x", padx=10, pady=(2, 2))
        self._src_entry = ctk.CTkEntry(sinner, textvariable=self.src_var, fg_color=CARD2, text_color=WHITE)
        self._src_entry.pack(side="left", fill="x", expand=True)
        # the keyboard goes to the editor, not to the queue list behind it
        self.after(60, lambda: self._src_entry.winfo_exists() and self._src_entry.focus_set())
        ctk.CTkButton(sinner, text="File…", width=76, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                       command=self._pick_file, border_width=1, border_color=BTN_BORDER).pack(side="left", padx=(6, 0))
        ctk.CTkButton(sinner, text="Folder…", width=88, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                       command=self._pick_folder, border_width=1, border_color=BTN_BORDER).pack(side="left", padx=(6, 0))
        dline = ctk.CTkFrame(srow, fg_color=PANEL); dline.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(dline, textvariable=self.detect_var, text_color=MUTED, font=ctk.CTkFont(size=12),
                      wraplength=560, justify="left").pack(side="left")
        self._look_btn = ctk.CTkButton(dline, text="Look inside…", width=120, height=24, fg_color="transparent",
                                       hover_color=CARD2, text_color=ACCENT, font=ctk.CTkFont(size=12),
                                       command=self._look_inside)
        self._bind_help(self._HELP["source"], sinner)
        self._bind_help(self._HELP["look"], self._look_btn)

        # options sit inside their own box, so they stay under their checkbox
        crow = self._crow = self._group("2", "Change the content", "optional · applied in this order")
        cin = ctk.CTkFrame(crow, fg_color=PANEL); cin.pack(fill="x", padx=10, pady=(2, 6))

        opt_font = ctk.CTkFont(size=13)
        opt_w = max(opt_font.measure(s) for s in ("Integrate a patch", "Backport", "Sign executables")) + 36

        def option(var, text, note, help_key):
            box = ctk.CTkFrame(cin, fg_color=PANEL); box.pack(fill="x", pady=(2, 2))
            line = ctk.CTkFrame(box, fg_color=PANEL); line.pack(fill="x")
            cb = ctk.CTkCheckBox(line, text=text, variable=var, checkbox_width=18, checkbox_height=18, width=opt_w,
                                 fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE, font=opt_font)
            cb.pack(side="left")
            nl = ctk.CTkLabel(line, text=note, text_color=MUTED, font=ctk.CTkFont(size=12))
            nl.pack(side="left", padx=(10, 0))
            opts = ctk.CTkFrame(box, fg_color=PANEL)              # packed by _refresh when checked
            ctk.CTkFrame(opts, width=2, height=1, fg_color=ACCENT_HOVER).pack(side="left", fill="y", padx=(8, 12), pady=2)   # height=1: a CTkFrame asks for 200 px otherwise
            inner = ctk.CTkFrame(opts, fg_color=PANEL); inner.pack(side="left", fill="x", expand=True)
            self._bind_help(self._HELP[help_key], line)
            return types.SimpleNamespace(box=box, line=line, cb=cb, note=nl, opts=opts, inner=inner)

        po = option(self.patch_on_var, "Integrate a patch", "merge an update (folder, .zip or .rar) into the game", "patch")
        self._patch_opts = po.opts
        pin = ctk.CTkFrame(po.inner, fg_color=PANEL); pin.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(pin, text="Patch:", text_color=MUTED, width=120, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
        ctk.CTkEntry(pin, textvariable=self.patch_var, fg_color=CARD2, text_color=WHITE).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(pin, text="File…", width=64, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                       command=self._pick_patch_file, border_width=1, border_color=BTN_BORDER).pack(side="left", padx=(6, 0))
        ctk.CTkButton(pin, text="Folder…", width=76, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                       command=self._pick_patch_folder, border_width=1, border_color=BTN_BORDER).pack(side="left", padx=(6, 0))
        self._bind_help(self._HELP["patch"], pin)

        bo = option(self.backport_on_var, "Backport", "lower the SDK so the game runs on an older firmware", "backport")
        self._backport_opts = bo.opts
        b1 = ctk.CTkFrame(bo.inner, fg_color=PANEL); b1.pack(fill="x", pady=(2, 2))
        ctk.CTkLabel(b1, text="Target firmware:", text_color=MUTED, width=120, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
        self._target_menu = ctk.CTkOptionMenu(b1, values=self._backport_targets(), variable=self.backport_target_var,
                                              width=92, height=26, dynamic_resizing=False)
        self._target_menu.pack(side="left")
        ctk.CTkLabel(b1, textvariable=self.backport_hint_var, text_color=MUTED, font=ctk.CTkFont(size=12),
                      wraplength=380, justify="left").pack(side="left", padx=(10, 0))
        self._bind_help(self._HELP["target"], b1)
        b3 = ctk.CTkFrame(bo.inner, fg_color=PANEL); b3.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(b3, text="Compatibility:", text_color=MUTED, width=120, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
        self._check_btn = ctk.CTkButton(b3, text="Check", width=72, height=26, fg_color=BTN, hover_color=BTN_HOVER,
                                        text_color=WHITE, command=self._check_compat, border_width=1, border_color=BTN_BORDER)
        self._check_btn.pack(side="left")
        ctk.CTkLabel(b3, textvariable=self.check_var, text_color=MUTED, font=ctk.CTkFont(size=12),
                      wraplength=440, justify="left").pack(side="left", padx=(10, 0))
        self._bind_help(self._HELP["check"], b3)

        # the .pkg builder always signs, so a fixed line replaces the checkbox there
        so = option(self.sign_var, "Sign executables", "fake-sign eboot.bin and every prx/sprx", "sign")
        self._sign_cb, self._sign_note_lbl, self._sign_line = so.cb, so.note, so.line
        self._sign_fixed = ctk.CTkLabel(so.line, text="Signed by the package builder - every executable in a .pkg is fake-signed",
                                        text_color=MUTED, font=ctk.CTkFont(size=12))

        # LZ4 last, after the other changes
        lo = option(self.lz4_var, "LZ4 asset packs", "smaller and quicker from USB, for games that use AMPR", "lz4")
        self._lz4_opts, self._lz4_cb = lo.opts, lo.cb
        l0 = ctk.CTkFrame(lo.inner, fg_color=PANEL); l0.pack(fill="x", pady=(2, 2))
        ctk.CTkLabel(l0, text="Action:", text_color=MUTED, width=120, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
        _modes = {"pack": "Pack", "unpack": "Unpack"}
        self._lz4_mode_seg = ctk.CTkSegmentedButton(
            l0, values=list(_modes.values()), selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            command=lambda v: self.lz4_mode_var.set(next(k for k, lbl in _modes.items() if lbl == v)))
        self._lz4_mode_seg.set(_modes[self.lz4_mode_var.get()])
        self._lz4_mode_seg.pack(side="left")
        self._lz4_modes = _modes
        l1 = self._lz4_profile_row = ctk.CTkFrame(lo.inner, fg_color=PANEL); l1.pack(fill="x", pady=(2, 2))
        ctk.CTkLabel(l1, text="Profile:", text_color=MUTED, width=120, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
        ctk.CTkEntry(l1, textvariable=self.lz4_profile_var, fg_color=CARD2, text_color=WHITE,
                     placeholder_text=tr("empty: the careful default")).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(l1, text="File…", width=64, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      command=self._pick_lz4_profile, border_width=1, border_color=BTN_BORDER).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(lo.inner, textvariable=self.lz4_note_var, text_color=MUTED, font=ctk.CTkFont(size=12),
                     wraplength=560, justify="left", anchor="w").pack(fill="x", pady=(0, 4))
        self._bind_help(self._HELP["lz4"], l1)
        # warns when the user's own game folder is changed in place, not a copy
        self._own_note = ctk.CTkLabel(cin, text="", text_color=YELLOW, font=ctk.CTkFont(size=12),
                                      wraplength=640, justify="left", anchor="w")

        # PS4 package: keystone and version, read in the background; PS4 PKG Tools repairs
        kb = self._ks_box = ctk.CTkFrame(cin, fg_color=PANEL)
        kl = ctk.CTkFrame(kb, fg_color=PANEL); kl.pack(fill="x")
        ctk.CTkLabel(kl, text="Package", text_color=WHITE, width=opt_w, anchor="w", font=opt_font).pack(side="left")
        self._ks_note = ctk.CTkLabel(kl, textvariable=self.ks_state_var, text_color=MUTED, font=ctk.CTkFont(size=12),
                                     wraplength=420, justify="left", anchor="w")
        self._ks_note.pack(side="left", padx=(10, 0), fill="x", expand=True)
        ctk.CTkButton(kl, text="PS4 PKG Tools…", width=140, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=self._open_keystone).pack(side="right")
        self._bind_help(self._HELP["keystone"], kl)
        self._ps5_boxes = (po.box, bo.box, so.box, lo.box)
        pb = self._ps5r_box = ctk.CTkFrame(cin, fg_color=PANEL)
        pl = ctk.CTkFrame(pb, fg_color=PANEL); pl.pack(fill="x")
        ctk.CTkLabel(pl, text="Package", text_color=WHITE, width=opt_w, anchor="w", font=opt_font).pack(side="left")
        self._ps5_note = ctk.CTkLabel(pl, textvariable=self.ps5_state_var, text_color=MUTED, font=ctk.CTkFont(size=12),
                                      wraplength=420, justify="left", anchor="w")
        self._ps5_note.pack(side="left", padx=(10, 0), fill="x", expand=True)
        ctk.CTkButton(pl, text="PS5 PKG Tools…", width=140, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=self._open_ps5_repair).pack(side="right")
        self._bind_help(self._HELP["ps5repair"], pl)

        orow = self._orow = self._group("3", "Output")
        self._ps5_head_var = TextVar(value="")
        self._ps5_head = ctk.CTkLabel(orow, textvariable=self._ps5_head_var, text_color=WHITE, anchor="w",
                                      font=ctk.CTkFont(size=12, weight="bold"))
        oin = ctk.CTkFrame(orow, fg_color=PANEL); oin.pack(fill="x", padx=10, pady=(2, 2))
        self._to_seg = ctk.CTkSegmentedButton(oin, values=[self._TARGET_LABEL[k] for k in self._TARGETS],
                                              variable=self.to_var, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER)
        self._to_seg.pack(side="left")
        self._to_hint = TextVar(value="")
        ctk.CTkLabel(oin, textvariable=self._to_hint, text_color=MUTED, font=ctk.CTkFont(size=12),
                      wraplength=420, justify="left").pack(side="left", padx=(10, 0))
        self._bind_help(self._HELP["output"], oin)
        self._oin = oin
        # one row for both: zlib level for .ffpfsc, Kraken preset for .pkg
        cr = self._comp_row = ctk.CTkFrame(orow, fg_color=PANEL)
        ctk.CTkLabel(cr, text="Compression:", text_color=MUTED, width=100, anchor="w",
                      font=ctk.CTkFont(size=12)).pack(side="left")
        self._comp_ffpfsc = ctk.CTkFrame(cr, fg_color=PANEL)
        ctk.CTkSlider(self._comp_ffpfsc, from_=1, to=9, number_of_steps=8, variable=self.level_var, width=180,
                      height=16, fg_color=BORDER2, progress_color=ACCENT, button_color=ACCENT,
                      button_hover_color=ACCENT_HOVER).pack(side="left")
        self._level_lbl = ctk.CTkLabel(self._comp_ffpfsc, text=tr("level {n}", n=self.level_var.get()), text_color=WHITE,
                                       width=56, anchor="w", font=ctk.CTkFont(size=12))
        self._level_lbl.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(self._comp_ffpfsc, text="higher: smaller file, slower build", text_color=MUTED,
                      font=ctk.CTkFont(size=12)).pack(side="left", padx=(4, 0))
        self.level_var.trace_add("write", lambda *_: self._level_lbl.winfo_exists()
                                 and self._level_lbl.configure(text=tr("level {n}", n=self.level_var.get())))
        self._comp_pkg = ctk.CTkFrame(cr, fg_color=PANEL)
        ctk.CTkSlider(self._comp_pkg, from_=-4, to=9, number_of_steps=13, variable=self.pkg_level_var, width=180,
                      height=16, fg_color=BORDER2, progress_color=ACCENT, button_color=ACCENT,
                      button_hover_color=ACCENT_HOVER).pack(side="left")
        self._pkg_level_lbl = ctk.CTkLabel(self._comp_pkg, text=tr("level {n}", n=self.pkg_level_var.get()), text_color=WHITE,
                                           width=56, anchor="w", font=ctk.CTkFont(size=12))
        self._pkg_level_lbl.pack(side="left", padx=(8, 0))
        self._pkg_level_hint = ctk.CTkLabel(self._comp_pkg, text=self._pkg_level_text(self.pkg_level_var.get()),
                                            text_color=MUTED, font=ctk.CTkFont(size=12))
        self._pkg_level_hint.pack(side="left", padx=(4, 0))

        def _pkg_level_cb(*_):
            if not self._pkg_level_lbl.winfo_exists():
                return
            try:
                v = int(self.pkg_level_var.get())
            except (TypeError, ValueError):
                return
            self._pkg_level_lbl.configure(text=tr("level {n}", n=v))
            self._pkg_level_hint.configure(text=self._pkg_level_text(v))
        self.pkg_level_var.trace_add("write", _pkg_level_cb)
        self._bind_help(self._HELP["compression"], cr)
        fin = ctk.CTkFrame(orow, fg_color=PANEL); fin.pack(fill="x", padx=10, pady=(4, 2))
        ctk.CTkLabel(fin, text="Save to:", text_color=MUTED, width=100, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
        self._out_entry = ctk.CTkComboBox(fin, variable=self.out_var, values=self.app._recent_outputs("ps5") or [self._out_default["ps5"]],
                                          fg_color=CARD2, text_color=WHITE, border_color=BTN_BORDER,
                                          button_color=BTN, button_hover_color=BTN_HOVER)
        self._out_entry.pack(side="left", fill="x", expand=True)
        self._out_btn = ctk.CTkButton(fin, text="Folder…", width=76, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                                      command=self._pick_out, border_width=1, border_color=BTN_BORDER)
        self._out_btn.pack(side="left", padx=(6, 0))
        self._bind_help(self._HELP["saveto"], fin)
        self._fin = fin
        # PS4 packages in a mixed folder get their own target and folder
        pb4 = self._ps4_box = ctk.CTkFrame(orow, fg_color=PANEL)
        self._ps4_head_var = TextVar(value="")
        ctk.CTkLabel(pb4, textvariable=self._ps4_head_var, text_color=WHITE, anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(fill="x", padx=10, pady=(8, 2))
        p4t = ctk.CTkFrame(pb4, fg_color=PANEL); p4t.pack(fill="x", padx=10, pady=(2, 2))
        self._PS4_TARGETS = dict(self._PS4_LABEL)
        self._ps4_seg = ctk.CTkSegmentedButton(
            p4t, values=list(self._PS4_TARGETS.values()), selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            command=lambda v: self.ps4_to_var.set(next(k for k, lb in self._PS4_TARGETS.items() if lb == v)))
        self._ps4_seg.set(self._PS4_TARGETS["pkg"])
        self._ps4_seg.pack(side="left")
        ctk.CTkLabel(p4t, text="PS4 packages are sorted as they are; the changes above are for PS5 games",
                     text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=420, justify="left"
                     ).pack(side="left", padx=(10, 0))
        p4f = ctk.CTkFrame(pb4, fg_color=PANEL); p4f.pack(fill="x", padx=10, pady=(4, 2))
        ctk.CTkLabel(p4f, text="Save to:", text_color=MUTED, width=100, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._ps4_out_entry = ctk.CTkComboBox(p4f, variable=self.ps4_out_var,
                                              values=self.app._recent_outputs("ps4") or [self._out_default["ps4"]],
                                              fg_color=CARD2, text_color=WHITE, border_color=BTN_BORDER,
                                              button_color=BTN, button_hover_color=BTN_HOVER)
        self._ps4_out_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(p4f, text="Folder…", width=76, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      command=self._pick_ps4_out, border_width=1, border_color=BTN_BORDER).pack(side="left", padx=(6, 0))
        self._bind_help(self._HELP["ps4out"], pb4)
        # height=1: an empty CTkFrame asks for 200 px, because why not
        oline = ctk.CTkFrame(orow, fg_color=PANEL, height=1); oline.pack(fill="x", padx=10, pady=(2, 6))
        self._organize_cb = ctk.CTkCheckBox(oline, text="Auto-organize - folder and file named from the game's own metadata",
                                            variable=self.organize_var, checkbox_width=18, checkbox_height=18,
                                            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE, font=ctk.CTkFont(size=12))
        self._organize_cb.pack(side="left")
        self._bind_help(self._HELP["organize"], self._organize_cb)
        self._oline = oline
        aj = _after_job_module()
        self._after_box = ab = ctk.CTkFrame(orow, fg_color=PANEL)
        ar = self._after_row = ctk.CTkFrame(ab, fg_color=PANEL); ar.pack(fill="x")
        # mixed folder: one choice for both platforms
        self._after_head = ctk.CTkLabel(ab, text="PS4 and PS5", text_color=WHITE, anchor="w",
                                        font=ctk.CTkFont(size=12, weight="bold"))
        ctk.CTkLabel(ar, text="After the job:", text_color=MUTED, width=100, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self._after_seg = ctk.CTkSegmentedButton(
            ar, values=[aj.LABELS[k] for k in aj.ACTIONS], selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            height=24, command=lambda v: self.after_var.set(next(k for k in aj.ACTIONS if aj.LABELS[k] == v)))
        self._after_seg.set(aj.LABELS[self.after_var.get()])
        self._after_seg.pack(side="left")
        self._bind_help(self._HELP["after"], ar)
        self._after_dir_row = ctk.CTkFrame(ab, fg_color=PANEL)
        ctk.CTkLabel(self._after_dir_row, text="Move to:", text_color=MUTED, width=100, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        ctk.CTkEntry(self._after_dir_row, textvariable=self.after_dir_var, fg_color=CARD2,
                     text_color=WHITE).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(self._after_dir_row, text="Folder…", width=76, fg_color=BTN, hover_color=BTN_HOVER,
                      text_color=WHITE, border_width=1, border_color=BTN_BORDER,
                      command=self._pick_after_dir).pack(side="left", padx=(6, 0))
        self._after_note = ctk.CTkLabel(ab, text=tr("Deleted for good once the job is Done, with its download folder when "
                                                    "the add-ons went along; it does not go to the {TRASH_NAME}.", TRASH_NAME=TRASH_NAME),
                                        text_color=RED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                                        wraplength=620)
        self._pkg_opts = ctk.CTkFrame(orow, fg_color=PANEL)
        ctk.CTkFrame(self._pkg_opts, width=2, height=1, fg_color=ACCENT_HOVER).pack(side="left", fill="y", padx=(10, 12), pady=2)
        pk = ctk.CTkFrame(self._pkg_opts, fg_color=PANEL); pk.pack(side="left", fill="x", expand=True)
        p1 = ctk.CTkFrame(pk, fg_color=PANEL); p1.pack(fill="x", pady=(2, 2))
        ctk.CTkLabel(p1, text=".pkg options:", text_color=MUTED, width=100, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
        _rc = ctk.CTkCheckBox(p1, text="Retail fixes", variable=self.retail_var, checkbox_width=18, checkbox_height=18,
                              fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE, font=ctk.CTkFont(size=12))
        _rc.pack(side="left", padx=(0, 18)); self._bind_help(self._HELP["retail"], _rc)
        _pc = ctk.CTkCheckBox(p1, text="Rebuild PlayGo", variable=self.regen_var, checkbox_width=18, checkbox_height=18,
                              fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE, font=ctk.CTkFont(size=12))
        _pc.pack(side="left", padx=(0, 18)); self._bind_help(self._HELP["playgo"], _pc)
        p2 = ctk.CTkFrame(pk, fg_color=PANEL); p2.pack(fill="x", pady=(2, 6))
        ctk.CTkLabel(p2, text="", width=100).pack(side="left")
        _h1 = ctk.CTkFrame(p2, fg_color=PANEL); _h1.pack(side="left")
        ctk.CTkLabel(_h1, text="HDR:", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
        ctk.CTkSegmentedButton(_h1, values=["auto", "on", "off"], variable=self.hdr_var, selected_color=ACCENT,
                                selected_hover_color=ACCENT_HOVER, height=24).pack(side="left")
        self._bind_help(self._HELP["hdr"], _h1)
        p3 = ctk.CTkFrame(pk, fg_color=PANEL); p3.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(p3, text="", width=100).pack(side="left")
        self._pkg_more_btn = ctk.CTkButton(p3, text="More options…", width=120, height=24, fg_color="transparent",
                                           hover_color=CARD2, text_color=ACCENT, font=ctk.CTkFont(size=12),
                                           anchor="w", command=self._toggle_pkg_more)
        self._pkg_more_btn.pack(side="left")
        ib = self._pkg_more_box = ctk.CTkFrame(pk, fg_color=PANEL)        # packed by _layout_pkg_more
        self._ident_head = TextVar(value=self._IDENT_OPTIONAL)
        ctk.CTkLabel(ib, textvariable=self._ident_head, text_color=WHITE, font=ctk.CTkFont(size=12), anchor="w",
                      justify="left", wraplength=600).pack(fill="x", pady=(2, 4))
        ig = ctk.CTkFrame(ib, fg_color=PANEL); ig.pack(fill="x")
        ig.grid_columnconfigure(1, weight=3); ig.grid_columnconfigure(3, weight=2)

        def _cell(r, c, label, var, hint):
            ctk.CTkLabel(ig, text=label, text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w").grid(
                row=r, column=c, sticky="w", padx=(0, 6), pady=2)
            ctk.CTkEntry(ig, textvariable=var, fg_color=CARD2, text_color=WHITE, height=26,
                         placeholder_text=hint).grid(row=r, column=c + 1, sticky="ew", padx=(0, 14), pady=2)
        _cell(0, 0, "Content ID", self.cid_var, "UP9000-PPSA12345_00-GAMENAME00000000")
        _cell(0, 2, "Title ID", self.tid_var, "PPSA12345")
        _cell(1, 0, "Title", self.title_var, "")
        _cell(1, 2, "Version", self.ver_var, "01.000.000")
        self._ident_note = TextVar(value="")
        ctk.CTkLabel(ib, textvariable=self._ident_note, text_color=MUTED, font=ctk.CTkFont(size=11), anchor="w",
                      justify="left", wraplength=600).pack(fill="x", pady=(2, 6))
        cr = ctk.CTkFrame(ib, fg_color=PANEL); cr.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(cr, text="Codec layer:", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
        ctk.CTkSegmentedButton(cr, values=list(self._INNER), variable=self.inner_var, selected_color=ACCENT,
                                selected_hover_color=ACCENT_HOVER, height=24).pack(side="left")
        self._bind_help(self._HELP["codec"], cr)
        self._back_row = None
        if sys.platform == "win32":  # LibProsperoPkg refuses it elsewhere
            br = self._back_row = ctk.CTkFrame(ib, fg_color=PANEL); br.pack(fill="x", pady=(0, 4))
            ctk.CTkLabel(br, text="Kraken backend:", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
            ctk.CTkSegmentedButton(br, values=list(self._BACKEND), variable=self.back_var, selected_color=ACCENT,
                                    selected_hover_color=ACCENT_HOVER, height=24).pack(side="left")
        self._bind_help(self._HELP["identity"], ig)

        # footer lives outside the scroll area, always visible
        foot = self._foot = ctk.CTkFrame(self, fg_color=BLACK); foot.pack(fill="x", padx=20, pady=(0, 14))
        ctk.CTkFrame(foot, height=1, corner_radius=0, fg_color=BORDER).pack(fill="x", pady=(0, 10))
        # help box fits the longest hover text, so the buttons never jump
        help_font = ctk.CTkFont(size=12)
        help_box = ctk.CTkFrame(foot, fg_color=BLACK, corner_radius=0,
                                height=3 * help_font.metrics("linespace") + 4)
        help_box.pack(fill="x", pady=(0, 8))
        help_box.pack_propagate(False)
        help_lbl = ctk.CTkLabel(help_box, textvariable=self.help_var, text_color=MUTED, font=help_font,
                                justify="left", anchor="nw")
        help_lbl.pack(fill="both", expand=True)
        help_texts = [self._HELP_IDLE, *self._HELP.values()]
        row = ctk.CTkFrame(foot, fg_color=BLACK); row.pack(fill="x")
        btns = ctk.CTkFrame(row, fg_color=BLACK); btns.pack(side="right")
        self._add_btn = ctk.CTkButton(btns, text="Save changes" if self.edit_item else "Add to queue",
                                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT,
                                      font=ctk.CTkFont(size=13), command=self._add)
        self._add_btn.pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Cancel", fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                       command=self.destroy, border_width=1, border_color=BTN_BORDER).pack(side="right")
        info = ctk.CTkFrame(row, fg_color=BLACK); info.pack(side="left", fill="x", expand=True, padx=(0, 16))
        sum_lbl = ctk.CTkLabel(info, textvariable=self.summary_var, text_color=WHITE, font=ctk.CTkFont(size=13, weight="bold"),
                               justify="left", anchor="w")
        sum_lbl.pack(anchor="w")
        dest_lbl = ctk.CTkLabel(info, textvariable=self.summary_dest_var, text_color=MUTED, font=ctk.CTkFont(size=12),
                                justify="left", anchor="w")
        dest_lbl.pack(anchor="w")

        def _wrap(_e=None, width=None):
            try:
                width = width or max(280, foot.winfo_width() - 8)
                help_lbl.configure(wraplength=width)
                lines = max(_wrapped_line_count(help_font, tr(text), width) for text in help_texts)
                height = lines * help_font.metrics("linespace") + 4
                if int(float(help_box.cget("height"))) != height:
                    help_box.configure(height=height)
                for lbl in (sum_lbl, dest_lbl):
                    lbl.configure(wraplength=max(200, info.winfo_width() - 8))
            except tk.TclError:
                pass
        self._help_wrap = _wrap          # also called by the GUI tests with a fixed width
        foot.bind("<Configure>", _wrap, add="+")
        info.bind("<Configure>", _wrap, add="+")
        # 1 px inset, or the scroll area paints over the panel border
        self.body.pack(fill="both", expand=True, padx=1, before=foot)

    def _bind_help(self, text: str, *widgets):
        """CTk widgets are composites, so every descendant gets the binding"""
        def _show(_e=None):
            self.help_var.set(text)
        def _leave(_e=None):
            try:
                w = widgets[0]
                x, y = self.winfo_pointerxy()
                inside = (w.winfo_rootx() <= x < w.winfo_rootx() + w.winfo_width()
                          and w.winfo_rooty() <= y < w.winfo_rooty() + w.winfo_height())
            except Exception:
                inside = False
            if not inside:
                self.help_var.set(self._HELP_IDLE)
        def _walk(w):
            try:
                w.bind("<Enter>", _show, add="+"); w.bind("<Leave>", _leave, add="+")
                if isinstance(w, tk.Entry):      # typing in a field shows its help too
                    w.bind("<FocusIn>", _show, add="+")
            except Exception:
                pass
            for c in w.winfo_children():
                _walk(c)
        for w in widgets:
            _walk(w)

    def _initial_dir(self) -> str | None:
        """the folder of the current source, else of the last one"""
        for cand in (self.src_var.get().strip(), self._last_src_dir):
            if not cand:
                continue
            path = Path(cand)
            try:
                parent = path if path.is_dir() else path.parent
                if parent.is_dir():
                    return str(parent)
            except OSError:
                continue
        return None

    def _remember_source(self, p: str) -> None:
        self.src_var.set(p)
        try:
            path = Path(p)
            self._last_src_dir = str(path if path.is_dir() else path.parent)
            save_settings({"last_source": p, "last_source_dir": self._last_src_dir})
        except Exception:
            pass

    def _pick_file(self):
        p = filedialog.askopenfilename(parent=self, title="Choose a source file", initialdir=self._initial_dir(),
                                       filetypes=[("PS5 sources", "*.ffpfsc *.ffpfs *.pkg *.exfat *.ffpkg *.zip *.rar *.7z"),
                                                  ("All files", "*.*")])
        if p:
            self._remember_source(p)

    def _pick_folder(self):
        p = filedialog.askdirectory(parent=self, title="Choose a game folder or a parent folder of games",
                                    initialdir=self._initial_dir())
        if p:
            self._remember_source(p)

    def _pick_patch_file(self):
        p = filedialog.askopenfilename(parent=self, title="Choose the patch archive",
                                       filetypes=[("Patch archives", "*.zip *.rar"), ("All files", "*.*")])
        if p:
            self.patch_var.set(p)

    def _probe_keystone(self, p: Path, raw: str):
        """reads off the main thread; the note shows only if the source is still p"""
        self._ks_default = None
        self.ks_state_var.set("")
        if not (p.is_file() and p.suffix.lower() == ".pkg"):
            return
        box: dict = {}

        def work():
            box["vp"] = self.app._ps4_version_problem(p)
            box["v"] = self.app._ps4_keystone_state(p)

        def poll():
            try:
                if not self.winfo_exists() or (self.src_var.get() or "").strip() != raw:
                    return
            except Exception:
                return
            if "v" not in box:
                self.after(80, poll); return
            st, vp = box["v"], box.get("vp")
            bad = None if st is None else bool(st["default"] or not st.get("valid", True))
            self._ks_default = bad
            if bad:
                self.detect_var.set(self._ps4_detect_text(p) + " · " + tr("default keystone"))
            text = ("" if st is None else
                    tr("This package has the default keystone: saves of the retail game will show "
                       "as corrupted. Replace it with the keystone exported from one of your saves.")
                    if st["default"] else tr("The keystone is damaged.") if not st.get("valid", True)
                    else tr("Keystone {fp}…, not the default one.", fp=st["fingerprint"][:8]))
            if st is not None and vp:
                text += " " + version_note(vp)
            self.ks_state_var.set(text)
            try:
                self._ks_note.configure(text_color=YELLOW if bad or (vp and vp["named"]) else MUTED)
            except Exception:
                pass
            self._refresh()
        threading.Thread(target=work, daemon=True).start()
        self.after(80, poll)

    def _probe_ps5(self, p: Path, raw: str):
        """checks run off the main thread; the note shows only if the source is still p"""
        box: dict = {}
        fw = _ps5_console_fw()

        def work():
            try:
                m = _ps5_repair_module()
                info = m.examine(p)
                box["v"] = m.findings(info, fw) if info.get("results") else None
            except Exception:
                box["v"] = None

        def poll():
            try:
                if not self.winfo_exists() or (self.src_var.get() or "").strip() != raw:
                    return
            except Exception:
                return
            if "v" not in box:
                self.after(80, poll); return
            rows = box["v"]
            self._ps5_rows = rows
            if rows is not None:
                bad = any(r["level"] in ("fail", "warn") for r in rows)
                self.ps5_state_var.set(_ps5_check_text(rows))
                try:
                    self._ps5_note.configure(text_color=YELLOW if bad else MUTED)
                except Exception:
                    pass
            self._refresh()
        threading.Thread(target=work, daemon=True).start()
        self.after(80, poll)

    def _open_ps5_repair(self):
        """Hand the package to PS5 PKG Tools (closes this editor)."""
        src = (self.src_var.get() or "").strip()
        self.app._show_view("ps5repair")
        self.app._ps5_view.preset(src)

    def _keystone_shown(self) -> bool:
        return self._kind == "ps4" and self._ks_default is not None

    def _open_keystone(self):
        """Hand the package to PS4 PKG Tools (closes this editor)."""
        src = (self.src_var.get() or "").strip()
        self.app._show_view("keystone")
        self.app._keystone_view.preset(src)

    def _pick_lz4_profile(self):
        p = filedialog.askopenfilename(parent=self, title="Choose an LZ4 pack profile",
                                       filetypes=[("Pack profiles", "*.toml"), ("All files", "*.*")])
        if p:
            self.lz4_profile_var.set(p)

    def _lz4_packs(self) -> bool:
        return self.lz4_var.get() and self.lz4_mode_var.get() == "pack"

    def _lz4_toggled(self):
        # packing goes into an .exfat or a folder; unpacking into anything
        if self._lz4_packs() and self._to_key() not in ultra_core.LZ4_TARGETS:
            self.to_var.set(self._TARGET_LABEL["exfat"])

    def _lz4_output_changed(self):
        if self._lz4_packs() and self._to_key() not in ultra_core.LZ4_TARGETS:
            self.lz4_var.set(False)

    def _lz4_source_packed(self) -> bool:
        raw = (self.src_var.get() or "").strip()
        return self._kind == "folder" and bool(raw) and (Path(raw) / "ampr_assets.index").is_file()

    def _lz4_state(self) -> object:
        """True/False from a folder's eboot.bin, None when only the job can tell"""
        raw = (self.src_var.get() or "").strip()
        if self._kind != "folder" or not raw:
            return None
        if raw not in self._lz4_seen:
            try:
                _backport_module()
                import lz4_packs
                self._lz4_seen[raw] = lz4_packs.uses_ampr(Path(raw))
            except Exception:
                self._lz4_seen[raw] = None
        return self._lz4_seen[raw]

    def _pick_patch_folder(self):
        p = filedialog.askdirectory(parent=self, title="Choose the patch folder")
        if p:
            self.patch_var.set(p)

    def _pick_out(self):
        p = filedialog.askdirectory(parent=self, title="Choose the output folder for this job")
        if p:
            self.out_var.set(p)

    def _pick_ps4_out(self):
        p = filedialog.askdirectory(parent=self, title="Choose the output folder for the PS4 packages")
        if p:
            self.ps4_out_var.set(p)

    @staticmethod
    def _ps5_named(archive: Path, root: Path) -> bool:
        """a PS5 id and no PS4 id in the archive's name or the folders down from the source"""
        try:
            names = [root.name, *archive.parent.relative_to(root).parts, archive.name]
        except ValueError:
            names = [archive.parent.name, archive.name]
        text = " ".join(names)
        return bool(re.search(r"PPSA\d{5}", text, re.I)) and not re.search(r"CUSA\d{5}", text, re.I)

    def _probe_parent_archives(self, sources, root: Path) -> None:
        """off the main thread; Add waits until every archive is checked"""
        archives = [s for s in sources if is_archive_file(s)]
        if not archives:
            return
        # a PS5 id says no PS4 packages; their listing is read ahead for Add instead
        later = [a for a in archives if self._ps5_named(a, root)]
        archives = [a for a in archives if a not in later]
        seq = self._read_seq
        pw = self.app._candidate_passwords()
        box: dict = {}

        def stop():
            return seq != self._read_seq or self._gone

        def ahead():
            for a in later:
                if stop():
                    return
                try:
                    ArchiveExtractor.member_entries(a, pw)
                except Exception:
                    pass
        if not archives:
            threading.Thread(target=ahead, daemon=True).start()
            return
        count = [0, len(archives)]  # own counter: an older check may still finish one
        self._reading, self._read_count = "archives", count

        def work():
            found = set()
            for a in archives:
                if stop():
                    return
                try:
                    if ArchiveExtractor.ps4_archive_info(a, pw) is not None:
                        found.add(str(a))
                except Exception:
                    pass
                count[0] += 1
            box["v"] = found
            ahead()

        def poll():                       # Tk is touched on the main thread only
            if stop():
                return
            if "v" not in box:
                self.summary_var.set(self._reading_text())
                self.after(120, poll); return
            self._reading = ""
            self._ps4_set |= box["v"]
            self._ps4_n = len(self._ps4_set)
            self._ps5_n = len(self._games) - self._ps4_n
            self.detect_var.set(self._parent_text())
            self._refresh()
        threading.Thread(target=work, daemon=True).start()
        self.after(120, poll)

    def _reading_text(self) -> str:
        if self._reading == "folder":
            return tr("Reading the folder…")
        done, total = self._read_count
        return tr("Checking the archives for PS4 packages: {i} of {n}…", i=done, n=total) + reading_note()

    def _remember_outputs(self, out: str) -> None:
        if out:
            self.app._remember_output("ps5", out)
        route = getattr(self, "_route", None)
        if route and route[0]:
            self.app._remember_output("ps4", route[0])

    def _drop_shown(self) -> bool:
        try:
            return bool(self._dropped) and Path((self.src_var.get() or "").strip()) == self._dropped_root
        except (TypeError, ValueError):
            return False

    def _mixed(self) -> bool:
        """PS4 and PS5 side by side; dropped PS4 packages count as mixed even alone"""
        return self._kind == "parent" and self._ps4_n > 0 and (self._ps5_n > 0 or self._drop_shown())

    def _show_platform(self, platform: str) -> None:
        """keeps a folder the user picked, else switches to the platform's last one"""
        if platform == self._platform:
            return
        cur = (self.out_var.get() or "").strip()
        if self.edit_item is None and cur in ("", self._out_default[self._platform]):
            self.out_var.set(self._out_default[platform])
        self._platform = platform
        try:
            self._out_entry.configure(values=self.app._recent_outputs(platform) or [self._out_default[platform]])
        except Exception:
            pass

    def _look_inside(self):
        p = Path((self.src_var.get() or "").strip())
        if p.is_file():
            self.app.open_pfs_browser(str(p))      # a view: this panel closes

    def _auto_hint(self) -> str:
        acts = self._auto_table
        name = {"folder": tr("Folder")}
        pairs = ", ".join(f"{name.get(k, '.' + k)} → {tr(self._TARGET_LABEL.get(v, v))}" for k, v in acts.items())
        text = tr("each source as Settings › Compression sets it: {pairs}", pairs=pairs)
        if ORGANIZE_TARGET in acts.values():
            text += " " + tr("Organize leaves a source as it is: the changes above apply to the sources that are built.")
        return text

    def _to_key(self) -> str:
        return self._TARGET_KEY.get(self.to_var.get(), "ffpfsc")

    @staticmethod
    def _sdk_text(folder: Path) -> str:
        """'SDK 8.00' from eboot's SCE param segment, 'encrypted eboot', or ''"""
        try:
            eboot = folder / "eboot.bin"
            if not eboot.is_file():
                return ""
            _bp = _backport_module()
            words = _bp.sdk_words_of_file(eboot)
            if words:
                return f"SDK {_bp.sdk_firmware(words[0])}"
            return "encrypted eboot" if _bp.is_encrypted_self(eboot) else ""
        except Exception:
            return ""

    def _fw_root(self) -> Path | None:
        raw = (self.app.fw_libs_var.get() or "").strip()
        return Path(raw) if raw and Path(raw).is_dir() else None

    def _backport_targets(self) -> list[str]:
        """7.61, 6.02, 10.xx plus every folder under the firmware libraries"""
        try:
            names = _backport_module().available_targets(self._fw_root())
        except Exception:
            names = list(self._BACKPORT_HINTS)
        cur = self.backport_target_var.get()
        if is_backport_target(cur) and cur not in names:
            names.append(cur)            # a job made with a firmware folder that is gone
        return names

    def _backport_hint(self, target: str) -> str:
        if target in self._BACKPORT_HINTS:
            return self._BACKPORT_HINTS[target]
        if target not in self._hint_cache:
            fw = self._fw_root()
            try:
                bp = _backport_module()
                problem = (bp.firmware_problem(bp.firmware_folder(fw, target), target) if fw
                           else "no firmware libraries folder set in Settings")
            except Exception as e:
                problem = str(e)
            self._hint_cache[target] = (tr("{target}: cannot be used, {problem}", target=target, problem=problem) if problem else
                                        tr("{target}: SDK values from your {target} libraries; Check shows whether patched "
                                          "libraries are needed", target=target))
        return self._hint_cache[target]

    def _probe_ps4_archive(self, p: Path, raw: str):
        """off the main thread; switches to the PS4 view only if the source is unchanged"""
        pw = self.app._candidate_passwords()
        box: dict = {}

        def work():
            try:
                box["info"] = ArchiveExtractor.ps4_archive_info(p, pw)
            except Exception:
                box["info"] = None
            if box["info"] is None:
                box["content"] = ArchiveExtractor.content_kind(p, pw)

        def poll():                       # Tk is touched on the main thread only
            try:
                if not self.winfo_exists() or (self.src_var.get() or "").strip() != raw:
                    return
            except Exception:
                return
            if "info" not in box:
                self.after(80, poll); return
            info = box["info"]
            if info is None:
                content = box.get("content", "")
                self._archive_content[str(p)] = content
                head = (self._KIND_LABEL["folder"] if content == "folder" else "." + content) if content \
                    else tr("Archive, content not readable before unpacking")
                self.detect_var.set(" · ".join(x for x in (tr(head), *self._detect_rest) if x))
                self._refresh()
                return
            self._ps4_info = info
            self._kind = "ps4"; self._games = [p]
            self.detect_var.set(self._ps4_detect_text(p))
            self._refresh()
        threading.Thread(target=work, daemon=True).start()
        self.after(80, poll)

    def _ps4_detect_text(self, p: Path) -> str:
        info = getattr(self, "_ps4_info", None)
        if self.app._is_archive_path(p) and info is not None:
            i = info.get("ident")
            n = len(info.get("packages") or [])
            bits = ["PS4 archive", plural(n, "1 package", "{n} packages")]
            if i is not None:
                bits += [i.title, i.title_id]
            return " · ".join(b for b in bits if b)
        m = _ps4pkg_module()
        pkgs = [p] if p.is_file() else sorted(x for x in p.rglob("*.pkg") if x.is_file() and not is_fs_junk_name(x.name))
        kinds: dict[str, int] = {}
        first = None
        for x in pkgs:
            try:
                i = m.read_identity(x)
            except Exception:
                kinds["unreadable"] = kinds.get("unreadable", 0) + 1
                continue
            first = first or i
            kinds[i.kind] = kinds.get(i.kind, 0) + 1
        what = ", ".join(f"{n} {k if k != 'dlc' else 'DLC'}{'s' if n != 1 and k != 'dlc' else ''}"
                         for k, n in kinds.items())
        bits = [tr("PS4 package") if p.is_file() else f"{tr('PS4 packages')} · {what}"]
        if first:
            bits += [first.title_id, f"v{first.version}" if first.version else ""]
            if p.is_file():
                bits.insert(1, tr({"game": "game", "update": "update", "dlc": "DLC"}.get(first.kind, first.kind)))
        return " · ".join(b for b in bits if b)

    _DRAFT_VARS = ("to_var", "out_var", "ps4_out_var", "ps4_to_var", "patch_on_var", "patch_var",
                   "backport_on_var", "backport_target_var", "sign_var", "lz4_var", "lz4_mode_var",
                   "lz4_profile_var", "level_var", "pkg_level_var", "organize_var", "after_var",
                   "after_dir_var", "retail_var", "regen_var")

    def keep_draft(self):
        if self.edit_item is not None or not (self.src_var.get() or "").strip():
            return
        d = {"src": self.src_var.get(), "dropped": [str(x) for x in self._dropped],
             "dropped_root": str(self._dropped_root) if self._dropped_root else ""}
        for name in self._DRAFT_VARS:
            v = getattr(self, name, None)
            if v is not None:
                try:
                    d[name] = v.get()
                except tk.TclError:
                    pass
        self.app._job_draft = d

    def _restore_draft(self, d: dict) -> None:
        if d.get("dropped") and d.get("dropped_root"):
            self._dropped = [Path(x) for x in d["dropped"]]
            self._dropped_root = Path(d["dropped_root"])
        self.src_var.set(d.get("src", ""))
        for name in self._DRAFT_VARS:
            v = getattr(self, name, None)
            if v is not None and name in d:
                try:
                    v.set(d[name])
                except tk.TclError:
                    pass
        self.help_var.set(tr("Your unsaved job is back. Cancel discards it."))

    def _on_source_changed(self):
        raw = (self.src_var.get() or "").strip()
        p = Path(raw) if raw else None
        self._read_seq += 1                       # a read still running for the last source stops
        self._reading = ""
        self._games = []
        self.check_var.set("")
        try:
            self._look_btn.pack_forget()
        except Exception:
            pass
        if not p or not p.exists():
            self._kind = "none"
            self.detect_var.set("Choose or drop a source." if not raw else "Not found")
            self._update_identity_for_source(None)
            self._refresh(); return
        self._ps4_info = None
        self._ps4_n = self._ps5_n = 0
        self._ps4_set = set()
        self._ks_default = None
        self.ks_state_var.set("")
        self._ps5_rows = None
        self.ps5_state_var.set("")
        dropped_root = bool(self._dropped) and p == self._dropped_root
        # a PS4-only folder is known only after _read_folder
        if p.is_file() and _is_ps4_source(p):
            self._kind = "ps4"; self._games = [p]
            self.detect_var.set(self._ps4_detect_text(p))
            if p.is_file():
                self._look_btn.pack(side="left", padx=(10, 0))
                self._probe_keystone(p, raw)
            self._update_identity_for_source(None)
            self._refresh(); return
        if p.is_dir():
            if is_game_folder(p) and not dropped_root:
                self._kind = "folder"; self._games = [p]
                bits = [self._KIND_LABEL["folder"], parse_title_id(p) or "no title id"]
                ver = ""
                try:
                    pj = json.loads((p / "sce_sys" / "param.json").read_text(encoding="utf-8-sig", errors="replace"))
                    ver = str(pj.get("contentVersion") or pj.get("masterVersion") or "")
                except Exception:
                    pass
                if ver:
                    bits.append(f"v{ver}")
                sdk = self._sdk_text(p)
                if sdk:
                    bits.append(sdk)
                if (p / "fakelib").is_dir():
                    bits.append("fakelib present")
                self.detect_var.set(" · ".join(bits))
            else:
                known = self._cached_sources(p)
                if known is None:
                    self._read_folder(p)
                else:
                    self._show_parent(p, *known)
        else:
            suf = p.suffix.lower()
            kind = {".exfat": "exfat", ".ffpkg": "ffpkg", ".ffpfs": "ffpfs", ".ffpfsc": "ffpfsc", ".pkg": "pkg"}.get(suf)
            if kind is None and (suf in (".zip", ".rar", ".7z", ".r00") or re.match(r"^\.r\d{2,}$", suf)
                                 or re.search(r"\.part\d+\.rar$", p.name, re.I)):
                kind = "archive"
            if kind is None:
                self._kind = "file-unknown"
                self.detect_var.set(tr("Unsupported file type {v}", v=suf or tr('(none)')))
                self._update_identity_for_source(None)
                self._refresh(); return
            self._kind = kind
            tid = ""
            _m = re.search(r"[A-Z]{4}[0-9]{5}", p.stem.upper())
            if _m:
                tid = _m.group(0)
            try:
                size = format_size(p.stat().st_size)
            except Exception:
                size = ""
            self._detect_rest = (tid, size)
            self.detect_var.set(" · ".join(x for x in (tr(self._KIND_LABEL[kind]), tid, size) if x))
            if kind in ("ffpfs", "ffpfsc", "pkg"):
                self._look_btn.pack(side="left", padx=(10, 0))
            if kind == "pkg":
                self._probe_ps5(p, raw)
            if kind == "archive":
                self._probe_ps4_archive(p, raw)
        self._update_identity_for_source(p)
        # remembered per source kind, add mode only
        if self.edit_item is None and self._defaults_kind != self._kind:
            self._defaults_kind = self._kind
            d = self._defaults.get(self._kind)
            if d:
                self.to_var.set(self._TARGET_LABEL.get(d.get("to", "ffpfsc"), ".ffpfsc"))
                self.sign_var.set(bool(d.get("sign", False)))
                self.backport_on_var.set(bool(d.get("backport", False)))
                if is_backport_target(d.get("backport_target")):
                    self.backport_target_var.set(d["backport_target"])
        self._refresh()

    @staticmethod
    def _target_of(item) -> str:
        op = getattr(item, "operation", "pack")
        if op == "chain":
            return getattr(item, "chain_to", None) or "ffpfsc"
        if op == "fpkg-build":
            return "pkg"
        if op == "unpack":
            return "folder" if getattr(item, "unwrap", True) else "ffpfs"
        if op in ("fpkg-extract", "fake-sign", "ps4-keystone"):
            return "folder"
        if op == "patch":
            return "ffpfsc"                  # a patch job always writes "<game> [patched].ffpfsc"
        if op == "copy" and getattr(item, "content_kind", "") == ORGANIZE_TARGET:
            return ORGANIZE_TARGET
        if op == "copy":
            return {".ffpfsc": "ffpfsc", ".ffpfs": "ffpfs", ".pkg": "pkg"}.get(
                Path(str(getattr(item, "path", "") or "")).suffix.lower(), "ffpfsc")
        # output_compressed None means not chosen yet, which packs a .ffpfsc
        return "ffpfs" if getattr(item, "output_compressed", None) is False else "ffpfsc"

    def _update_identity_for_source(self, p: Path | None):
        """fills from param.json, never over typed values; required when there is none"""
        for key, val in list(self._auto_ident.items()):       # values of the previous source
            var = {"content_id": self.cid_var, "title_id": self.tid_var,
                   "title": self.title_var, "version": self.ver_var}[key]
            if var.get() == val:
                var.set("")
        self._auto_ident = {}
        forced = False
        if self._kind == "folder" and p is not None:
            pj = p / "sce_sys" / "param.json"
            if pj.is_file():
                self._prefill_identity(pj)
            else:
                forced = True
                self._ident_note.set("Content ID and Title ID must be filled in; Title and Version are optional.")
        elif self._kind == "parent":
            self._ident_note.set("One job per game: each game's own param.json decides. Leave these empty.")
        else:
            self._ident_note.set("Read from the game's sce_sys/param.json when it is built. Leave empty; anything "
                                 "typed here only fills what param.json lacks.")
        self._ident_forced = forced
        self._layout_pkg_more()

    def _prefill_identity(self, pj: Path):
        try:
            d = json.loads(pj.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError) as e:
            self._ident_note.set(tr("param.json could not be read ({e}); the identity is used as typed.", e=e))
            return
        d = d if isinstance(d, dict) else {}
        title = param_title(d)
        found = {"content_id": str(d.get("contentId") or "").strip(), "title_id": str(d.get("titleId") or "").strip(),
                 "title": title, "version": str(d.get("contentVersion") or d.get("masterVersion") or "").strip()}
        for key, var in (("content_id", self.cid_var), ("title_id", self.tid_var),
                         ("title", self.title_var), ("version", self.ver_var)):
            if found[key] and not var.get().strip():
                var.set(found[key])
                self._auto_ident[key] = found[key]
        self._ident_note.set("Read from this folder's param.json, which still decides at build time; a value here "
                             "only fills what it lacks.")

    def _toggle_pkg_more(self):
        self._pkg_more = not self._pkg_more
        self._layout_pkg_more()
        if self._pkg_more:
            self._scroll_to_end()

    def _reveal_identity(self):
        if not self._ident_forced:           # required rows are shown anyway
            self._pkg_more = True
        self._layout_pkg_more()
        self._scroll_to_end()

    def _scroll_to_end(self):
        def _go():
            try:
                self.update_idletasks()
                self.body._parent_canvas.yview_moveto(1.0)
            except (AttributeError, tk.TclError):
                pass
        self.after_idle(_go)

    def _layout_pkg_more(self):
        shown = self._pkg_more or self._ident_forced
        try:
            if shown and not self._pkg_more_box.winfo_manager():
                self._pkg_more_box.pack(fill="x", pady=(0, 6))
            elif not shown and self._pkg_more_box.winfo_manager():
                self._pkg_more_box.pack_forget()
            self._pkg_more_btn.configure(text="Fewer options" if shown else "More options…",
                                         state="disabled" if self._ident_forced else "normal")
            self._ident_head.set(self._IDENT_REQUIRED if self._ident_forced else self._IDENT_OPTIONAL)
        except (AttributeError, tk.TclError):
            pass

    def _collect_pkg_identity(self) -> dict | None:
        """None after telling the user what's wrong and showing those rows"""
        cid = self.cid_var.get().strip().upper()
        tid = self.tid_var.get().strip().upper()
        ver = self.ver_var.get().strip()
        title = self.title_var.get().strip()

        def _bad(head: str, msg: str):
            self._reveal_identity()
            messagebox.showerror(head, msg, parent=self)
            return None
        if cid and not self._CID_RE.match(cid):
            return _bad("Content ID", "Content ID must look like  UP9000-PPSA12345_00-GAMENAME00000000\n"
                        "(2 letters + 4 digits, dash, 4 letters + 5 digits, _00-, 16 upper-case letters or digits), "
                        "or stay empty to use the game's param.json.")
        if tid and not self._TID_RE.match(tid):
            return _bad("Title ID", "Title ID must look like  PPSA12345  (4 letters + 5 digits), or stay empty to "
                        "use the game's param.json.")
        if cid and tid and tid not in cid:
            return _bad("Mismatch", tr("The title id {tid} does not appear inside the content id {cid}.", tid=tid, cid=cid))
        if cid and not tid:
            tid = cid[7:16]
        if ver and not self._VER_RE.match(ver):
            return _bad("Version", "Version must be NN.NNN.NNN (or NN.NN), or stay empty to use param.json.")
        if self._ident_forced and not (cid and tid):
            return _bad("Identity needed", "This game folder has no sce_sys/param.json, so Content ID and Title ID "
                        "must be filled in (the builder generates param.json from them).")
        inner = self.inner_var.get() if self.inner_var.get() in self._INNER else "kraken"
        back = self.back_var.get() if (sys.platform == "win32" and self.back_var.get() in self._BACKEND) else "builtin"
        dll = (self.app.pubtools_dll_var.get() or "").strip() if back == "publishingtools" else ""
        return {"content_id": cid, "title_id": tid, "title": title, "version": ver or "01.000.000",
                "inner": inner, "backend": back, "dll": dll}

    def _cached_sources(self, p: Path):
        """cached per folder and mtime; None means _read_folder still has to run"""
        if self._dropped and self._dropped_root is not None and Path(p) == self._dropped_root:
            got = list(self._dropped)
            return got, self._loose_ps4(got), None
        try:
            key = (str(p), p.stat().st_mtime_ns)
        except OSError:
            return [], set(), None
        return self.__dict__.setdefault("_source_scan_cache", {}).get(key)

    @staticmethod
    def _loose_ps4(sources) -> set:
        return {str(s) for s in sources if s.suffix.lower() == ".pkg" and _is_ps4_source(s)}

    def _read_folder(self, p: Path) -> None:
        """off the main thread; while typing, only the folder the typing stops at"""
        seq = self._read_seq
        self._kind, self._games, self._reading = "parent", [], "folder"
        self.detect_var.set("Reading the folder…")
        try:
            key = (str(p), p.stat().st_mtime_ns)
        except OSError:
            key = None
        box = {"n": 0}

        def stop():
            return seq != self._read_seq or self._gone

        def work():
            got = find_job_sources(p, progress=lambda n: box.__setitem__("n", n), stop=stop)
            ps4 = self._loose_ps4(got)
            # PS4 packages and nothing else: one PS4 source (see _is_ps4_source)
            line = self._ps4_detect_text(p) if got and len(ps4) == len(got) else None
            if not stop():
                box["v"] = (got, ps4, line)

        def poll():                       # Tk is touched on the main thread only
            if stop():
                return
            if "v" not in box:
                if box["n"]:
                    self.detect_var.set(plural(box["n"], "Reading the folder… 1 source so far",
                                               "Reading the folder… {n} sources so far"))
                self.after(150, poll)
                return
            if key is not None:
                self.__dict__.setdefault("_source_scan_cache", {})[key] = box["v"]
            self._reading = ""
            self._show_parent(p, *box["v"])
            self._update_identity_for_source(p)
            self._refresh()

        def begin():
            if not stop():
                threading.Thread(target=work, daemon=True).start()
                self.after(150, poll)
        self.after(250, begin)

    def _show_parent(self, p: Path, sources: list, ps4: set, ps4_line: str | None) -> None:
        """a PS4-only folder is one PS4 source; otherwise one job per source"""
        if ps4_line is not None:
            self._kind, self._games = "ps4", [p]
            self.detect_var.set(ps4_line)
            return
        if not sources:
            self._kind = "folder-unknown"
            self.detect_var.set("No game folder, archive, disk image, .ffpfs, .ffpfsc or .pkg in this folder")
            return
        self._kind, self._games = "parent", sources
        self._ps4_set = set(ps4)
        self._ps4_n = len(self._ps4_set)
        self._ps5_n = len(sources) - self._ps4_n
        self._parent_label = ("Dropped items" if bool(self._dropped) and p == self._dropped_root
                              else self._KIND_LABEL["parent"])
        self.detect_var.set(self._parent_text())
        self._probe_parent_archives(sources, p)

    def _parent_text(self) -> str:
        return tr("{parent} · {v} - one job each", parent=tr(self._parent_label),
                  v=self._describe_sources(self._games, self._ps4_set))

    @staticmethod
    def _describe_sources(sources: list[Path], ps4: set | None = None) -> str:
        """'4 sources: 1 game folder, 2 archives, 1 .pkg'; ps4 read from headers if not given"""
        counts: dict[str, int] = {}
        for s in sources:
            if s.is_dir():
                label = "game folder"
            elif is_archive_file(s):
                label = "ps4 archive" if ps4 is not None and str(s) in ps4 else "archive"
            elif s.suffix.lower() in (".exfat", ".ffpkg"):
                label = "disk image"
            elif s.suffix.lower() == ".pkg" and (str(s) in ps4 if ps4 is not None else _is_ps4_source(s)):
                label = "ps4"
            else:
                label = s.suffix.lower()
            counts[label] = counts.get(label, 0) + 1
        order = ["game folder", "archive", "disk image", ".ffpfs", ".ffpfsc", ".pkg", "ps4", "ps4 archive"]
        parts = []
        forms = {"game folder": ("1 game folder", "{n} game folders"), "archive": ("1 archive", "{n} archives"),
                 "disk image": ("1 disk image", "{n} disk images"), "ps4": ("1 PS4 package", "{n} PS4 packages"),
                 "ps4 archive": ("1 archive of PS4 packages", "{n} archives of PS4 packages")}
        for label in sorted(counts, key=lambda k: order.index(k) if k in order else 99):
            n = counts[label]
            parts.append(plural(n, *forms[label]) if label in forms else f"{n} {label}")
        return plural(len(sources), "1 source: {parts}", "{n} sources: {parts}", parts=", ".join(parts))

    def _parent_todo(self) -> list[Path]:
        """skips sources with nothing to do, like a game folder to folder without changes"""
        return [s for s in self._games if not chain_summary(self._stand_in(s)).startswith("Nothing to do")]

    @staticmethod
    def _copy_only(stand_in, to: str) -> bool:
        """The job ends as a plain copy: same format in and out and nothing to change."""
        try:
            return (to not in ("folder", ORGANIZE_TARGET, AUTO_TARGET) and chain_source_kind(stand_in) == to
                    and not chain_changes(stand_in) and not getattr(stand_in, "chain_rebuild", False))
        except Exception:
            return False

    def _own_folder_note(self, to: str, ps4: bool) -> str:
        """for when a change hits the user's own game folder, not a copy"""
        own = (self._kind == "folder" or (self._kind == "parent" and bool(self._games)
                                          and all(Path(g).is_dir() for g in self._games)))
        if not own or ps4:
            return ""
        bp, sg = bool(self.backport_on_var.get()), bool(self.sign_var.get()) and to != "pkg"
        parts = []
        if bp and sg:
            parts.append(tr("Backport and Sign change eboot.bin and the libraries in your game folder itself, "
                            "not a copy."))
        elif bp or sg:
            parts.append(tr("{what} changes eboot.bin and the libraries in your game folder itself, not a copy.",
                            what=tr("Backport") if bp else tr("Sign")))
        if self.patch_on_var.get():
            parts.append(tr("Integrate a patch writes into your game folder; the files it replaces are kept "
                            "beside the output."))
        return " ".join(parts)

    def _in_place(self, to: str) -> bool:
        """folder output changes game folders where they are"""
        if to != "folder":
            return False
        if self._kind == "folder":
            return True
        return self._kind == "parent" and bool(self._games) and all(s.is_dir() for s in self._games)

    def _auto_guess(self, p: Path | None, is_archive: bool) -> str:
        """best guess for the dialog text; an unread archive counts as a game folder"""
        if p is None:
            kind = "folder"
        elif is_archive:
            kind = self._archive_content.get(str(p)) or "folder"
        elif p.is_dir():
            kind = "folder"
        else:
            kind = chain_source_kind(types.SimpleNamespace(path=p))
        return auto_target(kind, self._auto_table)

    def _stand_in(self, p: Path | None):
        """just the attributes chain_summary reads, no folder walk"""
        kind = self._kind
        is_archive = kind == "archive" or (kind == "parent" and p is not None and p.is_file() and is_archive_file(p))
        to = self._to_key()
        if to == AUTO_TARGET:
            to = self._auto_guess(p, is_archive)
        return types.SimpleNamespace(
            path=p, archive_path=(p if is_archive else None),
            content_kind=ORGANIZE_TARGET if to == ORGANIZE_TARGET else "",
            chain_to=to, chain_sign=bool(self.sign_var.get()) and to != "pkg",
            chain_lz4=self._lz4_packs() and to in ultra_core.LZ4_TARGETS,
            chain_lz4_unpack=bool(self.lz4_var.get()) and self.lz4_mode_var.get() == "unpack",
            patch_source=(self.patch_var.get().strip() if self.patch_on_var.get() else None),
            backport_target=(self.backport_target_var.get() if self.backport_on_var.get() else None))

    def _refresh(self):
        ps4 = self._kind == "ps4"
        seg = getattr(self, "_to_seg", None)
        if ps4:
            # one PS4 .pkg goes to the library or gets unpacked; archives and folders library only
            src = Path((self.src_var.get() or "").strip())
            one_pkg = src.is_file() and src.suffix.lower() == ".pkg"
            allowed = ("pkg", "folder") if one_pkg else ("pkg",)
            key = self._to_key() if self._to_key() in allowed else "pkg"
            if self.to_var.get() != self._PS4_LABEL[key]:
                self.to_var.set(self._PS4_LABEL[key])
            if seg is not None:
                seg.configure(values=[self._PS4_LABEL[k] for k in allowed])
                seg.set(self.to_var.get())
            for v in (self.patch_on_var, self.backport_on_var, self.sign_var, self.lz4_var):
                v.set(False)
        crow, orow = getattr(self, "_crow", None), getattr(self, "_orow", None)
        org = not ps4 and self._to_key() == ORGANIZE_TARGET     # the source goes over as it is
        ocb = getattr(self, "_organize_cb", None)
        if ocb is not None:            # PS4 packages and Organize name everything anyway
            if ps4 or org:
                ocb.pack_forget()
            elif not ocb.winfo_manager():
                ocb.pack(side="left")
        if crow is not None and orow is not None:
            if (ps4 and not self._keystone_shown()) or org:
                crow.pack_forget()
            elif not crow.winfo_manager():
                crow.pack(fill="x", padx=20, pady=5, before=orow)
        ks_box = getattr(self, "_ks_box", None)
        if ks_box is not None:
            # a PS4 package offers only its keystone; a PS5 source never does
            if ps4:
                for b in self._ps5_boxes:
                    b.pack_forget()
                if not ks_box.winfo_manager():
                    ks_box.pack(fill="x", pady=(2, 2))
            else:
                ks_box.pack_forget()
                if not all(b.winfo_manager() for b in self._ps5_boxes):
                    for b in self._ps5_boxes:
                        b.pack_forget()
                    for b in self._ps5_boxes:
                        b.pack(fill="x", pady=(2, 2))
        pb = getattr(self, "_ps5r_box", None)
        if pb is not None:
            if not ps4 and self._kind == "pkg" and self._ps5_rows is not None:
                pb.pack(fill="x", pady=(2, 2), after=self._ps5_boxes[-1])
            else:
                pb.pack_forget()
        if getattr(self, "_ps4_box", None) is not None:
            self._show_platform("ps4" if ps4 else "ps5")
            if self._mixed():
                self._ps5_head_var.set(plural(self._ps5_n, "PS5 games (1)", "PS5 games ({n})"))
                self._ps4_head_var.set(plural(self._ps4_n, "PS4 packages (1)", "PS4 packages ({n})"))
                if not self._ps5_head.winfo_manager():
                    self._ps5_head.pack(fill="x", padx=10, pady=(4, 0), before=self._oin)
                if not self._ps4_box.winfo_manager():
                    self._ps4_box.pack(fill="x", after=self._oline)    # Auto-organize belongs to the PS5 games
            else:
                self._ps5_head.pack_forget()
                self._ps4_box.pack_forget()
        if not ps4 and seg is not None and len(seg.cget("values")) != len(self._TARGETS):
            self.to_var.set(self._TARGET_LABEL.get(self._to_key(), ".ffpfsc"))
            seg.configure(values=[self._TARGET_LABEL[k] for k in self._TARGETS])
            seg.set(self.to_var.get())
        to = self._to_key()
        (self._patch_opts.pack(fill="x", pady=(2, 2)) if self.patch_on_var.get() else self._patch_opts.pack_forget())
        (self._backport_opts.pack(fill="x", pady=(2, 2)) if self.backport_on_var.get() else self._backport_opts.pack_forget())
        ampr, packed = self._lz4_state(), self._lz4_source_packed()
        if self._kind == "folder":
            want = "unpack" if packed else "pack"      # a game folder says which way it can go
            if self.lz4_mode_var.get() != want:
                self.lz4_mode_var.set(want)
        if self._lz4_mode_seg.get() != self._lz4_modes[self.lz4_mode_var.get()]:
            self._lz4_mode_seg.set(self._lz4_modes[self.lz4_mode_var.get()])
        if ampr is False and not packed:
            if self.lz4_var.get():
                self.lz4_var.set(False)
            self._lz4_cb.configure(state="disabled")
            self.lz4_note_var.set("")
        else:
            self._lz4_cb.configure(state="normal")
            if self.lz4_mode_var.get() == "unpack":
                self.lz4_note_var.set(tr("Unpack puts every file of the packs back and removes the packs; "
                                         "the output can be any format."))
            elif ampr:
                self.lz4_note_var.set("")
            else:
                self.lz4_note_var.set(tr("Checked when the job runs: only a game that loads through AMPR can use LZ4 packs."))
        if self.lz4_mode_var.get() == "pack":
            if not self._lz4_profile_row.winfo_manager():
                self._lz4_profile_row.pack(fill="x", pady=(2, 2), after=self._lz4_mode_seg.master)
        else:
            self._lz4_profile_row.pack_forget()
        (self._lz4_opts.pack(fill="x", pady=(2, 2)) if self.lz4_var.get() else self._lz4_opts.pack_forget())
        (self._pkg_opts.pack(fill="x", pady=(2, 0)) if to == "pkg" and not ps4 else self._pkg_opts.pack_forget())
        if to in ("ffpfsc", "pkg") and not ps4:
            show, hide = (self._comp_pkg, self._comp_ffpfsc) if to == "pkg" else (self._comp_ffpfsc, self._comp_pkg)
            hide.pack_forget()
            if not show.winfo_manager():
                show.pack(side="left")
            if not self._comp_row.winfo_manager():
                self._comp_row.pack(fill="x", padx=10, pady=(4, 0), after=self._oin)
        else:
            self._comp_row.pack_forget()
        self.backport_hint_var.set(self._backport_hint(self.backport_target_var.get()))
        if to == "pkg":
            self._sign_cb.configure(state="disabled")
            self._sign_cb.pack_forget(); self._sign_note_lbl.pack_forget()
            if not self._sign_fixed.winfo_manager():
                self._sign_fixed.pack(side="left")
        else:
            self._sign_fixed.pack_forget()
            if not self._sign_cb.winfo_manager():
                self._sign_cb.pack(side="left"); self._sign_note_lbl.pack(side="left", padx=(10, 0))
            self._sign_cb.configure(state="normal")
        try:
            self._check_btn.configure(state="normal" if self._check_folder() is not None else "disabled")
        except Exception:
            pass
        self._own_note.configure(text=self._own_folder_note(to, ps4))
        if self._own_note.cget("text"):
            if not self._own_note.winfo_manager():
                self._own_note.pack(fill="x", pady=(4, 2))
        else:
            self._own_note.pack_forget()
        self._to_hint.set({"folder": "plain /app0 folder - for a folder source: changes in place",
                           "ffpfs": "uncompressed image - fastest to build and mount, full size",
                           "ffpfsc": "compressed image - mounts with ShadowMount",
                           "pkg": "installable package",
                           "organize": "into the library as it is: '<Title> [ID] [vX]/…', the same format, "
                                       "joining a title folder that is already there",
                           "auto": self._auto_hint()}.get(to, "") if not ps4 else
                          {"pkg": "sorted as it is into '<Title> [CUSA…] [vX]', with its updates and DLC",
                           "folder": "the package's files in a folder"}.get(to, ""))
        in_place = self._in_place(to)
        for w in (self._out_entry, self._out_btn):
            try:
                w.configure(state="disabled" if in_place else "normal")
            except Exception:
                pass
        # an in-place change writes into the source, so no after-job action
        try:
            aj = _after_job_module()
            if in_place or self._kind == "none":
                self._after_box.pack_forget()
            else:
                anchor = self._ps4_box if self._ps4_box.winfo_manager() else self._oline
                if not self._after_box.winfo_manager() or getattr(self, "_after_anchor", None) is not anchor:
                    self._after_box.pack_forget()
                    self._after_box.pack(fill="x", padx=10, pady=(0, 6), after=anchor)
                    self._after_anchor = anchor
                if anchor is not self._ps4_box:
                    self._after_head.pack_forget()
                elif not self._after_head.winfo_manager():
                    self._after_head.pack(anchor="w", pady=(6, 2), before=self._after_row)
                act = self.after_var.get()
                self._after_seg.set(aj.LABELS.get(act, aj.LABELS[aj.KEEP]))
                self._after_dir_row.pack_forget(); self._after_note.pack_forget()
                if act == aj.MOVE:
                    self._after_dir_row.pack(fill="x", pady=(4, 0))
                elif act == aj.DELETE:
                    self._after_note.pack(anchor="w", padx=(100, 0), pady=(4, 0))
        except Exception:
            pass
        raw = (self.src_var.get() or "").strip()
        p = Path(raw) if raw else None
        copy_only = False
        if self._reading:
            text = self._reading_text()
            ok = False
        elif self._kind in ("none", "folder-unknown", "file-unknown") or p is None:
            text = "Choose a source"
            ok = False
        elif ps4:
            text = "Sort into the PS4 library" if to == "pkg" else "Unpack to folder"
            ok = True
        elif self._kind == "parent":
            todo = self._parent_todo()
            mixed = self._mixed()
            ps4_set = getattr(self, "_ps4_set", set()) if mixed else set()
            todo5 = [s for s in todo if str(s) not in ps4_set]
            if todo5:
                _si = self._stand_in(todo5[0])
                copy_only = all(self._copy_only(self._stand_in(x), to) for x in todo5)
                text = chain_summary(_si) + plural(len(todo5), "  × 1 job", "  × {n} jobs")
            else:
                text = "Nothing to do"
            if mixed:
                text += "  ·  " + (plural(self._ps4_n, "1 PS4 package into the library", "{n} PS4 packages into the library")
                                   if self.ps4_to_var.get() == "pkg" else
                                   plural(self._ps4_n, "1 PS4 package unpacked", "{n} PS4 packages unpacked"))
            ok = bool(todo)
        else:
            _si = self._stand_in(p)
            copy_only = self._copy_only(_si, to)
            text = chain_summary(_si)
            ok = not text.startswith("Nothing to do")
        out = (self.out_var.get() or "").strip()
        if p is None or self._kind in ("none", "folder-unknown", "file-unknown"):
            dest = ""
        elif ok and not in_place and not out:
            ok = False
            dest = "Choose an output folder"
        elif in_place:
            dest = f"in {p}" if p else ""
        else:
            dest = f"→ {out}"
            if self._kind == "parent" and self._mixed():
                dest += "  ·  " + tr("PS4: {folder}", folder=(self.ps4_out_var.get() or "").strip() or "–")
        if copy_only:
            # nothing is rebuilt: the compression and .pkg options would do nothing
            self._comp_row.pack_forget()
            self._pkg_opts.pack_forget()
            self._to_hint.set("same format and no changes: copied as it is"
                              + ("; PS5 PKG Tools rebuild a package" if to == "pkg" else ""))
        act = self.after_var.get() if self._after_box.winfo_manager() else "keep"
        then = {"trash": tr("then the source goes to the {TRASH_NAME}", TRASH_NAME=TRASH_NAME),
                "move": tr("then the source moves"),
                "delete": tr("then the source is deleted")}.get(act)
        if ok and then:
            text += "  ·  " + then
        try:
            danger = act == "delete"
            self._after_seg.configure(selected_color=RED if danger else ACCENT,
                                      selected_hover_color=DANGER_HOVER if danger else ACCENT_HOVER)
        except Exception:
            pass
        self.summary_var.set(text)
        self.summary_dest_var.set(dest)
        try:
            # disabled it greys out like Start instead of staying blue
            self._add_btn.configure(state="normal" if ok else "disabled", fg_color=ACCENT if ok else BTN)
        except Exception:
            pass

    _CHECK_CONTAINERS = (".pkg", ".ffpfs", ".ffpfsc")

    def _check_folder(self) -> Path | None:
        """None for archives and disk images: those are checked when the job runs"""
        raw = (self.src_var.get() or "").strip()
        if self._kind in ("pkg", "ffpfs", "ffpfsc") and raw:
            return Path(raw)
        if self._kind not in ("folder", "parent"):
            return None
        return (next((s for s in self._games if s.is_dir()), None)          # a folder reads fastest
                or next((s for s in self._games
                         if s.is_file() and s.suffix.lower() in self._CHECK_CONTAINERS), None))

    def _pick_after_dir(self):
        cur = self.after_dir_var.get().strip()
        kw = {"initialdir": cur} if cur and Path(cur).is_dir() else {}
        p = filedialog.askdirectory(parent=self, title="Choose the folder the source moves to", **kw)
        if p:
            self.after_dir_var.set(p)

    def _check_compat(self):
        folder = self._check_folder()
        if folder is None:
            self.check_var.set("Check reads a game folder, a .pkg, .ffpfs or .ffpfsc. An archive or a disk image "
                               "is checked when its job runs, after it is unpacked.")
            return
        target = self.backport_target_var.get()
        libs = (self.app.backport_libs_var.get() or "").strip()
        fw_path = self._fw_root()
        if fw_path is None:
            self.check_var.set("Set the firmware libraries folder in Settings (one subfolder per firmware, "
                               "e.g. 7.61, 10.01) to check a game against its target.")
            return
        fw = str(fw_path)
        pycmd = get_backend_python_command()
        if not pycmd:
            self.check_var.set("Backend not found."); return
        cli_py = backend_base_dir() / "cli.py"
        head = pycmd if getattr(sys, "frozen", False) else pycmd + ["-u", str(cli_py)]
        argv = head + ["--backport-analyze", str(folder), "--backport-target", target, "--fw-libs-root", fw]
        if libs:
            argv += ["--backport-libs", libs]
        self.check_var.set("Checking…")
        self._check_btn.configure(state="disabled")
        result: dict = {}

        def work():                      # no Tk calls here: the main thread polls for the result
            try:
                r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
                out = (r.stdout or "").splitlines()
                verdict = next((l[len("[verdict] "):] for l in out if l.startswith("[verdict] ")), "")
                result["msg"] = verdict or (r.stderr or "").strip()[-300:] or "no result"
            except Exception as e:
                result["msg"] = tr("check failed: {e}", e=e)

        worker = threading.Thread(target=work, daemon=True)

        def poll():
            try:
                if not self.winfo_exists():
                    return
                if worker.is_alive():
                    self.after(150, poll)
                    return
                self.check_var.set(result.get("msg", "no result"))
                self._check_btn.configure(state="normal")
            except tk.TclError:
                pass                     # the dialog closed while the check ran
        worker.start()
        self.after(150, poll)

    def _add_ps4(self, p: Path, to: str, out: str):
        aj = _after_job_module()
        after = self.after_var.get() if self.after_var.get() in aj.ACTIONS else aj.KEEP
        after_dir = (self.after_dir_var.get() or "").strip()
        if to == "folder":
            dest = Path(out) / f"{sanitize_filename(p.stem)} [extracted]"
            it = GameItem.from_fpkg_extract(p, output_path=str(dest))
        else:
            it = self.app._ps4_item_for(p, output_path=out, info=getattr(self, "_ps4_info", None))
        it.after_source, it.after_move_to = after, after_dir or None
        it.source_root = str(p.parent if p.is_file() else p)
        if self.edit_item is not None:
            try:
                self.app.queue[self.app.queue.index(self.edit_item)] = it
            except ValueError:
                self.app.queue.append(it)
        else:
            self.app.queue.append(it)
        self.app.update_queue_box(select_item=it)
        self.app.log("OK", f"Queued: {chain_summary(it)} - {it.display_name or it.name}.  Press Start to run.")
        self.destroy()

    def _add_organize(self, p: Path, out: str):
        aj = _after_job_module()
        after = self.after_var.get() if self._after_box.winfo_manager() else aj.KEEP
        after_dir = self.after_dir_var.get().strip() if after == aj.MOVE else ""
        if after == aj.MOVE and not after_dir:
            messagebox.showerror("After the job", "Choose the folder the source moves to once the job is Done.",
                                 parent=self); return
        self._defaults[self._kind] = dict(self._defaults.get(self._kind) or {}, to=ORGANIZE_TARGET)
        try:
            # dropped paths get no Rescan folder: their common folder may be a whole drive
            save_settings({"job_dialog_defaults": self._defaults} if self._drop_shown() else
                          {"job_dialog_defaults": self._defaults, "last_source": str(p),
                           "last_source_dir": str(p if p.is_dir() else p.parent),
                           "rescan_template": {"to": ORGANIZE_TARGET, "output": out, "after_source": after,
                                               "after_move_to": after_dir or None}})
        except Exception:
            pass
        self.app.output_var.set(out)
        sources = (self._parent_todo() if self._kind == "parent"
                   else self._games if self._kind == "folder" else [p])
        root = str(p if (p.is_dir() and not ultra_core.is_game_folder(p)) else p.parent)
        app = self.app

        route = getattr(self, "_route", None)
        passwords = app._candidate_passwords() if route else None
        self._remember_outputs(out)

        def make(s):
            it = ((app._ps4_route(s, route[0], passwords, route[1]) if route else None)
                  or app._organize_item_for(s, output_path=out))
            it.after_source, it.after_move_to = after, after_dir or None
            it.source_root = root
            return it

        if self.edit_item is None:
            plan = app._release_planner(p, app._candidate_passwords()) if self._kind == "parent" else None
            app._add_jobs_async(list(sources), make, plan=plan)
            self.destroy(); return
        try:
            new = make(sources[0])
        except Exception as e:
            messagebox.showerror("Source", tr("Could not read {v}:\n{e}", v=sources[0], e=e), parent=self); return
        _old_src = getattr(self.edit_item, "archive_path", None) or getattr(self.edit_item, "path", None)
        if _old_src is not None and str(_old_src) == str(sources[0]):
            for attr in ("bundle_siblings", "bundle_release"):     # its add-ons stay with it
                if getattr(self.edit_item, attr, None):
                    setattr(new, attr, getattr(self.edit_item, attr))
        try:
            app.queue[app.queue.index(self.edit_item)] = new
        except ValueError:
            app.queue.append(new)
        app.update_queue_box(select_item=new)
        app.log("OK", f"Job updated: {chain_summary(new)} - {new.display_name or new.name}.")
        self.destroy()

    def _add(self):
        if self._reading:
            return                       # Return while the folder is read: Add is greyed out
        raw = (self.src_var.get() or "").strip()
        p = Path(raw) if raw else None
        if not p or not p.exists() or self._kind in ("none", "folder-unknown", "file-unknown"):
            messagebox.showerror("Source", "Choose a game folder, a parent folder, an archive, a disk image, "
                                           "a .ffpfs/.ffpfsc or a .pkg.", parent=self); return
        to = self._to_key()
        in_place = self._in_place(to)
        out = (self.out_var.get() or "").strip()
        if not in_place and not out:
            messagebox.showerror("Output", "Choose an output folder for this job.", parent=self); return
        patch = self.patch_var.get().strip() if self.patch_on_var.get() else ""
        if self.patch_on_var.get() and not (patch and Path(patch).exists()):
            messagebox.showerror("Patch", "Choose the patch folder or archive to integrate.", parent=self); return
        if patch and self._kind != "parent":
            verdict, why = self.app._patch_fit(p, Path(patch))
            if verdict == "refuse":
                messagebox.showerror("Patch", why, parent=self); return
            if verdict == "warn" and not ask_confirm("Add the job anyway?", why, "Add anyway"):
                return
        target = self.backport_target_var.get() if self.backport_on_var.get() else None
        glibs = (self.app.backport_libs_var.get() or "").strip() if target else ""
        if glibs and not Path(glibs).is_dir():
            messagebox.showerror("Backport", "The patched libraries folder set in Settings › Backport & libraries "
                                             "does not exist. Choose it again there, or clear the field.",
                                 parent=self); return
        if glibs:
            misuse = _backport_module().patched_libs_problem(glibs, self._fw_root())
            if misuse:
                messagebox.showerror("Backport", "Settings › Backport & libraries: " + misuse + ".", parent=self); return
        libs = ""                       # the job takes the Settings folder when it runs
        if target and target not in self._BACKPORT_HINTS:
            try:
                _backport_module().target_words(target, self._fw_root())
            except ValueError as e:
                messagebox.showerror("Backport", str(e), parent=self); return
        if self._kind == "ps4":
            self.app._remember_output("ps4", out)
            return self._add_ps4(p, to, out)
        mixed = self._mixed()
        ps4_out = (self.ps4_out_var.get() or "").strip() if mixed else ""
        if mixed and not ps4_out:
            messagebox.showerror("Output", "Choose an output folder for the PS4 packages.", parent=self); return
        ps4_to = self.ps4_to_var.get() if mixed else "pkg"
        self._route = (ps4_out, ps4_to) if mixed else None
        if to == ORGANIZE_TARGET:
            return self._add_organize(p, out)
        stand = self._stand_in(p)
        if chain_summary(stand).startswith("Nothing to do"):
            messagebox.showerror("Nothing to do", "A folder to a folder with no changes is nothing to do.", parent=self); return

        level = max(-4, min(9, int(self.pkg_level_var.get())))
        ff_level = max(1, min(9, int(self.level_var.get())))
        ident = {"content_id": "", "title_id": "", "title": "", "version": "01.000.000",
                 "inner": "kraken", "backend": "builtin", "dll": ""}
        if to == "pkg":
            ident = self._collect_pkg_identity()
            if ident is None:
                return
        pkg_params = {"content_id": ident["content_id"], "title_id": ident["title_id"], "title": ident["title"],
                      "version": ident["version"], "inner": ident["inner"], "backend": ident["backend"],
                      "level": level, "dll": ident["dll"],
                      "retail_normalize": bool(self.retail_var.get()), "hdr_flag": _hdr_mode(self.hdr_var.get()),
                      "regen_playgo": bool(self.regen_var.get()), "fake_sign": True,
                      "backport_target": target, "backport_libs": libs}
        organize = bool(self.organize_var.get())
        aj = _after_job_module()
        after = self.after_var.get() if self._after_box.winfo_manager() else aj.KEEP
        after_dir = self.after_dir_var.get().strip() if after == aj.MOVE else ""
        if after == aj.MOVE and not after_dir:
            messagebox.showerror("After the job", "Choose the folder the source moves to once the job is Done.",
                                 parent=self); return

        self._defaults[self._kind] = {"to": to, "sign": bool(self.sign_var.get()),
                                      "backport": bool(self.backport_on_var.get()),
                                      "backport_target": self.backport_target_var.get()}
        upd = {"job_dialog_defaults": self._defaults,
               "last_source": str(p),
               "last_source_dir": str(p if p.is_dir() else p.parent),
               # what Rescan reuses on new sources; no patch, a patch belongs to one game
               "rescan_template": {"to": to, "output": out, "sign": bool(self.sign_var.get()),
                                   "backport_target": target,
                                   "organize": organize, "ff_level": ff_level,
                                   "after_source": after, "after_move_to": after_dir or None,
                                   "pkg_params": pkg_params if to == "pkg" else None}}
        if target:
            upd["backport_target_default"] = target
        if self._drop_shown():  # no Rescan folder from a drop
            for k in ("last_source", "last_source_dir", "rescan_template"):
                upd.pop(k, None)
        try:
            save_settings(upd)
        except Exception:
            pass
        if to == "pkg":
            self.app.fpkg_defaults = {k: pkg_params[k] for k in ("inner", "backend", "level", "retail_normalize",
                                                                "hdr_flag", "regen_playgo", "fake_sign")}
            self.app.fpkg_defaults["v1112"] = True
            try:
                save_settings({"fpkg_defaults": self.app.fpkg_defaults})
            except Exception:
                pass
        elif to == "ffpfsc":
            self.app.compression_level_var.set(ff_level)
        if out:
            self.app.output_var.set(out)
        self.app.auto_organize_var.set(organize)

        sources = (self._parent_todo() if self._kind == "parent"
                   else self._games if self._kind == "folder" else [p])
        # the picked folder stays even when its sources are moved or cleaned up
        root = str(p if (p.is_dir() and not ultra_core.is_game_folder(p)) else p.parent)
        sign_on, lz4_on = bool(self.sign_var.get()), self._lz4_packs()
        lz4_unpack = bool(self.lz4_var.get()) and self.lz4_mode_var.get() == "unpack"
        lz4_profile = (self.lz4_profile_var.get() or "").strip() if lz4_on else ""
        if lz4_profile and not Path(lz4_profile).is_file():
            messagebox.showerror("LZ4 asset packs", tr("The profile does not exist:\n{p}", p=lz4_profile), parent=self)
            return
        app = self.app
        auto = to == AUTO_TARGET
        table = self._auto_table if auto else None
        routed = self._kind == "parent"
        ps4_out, ps4_to = getattr(self, "_route", None) or (out or None, "pkg")
        passwords = app._candidate_passwords() if (auto or routed) else None

        def build(s, to):
            lz4 = lz4_on and to in ultra_core.LZ4_TARGETS
            it = GameItem.from_chain(s, to=to, output_path=out or None,
                                     sign=sign_on and to != "pkg",     # a .pkg is always signed by its builder
                                     patch_source=patch or None, backport_target=target,
                                     backport_libs_root=libs or None, lz4=lz4,
                                     lz4_profile=(lz4_profile or None) if lz4 else None, lz4_unpack=lz4_unpack)
            if to == "pkg":
                app._apply_fpkg_params(it, pkg_params)
            it.compression_level = ff_level if to == "ffpfsc" else None
            it.auto_organize = organize
            return it

        def make(s):
            # Tk-free: runs on the add thread and reads archive headers
            it = ((app._ps4_route(s, ps4_out, passwords, ps4_to) if routed else None)
                  or (app._auto_item_for(s, table, build, output_path=out or None, passwords=passwords) if auto
                      else build(s, to)))
            if auto:  # in case the unpacked content picks another output
                it.auto_sign, it.auto_lz4 = sign_on, lz4_on
                it.auto_lz4_profile = lz4_profile or None
            it.after_source, it.after_move_to = after, after_dir or None
            it.source_root = root
            return it

        self._remember_outputs(out)
        if self.edit_item is None:
            plan = app._release_planner(p, app._candidate_passwords()) if self._kind == "parent" else None
            app._add_jobs_async(list(sources), make, plan=plan)
            self.destroy(); return
        made = []
        for s in sources:
            try:
                made.append(make(s))
            except Exception as e:
                messagebox.showerror("Source", tr("Could not read {s}:\n{e}", s=s, e=e), parent=self); return
        if not made:
            return

        if self.edit_item is not None:
            old = self.edit_item
            new = made[0]
            for attr in ("display_name", "bundle_subfolder", "password"):
                if getattr(old, attr, None) is not None and getattr(new, attr, None) is None:
                    setattr(new, attr, getattr(old, attr))
            _old_src = getattr(old, "archive_path", None) or getattr(old, "path", None)
            if _old_src is not None and str(_old_src) == str(p):
                # same source: keep the origin, so retry and removal hit the job's own kept copy
                for attr in ("origin_archive", "origin_extracted_size", "origin_payload", "organize_folder",
                             "archive_games", "archive_index", "kept_extract", "bundle_siblings", "bundle_release",
                             "pkg_content_size", "pkg_largest_file", "pkg_kept_bytes",
                             "extracted_size", "header_locked"):
                    if hasattr(old, attr):
                        setattr(new, attr, getattr(old, attr))
            new.status = "Pending Extract" if getattr(new, "archive_path", None) else "Queued"
            new._output_checked = False
            new._replace_output = False
            new._keep_both = False
            try:
                idx = self.app.queue.index(old)
                self.app.queue[idx] = new
            except ValueError:
                self.app.queue.append(new)
            self.app.update_queue_box(select_item=new)
            _run = "" if self.app._batch_running else " Start runs it."
            self.app.log("OK", f"Job updated: {chain_summary(new)} - {new.display_name or new.name}.{_run}")
            self.destroy(); return

        for it in made:
            self.app.queue.append(it)
            try:
                if getattr(it, "archive_path", None):
                    self.app._resolve_archive_password(it)
            except Exception:
                pass
        self.app.update_queue_box(select_item=made[-1])
        what = chain_summary(made[0]) + (f" × {len(made)}" if len(made) > 1 else "")
        self.app.log("OK", f"Queued: {what} - {made[0].display_name or made[0].name}"
                           + (f" (+{len(made) - 1} more)" if len(made) > 1 else "") + ".  Press Start to run.")
        self.destroy()




class ArchivePasswordPrompt(MessageWindow):
    """Asks for one archive's password; .password is '' on Skip."""

    def __init__(self, app, archive_name: str):
        super().__init__(app.root)
        self.app = app
        self.password = ""
        self.title("Archive password needed")
        self.configure(fg_color=BLACK); self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._skip)
        self.after(50, self.grab_set)

        ctk.CTkLabel(self, text="Archive password needed",
                      font=ctk.CTkFont(size=17, weight="bold"), text_color=WHITE
                      ).pack(anchor="w", padx=20, pady=(16, 2))
        ctk.CTkLabel(self,
                      text=tr("None of your saved passwords opens '{archive_name}'. Enter its password so the real "
                             "extracted size is known for the drive routing; the password is then saved for later "
                             "archives. Skip adds the job anyway, and the extraction tries every saved password again.", archive_name=archive_name),
                      text_color=MUTED, wraplength=500, justify="left"
                      ).pack(anchor="w", padx=20, pady=(0, 10))

        self.pw_var = tk.StringVar()
        prow = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=8); prow.pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(prow, text="Password:", text_color=WHITE,
                      font=ctk.CTkFont(size=12)).pack(anchor="w", padx=10, pady=(6, 0))
        pinner = ctk.CTkFrame(prow, fg_color=PANEL); pinner.pack(fill="x", padx=10, pady=(2, 8))
        self.entry = ctk.CTkEntry(pinner, textvariable=self.pw_var, fg_color=CARD2,
                                   text_color=WHITE, show="•")
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.focus_set()
        self.show_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(pinner, text="Show", variable=self.show_var,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE,
                         command=self._toggle_show, checkbox_width=18, checkbox_height=18).pack(side="left", padx=(6, 0))

        btns = ctk.CTkFrame(self, fg_color=BLACK); btns.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(btns, text="OK", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                       text_color=ON_ACCENT, font=ctk.CTkFont(size=13),
                       command=self._ok).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Skip", fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER, command=self._skip, border_width=1, border_color=BTN_BORDER).pack(side="right")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._skip())

    def _toggle_show(self):
        self.entry.configure(show="" if self.show_var.get() else "•")

    def _ok(self):
        self.password = self.pw_var.get().strip()
        self.destroy()

    def _skip(self):
        self.password = ""
        self.destroy()


class OutputExistsDialog(MessageWindow):
    """One answer for all clashing outputs; .choice is skip, overwrite, keep or cancel."""

    MAX_ROWS = 8

    def __init__(self, parent, conflicts):
        super().__init__(parent)
        self.choice = "cancel"
        n = len(conflicts)
        head = "Already in the output folder"
        self.title(head)
        self.configure(fg_color=BLACK); self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: self._done("cancel"))
        self.after(50, self.grab_set)
        ctk.CTkLabel(self, text=head, font=ctk.CTkFont(size=17, weight="bold"), text_color=WHITE
                     ).pack(anchor="w", padx=20, pady=(16, 2))
        ctk.CTkLabel(self, text=plural(
                         n, "1 job would write a file that is already there. Skip leaves it out of this run; "
                            "Overwrite replaces the file once the new build is finished; Keep both gives the new "
                            "build a numbered name. Settings › General can answer this for every start.",
                         "{n} jobs would write a file that is already there. Skip leaves them out of this run; "
                         "Overwrite replaces the files once the new build is finished; Keep both gives the new "
                         "build a numbered name. Settings › General can answer this for every start."),
                     text_color=MUTED, wraplength=520, justify="left").pack(anchor="w", padx=20, pady=(0, 10))
        box = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=8)
        box.pack(fill="x", padx=20, pady=(0, 4))
        for name, hit in conflicts[:self.MAX_ROWS]:
            ctk.CTkLabel(box, text=str(name), text_color=WHITE, font=ctk.CTkFont(size=13),
                         anchor="w", justify="left", wraplength=500).pack(anchor="w", padx=12, pady=(8, 0))
            ctk.CTkLabel(box, text=str(hit), text_color=MUTED, font=ctk.CTkFont(size=11),
                         anchor="w", justify="left", wraplength=500).pack(anchor="w", padx=12, pady=(0, 6))
        if n > self.MAX_ROWS:
            ctk.CTkLabel(box, text=f"… and {n - self.MAX_ROWS} more", text_color=MUTED,
                         font=ctk.CTkFont(size=12)).pack(anchor="w", padx=12, pady=(0, 8))
        btns = ctk.CTkFrame(self, fg_color=BLACK); btns.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(btns, text="Skip", fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT,
                      width=96, command=lambda: self._done("skip")).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Keep both", fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                      width=96, border_width=1, border_color=BTN_BORDER,
                      command=lambda: self._done("keep")).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Overwrite", fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                      width=96, border_width=1, border_color=BTN_BORDER,
                      command=lambda: self._done("overwrite")).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Cancel", fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                      width=96, border_width=1, border_color=BTN_BORDER,
                      command=lambda: self._done("cancel")).pack(side="right")
        self.bind("<Return>", lambda e: self._done("skip"))
        self.bind("<Escape>", lambda e: self._done("cancel"))

    def _done(self, choice: str):
        self.choice = choice
        self.destroy()


class CountdownWindow(MessageWindow):
    SECONDS = 30

    def __init__(self, parent, action, on_go, on_cancel=None):
        super().__init__(parent)
        self._action, self._on_go, self._on_cancel = action, on_go, on_cancel
        self._left, self._job = self.SECONDS, None
        head = "Queue finished"
        self.title(head)
        self.configure(fg_color=BLACK); self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        ctk.CTkLabel(self, text=head, font=ctk.CTkFont(size=17, weight="bold"), text_color=WHITE
                     ).pack(anchor="w", padx=20, pady=(16, 2))
        self._msg = TextVar()
        ctk.CTkLabel(self, textvariable=self._msg, text_color=MUTED, wraplength=380, justify="left",
                     width=380, anchor="w").pack(anchor="w", padx=20, pady=(0, 6))
        btns = ctk.CTkFrame(self, fg_color=BLACK); btns.pack(fill="x", padx=20, pady=16)
        now = "Sleep now" if action == "sleep" else "Quit now"
        ctk.CTkButton(btns, text=now, fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT,
                      width=104, command=self._go).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Cancel", fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER, width=96,
                      border_width=1, border_color=BTN_BORDER, command=self._cancel).pack(side="right")
        self._tick()

    def _text(self) -> str:
        who = "The Mac" if IS_MAC else "The computer"
        return (tr("{who} goes to sleep in {left} s.", who=who, left=self._left) if self._action == "sleep"
                else tr("PS5 UltraPack quits in {left} s.", left=self._left))

    def _tick(self):
        self._job = None
        if self._left <= 0:
            self._go(); return
        self._msg.set(self._text())
        self._left -= 1
        self._job = self.after(1000, self._tick)

    def _stop(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _go(self):
        self._stop()
        go = self._on_go
        self.destroy()
        go()

    def _cancel(self):
        self._stop()
        cb = self._on_cancel
        self.destroy()
        if cb:
            cb()

    def destroy(self):
        self._stop()
        super().destroy()


class OrganizeView:
    """The backend does all the moving; nothing here touches the files."""
    CHECK, UNCHECK = "☑", "☐"

    def __init__(self, parent, app):
        self.app = app
        kit = app.kit
        self._q = queue.Queue()
        self._busy = False
        self._plan = None
        self._checked: set[int] = set()
        self._iid_move: dict[str, int] = {}
        self.plan_file = Path(APP_DIR) / "organize_plan.json"
        self.journal_dir = Path(APP_DIR) / "organize-undo"        # one journal per Apply: Undo goes back step by step
        self.legacy_journal = Path(APP_DIR) / "organize_journal.json"
        v = self.frame = kit.frame(parent)
        v.grid_columnconfigure(0, weight=1)
        v.grid_rowconfigure(4, weight=1)
        head, btns = app._column_header(v, "Organize", subtitle="Names and sorts everything in one folder in "
                                        "place: one title folder per game. Archives keep their names.")
        head.grid(row=0, column=0, sticky="ew", padx=(18, 14), pady=(18, 12))
        self.undo_btn = app._small(btns, "Undo last organize", "history", self.undo,
                                   tooltip="Move back what the last Apply moved")
        self.undo_btn.pack(side="left", padx=(0, 2))
        kit.rule(v).grid(row=1, column=0, sticky="ew")
        row = ctk.CTkFrame(v, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=18, pady=(12, 4))
        self.root_var = tk.StringVar(value=str(app.output_var.get() or ""))
        self._root_entry = ctk.CTkEntry(row, textvariable=self.root_var, fg_color=CARD2, text_color=WHITE)
        self._root_entry.pack(side="left", fill="x", expand=True)
        self._root_entry.bind("<Return>", lambda _e: (self.scan(), "break")[1])
        ctk.CTkButton(row, text="Choose…", width=90, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=self._pick).pack(side="left", padx=(6, 0))
        self.scan_btn = ctk.CTkButton(row, text="Scan", width=90, fg_color=BTN, hover_color=BTN_HOVER,
                                      text_color=WHITE, border_width=1, border_color=BTN_BORDER, command=self.scan)
        self.scan_btn.pack(side="left", padx=(6, 0))
        self.status_var = TextVar(value="Pick a folder and press Scan. Nothing moves before Apply.")
        ctk.CTkLabel(v, textvariable=self.status_var, text_color=MUTED, anchor="w", justify="left").grid(
            row=3, column=0, sticky="ew", padx=18, pady=(2, 6))
        _pal = style_tree(v, "Org.Treeview", self.app.kit)
        tframe = ctk.CTkFrame(v, fg_color=PANEL, corner_radius=8)
        tframe.grid(row=4, column=0, sticky="nsew", padx=18, pady=(0, 6))
        self.tree = ttk.Treeview(tframe, columns=("game", "src", "dst"), style="Org.Treeview", selectmode="browse")
        for col, text, w in (("#0", "", 34), ("game", "Game", 170), ("src", "Now", 330), ("dst", "Becomes", 330)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=round(w * ui_kit.SCALE), minwidth=round((30 if col == "#0" else 80) * ui_kit.SCALE),
                             stretch=col != "#0", anchor="w")
        self.tree.tag_configure("stay", foreground=_pal["muted"])
        watch_tree_theme(self.tree, "Org.Treeview", self.app.kit, lambda pal: self.tree.tag_configure("stay", foreground=pal["muted"]))
        vsb = ThinScrollbar(tframe, self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6)
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", lambda _e: self._toggle(self.tree.focus()))
        bottom = ctk.CTkFrame(v, fg_color="transparent")
        bottom.grid(row=5, column=0, sticky="ew", padx=18, pady=(0, 14))
        ctk.CTkButton(bottom, text="Select all", width=90, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=lambda: self._select_all(True)).pack(side="left")
        ctk.CTkButton(bottom, text="Select none", width=90, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=lambda: self._select_all(False)).pack(
            side="left", padx=(6, 0))
        self.apply_btn = ctk.CTkButton(bottom, text="Apply", width=120, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                       text_color=ON_ACCENT, command=self.apply, state="disabled")
        self.apply_btn.pack(side="right")
        self._refresh_buttons()

    def on_show(self):
        if not self.root_var.get().strip():
            self.root_var.set(str(self.app.output_var.get() or ""))
        self._refresh_buttons()
        self.frame.after(60, lambda: self._root_entry.winfo_exists() and self._root_entry.focus_set())

    def _pick(self):
        d = filedialog.askdirectory(title="Organize - the folder to sort in place", parent=self.app.root,
                                    initialdir=self.root_var.get() or str(self.app.output_var.get() or Path.home()))
        if d:
            self.root_var.set(d)
            self.scan()

    def _refresh_buttons(self):
        n = len(self._checked)
        queue_runs = bool(getattr(self.app, "_batch_running", False))
        busy = self._busy or queue_runs
        self.apply_btn.configure(text=(tr("Apply when the queue is done") if queue_runs and n else
                                       tr("Apply ({n})", n=n) if n else "Apply"),
                                 state="normal" if n and not busy else "disabled")
        self.scan_btn.configure(state="disabled" if self._busy else "normal")
        self.undo_btn.configure(state="normal" if self._last_journal() is not None and not busy else "disabled")

    def _rel(self, p: str) -> str:
        root = (self._plan or {}).get("root", "")
        return p[len(root):].lstrip("/\\") if root and p.startswith(root) else p

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._iid_move.clear()
        plan = self._plan or {"moves": [], "stays": []}
        for i, m in enumerate(plan["moves"]):
            iid = self.tree.insert("", "end", text=self.CHECK if i in self._checked else self.UNCHECK,
                                   values=(m.get("label", ""), self._rel(m["src"]), self._rel(m["dst"])))
            self._iid_move[iid] = i
        for st in plan["stays"]:
            self.tree.insert("", "end", text="", tags=("stay",),
                             values=("", self._rel(st["path"]), "stays: " + st["reason"]))
        self._refresh_buttons()

    def _toggle(self, iid):
        i = self._iid_move.get(iid)
        if i is None:
            return
        self._checked.symmetric_difference_update({i})
        self.tree.item(iid, text=self.CHECK if i in self._checked else self.UNCHECK)
        self._refresh_buttons()

    def _click(self, e):
        if self.tree.identify_column(e.x) == "#0" and self.tree.identify_region(e.x, e.y) in ("tree", "cell"):
            self._toggle(self.tree.identify_row(e.y))
            return "break"

    def _select_all(self, on: bool):
        self._checked = set(range(len((self._plan or {}).get("moves", [])))) if on else set()
        self._render()

    def _run(self, kind, args):
        self._busy = True
        self._refresh_buttons()
        cmd = self.app._backend_cmd(*args)

        def work():
            lines = []
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace", bufsize=1)
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    lines.append(line)
                    if line.startswith("ORGANIZE_PROGRESS: "):
                        self._q.put(("progress", line[len("ORGANIZE_PROGRESS: "):]))
                rc = proc.wait()
            except Exception as ex:
                lines.append(f"[ERROR] {ex}")
                rc = 1
            self._q.put((kind, (rc, lines)))
        threading.Thread(target=work, daemon=True).start()
        self.frame.after(100, self._poll)

    def _poll(self):
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind == "progress":
                    self.status_var.set(f"Reading {data}")
                    continue
                self._busy = False
                getattr(self, "_done_" + kind)(*data)
                self._refresh_buttons()
                return
        except queue.Empty:
            pass
        self.frame.after(100, self._poll)

    def scan(self, then_status: str = ""):
        root = self.root_var.get().strip()
        if not root or not Path(root).is_dir():
            messagebox.showerror("Organize", "Pick a folder first.", parent=self.app.root)
            return
        self._then = then_status
        self.status_var.set("Looking through the folder…")
        try:
            self.plan_file.unlink()
        except OSError:
            pass
        self._run("scan", ("--organize-scan", root, "--organize-plan", str(self.plan_file)))

    def _done_scan(self, rc, lines):
        try:
            self._plan = json.loads(self.plan_file.read_text(encoding="utf-8")) if rc == 0 else None
        except Exception:
            self._plan = None
        if self._plan is None:
            err = next((ln for ln in reversed(lines) if ln.startswith("[ERROR]")), lines[-1] if lines else "no output")
            self.status_var.set(tr("Scan failed: {err}", err=err))
            self.app.log("ERROR", f"Organize: scan failed: {err}")
            return
        self._checked = set(range(len(self._plan["moves"])))
        moves, stays, ok = len(self._plan["moves"]), len(self._plan["stays"]), self._plan.get("in_place", 0)
        summary = (tr("{moves} to move", moves=moves) if moves else "Nothing to move") + tr(", {ok} already in place", ok=ok) \
            + (tr(", {stays} staying (see why below)", stays=stays) if stays else "") + "."
        self.status_var.set(" ".join(x for x in (self._then, summary) if x))
        self._render()

    def apply(self):
        if not self._plan or not self._checked or self._busy:
            return
        if getattr(self.app, "_batch_running", False):
            messagebox.showinfo("Organize", "Wait until the queue has finished.", parent=self.app.root)
            return
        only = ",".join(str(i) for i in sorted(self._checked))
        self.status_var.set("Moving…")
        journal = _organize_module().new_journal_path(self.journal_dir)
        self._run("apply", ("--organize-apply", "--organize-plan", str(self.plan_file),
                            "--organize-journal", str(journal), "--organize-only", only))

    def _done_apply(self, rc, lines):
        for ln in lines:
            if ln.startswith(("[ORGANIZE]", "[WARN]", "[ERROR]")):
                self.app.log("WARN" if ln.startswith("[WARN]") else "ERROR" if ln.startswith("[ERROR]") else "INFO",
                             "Organize: " + ln.split("] ", 1)[-1])
        last = next((ln for ln in reversed(lines) if ln.startswith(("[OK]", "[ERROR]"))), "")
        self.app.log("OK" if rc == 0 else "WARN", "Organize: " + last.split("] ", 1)[-1])
        self.scan(then_status=self._result_text(last) + ".")

    @staticmethod
    def _result_text(line: str) -> str:
        """Backend's '[OK] Organized: 3 moved, 1 failed' in the UI language."""
        m = re.match(r"\[OK\] (Organized|Undone): (\d+) moved(?: back)?(?:, (\d+) failed)?", line)
        if not m:
            return line.split("] ", 1)[-1]
        moved, failed = int(m.group(2)), int(m.group(3) or 0)
        text = (plural(moved, "Organized: 1 item moved", "Organized: {n} items moved") if m.group(1) == "Organized"
                else plural(moved, "Undone: 1 item moved back", "Undone: {n} items moved back"))
        return text + (plural(failed, ", 1 failed", ", {n} failed") if failed else "")

    def _last_journal(self):
        try:
            found = _organize_module().journals(self.journal_dir, legacy=self.legacy_journal)
        except Exception:
            return None
        return found[0] if found else None

    def undo(self):
        j = self._last_journal()
        if self._busy or j is None:
            return
        try:
            d = _organize_module().describe(j)
        except Exception:
            d = {"root": "", "moves": 0, "when": ""}
        text = plural(d["moves"], "Move back the 1 item the Apply of {when} moved in {root}?",
                      "Move back the {n} items the Apply of {when} moved in {root}?",
                      when=d["when"] or "–", root=d["root"] or "–")
        if not ask_confirm("Undo last organize", text, "Move back", parent=self.app.root):
            return
        self.status_var.set("Moving back…")
        self._run("undo", ("--organize-undo", "--organize-journal", str(j)))

    def _done_undo(self, rc, lines):
        last = next((ln for ln in reversed(lines) if ln.startswith(("[OK]", "[ERROR]"))), "")
        self.app.log("OK" if rc == 0 else "WARN", "Organize: " + last.split("] ", 1)[-1])
        for ln in lines:
            if ln.startswith("[WARN]"):
                self.app.log("WARN", "Organize: " + ln[7:])
        if self.root_var.get().strip() and Path(self.root_var.get().strip()).is_dir():
            self.scan(then_status=self._result_text(last) + ".")
        else:
            self.status_var.set(self._result_text(last))


class KeystoneView:
    CHECK, UNCHECK = "☑", "☐"
    HOWTO = ("How to get the keystone: on a jailbroken console, open Apollo Save Tool and pick a save of this game "
             "that the retail game wrote. Choose Export Keystone and fetch the file by FTP from "
             "/data/apollo/<user id>/<title id>/keystone, or choose Show Keystone Fingerprint and type the "
             "64 hex digits it shows.")

    def __init__(self, parent, app):
        self.app = app
        kit = app.kit
        self._q = queue.Queue()
        self._busy = False
        self._base_ok = False
        self._base_ident = None
        self._updates: list[dict] = []       # {"path", "name", "version", "note", "on"}
        self._iid_row: dict[str, int] = {}
        self._probe_seq = 0
        self._version = None                 # version_problem() of the game package
        self._base_sfo = None
        self._wanted_fix = None              # editing a job: its version choice
        self.undo_root = Path(APP_DIR) / "keystone-undo"
        v = self.frame = kit.frame(parent)
        v.grid_columnconfigure(0, weight=1)
        v.grid_rowconfigure(9, weight=1)
        head, btns = app._column_header(v, "PS4 PKG Tools", subtitle="Makes a PS4 game package and its updates fit "
                                        "your saves, and stops the console offering an update that is merged in.")
        head.grid(row=0, column=0, sticky="ew", padx=(18, 14), pady=(18, 12))
        self.undo_btn = app._small(btns, "Undo last change", "history", self.undo,
                                   tooltip="Put back what the last PS4 PKG Tools job changed")
        self.undo_btn.pack(side="left", padx=(0, 2))
        kit.rule(v).grid(row=1, column=0, sticky="ew")

        def field_row(r, label, var, pick_text, pick_cmd, placeholder=""):
            row = ctk.CTkFrame(v, fg_color="transparent")
            row.grid(row=r, column=0, sticky="ew", padx=18, pady=(10, 2))
            ctk.CTkLabel(row, text=label, text_color=MUTED, width=110, anchor="w").pack(side="left")
            e = ctk.CTkEntry(row, textvariable=var, fg_color=CARD2, text_color=WHITE, placeholder_text=placeholder)
            e.pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text=pick_text, width=90, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                          border_width=1, border_color=BTN_BORDER, command=pick_cmd).pack(side="left", padx=(6, 0))
            return e

        def note(r, var, pady=(0, 2)):
            lbl = ctk.CTkLabel(v, textvariable=var, text_color=MUTED, anchor="w", justify="left", wraplength=760)
            lbl.grid(row=r, column=0, sticky="ew", padx=(128, 18), pady=pady)
            return lbl

        self.base_var = tk.StringVar(value="")
        self.base_note = TextVar(value="Choose the PS4 game package (.pkg) to repair.")
        self._base_entry = field_row(2, "Package:", self.base_var, "Choose…", self._pick_base)
        nr = ctk.CTkFrame(v, fg_color="transparent")
        nr.grid(row=3, column=0, sticky="ew", padx=(128, 18), pady=(0, 2))
        self._base_lbl = ctk.CTkLabel(nr, textvariable=self.base_note, text_color=MUTED, anchor="w",
                                      justify="left", wraplength=760)
        self._base_lbl.pack(side="left")
        self._other_tool = ctk.CTkButton(nr, text="Open in PS5 PKG Tools", width=130, height=24, fg_color=BTN,
                                         hover_color=BTN_HOVER, text_color=WHITE, border_width=1,
                                         border_color=BTN_BORDER, command=self._to_ps5_repair)
        self.ks_var = tk.StringVar(value="")
        self.ks_note = TextVar(value=self.HOWTO)
        field_row(4, "Keystone:", self.ks_var, "File…", self._pick_keystone,
                  placeholder=tr("a file or its hex digits; empty: keep it"))
        ks_lbl = note(5, self.ks_note)
        # version row: only for a full game with an update merged in
        self._ver_row = ctk.CTkFrame(v, fg_color="transparent")
        ctk.CTkLabel(self._ver_row, text="Version:", text_color=MUTED, width=110, anchor="w").pack(side="left")
        self.ver_var = tk.BooleanVar(value=False)
        self.ver_text = TextVar(value="")
        ctk.CTkCheckBox(self._ver_row, textvariable=self.ver_text, variable=self.ver_var, fg_color=ACCENT,
                        hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18, checkbox_height=18,
                        command=self._refresh_buttons).pack(side="left")
        self.ver_note = TextVar(value="")
        self._ver_note = ctk.CTkLabel(v, textvariable=self.ver_note, text_color=MUTED, anchor="w", justify="left",
                                      wraplength=760)
        self._notes = [self._base_lbl, ks_lbl, self._ver_note]
        v.bind("<Configure>", lambda _e: self._fit_notes(), add="+")
        upd_head = ctk.CTkFrame(v, fg_color="transparent")
        upd_head.grid(row=8, column=0, sticky="ew", padx=18, pady=(12, 4))
        upd_lbl = ctk.CTkLabel(upd_head, text="Updates", text_color=WHITE, anchor="w")
        upd_lbl.pack(side="left")
        self.upd_note = TextVar(value="Updates of this game record the package they belong to; checked ones are "
                                      "bound to the changed package.")
        self.remove_btn = ctk.CTkButton(upd_head, text="Remove from list", width=120, fg_color=BTN, hover_color=BTN_HOVER,
                                        text_color=WHITE, border_width=1, border_color=BTN_BORDER,
                                        command=self._remove_update, state="disabled")
        self.remove_btn.pack(side="right")
        add_upd = ctk.CTkButton(upd_head, text="Add…", width=80, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                                border_width=1, border_color=BTN_BORDER, command=self._add_update)
        add_upd.pack(side="right", padx=(0, 6))
        upd_note = ctk.CTkLabel(upd_head, textvariable=self.upd_note, text_color=MUTED, anchor="w", justify="left")
        upd_note.pack(side="left", padx=(10, 0))
        wrap_beside(upd_head, upd_note, upd_lbl, add_upd, self.remove_btn, pad=28)
        _pal = style_tree(v, "Ks.Treeview", self.app.kit)
        tframe = ctk.CTkFrame(v, fg_color=PANEL, corner_radius=8)
        tframe.grid(row=9, column=0, sticky="nsew", padx=18, pady=(0, 6))
        self.tree = ttk.Treeview(tframe, columns=("pkg", "ver", "note"), style="Ks.Treeview", selectmode="browse")
        watch_tree_theme(self.tree, "Ks.Treeview", self.app.kit)
        for col, text, w in (("#0", "", 34), ("pkg", "Package", 380), ("ver", "Version", 80), ("note", "Notes", 300)):
            self.tree.heading(col, text=tr(text) if text else "", anchor="w")
            self.tree.column(col, width=round(w * ui_kit.SCALE), minwidth=round((30 if col == "#0" else 60) * ui_kit.SCALE),
                             stretch=col not in ("#0", "ver"), anchor="w")
        vsb = ThinScrollbar(tframe, self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6)
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", lambda _e: self._toggle(self.tree.focus()))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_buttons(), add="+")
        fit_columns(self.tree)
        bottom = ctk.CTkFrame(v, fg_color="transparent")
        bottom.grid(row=10, column=0, sticky="ew", padx=18, pady=(0, 14))
        self.add_btn = ctk.CTkButton(bottom, text="Add to queue", width=130, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                     text_color=ON_ACCENT, command=self.add_to_queue, state="disabled")
        self.add_btn.pack(side="right")
        foot = ctk.CTkLabel(bottom, text="Changes the packages themselves. What it replaces is kept in the app folder; "
                                         "Undo last change puts it back.", text_color=MUTED, anchor="w", justify="left")
        foot.pack(side="left")
        wrap_beside(bottom, foot, self.add_btn)
        self.base_var.trace_add("write", lambda *_: self._base_changed())
        self.ks_var.trace_add("write", lambda *_: self._ks_changed())
        self._refresh_buttons()

    def on_show(self):
        self._refresh_buttons()
        if not (self.base_var.get() or "").strip():
            self.frame.after(60, lambda: self._base_entry.winfo_exists() and self._base_entry.focus_set())

    def preset(self, base: str | None = None):
        self._edit_item = None
        self._wanted_fix = None
        self.add_btn.configure(text="Add to queue")
        if base:
            self.base_var.set(str(base))

    def edit(self, item):
        self._edit_item = item
        self._wanted_fix = bool(getattr(item, "ps4_fix_version", False))
        self.add_btn.configure(text="Save changes")
        self.ks_var.set(str(getattr(item, "ps4_keystone", "") or ""))
        self._wanted = [u for u in (getattr(item, "ps4_rebind", "") or "").splitlines() if u.strip()]
        self.base_var.set(str(item.path))

    def _keystone(self):
        return _ps4pkg_module().keystone_from_text(self.ks_var.get())

    def _fix_version(self) -> bool:
        return self._version is not None and bool(self.ver_var.get())

    def _refresh_buttons(self):
        raw = (self.ks_var.get() or "").strip()
        ks = self._keystone()[0] if raw else None
        todo = ks is not None or self._fix_version() or any(u["on"] and u.get("needs", True) for u in self._updates)
        ok = self._base_ok and (not raw or ks is not None) and todo
        busy = self._busy or bool(getattr(self.app, "_batch_running", False))
        self.add_btn.configure(state="normal" if ok else "disabled", fg_color=ACCENT if ok else BTN)
        self.undo_btn.configure(state="normal" if not busy and _ks_job_module().last_journal(self.undo_root)
                                else "disabled")
        if getattr(self, "remove_btn", None) is not None and getattr(self, "tree", None) is not None:
            self.remove_btn.configure(state="normal" if self.tree.selection() else "disabled")

    def _ks_changed(self):
        raw = (self.ks_var.get() or "").strip()
        if not raw:
            self.ks_note.set(self.HOWTO)
        else:
            ks, why = self._keystone()
            self.ks_note.set(tr("Keystone {fp}…, valid.", fp=ks[0x20:0x28].hex().upper()) if ks is not None
                             else tr(why) + ".")
        self._refresh_buttons()

    def _to_ps5_repair(self):
        p = (self.base_var.get() or "").strip()
        self.app._show_view("ps5repair")
        self.app._ps5_view.preset(p)

    def _pick_base(self):
        p = filedialog.askopenfilename(parent=self.app.root, title="Choose the PS4 game package",
                                       filetypes=[("PS4 packages", "*.pkg"), ("All files", "*.*")])
        if p:
            self.base_var.set(p)

    def _pick_keystone(self):
        p = filedialog.askopenfilename(parent=self.app.root, title="Choose the keystone",
                                       filetypes=[("All files", "*.*")])
        if p:
            self.ks_var.set(p)

    def _fit_notes(self):
        fit_notes(self.frame, self._notes, beside=self._other_tool)

    def _base_changed(self):
        """Reads package, keystone and the updates beside it off the main thread."""
        self._probe_seq += 1
        seq = self._probe_seq
        self._base_ok = False
        self._updates = []
        self._version = self._base_sfo = None
        self._show_version()
        self._render()
        raw = (self.base_var.get() or "").strip()
        if not raw:
            self.base_note.set("Choose the PS4 game package (.pkg) to repair.")
            return
        p = Path(raw)
        if not (p.is_file() and p.suffix.lower() == ".pkg"):
            self.base_note.set("Not a .pkg file.")
            return
        self.base_note.set("Reading the package…")
        box: dict = {}

        def work():
            box.update(self.probe(p))

        def poll():
            if seq != self._probe_seq:
                return
            if "done" not in box:
                self.frame.after(80, poll)
                return
            self._base_ok = box["ok"]
            self._base_ident = box.get("ident")
            self._base_sfo = box.get("sfo")
            self._own = box.get("own")
            self.base_note.set(box["note"])
            (self._other_tool.pack(side="left", padx=(10, 0)) if box.get("ps5") else self._other_tool.pack_forget())
            self._fit_notes()
            self._updates = box.get("updates", [])
            self._version = box.get("version")
            if self._version is not None:
                fix = self._wanted_fix
                self.ver_var.set(self._version["named"] if fix is None else fix)
            self._wanted_fix = None
            self._show_version()
            wanted = getattr(self, "_wanted", None)
            if wanted is not None:               # editing a job: its own choice of updates
                for u in self._updates:
                    u["on"] = u["path"] in wanted
                for w in wanted:
                    if not any(u["path"] == w for u in self._updates) and Path(w).is_file():
                        self._updates.append({"path": w, "name": Path(w).name, "version": "", "on": True,
                                              "note": tr("added by hand")})
                self._wanted = None
            self._render()
        threading.Thread(target=work, daemon=True).start()
        self.frame.after(80, poll)

    def _show_version(self):
        """Only shown when the version makes the console offer the update."""
        vp = self._version
        if vp is None:
            self._ver_row.grid_remove()
            self._ver_note.grid_remove()
            return
        self.ver_text.set(tr("Set the app version to {ver} and the master version to {app}",
                             ver=vp["version"], app=vp["app_ver"]))
        self.ver_note.set(version_note(vp))
        self._ver_note.configure(text_color=YELLOW if vp["named"] else MUTED)
        self._ver_row.grid(row=6, column=0, sticky="ew", padx=18, pady=(10, 2))
        self._ver_note.grid(row=7, column=0, sticky="ew", padx=(128, 18), pady=(0, 2))

    @staticmethod
    def update_info(x, base_sfo, own) -> tuple[list[str], bool]:
        """Notes on update x, and whether the job changes more than its keystone (Tk-free)."""
        m, job = _ps4pkg_module(), _ks_job_module()
        try:
            belongs = m.general_digests(x).get(m.BASE_CONTENT_SLOT) == own
            plan = job.plan_update(x, base_sfo or {})
        except Exception:
            return [], True
        sf, note = plan.get("save_folder"), plan.get("save_folder_note")
        out = [tr("belongs to this package") if belongs else tr("made for another build of this game")]
        if sf:
            out.append(tr("save folder {old} → {new}", old=sf[0], new=sf[1]))
        if note:
            out.append(tr("saves under {old}, the game under {new}; this update cannot be changed for that",
                          old=note[0], new=note[1]))
        if job.region_changed(x):
            out.append(tr("converted from another region: its language can differ from the game's"))
        return out, not belongs or bool(sf)

    @staticmethod
    def probe(p: Path) -> dict:
        """Tk-free: identity, keystone, version and same-game updates of package p."""
        m = _ps4pkg_module()
        out = {"done": True, "ok": False}
        if _package_platform(p) == "ps5":
            out["note"] = tr("This is a PS5 package. PS5 PKG Tools check it and build a fixed copy.")
            out["ps5"] = True
            return out
        try:
            ident = m.read_identity(p)
        except Exception as e:
            out["note"] = tr("This file cannot be read as a PS4 package: {e}", e=e)
            return out
        if ident.kind != "game":
            out["note"] = ({"update": tr("This is an update, not the game. Choose the game package: its updates "
                                         "in the same folder show up below."),
                            "dlc": tr("This is DLC, not the game. Choose the game package.")}.get(ident.kind)
                           or tr("This is not a game package. Choose the game package."))
            return out
        out["ident"] = ident
        info = App._ps4_keystone_state(p)
        name = f"{ident.title or p.stem} [{ident.title_id}] [v{ident.version}]"
        if info is None:
            out["note"] = tr("{name}: its keystone cannot be read (not a fake package?).", name=name)
            return out
        if info["default"]:
            state = tr("has the default keystone, so saves of the retail game show as corrupted")
        elif not info.get("valid", True):
            state = tr("has a damaged keystone")
        else:
            state = tr("has keystone {fp}…", fp=info["fingerprint"][:16])
        out["note"] = f"{name} {state}."
        out["ok"] = True
        try:
            out["sfo"] = m.read_sfo(p)
            out["version"] = m.version_problem(p, out["sfo"])
        except Exception:
            pass
        own = out["own"] = m.general_digests(p).get(1)
        ups = []
        try:
            near = [x for x in p.parent.rglob("*.pkg") if x != p and x.is_file() and not is_fs_junk_name(x.name)
                    and len(x.relative_to(p.parent).parts) <= 3]
        except OSError:
            near = []
        for x in sorted(near):
            try:
                ui = m.read_identity(x)
                if ui.content_id != ident.content_id or ui.kind != "update":
                    continue
                slots = m.general_digests(x)
            except Exception:
                continue
            if m.BASE_CONTENT_SLOT not in slots:
                continue
            notes, needs = KeystoneView.update_info(x, out.get("sfo"), own)
            ups.append({"path": str(x), "name": x.name, "version": ui.version, "on": True,
                        "note": " · ".join(notes), "needs": needs})
        out["updates"] = ups
        return out

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._iid_row.clear()
        for i, u in enumerate(self._updates):
            iid = self.tree.insert("", "end", text=self.CHECK if u["on"] else self.UNCHECK,
                                   values=(u["name"], u["version"], u["note"]))
            self._iid_row[iid] = i
        self._render_note()

    def _render_note(self):
        self.upd_note.set(plural(sum(1 for u in self._updates if u["on"]), "1 update will be bound to the changed package.",
                                 "{n} updates will be bound to the changed package.") if self._updates
                          else tr("No update of this game found next to it; add one with Add… if you have it elsewhere."))
        self._refresh_buttons()

    def _toggle(self, iid):
        i = self._iid_row.get(iid)
        if i is None:
            return
        self._updates[i]["on"] = not self._updates[i]["on"]
        # the row changes in place, so the focus stays and Space goes on working
        self.tree.item(iid, text=self.CHECK if self._updates[i]["on"] else self.UNCHECK)
        self._render_note()

    def _click(self, e):
        row = self.tree.identify_row(e.y)
        if row:
            self.tree.focus(row)
            self.tree.selection_set(row)
            self._refresh_buttons()
        if self.tree.identify_column(e.x) == "#0" and self.tree.identify_region(e.x, e.y) in ("tree", "cell"):
            self._toggle(row)
            return "break"

    def _add_update(self):
        paths = filedialog.askopenfilenames(parent=self.app.root, title="Choose update packages",
                                            filetypes=[("PS4 packages", "*.pkg"), ("All files", "*.*")])
        m = _ps4pkg_module()
        for p in paths or ():
            if any(u["path"] == p for u in self._updates):
                continue
            why = _ks_job_module().check(self.base_var.get().strip(), [p]) if self._base_ok else []
            if why:
                messagebox.showerror("PS4 PKG Tools", "\n".join(why), parent=self.app.root)
                continue
            try:
                ver = m.read_identity(p).version
            except Exception:
                ver = ""
            notes, needs = self.update_info(p, self._base_sfo, getattr(self, "_own", None))
            self._updates.append({"path": p, "name": Path(p).name, "version": ver, "on": True,
                                  "note": " · ".join([tr("added by hand")] + notes), "needs": needs})
        self._render()

    def _remove_update(self):
        """Off the list only, the file stays put."""
        sel = self.tree.selection()
        i = self._iid_row.get(sel[0] if sel else self.tree.focus())
        if i is not None:
            del self._updates[i]
            self._render()

    def add_to_queue(self):
        base = (self.base_var.get() or "").strip()
        raw = (self.ks_var.get() or "").strip()
        ks = self._keystone()[0] if raw else None
        if not self._base_ok or (raw and ks is None):
            return
        ups = [u["path"] for u in self._updates if u["on"]]
        it = GameItem.from_ps4_keystone(Path(base), ks.hex().upper() if ks is not None else "", ups,
                                        fix_version=self._fix_version())
        ident = self._base_ident
        if ident is not None:
            it.title_id = ident.title_id
            it.display_name = ident.title or Path(base).stem
        old = getattr(self, "_edit_item", None)
        if old is not None and old in self.app.queue:
            self.app.queue[self.app.queue.index(old)] = it
        else:
            self.app.queue.append(it)
        self._edit_item = None
        self.add_btn.configure(text="Add to queue")
        self.app.update_queue_box(select_item=it)
        self.app.log("OK", tr("Queued: {name}: {what}.  Press Start to run.", name=it.display_name or it.name,
                              what=chain_summary(it)))
        self.app._show_view("queue")

    def undo(self):
        job = _ks_job_module()
        j = job.last_journal(self.undo_root)
        if self._busy or j is None:
            return
        if getattr(self.app, "_batch_running", False):      # a PS4 PKG Tools job may be writing that journal
            NoticeWindow(self.app.root, "Undo last change", tr("Undo waits until the queue has finished."))
            return
        try:
            d = job.describe(j)
        except Exception:
            d = {"base": "", "when": "", "packages": 0}
        name = Path(d["base"]).name if d["base"] else "–"
        text = (tr("Put back what PS4 PKG Tools changed in {name} on {when}?", name=name, when=d["when"])
                if d["when"] else tr("Put back what PS4 PKG Tools changed in {name}?", name=name))
        if d["packages"] > 1:
            text += " " + plural(d["packages"] - 1, "Its update is put back too.", "Its {n} updates are put back too.")
        if not ask_confirm("Undo last change", text, "Put back", parent=self.app.root):
            return
        self._busy = True
        self._undo_name = name
        self._refresh_buttons()
        cmd = self.app._backend_cmd("--ps4-keystone-undo", str(j))

        def work():
            lines = []
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace", bufsize=1)
                lines = [ln.rstrip("\n") for ln in proc.stdout]
                rc = proc.wait()
            except Exception as ex:
                lines.append(f"[ERROR] {ex}")
                rc = 1
            self._q.put((rc, lines))
        threading.Thread(target=work, daemon=True).start()
        self.frame.after(100, self._poll_undo)

    def _poll_undo(self):
        try:
            rc, lines = self._q.get_nowait()
        except queue.Empty:
            self.frame.after(100, self._poll_undo)
            return
        self._busy = False
        for ln in lines:
            if ln.startswith(("[PS4]", "[ERROR]")):
                self.app.log("ERROR" if ln.startswith("[ERROR]") else "INFO", "PS4 PKG Tools: " + ln.split("] ", 1)[-1])
        self.app.log("OK" if rc == 0 else "WARN", "PS4 PKG Tools: " + ("undone" if rc == 0 else "undo failed, see the log"))
        name = getattr(self, "_undo_name", "")
        NoticeWindow(self.app.root, "Undo last change",
                     tr("{name} is as it was before that change.", name=name) if rc == 0 else
                     tr("{name} could not be put back completely. The Log says what failed.", name=name),
                     warn=rc != 0)
        self._refresh_buttons()
        if (self.base_var.get() or "").strip():
            self._base_changed()


def _ps5_repair_module():
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import ps5_repair
    return ps5_repair


def _ps5_row_text(r: dict) -> str:
    return tr(r["tmpl"], **r["args"]) if r.get("tmpl") else r["text"]


def _ps5_console_fw() -> str:
    st = load_settings()
    fw = st.get("ps5_console_fw") or st.get("backport_target_default") or "7.61"
    return fw if is_backport_target(fw) else "7.61"


def _ps5_check_text(rows: list, keystone: bool = False, detail: bool = True) -> str:
    bad = [r for r in rows if r["level"] in ("fail", "warn")]
    if not bad:
        return plural(sum(1 for r in rows if r["level"] == "pass"), "1 check passed.", "{n} checks passed.")
    head = plural(len(bad), "1 problem", "{n} problems")
    if detail:
        head += ": " + "; ".join(_ps5_row_text(r) for r in bad[:2]) + (" …" if len(bad) > 2 else ".")
    else:
        head += "."
    unfixable = sum(1 for r in bad if not r["fix"])
    waiting = sum(1 for r in bad if r["fix"] == "keystone" and not keystone)
    fixable = len(bad) - unfixable - waiting
    parts = []
    if fixable:
        parts.append(tr("The rebuild fixes it.") if fixable == len(bad) == 1 else
                     tr("The rebuild fixes all of them.") if fixable == len(bad) else
                     plural(fixable, "The rebuild fixes 1 of them.", "The rebuild fixes {n} of them."))
    if waiting:
        parts.append(tr("The default keystone stays until you give the one from the game's dump."))
    if unfixable == len(bad):
        parts.append(tr("The rebuild cannot fix it.") if unfixable == 1 else tr("The rebuild cannot fix them."))
    tail = " ".join(parts)
    return head + " " + tail


class Ps5RepairView:
    """Repair builds a new copy (--to pkg --rebuild); the original stays as it is."""
    MARK = {"fail": "✗", "warn": "!", "info": "·", "pass": "✓"}
    FIX_TEXT = {"rebuild": "the rebuild fixes it", "trophy": "the rebuild fixes it",
                "playgo": "PlayGo option below", "firmware": "Firmware option below",
                "keystone": "Keystone field below", "standalone": "Updates option below"}
    HOWTO = ("The keystone of the game's own dump (its folder, .ffpfsc or .pkg), or a keystone file. "
             "Empty: the package keeps the one it has.")

    def __init__(self, parent, app):
        self.app = app
        kit = app.kit
        self._probe_seq = 0
        self._info = None                 # ps5_repair.examine() of the package
        self._rows: list[dict] = []
        self._edit_item = None
        self._wanted = None               # editing a job: its choices, applied once the package is read
        v = self.frame = kit.frame(parent)
        v.grid_columnconfigure(0, weight=1)
        v.grid_rowconfigure(5, weight=1)
        head, btns = app._column_header(v, "PS5 PKG Tools", subtitle="Checks a PS5 package and builds a fixed copy. "
                                        "The original stays as it is.")
        head.grid(row=0, column=0, sticky="ew", padx=(18, 14), pady=(18, 12))
        self.look_btn = app._small(btns, "Look inside", "search", self._look,
                                   tooltip="Browse the package and pull single files out")
        self.look_btn.pack(side="left", padx=(0, 2))
        self.unpack_btn = app._small(btns, "Unpack", "export", self._unpack,
                                     tooltip="Queue a job that unpacks the package into a folder")
        self.unpack_btn.pack(side="left", padx=(0, 2))
        kit.rule(v).grid(row=1, column=0, sticky="ew")

        def field_row(parent_, r, label, var, buttons, placeholder=""):
            row = ctk.CTkFrame(parent_, fg_color="transparent")
            row.grid(row=r, column=0, sticky="ew", padx=18, pady=(10, 2))
            ctk.CTkLabel(row, text=label, text_color=MUTED, width=110, anchor="w").pack(side="left")
            row.entry = ctk.CTkEntry(row, textvariable=var, fg_color=CARD2, text_color=WHITE,
                                     placeholder_text=placeholder)
            row.entry.pack(side="left", fill="x", expand=True)
            for text, cmd in buttons:
                ctk.CTkButton(row, text=text, width=90, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                              border_width=1, border_color=BTN_BORDER, command=cmd).pack(side="left", padx=(6, 0))
            return row

        self.base_var = tk.StringVar(value="")
        self.base_note = TextVar(value="Choose the PS5 package (.pkg) to check.")
        self._base_entry = field_row(v, 2, "Package:", self.base_var, [("Choose…", self._pick_base)]).entry
        nr = ctk.CTkFrame(v, fg_color="transparent")
        nr.grid(row=3, column=0, sticky="ew", padx=(128, 18), pady=(0, 2))
        self._base_lbl = ctk.CTkLabel(nr, textvariable=self.base_note, text_color=MUTED, anchor="w",
                                      justify="left", wraplength=760)
        self._base_lbl.pack(side="left")
        self._other_tool = ctk.CTkButton(nr, text="Open in PS4 PKG Tools", width=130, height=24, fg_color=BTN,
                                         hover_color=BTN_HOVER, text_color=WHITE, border_width=1,
                                         border_color=BTN_BORDER, command=self._to_ps4_repair)
        chk_head = ctk.CTkFrame(v, fg_color="transparent")
        chk_head.grid(row=4, column=0, sticky="ew", padx=18, pady=(12, 4))
        chk_lbl = ctk.CTkLabel(chk_head, text="Checks", text_color=WHITE, anchor="w")
        chk_lbl.pack(side="left")
        self.chk_note = TextVar(value="")
        self.show_pass_var = tk.BooleanVar(value=False)
        show_pass = ctk.CTkCheckBox(chk_head, text="Show passed checks", variable=self.show_pass_var, fg_color=ACCENT,
                                    hover_color=ACCENT_HOVER, text_color=MUTED, checkbox_width=16, checkbox_height=16,
                                    command=self._render)
        show_pass.pack(side="right")
        self._chk_label = ctk.CTkLabel(chk_head, textvariable=self.chk_note, text_color=MUTED, anchor="w",
                                       justify="left")
        self._chk_label.pack(side="left", padx=(10, 0))
        wrap_beside(chk_head, self._chk_label, chk_lbl, show_pass, pad=26)
        _pal = style_tree(v, "P5.Treeview", self.app.kit)
        tframe = ctk.CTkFrame(v, fg_color=PANEL, corner_radius=8)
        tframe.grid(row=5, column=0, sticky="nsew", padx=18, pady=(0, 6))
        self.tree = ttk.Treeview(tframe, columns=("area", "result", "fix"), style="P5.Treeview", selectmode="browse")
        for col, text, w in (("#0", "", 34), ("area", "Area", 110), ("result", "Result", 560), ("fix", "Fix", 160)):
            self.tree.heading(col, text=tr(text), anchor="w")
            self.tree.column(col, width=round(w * ui_kit.SCALE), minwidth=round((30 if col == "#0" else 60) * ui_kit.SCALE),
                             stretch=col == "result", anchor="w")
        def _tags(pal):
            for lv, key in (("fail", "danger"), ("warn", "warning"), ("pass", "muted")):
                self.tree.tag_configure(lv, foreground=pal[key])
        _tags(_pal)
        watch_tree_theme(self.tree, "P5.Treeview", self.app.kit, _tags)
        vsb = ThinScrollbar(tframe, self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6)
        fit_columns(self.tree)

        rep = ctk.CTkFrame(v, fg_color="transparent")
        rep.grid(row=6, column=0, sticky="ew")
        rep.grid_columnconfigure(0, weight=1)
        self.ks_var = tk.StringVar(value="")
        field_row(rep, 0, "Keystone:", self.ks_var, [("File…", self._pick_keystone), ("Dump…", self._pick_dump)],
                  placeholder=tr("the game's own dump or a keystone file; empty: keep it"))
        self.ks_note = TextVar(value=self.HOWTO)
        self._ks_note = ctk.CTkLabel(rep, textvariable=self.ks_note, text_color=MUTED, anchor="w", justify="left",
                                     wraplength=760)
        self._ks_note.grid(row=1, column=0, sticky="ew", padx=(128, 18), pady=(0, 2))
        fw = ctk.CTkFrame(rep, fg_color="transparent")
        fw.grid(row=2, column=0, sticky="ew", padx=18, pady=(8, 2))
        fw_lbl = ctk.CTkLabel(fw, text="Firmware:", text_color=MUTED, width=110, anchor="w")
        fw_lbl.pack(side="left")
        self.fw_on_var = tk.BooleanVar(value=False)
        self._fw_cb = ctk.CTkCheckBox(fw, text="Lower the required firmware to", variable=self.fw_on_var, fg_color=ACCENT,
                                      hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18,
                                      checkbox_height=18, command=self._refresh_buttons)
        self._fw_cb.pack(side="left")
        self.fw_var = tk.StringVar(value=_ps5_console_fw())
        self._fw_menu = ctk.CTkOptionMenu(fw, values=self._targets(), variable=self.fw_var, width=92, height=26,
                                          dynamic_resizing=False, command=lambda _v: self._fw_changed())
        self._fw_menu.pack(side="left", padx=(8, 0))
        fw_hint = ctk.CTkLabel(fw, text="your console's firmware, also used by the checks in Add job", text_color=MUTED,
                               anchor="w", justify="left")
        fw_hint.pack(side="left", padx=(8, 0))
        wrap_beside(fw, fw_hint, fw_lbl, self._fw_cb, self._fw_menu, pad=24)
        pg = ctk.CTkFrame(rep, fg_color="transparent")
        pg.grid(row=3, column=0, sticky="ew", padx=18, pady=(6, 2))
        ctk.CTkLabel(pg, text="PlayGo:", text_color=MUTED, width=110, anchor="w").pack(side="left")
        self.playgo_var = tk.BooleanVar(value=False)
        self._pg_cb = ctk.CTkCheckBox(pg, text="Regenerate the PlayGo tables from the files in the package",
                                      variable=self.playgo_var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                      text_color=WHITE, checkbox_width=18, checkbox_height=18,
                                      command=self._refresh_buttons)
        self._pg_cb.pack(side="left")
        sa = ctk.CTkFrame(rep, fg_color="transparent")
        sa.grid(row=4, column=0, sticky="ew", padx=18, pady=(6, 2))
        ctk.CTkLabel(sa, text="Updates:", text_color=MUTED, width=110, anchor="w").pack(side="left")
        self.standalone_var = tk.BooleanVar(value=False)
        self._sa_cb = ctk.CTkCheckBox(sa, text="Standalone game: clear the update-check URL in param.json",
                                      variable=self.standalone_var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                      text_color=WHITE, checkbox_width=18, checkbox_height=18,
                                      command=self._refresh_buttons)
        self._sa_cb.pack(side="left")
        self.out_var = tk.StringVar(value=(app.output_var.get() or "").strip())
        field_row(rep, 5, "Output:", self.out_var, [("Folder…", self._pick_out)])
        self.becomes = TextVar(value="")
        becomes = ctk.CTkLabel(rep, textvariable=self.becomes, text_color=MUTED, anchor="w", justify="left",
                               wraplength=760)
        becomes.grid(row=6, column=0, sticky="ew", padx=(128, 18), pady=(0, 2))
        self._notes = [self._base_lbl, self._ks_note, becomes]
        v.bind("<Configure>", lambda _e: self._fit_notes(), add="+")
        bottom = ctk.CTkFrame(v, fg_color="transparent")
        bottom.grid(row=7, column=0, sticky="ew", padx=18, pady=(10, 14))
        self.add_btn = ctk.CTkButton(bottom, text="Add to queue", width=130, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                     text_color=ON_ACCENT, command=self.add_to_queue, state="disabled")
        self.add_btn.pack(side="right")
        foot = ctk.CTkLabel(bottom, text="The rebuild unpacks the package and builds it again; images, license and "
                                         "PlayGo tables are put right on the way.", text_color=MUTED, anchor="w",
                            justify="left")
        foot.pack(side="left")
        wrap_beside(bottom, foot, self.add_btn)
        self.base_var.trace_add("write", lambda *_: self._base_changed())
        self.ks_var.trace_add("write", lambda *_: self._ks_changed())
        self.out_var.trace_add("write", lambda *_: self._refresh_buttons())
        self._refresh_buttons()

    def on_show(self):
        self._refresh_buttons()
        if not (self.base_var.get() or "").strip():
            self.frame.after(60, lambda: self._base_entry.winfo_exists() and self._base_entry.focus_set())

    def preset(self, base: str | None = None):
        self._edit_item = None
        self._wanted = None
        self.add_btn.configure(text="Add to queue")
        if base:
            self.base_var.set(str(base))

    def edit(self, item):
        self._edit_item = item
        self.add_btn.configure(text="Save changes")
        self._wanted = {"fw": getattr(item, "backport_target", None),
                        "playgo": bool(getattr(item, "fpkg_regen_playgo", False)),
                        "standalone": bool(getattr(item, "ps5_standalone", False))}
        self.ks_var.set(str(getattr(item, "fpkg_keystone", "") or ""))
        self.out_var.set(str(getattr(item, "output_path", "") or ""))
        self.base_var.set(str(item.path))

    def _targets(self) -> list[str]:
        raw = (self.app.fw_libs_var.get() or "").strip()
        try:
            return _backport_module().available_targets(Path(raw) if raw and Path(raw).is_dir() else None)
        except Exception:
            return ["7.61", "6.02", "10.xx"]

    def _keystone(self, raw: str):
        """Tk-free: runs on a worker."""
        img = lambda p, member, dest: self.app._extract_image_member(p, member, dest)
        return _ps5_repair_module().keystone_from(raw, image_member=img)

    def _is_update(self) -> bool:
        return self._info is not None and _ps5_repair_module().is_update(self._info)

    def _refresh_buttons(self):
        raw = (self.ks_var.get() or "").strip()
        ks_ok = not raw or getattr(self, "_ks_ok", False)
        ok = (self._info is not None and not self._info.get("error") and ks_ok and not self._is_update()
              and bool((self.out_var.get() or "").strip()))
        self.add_btn.configure(state="normal" if ok else "disabled", fg_color=ACCENT if ok else BTN)
        opts = "disabled" if self._is_update() else "normal"     # an update package is checked only
        for w in (self._fw_cb, self._fw_menu, self._pg_cb, self._sa_cb):
            w.configure(state=opts)
        has = bool((self.base_var.get() or "").strip()) and Path(self.base_var.get().strip()).is_file()
        for b in (self.look_btn, self.unpack_btn):
            b.configure(state="normal" if has else "disabled")
        self.becomes.set(self._becomes_text() if ok else "")

    def _becomes_text(self) -> str:
        """Says where the repair lands, so nobody fears for the original."""
        src = Path((self.base_var.get() or "").strip())
        out = (self.out_var.get() or "").strip()
        try:
            it = GameItem.from_chain(src, to="pkg", output_path=out)
            it.chain_rebuild = it.ps5_repair = True
            it.auto_organize = bool(self.app.auto_organize_var.get())
            param = (self._info or {}).get("param") or {}
            if param:
                it.display_name = param_title(param) or None
            target = self.app._predicted_output(it)
        except Exception:
            target = None
        if target is None:
            return tr("Becomes a new package in {folder}; the original stays as it is.", folder=out)
        if self.app._is_own_source(it, target):
            return tr("Becomes {name} beside the original, which stays as it is.",
                      name=f"{Path(target).stem} (2){Path(target).suffix}")
        if Path(target).exists():
            return tr("{name} is already in the output folder: Settings › General › When the output is "
                      "already there decides.", name=Path(target).name)
        return tr("Becomes {path}", path=target)

    def _ks_changed(self):
        """Off the main thread: a dump may need a small extraction."""
        raw = (self.ks_var.get() or "").strip()
        self._ks_ok = False
        self._ks_seq = getattr(self, "_ks_seq", 0) + 1
        seq = self._ks_seq
        if not raw:
            self.ks_note.set(self.HOWTO)
            self._ks_note.configure(text_color=MUTED)
            self._render()
            return
        self.ks_note.set("Reading the keystone…")
        box: dict = {}

        def work():
            try:
                box["v"] = self._keystone(raw)
            except Exception as e:
                box["v"] = (None, str(e))

        def poll():
            if seq != self._ks_seq:
                return
            if "v" not in box:
                self.frame.after(80, poll); return
            ks, why = box["v"]
            self._ks_ok = ks is not None
            self._ks_bytes = (raw, ks)
            self.ks_note.set(tr("Keystone {fp}…, will go into the package.", fp=ks[0x20:0x28].hex().upper())
                             if ks is not None else tr(why) + ".")
            self._ks_note.configure(text_color=MUTED if ks is not None else YELLOW)
            self._render()
        threading.Thread(target=work, daemon=True).start()
        self.frame.after(80, poll)

    def _pick_base(self):
        p = filedialog.askopenfilename(parent=self.app.root, title="Choose the PS5 package",
                                       filetypes=[("PS5 packages", "*.pkg"), ("All files", "*.*")])
        if p:
            self.base_var.set(p)

    def _pick_keystone(self):
        p = filedialog.askopenfilename(parent=self.app.root, title="Choose the keystone",
                                       filetypes=[("Keystone, image or package", "*"), ("All files", "*.*")])
        if p:
            self.ks_var.set(p)

    def _pick_dump(self):
        p = filedialog.askdirectory(parent=self.app.root, title="Choose the game's own dump (its folder)")
        if p:
            self.ks_var.set(p)

    def _pick_out(self):
        p = filedialog.askdirectory(parent=self.app.root, title="Choose the output folder")
        if p:
            self.out_var.set(p)

    def _fw_changed(self):
        try:
            save_settings({"ps5_console_fw": self.fw_var.get()})
        except Exception:
            pass
        if self._info is not None:
            self._rows = _ps5_repair_module().findings(self._info, self.fw_var.get())
            self.fw_on_var.set(any(r["fix"] == "firmware" and r["level"] in ("fail", "warn") for r in self._rows))
            self._render()

    def _fit_notes(self):
        fit_notes(self.frame, self._notes, beside=self._other_tool)

    def _base_changed(self):
        self._probe_seq += 1
        seq = self._probe_seq
        self._info = None
        self._rows = []
        self._render()
        raw = (self.base_var.get() or "").strip()
        if not raw:
            self.base_note.set("Choose the PS5 package (.pkg) to check.")
            self.chk_note.set("")
            return
        p = Path(raw)
        if not (p.is_file() and p.suffix.lower() == ".pkg"):
            self.base_note.set("Not a .pkg file.")
            return
        self.base_note.set("Checking the package…")
        self.chk_note.set("")
        fw = self.fw_var.get()
        box: dict = {}

        def work():
            m = _ps5_repair_module()
            try:
                if m.fpkg.is_ps4(p):
                    box["v"] = ({"error": "ps4"}, [])
                else:
                    info = m.examine(p, neighbours=True)
                    box["v"] = (info, m.findings(info, fw))     # one value: the poll never sees half
            except Exception as e:
                box["v"] = ({"error": str(e), "crash": True}, [])

        def poll():
            if seq != self._probe_seq:
                return
            if "v" not in box:
                self.frame.after(80, poll); return
            info, rows = box["v"]
            ps4 = info.get("error") == "ps4"
            (self._other_tool.pack(side="left", padx=(10, 0)) if ps4 else self._other_tool.pack_forget())
            self._fit_notes()
            if info.get("error") and not info.get("results"):
                self.base_note.set(self._unreadable_text(info))
                self._refresh_buttons()
                return
            self._info, self._rows = info, rows
            self.base_note.set(self._identity_text(p, info)
                               + (" · " + tr("an update package: checked only") if self._is_update() else ""))
            want = self._wanted
            fixes = {r["fix"] for r in self._rows if r["level"] in ("fail", "warn")}
            if want is not None:                 # editing a job: its own choices
                if want.get("fw"):
                    self.fw_var.set(want["fw"])
                self.fw_on_var.set(bool(want.get("fw")))
                self.playgo_var.set(bool(want.get("playgo")))
                self.standalone_var.set(bool(want.get("standalone")))
                self._wanted = None
            else:
                self.fw_on_var.set("firmware" in fixes)
                self.playgo_var.set("playgo" in fixes)
                self.standalone_var.set("standalone" in fixes)
            self._render()
        threading.Thread(target=work, daemon=True).start()
        self.frame.after(80, poll)

    @staticmethod
    def _unreadable_text(info: dict) -> str:
        if info.get("error") == "ps4":
            return tr("This is a PS4 package. PS4 PKG Tools are the tools for it.")
        if info.get("crash"):
            return tr("The check failed: {e}", e=info.get("error"))
        return tr("Not a readable PS5 package: {e}", e=info.get("error"))

    def _to_ps4_repair(self):
        p = (self.base_var.get() or "").strip()
        self.app._show_view("keystone")
        self.app._keystone_view.preset(p)

    @staticmethod
    def _identity_text(p: Path, info: dict) -> str:
        param = info.get("param") or {}
        ident = info.get("ident") or {}
        m = _ps5_repair_module()
        title = param_title(param) if param else ""
        bits = [f"{title or p.stem} [{param.get('titleId') or ident.get('title_id', '')}]"
                + (f" [v{param.get('contentVersion')}]" if param.get("contentVersion") else "")]
        need = m.fw_text(param.get("requiredSystemSoftwareVersion"))
        if need and need != "0.00":
            bits.append(tr("system software {v}", v=need))
        try:
            bits.append(format_size(p.stat().st_size))
        except OSError:
            pass
        if info.get("files"):
            bits.append(plural(info["files"], "1 file", "{n} files"))
        return " · ".join(bits)

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        show_pass = bool(self.show_pass_var.get())
        ks_given = bool(getattr(self, "_ks_ok", False) and (self.ks_var.get() or "").strip())
        for r in self._rows:
            if r["level"] == "pass" and not show_pass:
                continue
            fix = ("" if r["level"] in ("pass", "info") and not r["fix"] else
                   tr("the rebuild fixes it") if r["fix"] == "keystone" and ks_given else
                   tr(self.FIX_TEXT[r["fix"]]) if r["fix"] else tr("cannot be repaired here"))
            self.tree.insert("", "end", text=self.MARK.get(r["level"], "·"), tags=(r["level"],),
                             values=(tr(r["group"]), _ps5_row_text(r), fix))
        if self._rows:
            bad = [r for r in self._rows if r["level"] in ("fail", "warn")]
            self.chk_note.set(_ps5_check_text(self._rows, keystone=ks_given, detail=False))
            self._chk_label.configure(text_color=YELLOW if bad else MUTED)
            if not bad and not show_pass:
                self.tree.insert("", "end", text=self.MARK["pass"], tags=("pass",),
                                 values=("", tr("Every check passed. Show passed checks lists them."), ""))
        elif self._info is not None:
            self.tree.insert("", "end", text=self.MARK.get("info", "·"), tags=("info",),
                             values=("", tr("No check could run on this package. The Log says why."), ""))
        self._refresh_buttons()

    def _look(self):
        p = (self.base_var.get() or "").strip()
        if p and Path(p).is_file():
            self.app.open_pfs_browser(p)

    def _unpack(self):
        p = Path((self.base_var.get() or "").strip())
        out = (self.out_var.get() or "").strip()
        if not p.is_file():
            return
        if not out:
            messagebox.showerror("Output", "Choose an output folder for this job.", parent=self.app.root); return

        organize = bool(self.app.auto_organize_var.get())

        def make(s):                        # runs on the add worker: no Tk here
            it = GameItem.from_chain(s, to="folder", output_path=out)
            it.auto_organize = organize
            it.source_root = str(s.parent)
            return it
        self.app._add_jobs_async([p], make)
        self.app._show_view("queue")

    def add_to_queue(self):
        p = Path((self.base_var.get() or "").strip())
        out = (self.out_var.get() or "").strip()
        if self._info is None or not p.is_file() or not out:
            return
        raw = (self.ks_var.get() or "").strip()
        read_raw, ks = getattr(self, "_ks_bytes", ("", None))
        if not raw:
            ks = None
        elif ks is None or read_raw != raw:         # not read yet for this text
            return
        target = self.fw_var.get() if self.fw_on_var.get() else None
        app = self.app
        params = {"content_id": "", "title_id": "", "title": "", "version": "01.000.000", "inner": "kraken",
                  "backend": "builtin", "level": app._pkg_level_default(), "dll": "", "retail_normalize": True,
                  "hdr_flag": "auto", "regen_playgo": bool(self.playgo_var.get()), "fake_sign": True,
                  "backport_target": target, "backport_libs": ""}

        standalone, organize = bool(self.standalone_var.get()), bool(app.auto_organize_var.get())
        trophies = any(r["fix"] == "trophy" for r in self._rows)
        param = (self._info or {}).get("param") or {}

        def make(s):                        # runs on the add worker: no Tk here
            it = GameItem.from_chain(s, to="pkg", output_path=out, backport_target=target)
            app._apply_fpkg_params(it, params)
            it.chain_rebuild = it.ps5_repair = True
            it.ps5_standalone = standalone
            it.ps5_trophies = trophies
            it.fpkg_keystone = ks.hex().upper() if ks is not None else None
            it.auto_organize = organize
            it.source_root = str(s.parent)
            if param:
                it.display_name = param_title(param) or None
            return it
        old = self._edit_item
        self._edit_item = None
        self.add_btn.configure(text="Add to queue")
        if old is not None and old in app.queue:
            it = make(p)
            app.queue[app.queue.index(old)] = it
            app.update_queue_box(select_item=it)
            app.log("OK", tr("Changed: {name}: {what}.", name=it.display_name or it.name, what=chain_summary(it)))
        else:
            app._add_jobs_async([p], make)
        app._show_view("queue")


class LookInsideView:
    def __init__(self, parent, app):
        self.app = app
        kit = app.kit
        self.image_path = None
        self._entries = []          # full [{path, type, size}]
        self._iid_path = {}         # tree item id -> rel path
        self._proc = None           # running extract subprocess (for cancel)
        self._q = queue.Queue()
        self._title = "Look inside"
        v = self.frame = kit.frame(parent)
        v.grid_columnconfigure(0, weight=1)
        v.grid_rowconfigure(4, weight=1)
        head, _btns = app._column_header(v, "Look inside", subtitle="Opens a .ffpfs, .ffpfsc or .pkg and "
                                         "extracts single files or folders; the image is never unpacked as a whole.")
        head.grid(row=0, column=0, sticky="ew", padx=(18, 14), pady=(18, 12))
        kit.rule(v).grid(row=1, column=0, sticky="ew")
        row = ctk.CTkFrame(v, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=18, pady=(12, 4))
        self.src_var = tk.StringVar(value="")
        e = ctk.CTkEntry(row, textvariable=self.src_var, fg_color=CARD2, text_color=WHITE)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda _e: self._load())
        ctk.CTkButton(row, text="Choose…", width=90, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=self._pick).pack(side="left", padx=(6, 0))
        ctk.CTkButton(row, text="Open", width=90, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=self._load).pack(side="left", padx=(6, 0))
        srow = ctk.CTkFrame(v, fg_color="transparent")
        srow.grid(row=3, column=0, sticky="ew", padx=18, pady=(2, 6))
        self.status_var = TextVar(value="Choose an image, or drop one on the window. Nothing is unpacked.")
        ctk.CTkLabel(srow, textvariable=self.status_var, text_color=MUTED, anchor="w").pack(side="left")
        self.count_var = TextVar(value="")
        ctk.CTkLabel(srow, textvariable=self.count_var, text_color=MUTED).pack(side="right")
        self.filter_var = tk.StringVar()
        # a CTkEntry with a textvariable shows no placeholder, hence the label
        ctk.CTkEntry(srow, textvariable=self.filter_var, fg_color=CARD2, text_color=WHITE, width=220).pack(
            side="right", padx=(0, 10))
        ctk.CTkLabel(srow, text="Filter", text_color=MUTED).pack(side="right", padx=(16, 6))
        self.filter_var.trace_add("write", lambda *_: self._render())
        _pal = style_tree(v, "PFS.Treeview", self.app.kit)
        tframe = ctk.CTkFrame(v, fg_color=PANEL, corner_radius=8)
        tframe.grid(row=4, column=0, sticky="nsew", padx=18, pady=(0, 6))
        self.tree = ttk.Treeview(tframe, columns=("size", "type"), style="PFS.Treeview", selectmode="extended")
        watch_tree_theme(self.tree, "PFS.Treeview", self.app.kit)
        self.tree.heading("#0", text="Name", anchor="w"); self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("type", text="Type", anchor="center")
        self.tree.column("#0", width=round(470 * ui_kit.SCALE), anchor="w")
        self.tree.column("size", width=round(110 * ui_kit.SCALE), anchor="e", stretch=False)
        self.tree.column("type", width=round(80 * ui_kit.SCALE), anchor="center", stretch=False)
        vsb = ThinScrollbar(tframe, self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6)
        bottom = ctk.CTkFrame(v, fg_color="transparent")
        bottom.grid(row=5, column=0, sticky="ew", padx=18, pady=(0, 14))
        # unpack all goes through the queue, so it gets the space check
        self.extract_all_btn = ctk.CTkButton(bottom, text="Unpack all as a job…", width=160, fg_color=BTN,
                                             hover_color=BTN_HOVER, text_color=WHITE, border_width=1,
                                             border_color=BTN_BORDER, command=self._unpack_all,
                                             state="disabled")
        self.extract_all_btn.pack(side="left")
        self.cancel_btn = ctk.CTkButton(bottom, text="Cancel", width=90, fg_color=BTN, hover_color=BTN_HOVER,
                                        text_color=WHITE, border_width=1, border_color=BTN_BORDER,
                                        command=self._cancel)
        self.progress = ctk.CTkProgressBar(bottom, width=220)
        self.progress.set(0)
        self.extract_sel_btn = ctk.CTkButton(bottom, text="Extract selected…", width=150, fg_color=ACCENT,
                                             hover_color=ACCENT_HOVER, text_color=ON_ACCENT,
                                             command=lambda: self._extract(False), state="disabled")
        self.extract_sel_btn.pack(side="right")
        self._show_btn = ctk.CTkButton(bottom, text=REVEAL_LABEL, width=130, fg_color=BTN, hover_color=BTN_HOVER,
                                       text_color=WHITE, border_width=1, border_color=BTN_BORDER,
                                       command=lambda: open_path(getattr(self, "_ext_dest", "") or "."))
        self.frame.after(120, self._poll)

    def title(self) -> str:
        return self._title

    def open(self, path) -> None:
        self.src_var.set(str(path))
        self._load()

    def _pick(self):
        p = filedialog.askopenfilename(parent=self.app.root, title="Select a .ffpfs / .ffpfsc image or a .pkg (fPKG)",
                                       filetypes=[("PFS images / fPKG", "*.ffpfsc *.ffpfs *.pkg"),
                                                  ("PFS images", "*.ffpfsc *.ffpfs"),
                                                  ("PS5 packages", "*.pkg"), ("All files", "*.*")])
        if p:
            self.open(p)

    def _load(self):
        raw = (self.src_var.get() or "").strip()
        if not raw or not Path(raw).is_file():
            messagebox.showerror("Not found", "Pick a .ffpfs / .ffpfsc / .pkg file first.", parent=self.app.root)
            return
        self.image_path = Path(raw)
        kind = "  (fPKG)" if self.image_path.suffix.lower() == ".pkg" else ""
        self._title = tr("Look inside  ·  {name}{kind}", name=self.image_path.name, kind=kind)
        self.status_var.set(tr("Reading {name}…", name=self.image_path.name))
        self.extract_sel_btn.configure(state="disabled"); self.extract_all_btn.configure(state="disabled")
        cmd = self.app._backend_cmd("--list-image", str(self.image_path))

        def _worker():
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=600)
                self._q.put(("list_out", (r.stdout or "") + "\n" + (r.stderr or "")))
            except Exception as e:
                self._q.put(("list_err", str(e)))
        threading.Thread(target=_worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                try:
                    if kind == "list_out":
                        self._on_list(payload)
                    elif kind == "list_err":
                        self.status_var.set(tr("Could not read it: {e}", e=payload))
                    elif kind == "ext_line":
                        self._on_ext_line(payload)
                    elif kind == "ext_done":
                        self._on_ext_done(payload)
                except Exception as e:
                    # a handler failure must not stop the polling (the view would sit frozen)
                    try:
                        self.status_var.set(tr("Error: {e}", e=e))
                        self.app.log("ERROR", f"Look inside: '{kind}' handler failed: {e}")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        finally:
            try:
                if self.frame.winfo_exists():
                    self.frame.after(120, self._poll)
            except Exception:
                pass

    def _on_list(self, text):
        line = next((l for l in text.splitlines() if l.startswith("PFSBROWSE_JSON:")), None)
        if not line:
            err = next((l for l in text.splitlines() if l.startswith("PFSBROWSE_ERROR:")), None)
            self.status_var.set(err.split("PFSBROWSE_ERROR:", 1)[1].strip() if err
                                else "Could not read this image.")
            return
        try:
            data = json.loads(line.split("PFSBROWSE_JSON:", 1)[1])
        except Exception as e:
            self.status_var.set(tr("Bad listing: {e}", e=e)); return
        self._entries = data.get("entries", [])
        self._render()
        nf, nd = data.get("file_count", 0), data.get("dir_count", 0)
        name = self.image_path.name if self.image_path else ""
        self.status_var.set(tr("{name}: {files}, {folders}", name=name, files=plural(nf, "1 file", "{n} files"),
                               folders=plural(nd, "1 folder", "{n} folders")))
        self.extract_sel_btn.configure(state="normal"); self.extract_all_btn.configure(state="normal")

    def _render(self):
        flt = (self.filter_var.get() or "").strip().lower()
        self.tree.delete(*self.tree.get_children())
        self._iid_path = {}
        node = {"": ""}
        entries = self._entries
        if flt:
            keep = set()
            for e in entries:
                if flt in e["path"].lower():
                    parts = e["path"].split("/")
                    for i in range(len(parts)):
                        keep.add("/".join(parts[:i + 1]))
            entries = [e for e in entries if e["path"] in keep]
        for e in sorted((e for e in entries if e["type"] == "dir"),
                        key=lambda e: (e["path"].count("/"), e["path"])):
            path = e["path"]
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            iid = self.tree.insert(node.get(parent, ""), "end", text="" + path.rsplit("/", 1)[-1],
                                   values=("", tr("folder")), open=bool(flt))
            node[path] = iid
            self._iid_path[iid] = path
        for e in sorted((e for e in entries if e["type"] == "file"), key=lambda e: e["path"]):
            path = e["path"]
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            iid = self.tree.insert(node.get(parent, ""), "end", text="" + path.rsplit("/", 1)[-1],
                                   values=(format_size(e.get("size", 0)), tr("file")))
            self._iid_path[iid] = path
        self.count_var.set(tr("{n} shown", n=len(self._iid_path)))
    def _on_ext_line(self, line):
        m = re.search(r"\[#{2,}\]\s*(\d{1,3})%", line)
        if m:
            self.progress.set(min(1.0, int(m.group(1)) / 100.0))
            self.status_var.set(tr("Extracting… {pct} %", pct=m.group(1)))
        elif line.startswith("[ERROR]"):
            self._ext_last_error = line[len("[ERROR]"):].strip()
            self.status_var.set(line)

    def _extract(self, extract_all):
        if not self.image_path:
            return
        if extract_all:
            members = [e["path"] for e in self._entries if "/" not in e["path"]]
        else:
            members = [self._iid_path[i] for i in self.tree.selection() if i in self._iid_path]
            if not members:
                messagebox.showinfo("Nothing selected", "Select files or folders in the list first.",
                                    parent=self.app.root)
                return
        dest = filedialog.askdirectory(parent=self.app.root, title="Extract to folder")
        if not dest:
            return
        try:
            fd, mfpath = tempfile.mkstemp(prefix="ffpfsc_members_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(members))
        except Exception as e:
            messagebox.showerror("Error", tr("Could not stage the selection: {e}", e=e), parent=self.app.root); return
        cmd = self.app._backend_cmd("--extract-from", str(self.image_path), "--dest", dest, "--members-file", mfpath)
        self._ext_dest, self._ext_last_error = dest, ""
        self._show_btn.pack_forget()
        self.status_var.set("Extracting…"); self.progress.set(0)
        self.extract_sel_btn.pack_forget()
        self.progress.pack(side="right", padx=(8, 0))
        self.cancel_btn.pack(side="right", padx=(8, 0))
        self.extract_all_btn.configure(state="disabled")

        def _worker():
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                              text=True, encoding="utf-8", errors="replace", bufsize=1)
                for line in self._proc.stdout:
                    self._q.put(("ext_line", line.rstrip()))
                rc = self._proc.wait()
                self._q.put(("ext_done", rc))
            except Exception as e:
                self._q.put(("ext_done", f"err:{e}"))
            finally:
                self._proc = None
                try:
                    os.unlink(mfpath)
                except Exception:
                    pass
        threading.Thread(target=_worker, daemon=True).start()

    def _unpack_all(self):
        if not self.image_path:
            return
        dest = filedialog.askdirectory(parent=self.app.root, title="Unpack into folder")
        if not dest:
            return
        src = Path(self.image_path)

        def make(s):                        # runs on the add worker: no Tk here
            if s.suffix.lower() == ".pkg":
                return GameItem.from_fpkg_extract(s, output_path=dest)
            it = GameItem.from_chain(s, to="folder", output_path=dest)
            it.source_root = str(s.parent)
            return it
        self.app._add_jobs_async([src], make)
        self.status_var.set(tr("Queued: unpack {name} into {dest}. Start runs it.", name=src.name, dest=dest))

    def _on_ext_done(self, rc):
        for w in (self.progress, self.cancel_btn):
            try:
                w.pack_forget()
            except Exception:
                pass
        self.extract_sel_btn.pack(side="right")
        self.extract_sel_btn.configure(state="normal"); self.extract_all_btn.configure(state="normal")
        dest = getattr(self, "_ext_dest", "")
        last = getattr(self, "_ext_last_error", "")
        self.status_var.set(tr("Extracted into {dest}.", dest=dest) if rc == 0 else
                            (tr("Extraction failed: {why}", why=last) if last else
                             tr("Extraction failed ({rc}).", rc=rc)))
        if rc == 0:
            self._show_btn.pack(side="right", padx=(0, 8))
            self.app.log("OK", f"Look inside: extracted from {self.image_path.name if self.image_path else 'the image'} "
                               f"into {dest}.")

    def _cancel(self):
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self.status_var.set("Cancelled.")


class App:
    def __init__(self, root):
        self.root = root
        # read first-run before _setup() writes settings.json, or it is always False
        self._is_first_run = is_first_run()
        self.queue = []
        # no queue saving until the saved queue is back, or the empty first render wipes it
        self._queue_restored = False
        self.current_process = None
        self.cancel_requested = False
        self.extract_cancel_event = threading.Event()
        self.pending_start = False
        self.worker = None
        self._last_cmd_str = ""
        self._theme = "dark"
        self._batch_total   = 0
        self._batch_done    = 0
        self._batch_failed  = 0
        self._batch_running = False
        self._details_item  = None
        self._settings_win  = None
        self._active_item   = None   # item handed to the current worker (cleanup fallback)
        # no free-space recheck until cleanup rmtrees finish, stale space false-skips
        self._cleanup_inflight = 0
        self._restores_inflight = 0
        self._cleanup_lock     = threading.Lock()
        self._cleanup_wait_ticks = 0
        # a folder scan leaves the queue empty for a moment; START then would re-add it
        self._scan_in_flight = 0
        self._scan_lock      = threading.Lock()

        self.log_q      = queue.Queue()
        self.progress_q = queue.Queue()
        self.status_q   = queue.Queue()
        self.done_q     = queue.Queue()
        self.scan_q     = queue.Queue()
        self._extract_q = queue.Queue()
        self.visible_log_lines = 0
        self.auto_scroll_logs = True

        self._setup()
        self._build()
        self._restore_queue()  # needs the listbox from _build()
        self._poll()
        self._start_keep_awake()

        if self._is_first_run:
            self.root.after(200, self._show_first_run_wizard)

        self.root.after(600, self._restore_sashes)
        if sys.platform == "win32":
            self.root.after(2000, self._stay_awake_tick)
        self.root.after(1500, self._offer_startup_sweep)

    def _center_on_work_area(self, w, h):
        try:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA
            s = ctk.ScalingTracker.get_window_scaling(self.root)
            title = 32 * s
            w = min(w, int((r.right - r.left) / s) - 40)
            h = min(h, int((r.bottom - r.top - title) / s) - 40)
            x = r.left + max(0, int((r.right - r.left - w * s) / 2))
            y = r.top + max(0, int((r.bottom - r.top - title - h * s) / 2))
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _stay_awake_tick(self):
        """The sleep request belongs to the calling thread, so it lives on the Tk thread."""
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        try:
            import ctypes
            flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if getattr(self, "_batch_running", False) else 0)
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
        except Exception:
            return
        self.root.after(30000, self._stay_awake_tick)

    def _restore_window_geometry(self):
        """Only y is clamped, so the title bar never hides under the macOS menu bar."""
        geo = (getattr(self, "_saved_window_geometry", "") or "").strip()
        m = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geo)
        if m:
            try:
                w, h, x, y = (int(m.group(i)) for i in range(1, 5))
                if abs(x) > 20000 or abs(y) > 20000:
                    return  # corrupt coordinates, keep the default
                sh = self.root.winfo_screenheight()
                y = max(0, min(y, max(0, sh - 80)))   # keep the title bar grabbable
                self.root.geometry(f"{w}x{h}+{x}+{y}")
            except Exception:
                pass
        elif re.fullmatch(r"\d+x\d+", geo):
            try:
                self.root.geometry(geo)
            except Exception:
                pass

    def _on_window_configure(self, event):
        """Debounced, so the position survives any way of quitting."""
        if event.widget is not self.root:
            return
        if getattr(self, "_geo_save_after", None):
            try:
                self.root.after_cancel(self._geo_save_after)
            except Exception:
                pass
        self._geo_save_after = self.root.after(800, self._save_window_geometry)

    def _save_window_geometry(self):
        self._geo_save_after = None
        try:
            save_settings({"window_geometry": self.root.geometry()})
        except Exception:
            pass

    def _save_sashes(self):
        """The card keeps its width on resize, so save the width, not the divider spot."""
        card = getattr(self, "_card_pane", None)
        if card is not None:
            try:
                w = int(card.winfo_width())
                if w >= 300:
                    save_settings({"job_card_width": w})
            except Exception:
                pass

    def _restore_sashes(self):
        """Waits for the final window size, or the card ends up squeezed."""
        pw = getattr(self, "_paned_q", None)
        if not pw or not getattr(self, "_inspector_open", False):
            return
        try:
            self.root.update_idletasks()
            W = pw.winfo_width()
            prev = getattr(self, "_sash_last_w", None)
            self._sash_last_w = W
            tries = self._sash_restore_tries = getattr(self, "_sash_restore_tries", 0) + 1
            if (W < 626 or W != prev) and tries <= 20:
                self.root.after(150, self._restore_sashes)   # not settled yet
                return
            card_w = load_settings().get("job_card_width")
            if not isinstance(card_w, int):
                card_w = 560
            card_w = max(340, min(card_w, W - 286))
            pw.sash_place(0, W - card_w - int(pw.cget("sashwidth")), 1)
        except Exception:
            pass

    def _on_close(self):
        # Stop the keep-awake pinger so it can't touch a drive mid-teardown.
        try:
            if getattr(self, "_keepawake_stop", None):
                self._keepawake_stop.set()
        except Exception:
            pass
        # stop the backend tree before quitting, or backend and mkpfs pool run on headless
        try:
            proc_alive = bool(self.current_process and self.current_process.poll() is None)
            if proc_alive or self._job_active():
                q = self._stop_question() or tr("A job is still running.")
                if not ask_confirm("Quit and stop the job?", q, "Quit", "Keep running", danger=True):
                    return
                self.cancel_requested = True
                self.extract_cancel_event.set()
                if proc_alive:
                    _kill_process_tree(self.current_process)
            elif getattr(self, "_after_busy", False):
                if not ask_confirm("Quit now?", tr("A finished job is still moving or deleting its source "
                                                   "(After the job). Quit now and that stops half-way."),
                                   "Quit", "Wait", danger=True):
                    return
        except Exception:
            pass
        self._save_window_geometry()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _persisted_bool(self, settings, key, default):
        v = tk.BooleanVar(value=bool(settings.get(key, default)))
        v.trace_add("write", lambda *_: save_settings({key: v.get()}))
        return v

    def _set_window_icon(self) -> None:
        base = Path(getattr(sys, "_MEIPASS", "") or Path(__file__).resolve().parent) / "assets" / "icon"
        try:
            if sys.platform == "win32" and (base / "app-icon.ico").is_file():
                # default icon for all windows; the patch stops CTk dialogs swapping in its logo 200 ms late
                self.root.iconbitmap(default=str(base / "app-icon.ico"))
                ctk.CTkToplevel._windows_set_titlebar_icon = lambda _self: None
                return
            if IS_MAC and getattr(sys, "frozen", False):
                return  # frozen app: the bundle's .icns is the Dock icon
            self._icon_images = [tk.PhotoImage(master=self.root, file=str(base / n))
                                 for n in ("app-icon-256.png", "app-icon-32.png") if (base / n).is_file()]
            if self._icon_images:
                self.root.iconphoto(True, *self._icon_images)
        except Exception:
            pass                                  # an icon must never stop the app

    def _setup(self):
        settings = load_settings()
        # profile locked by another program: wait a bit, else run on defaults
        # and never save those defaults over the real settings
        self._settings_locked = False
        for _ in range(4):                        # about 4 s in all (each read retries briefly)
            _data, _read = ultra_core._read_settings()
            if _read:
                settings = _data
                break
            time.sleep(0.25)
        else:
            self._settings_locked = True
            block_settings_saves()
        self._theme = settings.get("appearance_mode", "dark")
        ctk.set_appearance_mode(self._theme)
        ctk.set_default_color_theme("blue")
        apply_ctk_theme(ctk)
        set_window_appearance(self.root, self._theme)
        self.root.title(f"{APP_NAME} {APP_VERSION}{APP_EDITION}")
        self._set_window_icon()
        self.root.geometry("1280x820")
        self.root.minsize(900, 600)

        self._saved_output = native_path(settings.get("output_folder", ""))
        self._saved_temp = native_path(settings.get("temp_folder", ""))
        self._saved_source = native_path(settings.get("source_path", ""))
        self._saved_auto_clear_temp = settings.get("auto_clear_temp", False)
        def _safe_int(v, default):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default
        # hand-edited junk here makes tk.IntVar raise TclError at startup
        self._saved_compression_level = max(1, min(9, _safe_int(settings.get("compression_level", 7), 7)))
        self._saved_cpu_count = _safe_int(settings.get("cpu_count", 0), 0)
        self._saved_block_size = settings.get("block_size", "auto")
        # auto-fit and sub-64K blocks build images the PS5 misreads; native is 64 KiB
        if self._saved_block_size in ("auto-fit", "4096", "8192", "16384", "32768"):
            self._saved_block_size = "auto"
            save_settings({"block_size": "auto"})
        self._saved_passwords = settings.get("archive_passwords")
        if self._saved_passwords is None:
            self._saved_passwords = []
            save_settings({"archive_passwords": self._saved_passwords})
        self._saved_copy_siblings = settings.get("copy_bundle_siblings", True)
        self._saved_window_geometry = settings.get("window_geometry", "")
        self._geo_save_after = None
        if not self._saved_window_geometry and sys.platform == "win32":
            self._center_on_work_area(1280, 820)
        self._restore_window_geometry()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._on_window_configure, add="+")

    def _show_first_run_wizard(self):
        if getattr(self, "_browser_only", False):
            return  # opened by double-clicking a .ffpfsc, no wizard
        wiz = FirstRunWizard(self.root)
        self.root.wait_window(wiz)
        if wiz.result.get("temp_folder"):
            self.temp_var.set(wiz.result["temp_folder"])
        if wiz.result.get("output_folder"):
            self.output_var.set(wiz.result["output_folder"])

    def panel(self, parent, **grid):
        frame = ctk.CTkFrame(parent, fg_color=PANEL, border_width=1, border_color=BORDER, corner_radius=10)
        frame.grid(**grid)
        return frame

    def _bind_dynamic_wrap(self, owner, labels, *, padding=28, min_width=180):
        def _update(event):
            wrap = max(min_width, event.width - padding)
            for label in labels:
                try:
                    label.configure(wraplength=wrap)
                except Exception:
                    pass

        owner.bind("<Configure>", _update, add="+")

    def _button(self, parent, text, command=None, green=False, red=False, yellow=False, **kw):
        if green:
            color, hover, txt = ACCENT, ACCENT_HOVER, ON_ACCENT
        elif red:
            color, hover, txt = RED, DANGER_HOVER, WHITE
        elif yellow:
            color, hover, txt = YELLOW, YELLOW, ("#ffffff", "#1b1300")
        else:
            color, hover, txt = BTN, BTN_HOVER, WHITE
        return ctk.CTkButton(parent, text=text, command=command, fg_color=color, hover_color=hover,
                              text_color=txt, border_width=0 if (green or red or yellow) else 1,
                              border_color=BTN_BORDER, **kw)

    def _build(self):
        if sys.platform == "win32":     # macOS scales the window itself
            ui_kit.set_scale(ctk.ScalingTracker.get_window_dpi_scaling(self.root))
        kit = self.kit = Kit(self.root, self._theme)
        self.root.configure(fg_color=ctk_pair("bg"))
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        # never shown, but live stage writes still go here
        self.header_status_var = tk.StringVar(value=f"v{APP_VERSION}  |  Backend: Ready")
        self._init_state_vars()
        self._init_display_vars()

        shell = kit.frame(self.root, bg="bg")
        shell.grid(row=0, column=0, sticky="nsew")
        shell.grid_columnconfigure(2, weight=1)
        shell.grid_rowconfigure(0, weight=1)
        self._build_sidebar(shell)
        kit.rule(shell, horizontal=False).grid(row=0, column=1, sticky="ns")
        views = self._views_parent = kit.frame(shell)
        views.grid(row=0, column=2, sticky="nsew")
        self._panels = PanelHost(self, views)
        EmbeddedDialog.host = self._panels
        views.grid_columnconfigure(0, weight=1)
        views.grid_rowconfigure(0, weight=1)
        self._views = {"queue": self._build_queue_view(views)}
        self._views["history"] = self._build_history_view(views)
        self._views["log"] = self._build_log_view(views)
        kit.rule(shell).grid(row=1, column=0, columnspan=3, sticky="ew")
        self._build_status_bar(shell).grid(row=2, column=0, columnspan=3, sticky="ew")
        self._show_view("queue")
        self.update_stages_display("", 0)
        self._bind_shortcuts()
        self._build_menubar()
        self._filter_root_configure()
        self.update_queue_box()

        if _HAS_DND:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)

    def _init_display_vars(self):
        S = TextVar
        self.overall_title_var = S(value="QUEUE")
        self.overall_pct_var = S(value="0%")
        self.cur_game_var = S(value="CURRENT STEP")
        self.stage_title_var = S(value="Ready")
        self.stage_detail_var = S(value="Add a job and start the queue.")
        self.stage_pct_var = S(value="")
        self.game_name_var = S(value="Name: No game selected")
        self.title_var = S(value="Title ID: - ")
        self.source_detail_var = S(value="Source: - ")
        self.orig_var = S(value="Original Size: - ")
        self.files_var = S(value="Files: - ")
        self.big_status_var = S(value="Ready")
        self.big_detail_var = S(value="Waiting for a game.")
        self.speed_var = S(value="Speed: - ")
        self.elapsed_var = S(value="Elapsed: 00:00")
        self.eta_var = S(value="ETA: - ")
        self.saved_var = S(value="Saved: - ")
        self.ratio_var = S(value="Compression: - ")
        self.rating_var = S(value="Rating: - ")
        self.temp_space_var = S(value="")
        self.ram_var = S(value="")
        self.footer_var = S(value="● Ready")
        self.queue_total_var = S(value="No jobs yet")
        self.batch_counter_var = S(value="")
        self.card_title_var = S(value="")
        self.card_meta_var = S(value="")
        self.card_target_var = S(value="")
        self.status_temp_var = S(value="")
        self.status_out_var = S(value="")
        self.last_result_var = S(value="")
        self.progress_title_var = S(value="Progress")
        self._footer_plain_var = S(value="Ready")
        self._cur_job_pct = 0
        self._space_result = None
        self._space_busy = False
        self._space_tick = 0
        self.m_speed_var, self.m_elapsed_var, self.m_eta_var = S(value=" - "), S(value=" - "), S(value=" - ")

        def _unprefix(src, dst):
            def f(*_):
                v = src.get()
                v = v.split(":", 1)[1].strip() if ":" in v else v.strip()
                dst.set(v if v and v not in (" - ", "-") else " - ")
            src.trace_add("write", f)
            f()
        _unprefix(self.speed_var, self.m_speed_var)
        _unprefix(self.elapsed_var, self.m_elapsed_var)
        _unprefix(self.eta_var, self.m_eta_var)

        def _last(*_):
            parts = [v.get() for v in (self.saved_var, self.ratio_var, self.rating_var)]
            if all(p.rstrip().endswith(" -") for p in parts):
                self.last_result_var.set("Totals and the last jobs, newest first.")
            else:
                self.last_result_var.set(tr("Last result:  {parts}", parts="   ·   ".join(parts)))
        for v in (self.saved_var, self.ratio_var, self.rating_var):
            v.trace_add("write", _last)
        _last()

        def _prog(*_):
            g = self.cur_game_var.get()
            name = g.split("·", 1)[1].strip() if "·" in g else ""
            self.progress_title_var.set(tr("Now running  ·  {name}", name=name) if name else "Progress")
        self.cur_game_var.trace_add("write", _prog)

        def _foot(*_):
            self._footer_plain_var.set(self.footer_var.get().lstrip("●").strip() or "Ready")
        self.footer_var.trace_add("write", _foot)

    def _build_sidebar(self, shell):
        kit = self.kit
        self._nav_all = []
        side = kit.frame(shell, bg="sidebar", width=188)
        side.grid(row=0, column=0, sticky="nsw")
        side.pack_propagate(False)
        brand = kit.frame(side, bg="sidebar")
        brand.pack(fill="x", padx=18, pady=(18, 14))
        kit.label(brand, bg="sidebar", text=APP_NAME, font=kit.fonts.app).pack(anchor="w")
        kit.label(brand, bg="sidebar", fg="faint", font=kit.fonts.caption, text=APP_VERSION + (" preview" if APP_EDITION else "")).pack(anchor="w", pady=(1, 0))

        def nav(parent, text, icon, cmd, tooltip=None):
            b = IconButton(parent, kit, text=text, icon=icon, command=cmd, variant="nav", height=28,
                           bg="sidebar", icon_size=15, padx=10, tooltip=tooltip)
            b.pack(fill="x", padx=8, pady=1)
            self._nav_all.append(b)
            return b
        self._nav = {
            "queue": nav(side, "Queue", "queue", lambda: self._show_view("queue"), SHORTCUT["queue"]),
            "history": nav(side, "History", "history", lambda: self._show_view("history"), SHORTCUT["history"]),
            "log": nav(side, "Log", "terminal", lambda: self._show_view("log"), SHORTCUT["log"]),
        }
        kit.rule(side, bg_token="border").pack(fill="x", padx=16, pady=(12, 10))
        kit.label(side, bg="sidebar", fg="faint", font=kit.fonts.caption, text="Tools").pack(anchor="w", padx=18, pady=(0, 3))
        self._nav["look"] = nav(side, "Look inside", "search", lambda: self._show_view("look"),
                                "Browse a .ffpfsc, .ffpfs or .pkg and pull single files out")
        self._nav["organize"] = nav(side, "Organize", "folders", lambda: self._show_view("organize"),
                                    "Name and sort everything in a folder in place")
        self._nav["keystone"] = nav(side, "PS4 PKG Tools", "key", lambda: self._show_view("keystone"),
                                    "Keystone, version and updates of a PS4 game package")
        self._nav["ps5repair"] = nav(side, "PS5 PKG Tools", "package", lambda: self._show_view("ps5repair"),
                                     "Check a PS5 package and build a fixed copy")
        nav(side, "Clean temp", "broom", self.clear_temp_files, "Delete leftovers in the temp folder")
        bottom = kit.frame(side, bg="sidebar")
        bottom.pack(side="bottom", fill="x", pady=(0, 10))
        self._nav["settings"] = nav(bottom, "Settings", "settings", self.open_settings, SHORTCUT["settings"])
        self._donate_nav = nav(bottom, "Buy me a coffee", "coffee", lambda: webbrowser.open(DONATE_URL),
                               "Support the development with a PayPal donation")
        kit.fit_width(side, 188, *(b.natural_width() + 16 for b in self._nav_all),
                      kit.fonts.app.measure(APP_NAME) + 36)

    def _show_view(self, name):
        self._panels.close_all()            # the sidebar always works: it leaves an open panel
        if name == "settings" and "settings" not in self._views:
            self._settings_view = SettingsView(self._views_parent, self)
            self._views["settings"] = self._settings_view.frame
        if name == "look" and "look" not in self._views:
            self._look_view = LookInsideView(self._views_parent, self)
            self._views["look"] = self._look_view.frame
        if name == "organize" and "organize" not in self._views:
            self._organize_view = OrganizeView(self._views_parent, self)
            self._views["organize"] = self._organize_view.frame
            # Apply and Undo wait while the queue runs: their state follows it
            self.batch_counter_var.trace_add("write", lambda *_: self._organize_view._refresh_buttons())
        if name == "keystone" and "keystone" not in self._views:
            self._keystone_view = KeystoneView(self._views_parent, self)
            self._views["keystone"] = self._keystone_view.frame
            self.batch_counter_var.trace_add("write", lambda *_: self._keystone_view._refresh_buttons())
        if name == "ps5repair" and "ps5repair" not in self._views:
            self._ps5_view = Ps5RepairView(self._views_parent, self)
            self._views["ps5repair"] = self._ps5_view.frame
        if name == "organize":
            self._organize_view.on_show()
        if name == "keystone":
            self._keystone_view.on_show()
        if name == "ps5repair":
            self._ps5_view.on_show()
        for n, f in self._views.items():
            if n == name:
                f.grid(row=0, column=0, sticky="nsew")
            else:
                f.grid_remove()
        for n, b in self._nav.items():
            b.configure(selected=(n == name))
        self._view = name
        if name == "log":
            try:
                self.log_box.see("end")
            except Exception:
                pass

    def _filter_root_configure(self):
        """Tk fires the toplevel's Configure for every child; drop those in Tcl, saves ~20 ms."""
        try:
            w = self.root._w
            script = self.root.bind("<Configure>")
            guard = 'if {"%W" ne "' + w + '"} continue'
            if script and not script.startswith(guard):
                self.root.tk.call("bind", w, "<Configure>", guard + "\n" + script)
        except Exception:
            pass

    def _bind_shortcuts(self):
        # macOS: the menu key equivalents win, these bindings are for the others
        keys = {f"<{MOD}-n>": self.open_job_dialog, f"<{MOD}-r>": self.start,
                f"<{MOD}-period>": self._stop_if_running, DETAILS_SEQ: self.toggle_inspector}
        views = {f"<{MOD}-o>": self.open_pfs_browser, f"<{MOD}-comma>": self.open_settings,
                 f"<{MOD}-Key-1>": lambda: self._show_view("queue"),
                 f"<{MOD}-Key-2>": lambda: self._show_view("history"),
                 f"<{MOD}-Key-3>": lambda: self._show_view("log")}
        for seq, fn in keys.items():
            try:
                self.root.bind(seq, lambda e, f=fn: (None if self._panels.stack else f(), "break")[1])
            except tk.TclError:
                pass
        for seq, fn in views.items():
            try:
                self.root.bind(seq, lambda e, f=fn: (f(), "break")[1])
            except tk.TclError:
                pass

    def _column_header(self, parent, title, subtitle=None, subtitle_var=None, bg="surface"):
        kit = self.kit
        head = kit.frame(parent, bg=bg)
        head.grid_columnconfigure(0, weight=1)
        tb = kit.frame(head, bg=bg)
        tb.grid(row=0, column=0, sticky="w")
        kit.label(tb, bg=bg, text=title, font=kit.fonts.view).pack(anchor="w")
        btns = kit.frame(head, bg=bg)
        btns.grid(row=0, column=1, sticky="e")
        if subtitle or subtitle_var:
            kw = {"textvariable": subtitle_var} if subtitle_var else {"text": subtitle}
            sub = kit.label(tb, bg=bg, fg="muted", font=kit.fonts.small, **kw)
            sub.pack(anchor="w", pady=(2, 0))
            # a narrow window wraps the line instead of cutting it under the buttons
            head.bind("<Configure>", lambda e: sub.configure(
                wraplength=max(200, e.width - btns.winfo_reqwidth() - round(16 * ui_kit.SCALE))), add="+")
        return head, btns

    def _small(self, parent, text, icon, cmd, variant="ghost", tooltip=None, bg="surface"):
        return IconButton(parent, self.kit, text=text, icon=icon, command=cmd, variant=variant, height=26,
                          padx=9, icon_size=13, font=self.kit.fonts.small, bg=bg, tooltip=tooltip)

    def _text_view(self, parent, fg="text", mirror=None):
        kit = self.kit
        box = kit.frame(parent)
        box.grid_columnconfigure(0, weight=1)
        box.grid_rowconfigure(0, weight=1)
        if mirror is not None:
            t = LogText(box, kit, mirror=mirror, mirror_lines=400, height=10)
        else:
            t = kit.text(box, fg=fg, height=8, state="disabled")
        t.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(0, 8))
        sb = tk.Scrollbar(box, orient="vertical", command=t.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=(0, 8))
        t.configure(yscrollcommand=sb.set)
        return box, t

    def _build_queue_view(self, parent):
        kit = self.kit
        pw = tk.PanedWindow(parent, orient="horizontal", sashwidth=6, sashrelief="flat", bd=0,
                            opaqueresize=True, showhandle=False, sashpad=0)
        kit.style(pw, bg="surface")
        self._paned_q = pw
        pw.bind("<ButtonRelease-1>", lambda e: self._save_sashes(), add="+")

        left = kit.frame(pw)
        pw.add(left, minsize=280, stretch="always")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(2, weight=1)
        self._q_sub_var = TextVar(value=self.queue_total_var.get())   # shortened when narrow
        head, btns = self._column_header(left, "Queue", subtitle_var=self._q_sub_var)
        head.grid(row=0, column=0, sticky="ew", padx=(18, 14), pady=(18, 12))
        self._parked_btn = self._small(head, "", "drive", self._show_parked,
                                       tooltip="Jobs whose drive is not connected: they come back when it is")
        self.queue_total_var.trace_add("write", lambda *_: self._fit_queue_header())
        self._clear_btn = self._small(btns, "Clear completed", "check-circle", lambda: self.clear_jobs("done"),
                                      tooltip="Remove the finished jobs. Right-click: clear failed, clear all")
        self._clear_btn.grid(row=0, column=0, padx=(0, 6))
        for seq in (("<Button-2>", "<Button-3>", "<Control-Button-1>") if IS_MAC else ("<Button-3>",)):
            self._clear_btn.bind(seq, self._clear_menu, add="+")
        self._rescan_btn = self._small(btns, "Rescan", "refresh", self.rescan_last_source,
                                       tooltip="Scan the last source folder for new downloads and add the ones that are not already here.")
        self._rescan_btn.grid(row=0, column=1, padx=(0, 6))
        self._add_btn = self._small(btns, "Add job", "plus", self.open_job_dialog, variant="secondary",
                                    tooltip=tr("Pick a source, what to change in it and what comes out  {add}", add=SHORTCUT['add']))
        self._add_btn.grid(row=0, column=2, padx=(0, 6))
        # one fixed-size slot for Start/Pause/Stop, so nothing jumps when a run starts
        self.transport = TransportControl(
            btns, kit, on_start=self.start, on_pause=self.toggle_pause, on_stop=self.request_stop,
            start_tip=tr("Run the jobs from the top  {start}", start=SHORTCUT['start']), pause_tip=self._PAUSE_TIPS[False],
            stop_tip=tr("Stop the running job and the queue  {stop}", stop=SHORTCUT['stop']))
        self.transport.grid(row=0, column=3)
        self.start_btn, self.pause_btn, self.stop_btn = self.transport.start, self.transport.pause, self.transport.stop
        self._details_btn = self._small(btns, "", "sidebar-right", self.toggle_inspector,
                                        tooltip=tr("Show or hide the details  {details}", details=SHORTCUT['details']))
        self._details_btn.grid(row=0, column=4, padx=(8, 0))
        self._q_head, self._q_title = head, head.grid_slaves(row=0, column=0)[0]
        head.bind("<Configure>", self._fit_queue_header, add="+")
        self.queue_listbox = QueueList(
            left, kit, on_select=lambda i: self._on_queue_clicked(), on_activate=self._on_queue_double_click,
            on_move=self.move_job,
            on_context=self._queue_context_menu,
            on_delete=self.queue_remove_selected, empty_title="Your queue is empty",
            empty_body="Drop a game folder, archive, disk image, .ffpfs, .ffpfsc or .pkg into this window, "
                       "or use Add job.")
        self.queue_listbox.grid(row=2, column=0, sticky="nsew")
        self._add_box = kit.frame(left)
        self._add_box.grid(row=1, column=0, sticky="ew", padx=(18, 14), pady=(0, 10))
        self._add_box.grid_columnconfigure(0, weight=1)
        self._add_label_var = TextVar(value="")
        kit.label(self._add_box, fg="muted", font=kit.fonts.small, textvariable=self._add_label_var,
                  anchor="w").grid(row=0, column=0, sticky="ew")
        self._add_bar = ProgressBar(self._add_box, kit, height=4)
        self._add_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._add_box.grid_remove()
        self._all_box = kit.frame(left)
        self._all_box.grid(row=3, column=0, sticky="ew")
        self._all_box.grid_columnconfigure(0, weight=1)
        kit.rule(self._all_box).grid(row=0, column=0, columnspan=2, sticky="ew")
        self._all_left_var, self._all_right_var = TextVar(value=""), TextVar(value="")
        kit.label(self._all_box, fg="text", font=kit.fonts.small, textvariable=self._all_left_var,
                  anchor="w").grid(row=1, column=0, sticky="w", padx=(18, 8), pady=(10, 0))
        kit.label(self._all_box, fg="muted", font=kit.fonts.small, textvariable=self._all_right_var,
                  anchor="e").grid(row=1, column=1, sticky="e", padx=(8, 14), pady=(10, 0))
        self._all_bar = ProgressBar(self._all_box, kit, height=3, fill="muted")
        self._all_bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(18, 14), pady=(6, 12))
        self._all_box.grid_remove()

        # the card keeps its width on resize; relaying it out per drag step cost ~60 ms
        right = self._card_pane = kit.frame(pw, bg="inspector")
        self._inspector_open = False
        right.grid_columnconfigure(1, weight=1)
        right.grid_rowconfigure(0, weight=1)
        kit.rule(right, bg_token="border_strong", horizontal=False).grid(row=0, column=0, sticky="ns")
        self._build_job_card(right)
        return pw

    def _queue_context_menu(self, event, idx):
        m = tk.Menu(self.root, tearoff=0)
        marked = self._marked_items()
        if len(marked) > 1:
            running = self._running_item()
            n = len([it for it in marked if it is not running])
            m.add_command(label=plural(n, "Remove 1 job", "Remove {n} jobs"), command=self.queue_remove_selected,
                          state="normal" if n else "disabled")
            m.add_command(label="Select only this job", command=lambda i=idx: (
                self.queue_listbox.selection_set(i), self._on_queue_clicked()))
            m.add_separator()
            self._add_clear_items(m)
            try:
                m.tk_popup(event.x_root, event.y_root)
            finally:
                m.grab_release()
            return
        m.add_command(label="Edit job…", command=lambda: self._on_queue_double_click(None))
        m.add_separator()
        _it = self.queue[idx] if 0 <= idx < len(self.queue) else None
        m.add_command(label="Run next", command=lambda it=_it: self.run_job_next(it),
                      state="normal" if (_it is not None and getattr(_it, "status", "") not in self._TERMINAL_STATUSES
                                         and _it is not self._running_item()) else "disabled")
        m.add_command(label="Move up", command=self.queue_move_up)
        m.add_command(label="Move down", command=self.queue_move_down)
        m.add_separator()
        m.add_command(label="Remove", command=self.queue_remove_selected)
        m.add_separator()
        self._add_clear_items(m)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _fit_queue_header(self, _e=None):
        try:
            width = self._q_head.winfo_width()
            if width <= 1:
                return
            full = self.queue_total_var.get()
            short = full.split("  ·  ")[0]         # "3 jobs" without the size, as a last step
            run = [self.transport]                 # one fixed size, running or not
            for sub, clear_t, rescan_t, add_t in (
                    (full, "Clear completed", "Rescan", "Add job"),
                    (full, "Clear completed", "", "Add job"),
                    (full, "", "", "Add job"),
                    (full, "", "", ""),
                    (short, "", "", "")):
                if self._q_sub_var.get() != sub:
                    self._q_sub_var.set(sub)
                if self._clear_btn._text != clear_t:
                    self._clear_btn.configure(text=clear_t)
                if self._rescan_btn._text != rescan_t:
                    self._rescan_btn.configure(text=rescan_t)
                if self._add_btn._text != add_t:
                    self._add_btn.configure(text=add_t)
                title_w = max(w.winfo_reqwidth() for w in self._q_title.winfo_children())
                need = sum(b.winfo_reqwidth() for b in (self._clear_btn, self._rescan_btn,
                                                       self._add_btn, *run, self._details_btn)) + 20
                if title_w + 16 + need <= width:
                    break
        except Exception:
            pass

    _CLEAR_KINDS = {"done": ("Done",), "failed": ("Failed", "Skipped", "Cancelled")}

    def _clearable(self, which: str) -> list:
        """Never includes the running job."""
        running = self._running_item()
        if which == "all":
            return [it for it in self.queue if it is not running]
        want = self._CLEAR_KINDS.get(which, ())
        return [it for it in self.queue if it is not running and getattr(it, "status", "") in want]

    def _add_clear_items(self, menu) -> None:
        for label, which in (("Clear completed", "done"), ("Clear failed", "failed"), ("Clear all", "all")):
            menu.add_command(label=label, command=lambda w=which: self.clear_jobs(w),
                             state="normal" if self._clearable(which) else "disabled")

    def _clear_menu(self, event):
        m = tk.Menu(self.root, tearoff=0)
        self._add_clear_items(m)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()
        return "break"

    def clear_jobs(self, which: str) -> None:
        """A failed job's kept extraction goes with it, hence the question first."""
        gone = self._clearable(which)
        if not gone:
            return
        kept = [it for it in gone if getattr(it, "kept_extract", False)]
        if which == "all" and self._batch_running:
            if not ask_confirm("Clear all", tr("A job is running. Remove every other job from the queue? The running "
                                               "job finishes normally."), "Remove them", danger=True):
                return
        elif kept:
            n = len(kept)
            if not ask_confirm(
                    "Clear jobs", plural(n, "1 of these jobs keeps what it extracted from its archive, for a retry. "
                             "Clearing deletes that copy.",
                             "{n} of these jobs keep what they extracted from their archives, for a retry. "
                             "Clearing deletes those copies."), "Clear", danger=True):
                return
        for it in gone:
            self._drop_kept_extract(it)
            self.queue.remove(it)
        if which == "all":
            self._queue_missing_saved = []   # a deliberate clear also drops parked entries
            self._refresh_parked()
        word = {"done": "completed", "failed": "failed or cancelled", "all": ""}[which]
        self.log("INFO", f"Cleared {len(gone)} {word + ' ' if word else ''}job{'s' if len(gone) != 1 else ''}.")
        self.update_queue_box()

    def _build_job_card(self, card):
        kit, B = self.kit, "inspector"
        body = kit.frame(card, bg=B)
        body.grid(row=0, column=1, sticky="nsew", padx=20, pady=18)
        body.grid_columnconfigure(0, weight=1)
        self._card_body = body
        self._card_empty = kit.label(card, bg=B, fg="faint", font=kit.fonts.body, anchor="center",
                                     justify="center", text="Select a job to see its details.")

        hdr = kit.frame(body, bg=B)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_columnconfigure(1, weight=1)
        self.art_label = ArtView(hdr, kit, size=self.ART_PX, bg=B)
        self.art_label.grid(row=0, column=0, rowspan=3, sticky="nw", padx=(0, 14))
        t = kit.label(hdr, bg=B, font=kit.fonts.title, textvariable=self.card_title_var)
        t.grid(row=0, column=1, sticky="ew")
        m = kit.label(hdr, bg=B, fg="muted", font=kit.fonts.small, textvariable=self.card_meta_var)
        m.grid(row=1, column=1, sticky="ew", pady=(3, 0))
        tg = kit.label(hdr, bg=B, fg="faint", font=kit.fonts.small, textvariable=self.card_target_var)
        tg.grid(row=2, column=1, sticky="ew", pady=(2, 0))
        self._bind_dynamic_wrap(hdr, [t, m, tg], padding=self.ART_PX + 54, min_width=160)
        self._small(hdr, "", "x", lambda: self._set_inspector(False), bg=B,
                    tooltip=tr("Hide the details  {details}", details=SHORTCUT['details'])).grid(row=0, column=2, sticky="ne", padx=(8, 0))

        self._card_chips = Chips(body, kit, bg=B)
        self._card_chips.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self._space_warn = kit.label(body, bg=B, fg="warning", font=kit.fonts.small, textvariable=self.temp_space_var)
        self._space_warn.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._space_warn.grid_remove()
        self.temp_space_var.trace_add("write", lambda *_: (
            self._space_warn.grid() if "LOW" in self.temp_space_var.get() else self._space_warn.grid_remove()))

        prog = self._progress_box = kit.frame(body, bg=B)
        prog.grid(row=3, column=0, sticky="ew", pady=(20, 0))
        prog.grid_columnconfigure(0, weight=1)
        kit.label(prog, bg=B, fg="faint", font=kit.fonts.caption, textvariable=self.progress_title_var).grid(
            row=0, column=0, columnspan=2, sticky="ew")
        self._steps = StepStrip(prog, kit, bg=B)
        self._steps.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        kit.label(prog, bg=B, font=kit.fonts.heading, textvariable=self.stage_title_var).grid(
            row=2, column=0, sticky="w", pady=(12, 0))
        kit.label(prog, bg=B, fg="accent_text", font=kit.fonts.heading, textvariable=self.stage_pct_var).grid(
            row=2, column=1, sticky="e", pady=(12, 0))
        self.stage_bar = ProgressBar(prog, kit, height=4, bg=B)
        self.stage_bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        dt = kit.label(prog, bg=B, fg="muted", font=kit.fonts.small, textvariable=self.stage_detail_var)
        dt.grid(row=4, column=0, columnspan=2, sticky="new", pady=(6, 0))
        prog.grid_rowconfigure(4, minsize=2 * kit.fonts.small.metrics("linespace") + 6)
        self._bind_dynamic_wrap(prog, [dt], padding=8, min_width=200)
        tiles = kit.frame(prog, bg=B)
        tiles.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        for i, (cap, var) in enumerate((("Speed", self.m_speed_var), ("Elapsed", self.m_elapsed_var),
                                        ("Left", self.m_eta_var))):
            tiles.grid_columnconfigure(i, weight=1, uniform="tile")
            Tile(tiles, kit, cap, var, bg=B).grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
        prog.grid_remove()

        info = kit.frame(body, bg=B)
        info.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        info.grid_columnconfigure(1, weight=1)
        self._card_info_rows = {}
        for r, key in enumerate(self._CARD_INFO_KEYS):
            var = TextVar(value="")
            k = kit.label(info, bg=B, fg="faint", font=kit.fonts.small, text=key, anchor="nw")
            v = kit.label(info, bg=B, fg="muted", font=kit.fonts.small, textvariable=var, anchor="nw",
                          justify="left")
            k.grid(row=r, column=0, sticky="nw", padx=(0, 16), pady=(0, 5))
            v.grid(row=r, column=1, sticky="ew", pady=(0, 5))
            self._card_info_rows[key] = (var, k, v)
        self._bind_dynamic_wrap(info, [v for _var, _k, v in self._card_info_rows.values()],
                                padding=110, min_width=160)

        line = kit.fonts.mono_small.metrics("linespace")
        tail_box = RoundBox(body, kit, bg=B, pad=10, height=4 * line + 2 * 10 + 2)
        tail_box.grid(row=5, column=0, sticky="nsew", pady=(14, 0))
        body.grid_rowconfigure(5, weight=1)
        self._log_tail = kit.text(tail_box.inner, bg="surface2", fg="log", font=kit.fonts.mono_small, height=4,
                                  state="disabled", padx=0, pady=0, wrap="word")
        tail_box.set_child(self._log_tail)

        def _tail_resized(e, line=line):
            box = getattr(self, "log_box", None)
            if box is not None and e.height > 1:
                box.set_mirror_visible(max(1, e.height // line))
        self._log_tail.bind("<Configure>", _tail_resized, add="+")

        acts = kit.frame(body, bg=B)
        acts.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        acts.grid_columnconfigure(5, weight=1)
        for col, (text, icon, cmd, tip) in enumerate((
                ("Full log", "terminal", lambda: self._show_view("log"), tr("The whole log  {log}", log=SHORTCUT['log'])),
                ("Command", "code", self._toggle_command, "Show the backend command for this job"),
                ("Edit", "edit", lambda: self._on_queue_double_click(None), "Change this job (or double-click it)"),
                ("Retry", "retry", self._retry_selected, "Run this job again"),
                ("Remove", "trash", self.queue_remove_selected,
                 tr("Take the selected jobs out of the queue ({v}-click or Shift-click selects several)", v='⌘' if IS_MAC else 'Ctrl')))):
            b = self._small(acts, text, icon, cmd, tooltip=tip, bg=B)
            b.grid(row=0, column=col, padx=(0, 2))
            if text == "Retry":
                self.retry_btn = b
                b.autohide = True          # only for a job that failed, was cancelled or skipped
                b.configure(state="disabled")
        self.cancel_btn = self._small(acts, "Stop", "x", self.request_stop, variant="danger",
                                      tooltip="Stop the running job and the queue", bg=B)
        self.cancel_btn.grid(row=0, column=6, sticky="e")
        self.cancel_btn.autohide = True
        self.cancel_btn.configure(state="disabled")

        multi = self._card_multi = kit.frame(card, bg=B)
        multi.grid_columnconfigure(0, weight=1)
        self._multi_title_var, self._multi_meta_var, self._multi_hint_var = (
            TextVar(value=""), TextVar(value=""), TextVar(value=""))
        mh = kit.frame(multi, bg=B)
        mh.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 0))
        mh.grid_columnconfigure(0, weight=1)
        kit.label(mh, bg=B, font=kit.fonts.title, textvariable=self._multi_title_var).grid(row=0, column=0, sticky="ew")
        self._small(mh, "", "x", lambda: self._set_inspector(False), bg=B,
                    tooltip=tr("Hide the details  {details}", details=SHORTCUT['details'])).grid(row=0, column=1, sticky="ne", padx=(8, 0))
        mm = kit.label(multi, bg=B, fg="muted", font=kit.fonts.small, textvariable=self._multi_meta_var)
        mm.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 0))
        mt = kit.label(multi, bg=B, fg="faint", font=kit.fonts.small, textvariable=self._multi_hint_var)
        mt.grid(row=2, column=0, sticky="ew", padx=20, pady=(12, 0))
        self._bind_dynamic_wrap(multi, [mm, mt], padding=48, min_width=160)
        ma = kit.frame(multi, bg=B)
        ma.grid(row=3, column=0, sticky="w", padx=20, pady=(14, 0))
        self._small(ma, "Remove", "trash", self.queue_remove_selected, bg=B,
                    tooltip="Take the selected jobs out of the queue").grid(row=0, column=0)
        multi.grid_remove()

        self._cmd_frame = RoundBox(body, kit, bg=B, height=90, pad=10)
        self.command_label = kit.label(self._cmd_frame.inner, bg="surface2", fg="muted", font=kit.fonts.mono_small, text="")
        self._cmd_frame.set_child(self.command_label)
        self._bind_dynamic_wrap(self._cmd_frame, [self.command_label], padding=26, min_width=200)

    def _sync_run_ui(self):
        running = bool(getattr(self, "_batch_running", False))
        if running != getattr(self, "_run_ui_shown", None):
            self._run_ui_shown = running
            if not running:
                self._prune_scratch_later()       # the queue stands still (also at startup)
            try:
                self._pause_requested = False        # a pause belongs to the run it was asked in
                self._show_pause_state()
                if running:
                    self.pause_btn.configure(state="normal")
                    self.stop_btn.configure(state="normal")
                self.transport.set_running(running)
            except Exception:
                pass
        self._sync_progress_box()

    def _scratch_bases(self) -> list:
        """Main thread only, reads Tk variables."""
        out = [self.temp_var.get().strip(), self.output_var.get().strip()]
        try:
            # every configured extra temp drive, also one the router skips today
            out += [str(p) for p in self._temp_pool_dirs()] + [str(p) for p in (getattr(self, "temp_pool", None) or [])]
        except Exception:
            pass
        for it in list(self.queue):
            try:
                d = self._job_output_dir(it)
                if d:
                    out.append(str(d))
            except Exception:
                pass
        try:
            for h in (load_history() or [])[-100:]:
                o = str((h or {}).get("output") or "").strip()
                if o:
                    # beside the output, or the library above its title folder
                    out += [o, str(Path(o).parent), str(Path(o).parent.parent)]
        except Exception:
            pass
        return [b for b in dict.fromkeys(out) if b]

    def _prune_scratch_later(self, delay: float = 15.0) -> None:
        """Waits until the last job's cleanup is done; nothing goes while a job runs."""
        if getattr(self, "_prune_pending", False):
            return
        self._prune_pending = True
        bases = self._scratch_bases()
        busy = lambda: bool(getattr(self, "_batch_running", False))

        def work():
            try:
                time.sleep(delay)
                if busy():
                    return
                gone = prune_empty_scratch(bases, min_age=10.0, stop=busy)
                if gone:
                    self.log("INFO", "Removed empty work folders: " + ", ".join(str(g) for g in gone[:6])
                                     + (f" (+{len(gone) - 6} more)" if len(gone) > 6 else ""))
            except Exception:
                pass
            finally:
                self._prune_pending = False
        threading.Thread(target=work, daemon=True).start()

    def _follow_next_job(self, before, item):
        """A job you picked yourself stays picked."""
        shown = getattr(self, "_details_item", None)
        if shown is None or shown is before or shown is item or shown not in self.queue:
            self.update_queue_box(select_item=item)
            if self._details_item is not item:
                self.update_game_details(item)

    def _sync_progress_box(self):
        """Progress block only while the selected job runs, or nothing is selected."""
        shown = getattr(self, "_details_item", None)
        run = self._running_item() if getattr(self, "_batch_running", False) else None
        want = run is not None and (shown is None or shown is run)
        if want == getattr(self, "_progress_shown", None):
            return
        self._progress_shown = want
        try:
            self._progress_box.grid() if want else self._progress_box.grid_remove()
        except Exception:
            pass

    _PAUSE_TIPS = {False: "Pause the queue once the running job is done; Start runs the rest",
                   True: "Keep running the queue after this job"}

    def toggle_pause(self):
        """The running job always finishes; the queue stops before the next."""
        if not self._batch_running:
            return
        self._pause_requested = not getattr(self, "_pause_requested", False)
        self._show_pause_state()
        self._update_batch_counter()
        self.log("INFO", "The queue pauses once the running job is done." if self._pause_requested
                 else "The queue keeps running after this job.")

    def _show_pause_state(self):
        p = bool(getattr(self, "_pause_requested", False))
        tc = getattr(self, "transport", None)
        if tc is not None:
            tc.set_armed(p)                      # the Pause half stays lit orange while armed
            self.pause_btn.tooltip.text = self._PAUSE_TIPS[p]
        lbl = getattr(self, "_batch_counter_lbl", None)
        if lbl is not None:
            self.kit.restyle(lbl, fg="pause" if p else "faint")
        menu, idx = getattr(self, "_queue_menu", None), getattr(self, "_pause_menu_index", None)
        if menu is not None and idx is not None:
            try:
                menu.entryconfigure(idx, label="Keep Running After This Job" if p else "Pause After This Job")
            except Exception:
                pass

    def _pause_queue(self) -> None:
        """Not the queue's end: no sleep, no quit, no summary."""
        left = sum(1 for it in self.queue if getattr(it, "status", "") not in self._TERMINAL_STATUSES)
        jobs = f"{left} job{'' if left == 1 else 's'}"
        self._pause_requested = False
        self._batch_running = False
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._update_batch_counter()
        self.update_queue_box()
        self.status_update("Paused", tr("{jobs} left. Start runs them.", jobs=jobs), "Ready", 0, 0, "00:00", " - ", " - ")
        self.log("INFO", f"Queue paused after the running job: {jobs} left. Start runs them.")
        if self.notify_var.get() in ("job", "queue"):
            _after_job_module().notify("PS5 UltraPack", tr("Queue paused: {jobs} left", jobs=jobs))

    def _sync_primary_action(self):
        """While a batch runs, the run code owns Start's state."""
        try:
            empty = not self.queue
            # finished jobs alone leave nothing to start (failed ones run again on Start)
            idle = not any(getattr(it, "status", "") != "Done" for it in self.queue)
            self._add_btn.configure(variant="primary" if idle else "secondary")
            self.start_btn.configure(variant="secondary" if idle else "primary")
            if not self._batch_running:
                self.start_btn.configure(state="disabled" if idle else "normal")
            self._clear_btn.configure(state="normal" if self._clearable("done") else "disabled")
            if empty:
                self._set_inspector(False)
        except Exception:
            pass

    def _card_show(self, visible: bool):
        marked = self._marked_items() if visible else []
        multi = getattr(self, "_card_multi", None)
        if visible and len(marked) > 1 and multi is not None:
            self._card_body.grid_remove()
            self._card_empty.grid_remove()
            self._fill_card_multi(marked)
            multi.grid(row=0, column=1, sticky="nsew")
            return
        if multi is not None:
            multi.grid_remove()
        if visible:
            self._card_empty.grid_remove()
            self._card_body.grid()
        else:
            self._card_body.grid_remove()
            self._card_empty.configure(text="Select a job to see its details." if self.queue else "")
            self._card_empty.grid(row=0, column=1, sticky="nsew")

    def _fill_card_multi(self, items) -> None:
        n = len(items)
        size = sum(int(display_size(it) or 0) for it in items)
        states = {}
        for it in items:
            st = str(getattr(it, "status", "") or "Queued")
            st = {"Pending Extract": "Queued", "Pending": "Queued"}.get(st, st)
            if it is self._running_item():
                st = "Running"
            states[st] = states.get(st, 0) + 1
        self._multi_title_var.set(tr("{n} jobs selected", n=n))
        parts = [format_size(size)] + [f"{c} {s.lower()}" for s, c in sorted(states.items(), key=lambda kv: -kv[1])]
        self._multi_meta_var.set("  ·  ".join(parts))
        running_in = any(it is self._running_item() for it in items)
        self._multi_hint_var.set(
            ("Remove takes them out of the queue; the running job stays. " if running_in
             else "Remove takes them out of the queue. ")
            + tr("{v}-click adds or drops a job, Shift-click a range, Escape keeps one.", v='⌘' if IS_MAC else 'Ctrl'))

    def _on_queue_clicked(self):
        self._on_queue_select()
        if self.queue and self._queue_sel_idx() is not None:
            self._set_inspector(True)

    def toggle_inspector(self):
        self._set_inspector(not getattr(self, "_inspector_open", False))

    def _set_inspector(self, open_: bool):
        pw, card = getattr(self, "_paned_q", None), getattr(self, "_card_pane", None)
        if pw is None or card is None or bool(open_) == getattr(self, "_inspector_open", False):
            return
        try:
            if open_:
                pw.add(card, minsize=340, stretch="never")
                self._inspector_open = True
                self._place_inspector()
            else:
                self._save_sashes()
                pw.forget(card)
                self._inspector_open = False
        except tk.TclError:
            return
        try:
            self._details_btn.configure(variant="secondary" if open_ else "ghost")
            self._view_menu.entryconfigure(self._details_menu_index,
                                           label="Hide Details" if open_ else "Show Details")
        except (AttributeError, tk.TclError):
            pass

    def _place_inspector(self, tries: int = 0):
        pw = self._paned_q
        try:
            self.root.update_idletasks()
            W = pw.winfo_width()
            if W < 626:
                if tries < 20:                       # the window is not laid out yet
                    self.root.after(100, lambda: self._place_inspector(tries + 1))
                return
            card_w = load_settings().get("job_card_width")
            if not isinstance(card_w, int):
                card_w = 560
            card_w = max(340, min(card_w, W - 286))
            pw.sash_place(0, W - card_w - int(pw.cget("sashwidth")), 1)
        except (tk.TclError, ValueError):
            pass

    def _toggle_command(self):
        if self._cmd_frame.winfo_ismapped():
            self._cmd_frame.grid_remove()
        else:
            self.update_command_preview()
            self._cmd_frame.grid(row=7, column=0, sticky="ew", pady=(10, 0))

    def _job_recipe(self, item, detail: bool = False) -> list[str]:
        parts = self._job_recipe_parts(item)
        if detail and parts:
            if parts[-1] == ".ffpfsc":
                parts[-1] = tr(".ffpfsc, level {n}", n=self._ffpfsc_level(item))
            elif parts[-1] == ".pkg":
                try:
                    _pl = int(getattr(item, "fpkg_level", None) if getattr(item, "fpkg_level", None) is not None
                              else self._pkg_level_default())
                except (TypeError, ValueError):
                    _pl = 0
                parts[-1] = tr(".pkg, level {n}", n=_pl)
        return parts

    def _job_recipe_parts(self, item) -> list[str]:
        op = getattr(item, "operation", "pack")
        src = source_label(item)
        if op == "ps4-keystone":
            n = len([u for u in (getattr(item, "ps4_rebind", "") or "").splitlines() if u.strip()])
            parts = (([tr("Keystone")] if getattr(item, "ps4_keystone", None) else [])
                     + ([tr("Version")] if getattr(item, "ps4_fix_version", False) else [])
                     + ([plural(n, "1 update", "{n} updates")] if n else []))
            return [src, " · ".join(parts)]
        if op == "copy" and getattr(item, "content_kind", "") == ORGANIZE_TARGET:
            return [src, "Organize"]
        if op == "copy" and getattr(item, "content_kind", "") == "ps4":
            n = int(getattr(item, "ps4_count", 0) or 0)
            lib = tr("PS4 library") + (plural(n, " · 1 package", " · {n} packages") if n else "")
            if getattr(item, "ps4_keystone", None):
                return [src, "Keystone", lib]
            return [src, lib]          # a default keystone is a warning beside the row, not a step
        if op == "chain":
            ch = []
            if getattr(item, "ps5_repair", False):
                ch = (["Rebuild"] + (["Keystone"] if getattr(item, "fpkg_keystone", None) else [])
                      + (["PlayGo"] if getattr(item, "fpkg_regen_playgo", False) else [])
                      + (["Standalone"] if getattr(item, "ps5_standalone", False) else [])
                      + (["Trophies"] if getattr(item, "ps5_trophies", False) else []))
            for c in chain_changes(item):
                ch.append("Patch" if c == "patch" else "Sign" if c == "sign" else "LZ4" if c == "lz4"
                          else "Unpack LZ4" if c == "lz4 unpack"
                          else ("Backport " + c.split(" ", 1)[1]) if c.startswith("backport") else c)
            to = getattr(item, "chain_to", None) or "ffpfsc"
            return [src, *ch, "Folder" if to == "folder" else CHAIN_TARGET_LABEL.get(to, to)]
        if op == "pack":
            parts = [src]
            if getattr(item, "patch_source", None):
                parts.append("Patch")
            parts.append(".ffpfs" if getattr(item, "output_compressed", True) is False else ".ffpfsc")
            return parts
        return {"fpkg-build": [src, ".pkg"], "unpack": [src, "Folder"], "fpkg-extract": [".pkg", "Folder"],
                "patch": [src, "Integrate patch"], "fake-sign": [src, "Sign in place"],
                "copy": [src, {"move": "Move", "organize": "Organize"}.get(getattr(item, "copy_mode", None), "Copy")]
                }.get(op, [src])

    def _build_history_view(self, parent):
        kit = self.kit
        v = kit.frame(parent)
        v.grid_columnconfigure(0, weight=3)
        v.grid_columnconfigure(2, weight=2)
        v.grid_rowconfigure(2, weight=1)
        head, btns = self._column_header(v, "History", subtitle_var=self.last_result_var)
        head.grid(row=0, column=0, columnspan=3, sticky="ew", padx=(18, 14), pady=(18, 12))
        for text, icon, cmd in (("Refresh", "history", lambda: (self.refresh_history(), self.refresh_statistics())),
                                ("Copy last result", "copy", self.copy_last_result),
                                ("Open output folder", "folders", self.open_output_folder)):
            self._small(btns, text, icon, cmd).pack(side="left", padx=(0, 2))
        kit.rule(v).grid(row=1, column=0, columnspan=3, sticky="ew")
        for col, caption, attr in ((0, "Recent jobs", "history_box"), (2, "Totals", "stats_box")):
            box = kit.frame(v)
            box.grid(row=2, column=col, sticky="nsew")
            box.grid_columnconfigure(0, weight=1)
            box.grid_rowconfigure(1, weight=1)
            kit.label(box, fg="faint", font=kit.fonts.caption, text=caption).grid(row=0, column=0, sticky="w", padx=16, pady=(10, 4))
            tb, text = self._text_view(box)
            tb.grid(row=1, column=0, sticky="nsew")
            setattr(self, attr, text)
        kit.rule(v, horizontal=False).grid(row=2, column=1, sticky="ns")
        self.refresh_history()
        self.refresh_statistics()
        return v

    def _build_log_view(self, parent):
        kit = self.kit
        v = kit.frame(parent)
        v.grid_columnconfigure(0, weight=1)
        v.grid_rowconfigure(2, weight=1)
        head, btns = self._column_header(v, "Log", subtitle="Everything the backend printed, newest at the bottom.")
        head.grid(row=0, column=0, sticky="ew", padx=(18, 14), pady=(18, 12))
        for text, icon, cmd, tip in (("Clear", "trash", self.clear_logs, None),
                                     ("Raw log", "export", self.open_raw_log, "Open the unfiltered backend output"),
                                     ("Diagnostics", "export", self.export_diagnostics, "Save a report for a bug report"),
                                     ("Copy last result", "copy", self.copy_last_result, None)):
            self._small(btns, text, icon, cmd, tooltip=tip).pack(side="left", padx=(0, 2))
        kit.rule(v).grid(row=1, column=0, sticky="ew")
        box, self.log_box = self._text_view(v, mirror=self._log_tail)
        box.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        return v

    def _build_status_bar(self, shell):
        kit = self.kit
        bar = kit.frame(shell, bg="sidebar")
        inner = kit.frame(bar, bg="sidebar")
        inner.pack(fill="x", padx=14, pady=5)
        f = kit.fonts.caption
        self._status_dot = IconView(inner, kit, "dot", size=11, color="success", bg="sidebar")
        self._status_dot.pack(side="left", padx=(0, 6))
        kit.label(inner, bg="sidebar", fg="muted", font=f, textvariable=self._footer_plain_var).pack(side="left")
        self._batch_counter_lbl = kit.label(inner, bg="sidebar", fg="faint", font=f, textvariable=self.batch_counter_var)
        self._batch_counter_lbl.pack(side="left", padx=(12, 0))
        # not shown, but the poll loop still feeds this bar
        self._overall_box = kit.frame(inner, bg="sidebar")
        self.overall_bar = ProgressBar(self._overall_box, kit, height=4, bg="sidebar")
        self._ram_label = kit.label(inner, bg="sidebar", fg="faint", font=f, textvariable=self.ram_var)
        self._ram_label.pack(side="right")
        kit.label(inner, bg="sidebar", fg="faint", font=f, textvariable=self.status_temp_var).pack(side="right", padx=(0, 14))
        kit.label(inner, bg="sidebar", fg="faint", font=f, textvariable=self.status_out_var).pack(side="right", padx=(0, 14))

        def _run_state(*_):
            running = bool(getattr(self, "_batch_running", False))
            # the state word as the code set it, not the translated footer text
            txt = str(getattr(self, "_status_state", "") or self._footer_plain_var.get()).lower()
            color = ("danger" if any(w in txt for w in ("fail", "error", "cancel")) else
                     "success" if (not running and any(w in txt for w in ("ready", "done", "complete"))) else "accent")
            self._status_dot.set(color=color)
        self.batch_counter_var.trace_add("write", _run_state)
        self._footer_plain_var.trace_add("write", _run_state)
        return bar

    def _tick_space_status(self):
        """disk_usage runs on a worker, a sleeping HDD can take seconds; Tk stays here."""
        res = self._space_result
        if res is not None:
            self._space_result = None
            self.status_temp_var.set(res[0])
            self.status_out_var.set(res[1])
        self._space_tick += 1
        if self._space_tick % 50 != 1 or self._space_busy:
            return
        paths = ((self.temp_var.get() or "").strip(), (self.output_var.get() or "").strip())

        def _label(kind, p):
            if not p:
                return ""
            try:
                free = get_free_space(Path(p))
            except Exception:
                return tr("{kind}: not reachable", kind=kind)
            drive = ultra_core.drive_display_name(p, system="Mac")
            return tr("{kind}: {drive}  ·  {free} free", kind=tr(kind), drive=drive, free=format_size(free))

        def _work():
            try:
                self._space_result = (_label("Temp", paths[0]), _label("Output", paths[1]))
            finally:
                self._space_busy = False
        self._space_busy = True
        threading.Thread(target=_work, daemon=True).start()

    def _build_menubar(self):
        """macOS: Tk routes Settings and Quit to the app menu; elsewhere they sit in File."""
        root = self.root
        guard = lambda fn: (lambda: None if self._panels.stack else fn())
        mod_key = (lambda k: f"Command-{k}") if IS_MAC else (lambda k: f"Ctrl+{k}")
        mb = tk.Menu(root)
        if IS_MAC:
            appm = tk.Menu(mb, name="apple", tearoff=0)
            appm.add_command(label=tr("About {app}", app=APP_NAME), command=guard(lambda: self._open_settings_page("about")))
            appm.add_separator()
            mb.add_cascade(menu=appm)
            root.createcommand("tk::mac::ShowPreferences", self.open_settings)
            root.createcommand("tk::mac::Quit", self._on_close)
        fm = tk.Menu(mb, tearoff=0)
        fm.add_command(label="Add Job…", accelerator=ACCEL["add"], command=guard(self.open_job_dialog))
        fm.add_command(label="Look Inside…", accelerator=ACCEL["open"], command=lambda: self.open_pfs_browser())
        fm.add_command(label="Organize…", command=lambda: self._show_view("organize"))
        fm.add_command(label="PS4 PKG Tools…", command=lambda: self._show_view("keystone"))
        fm.add_command(label="PS5 PKG Tools…", command=lambda: self._show_view("ps5repair"))
        fm.add_separator()
        fm.add_command(label="Start Queue", accelerator=ACCEL["start"], command=guard(self.start))
        fm.add_command(label="Pause After This Job", command=self.toggle_pause)
        self._queue_menu, self._pause_menu_index = fm, fm.index("end")
        fm.add_command(label="Stop Queue", accelerator=ACCEL["stop"], command=self._stop_if_running)
        fm.add_command(label="Clear Completed Jobs", command=guard(lambda: self.clear_jobs("done")))
        fm.add_separator()
        fm.add_command(label="Clean Temp…", command=guard(self.clear_temp_files))
        if not IS_MAC:
            fm.add_separator()
            fm.add_command(label="Settings…", accelerator=ACCEL["settings"], command=self.open_settings)
            fm.add_separator()
            fm.add_command(label="Exit", command=self._on_close)
        mb.add_cascade(label="File", menu=fm)
        em = tk.Menu(mb, tearoff=0)
        for label, key, ev in (("Cut", "X", "<<Cut>>"), ("Copy", "C", "<<Copy>>"), ("Paste", "V", "<<Paste>>")):
            em.add_command(label=label, accelerator=mod_key(key), command=lambda e=ev: self._edit_event(e))
        em.add_separator()
        em.add_command(label="Select All", accelerator=mod_key("A"), command=lambda: self._edit_event("<<SelectAll>>"))
        mb.add_cascade(label="Edit", menu=em)
        vm = self._view_menu = tk.Menu(mb, tearoff=0)
        for label, key in (("Queue", "queue"), ("History", "history"), ("Log", "log")):
            vm.add_command(label=label, accelerator=ACCEL[key], command=lambda k=key: self._show_view(k))
        vm.add_separator()
        vm.add_command(label="Show Details", accelerator=ACCEL["details"], command=guard(self.toggle_inspector))
        self._details_menu_index = vm.index("end")
        vm.add_separator()
        self._appearance_var = tk.StringVar(value=self._theme)
        am = tk.Menu(vm, tearoff=0)
        for label, mode in (("Dark", "dark"), ("Light", "light")):
            am.add_radiobutton(label=label, value=mode, variable=self._appearance_var,
                               command=lambda m=mode: self._set_theme(m))
        vm.add_cascade(label="Appearance", menu=am)
        mb.add_cascade(label="View", menu=vm)
        if IS_MAC:
            mb.add_cascade(label="Window", menu=tk.Menu(mb, name="window", tearoff=0))
        hm = tk.Menu(mb, name="help", tearoff=0)
        hm.add_command(label=tr("{app} on GitHub", app=APP_NAME), command=lambda: webbrowser.open(REPO_URL))
        hm.add_command(label="Buy Me a Coffee…", command=lambda: webbrowser.open(DONATE_URL))
        hm.add_separator()
        hm.add_command(label="Open Log Folder", command=self._open_log_folder)
        hm.add_command(label="Raw Backend Log", command=self.open_raw_log)
        hm.add_command(label="Export Diagnostics…", command=self.export_diagnostics)
        if not IS_MAC:
            hm.add_separator()
            hm.add_command(label=tr("About {app}", app=APP_NAME), command=guard(lambda: self._open_settings_page("about")))
        mb.add_cascade(label="Help", menu=hm)
        root.configure(menu=mb)
        self._menubar = mb

    def _open_settings_page(self, key):
        self.open_settings()
        try:
            self._settings_view.show(key)
        except (AttributeError, StopIteration, tk.TclError):
            pass

    def _edit_event(self, event_name):
        w = self.root.focus_get()
        if w is not None:
            try:
                w.event_generate(event_name)
            except tk.TclError:
                pass

    def _stop_if_running(self):
        if self._batch_running:
            self.request_stop()

    def _stop_question(self) -> str | None:
        """None when nothing is worth asking about: no job, or a fresh one."""
        item = self._running_item() or getattr(self, "_active_item", None)
        if item is None or not self._job_active():
            return None
        name = getattr(item, "display_name", None) or item.name
        if getattr(item, "operation", "") == "ps4-keystone":
            return tr("A PS4 PKG Tools job is changing {name} in place. Stopped now, the package stays half "
                      "changed until you press Undo last change in PS4 PKG Tools.", name=name)
        # time since the job was picked, its unpack included
        since = getattr(self, "_active_since", (None, 0))
        t0 = since[1] if since[0] == id(item) else None
        w = getattr(self, "worker", None)
        if t0 is None and w is not None and getattr(w, "item", None) is item and getattr(w, "start_time", 0):
            t0 = w.start_time
        ran = time.time() - t0 if t0 else 0
        if ran < 20:
            return None
        return tr("{name} has run for {time}. The work done so far is lost, and the queue stops.",
                  name=name, time=humanize_eta(int(ran)))

    def request_stop(self):
        q = self._stop_question()
        if q is not None and not ask_confirm("Stop the job?", q, "Stop job", "Keep running", danger=True):
            return
        self.cancel()

    def _open_log_folder(self):
        ensure_app_dir()
        open_path(APP_DIR)

    def _init_state_vars(self):
        # restore the last source only if it still exists
        _src0 = self._saved_source if (self._saved_source and Path(self._saved_source).exists()) else ""
        self.source_var = tk.StringVar(value=_src0)
        self.source_var.trace_add("write", lambda *_: save_settings({"source_path": self.source_var.get()}))
        self.output_var = tk.StringVar(value=self._saved_output)
        self.temp_var = tk.StringVar(value=self._saved_temp)
        self.output_var.trace_add("write", lambda *_: save_settings({"output_folder": self.output_var.get()}))
        self.temp_var.trace_add("write", lambda *_: save_settings({"temp_folder": self.temp_var.get()}))
        # probe SSD/HDD in the background, diskutil and PowerShell would block the UI
        self.temp_var.trace_add("write", lambda *_: self._warm_drive_types())
        self.output_var.trace_add("write", lambda *_: self._warm_drive_types())
        self.source_var.trace_add("write", lambda *_: self._warm_drive_types())
        self._warm_drive_types()
        self.password_var = tk.StringVar()
        self.archive_passwords: list[str] = list(getattr(self, "_saved_passwords", []))
        # extra temp drives let the router keep pass 1 SSD to SSD for big archives
        self.temp_pool: list[str] = [native_path(p) for p in (load_settings().get("temp_pool", []) or []) if str(p).strip()]
        self.copy_siblings_var = tk.BooleanVar(value=getattr(self, "_saved_copy_siblings", True))
        settings = load_settings()
        self.keep_pfs_var        = self._persisted_bool(settings, "keep_pfs", False)
        self.open_output_var     = self._persisted_bool(settings, "open_output", False)
        self.summary_popup_var   = self._persisted_bool(settings, "summary_popup", True)
        self.sound_complete_var  = self._persisted_bool(settings, "sound_complete", True)
        self.sound_error_var     = self._persisted_bool(settings, "sound_error", True)
        # names games that can't be read before unpacking from the public title list
        self.online_names_var    = self._persisted_bool(settings, "online_title_names", True)
        # per-step speeds measured on this machine, bytes of the game per second
        self._stage_rates = {k: float(v) for k, v in (settings.get("stage_rates") or {}).items()
                             if k in LONG_STEPS and isinstance(v, (int, float)) and v > 0}
        self.batch_var = tk.BooleanVar(value=False)  # not persisted
        self.unpack_mode_var = tk.BooleanVar(value=False)  # not persisted
        # .ffpfs mounts faster: ShadowMountPlus reads .ffpfsc at only ~150-250 MB/s
        self.output_compressed_var = self._persisted_bool(settings, "output_compressed", True)
        # AMPR/APR emu folder for PlayGo titles: libSceAmpr.sprx and libScePlayGo.sprx
        self.ampr_var = tk.StringVar(value=native_path(settings.get("ampr_folder", "")))
        # both user-supplied, never bundled: patched libs and original firmware libs
        _raw_libs, _fw = native_path(settings.get("backport_libs_root", "")), native_path(settings.get("fw_libs_root", ""))
        _libs = _sane_patched_libs(_raw_libs, _fw)
        self._cleared_libs_setting = _raw_libs if _libs != _raw_libs else ""
        if self._cleared_libs_setting:
            save_settings({"backport_libs_root": ""})
        self.backport_libs_var = tk.StringVar(value=_libs)
        self.fw_libs_var = tk.StringVar(value=_fw)
        # Sony Publishing Tools DLL: never bundled, empty means built-in encoder only
        self.pubtools_dll_var = tk.StringVar(value=native_path(settings.get("pubtools_dll", "")))
        # per-job format default; output_compressed_var is the old bool, kept in sync
        _fmt0 = str(settings.get("output_format") or "").strip().lower()
        if _fmt0 not in ("ffpfsc", "ffpfs", "pkg"):
            _fmt0 = "ffpfsc" if self.output_compressed_var.get() else "ffpfs"
        self.output_format_var = tk.StringVar(value=_fmt0)
        def _sync_format(*_):
            v = self.output_format_var.get()
            save_settings({"output_format": v})
            try:
                self.output_compressed_var.set(v != "ffpfs")
            except Exception:
                pass
        self.output_format_var.trace_add("write", _sync_format)
        # identity is never remembered, it comes from param.json per game at build time
        _fd = settings.get("fpkg_defaults") or {}
        try:
            _fl = int(_fd.get("level", 0) if _fd.get("level") is not None else 0)
        except Exception:
            _fl = 0
        if not _fd.get("v212"):
            # level 0 to 5 is 0.3 % larger than 7 but 4.5x faster; old presets move to 0 once
            _fl = 0
        _inner = _fd.get("inner") if _fd.get("inner") in ("none", "zlib", "kraken") else "kraken"
        if _inner == "none" and not _fd.get("v1112"):
            # 'none' was never tried on a console; move that old default to kraken once
            _inner = "kraken"
        self.fpkg_defaults: dict = {
            "inner":   _inner,
            "backend": _fd.get("backend") if _fd.get("backend") in ("builtin", "publishingtools") else "builtin",
            "level":   max(-4, min(9, _fl)),  # Kraken -4..9
            "retail_normalize": bool(_fd.get("retail_normalize", True)),
            "hdr_flag":         _hdr_mode(_fd.get("hdr_flag", "auto")),  # an old bool becomes auto/off
            "regen_playgo":     bool(_fd.get("regen_playgo", False)),
            "fake_sign":        bool(_fd.get("fake_sign", True)),
            "v1112":            True,
            "v212":             True,
        }
        self._pending_fpkg_identity = None  # (source path, identity dict), dialog to scan
        self.verify_output_var   = self._persisted_bool(settings, "verify_output", False)
        self.auto_clear_temp_var = self._persisted_bool(settings, "auto_clear_temp", False)
        # failed jobs always stay in the queue
        self.auto_remove_done_var = self._persisted_bool(settings, "auto_remove_done", False)
        _aj = _after_job_module()
        def _choice(key, allowed, default):
            v = settings.get(key, default)
            var = tk.StringVar(value=v if v in allowed else default)
            var.trace_add("write", lambda *_: save_settings({key: var.get()}))
            return var
        self.after_source_var = _choice("after_source", _aj.ACTIONS, _aj.KEEP)
        self.after_move_dir_var = tk.StringVar(value=native_path(settings.get("after_move_dir", "")))
        self.after_move_dir_var.trace_add("write", lambda *_: save_settings(
            {"after_move_dir": self.after_move_dir_var.get().strip()}))
        self.notify_var = _choice("notify", _aj.NOTIFY, "off")
        self.after_queue_var = _choice("after_queue", _aj.QUEUE_ACTIONS, "nothing")
        _oe = settings.get("output_exists", "skip")
        self.output_exists_var = tk.StringVar(value=_oe if _oe in self.OUTPUT_EXISTS_CHOICES else "skip")
        self.output_exists_var.trace_add("write", lambda *_: save_settings({"output_exists": self.output_exists_var.get()}))
        # off by default: it changes what lands in the .ffpfsc
        self.auto_integrate_patch_var = self._persisted_bool(settings, "auto_integrate_patch", False)
        # auto, temp or spread: where archives get extracted
        self.drive_mode_var = tk.StringVar(value=settings.get("drive_mode", "auto"))
        self.drive_mode_var.trace_add("write", lambda *_: save_settings({"drive_mode": self.drive_mode_var.get()}))
        self.show_space_dialog_var = self._persisted_bool(settings, "show_space_dialog", True)
        # exFAT intermediate like PSBrew's exfat->ffpfsc path; macOS only
        self.build_via_exfat_var = self._persisted_bool(settings, "build_via_exfat", False)
        self.fake_sign_before_pack_var = self._persisted_bool(settings, "fake_sign_before_pack", False)
        # '<Title> [TID] [vX.Y.Z]/' from param.json, whatever the source was called
        self.auto_organize_var = self._persisted_bool(settings, "auto_organize", True)
        # ping faster than WD IntelliPark's 8 s park timer; slower only adds load cycles
        self.keep_drives_awake_var = self._persisted_bool(settings, "keep_drives_awake", False)
        self.compression_level_var = tk.IntVar(value=self._saved_compression_level)
        self.cpu_count_var         = tk.IntVar(value=self._saved_cpu_count)
        self.verbose_var           = self._persisted_bool(settings, "verbose", False)
        self.block_size_var        = tk.StringVar(value=self._saved_block_size)
        self.compression_level_var.trace_add("write", lambda *_: save_settings({"compression_level": self.compression_level_var.get()}))
        self.cpu_count_var.trace_add("write", lambda *_: save_settings({"cpu_count": self.cpu_count_var.get()}))
        self.block_size_var.trace_add("write", lambda *_: save_settings({"block_size": self.block_size_var.get()}))

    def _set_theme(self, mode):
        if mode in ("dark", "light") and mode != self._theme:
            self._toggle_theme()

    def _toggle_theme(self):
        self._theme = "light" if self._theme == "dark" else "dark"
        ctk.set_appearance_mode(self._theme)
        save_settings({"appearance_mode": self._theme})
        try:
            self._appearance_var.set(self._theme)
        except AttributeError:
            pass
        for w in [self.root] + [c for c in self.root.winfo_children() if isinstance(c, tk.Toplevel)]:
            set_window_appearance(w, self._theme)
        # CTk follows its (light, dark) tuples by itself; plain Tk needs the kit
        try:
            self.kit.set_mode(self._theme)
        except Exception:
            pass
        restyle_trees()             # the tables of the tool views are ttk: they need telling



    def open_settings(self):
        self._show_view("settings")



    def _backend_cmd(self, *args) -> list:
        """Frozen: re-invoke this exe with --backend-internal; dev: backend/cli.py."""
        pycmd = get_backend_python_command() or ["python"]
        if getattr(sys, "frozen", False):
            return pycmd + list(args)
        return pycmd + ["-u", str(backend_base_dir() / "cli.py")] + list(args)

    def open_pfs_browser(self, image_path=None):
        self._show_view("look")
        if image_path:
            self._look_view.open(image_path)

    def open_job_dialog(self, init_src: str | None = None, init_sources: list | None = None):
        JobDialog(self, init_src=init_src, init_sources=init_sources)

    def _pkg_level_default(self) -> int:
        try:
            return max(-4, min(9, int(self.fpkg_defaults.get("level", 0))))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _pkg_level_text(level: int) -> str:
        """Numbers measured on a retail sample."""
        level = int(level)
        if level < 0:
            return "fastest; about 2 % larger"
        if level <= 5:
            return "nearly as fast; 0.3 % larger than 7"
        if level == 7:
            return "smallest; 4 to 5 times slower"
        if level == 6:
            return "between 5 and 7"
        return "no smaller than 7, slower still"

    def _ffpfsc_level(self, item) -> int:
        for value in (getattr(item, "compression_level", None), self.compression_level_var.get()):
            try:
                if value is not None:
                    return max(1, min(9, int(value)))
            except (TypeError, ValueError):
                continue
        return 7

    def _backport_args(self, item) -> list[str]:
        """With the firmware libs folder the backend reads SDK values of non-public targets."""
        bt = getattr(item, "backport_target", None)
        if not is_backport_target(bt):
            return []
        args = ["--backport-target", bt]
        blr = getattr(item, "backport_libs_root", None) or (self.backport_libs_var.get() or "").strip()
        if blr:
            args += ["--backport-libs", str(blr)]
        fw = (self.fw_libs_var.get() or "").strip()
        if fw:
            args += ["--fw-libs-root", fw]
        return args

    def prepare_backport_libs(self, target: str) -> None:
        """Runs in the background; progress per library goes to the log."""
        fw = (self.fw_libs_var.get() or "").strip()
        out = (self.backport_libs_var.get() or "").strip()
        if not fw or not Path(fw).is_dir():
            self.log("ERROR", f"Prepare {target}: set the firmware libraries folder in Settings first "
                              f"(“Backport - firmware libraries”), with your 10.01 libraries in a 10.01 subfolder.")
            return
        if _backport_module().firmware_folder(Path(fw), "10.01") is None:
            self.log("ERROR", f"Prepare {target}: no 10.01 folder in {fw}. The BackPork patches apply to "
                              f"10.01 libraries only.")
            return
        if not out:
            self.log("ERROR", f"Prepare {target}: set the PATCHED libraries folder in Settings first "
                              f"(“Backport - patched libraries folder”).")
            return
        pycmd = get_backend_python_command()
        if not pycmd:
            self.log("ERROR", "Backend not found; cannot run Prepare.")
            return
        cli_py = backend_base_dir() / "cli.py"
        head = pycmd if getattr(sys, "frozen", False) else pycmd + ["-u", str(cli_py)]
        argv = head + ["--prepare-backport-libs", target,
                       "--fw-libs-root", fw, "--backport-libs", out]
        self.log("INFO", f"Prepare {target}: downloading BackPork patches and applying them to {fw} → {Path(out) / target}…")

        def work():
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace")
                for line in proc.stdout or ():
                    self.root.after(0, self.log, "INFO", line.rstrip())
                proc.wait()
                if proc.returncode == 0:
                    self.root.after(0, self.log, "OK", tr("Prepare {target} finished - the job dialog will pick up {v} as Patched libraries.", target=target, v=Path(out) / target))
                else:
                    self.root.after(0, self.log, "ERROR", tr("Prepare {target} finished with exit code {returncode}.", target=target, returncode=proc.returncode))
            except Exception as e:
                self.root.after(0, self.log, "ERROR", f"Prepare {target} failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def fake_sign_folder(self):
        """Queues a job that signs eboot.bin, .elf, .prx and .sprx in place."""
        path = filedialog.askdirectory(
            title="Select a PS5 game folder (or a parent folder of dumps) to fake-sign")
        if not path:
            return
        folder = Path(path)
        if not folder.is_dir():
            messagebox.showerror("Not a folder", tr("This is not a folder:\n{folder}", folder=folder))
            return
        if not ask_confirm(
                "Fake-sign in place?",
                tr("Queue a fake-sign job for:\n\n{folder}\n\nWhen it runs it changes the executables in that folder "
                  "itself (files already signed are skipped, so it is safe to repeat).", folder=folder), "Add to queue"):
            return
        item = GameItem.from_fake_sign(folder)
        self.queue.append(item)
        self.update_queue_box(select_item=item)
        self.log("OK", f"Fake-sign queued: {folder.name}.  Press Start to run.")
        self.status_update("Ready", tr("Fake-sign queued: {name} - press START.", name=folder.name),
                            "Ready", 0, 0, "00:00", " - ", " - ", side=True)

    def fpkg_extract_dialog(self):
        """Bundled ffpfsc-pkg-tool, no external DLL needed."""
        path = filedialog.askopenfilename(
            title="Select a PS5 fake package (.pkg) to extract",
            filetypes=[("PS5 package", "*.pkg"), ("All files", "*.*")])
        if not path:
            return
        pkg = Path(path)
        if not pkg.is_file():
            messagebox.showerror("Not a file", tr("This is not a file:\n{pkg}", pkg=pkg)); return
        out = (self.output_var.get() or "").strip()
        if not out:
            out = str(pkg.parent / (pkg.stem + " [extracted]"))
        item = GameItem.from_fpkg_extract(pkg, output_path=out)
        self.queue.append(item)
        self.update_queue_box(select_item=item)
        self.log("OK", f"fPKG extract queued: {pkg.name} -> {out}.  Press Start to run.")


    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        found = []
        for raw in paths:
            p = Path(str(raw).strip("{}").strip())
            if p.exists():
                found.append(p)
            else:
                self.log("WARN", f"Dropped path not found: {p}")
        if not found:
            return
        # an open Add job takes a single drop as its source
        top = self._panels.stack[-1] if self._panels.stack else None
        if isinstance(top, JobDialog) and top.edit_item is None and len(found) == 1:
            top.src_var.set(str(found[0]))
            return
        if top is not None:
            self._panels.close_all()
        if len(found) == 1:
            self.open_job_dialog(init_src=str(found[0]))
        else:
            self.open_job_dialog(init_sources=[str(p) for p in found])

    def browse_source_folder(self):
        path = filedialog.askdirectory(title="Select PS5 game folder or parent folder")
        if not path:
            return
        p = Path(path)
        self.source_var.set(str(p))
        if not self.output_var.get():
            self.output_var.set(str(p.parent))
        if not self.temp_var.get():
            self.temp_var.set(str(p.parent / "_ffpfsc_temp"))
        self.preview_light(p)
        self.add_source_to_queue()

    def browse_source_archive(self):
        path = filedialog.askopenfilename(
            title="Select archive, disk image, or PFS image",
            filetypes=[
                ("Supported files", "*.zip *.rar *.7z *.exfat *.ffpkg *.ffpfs *.ffpfsc *.pkg"),
                ("Disk images",     "*.exfat *.ffpkg"),
                ("PFS images",      "*.ffpfs *.ffpfsc"),
                ("PS5 packages",    "*.pkg"),
                ("Archives",        "*.zip *.rar *.7z"),
                ("ZIP",             "*.zip"),
                ("RAR",             "*.rar"),
                ("7-Zip",           "*.7z"),
                ("All files",       "*.*"),
            ]
        )
        if not path:
            return
        p = Path(path)
        if p.suffix.lower() == ".pkg" and _is_ps4_source(p):
            out = (self.output_var.get() or "").strip()
            if not out:
                self.open_job_dialog(init_src=str(p)); return
            item = self._ps4_item_for(p, output_path=out)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f"PS4 package queued: {p.name} -> {out}.")
            return
        if p.suffix.lower() == ".pkg":
            out = (self.output_var.get() or "").strip()
            if not out:
                out = str(p.parent / (p.stem + " [extracted]"))
            item = GameItem.from_fpkg_extract(p, output_path=out)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f"fPKG extract queued: {p.name} -> {out}.")
            return
        self.source_var.set(str(p))
        if p.suffix.lower() == ".ffpfsc":  # .ffpfs is a re-pack source, not unpack
            self.unpack_mode_var.set(True)
        if not self.output_var.get():
            self.output_var.set(str(p.parent))
        if not self.temp_var.get():
            self.temp_var.set(str(p.parent / "_ffpfsc_temp"))
        self.add_source_to_queue()

    def browse_pfs_image(self):
        path = filedialog.askopenfilename(
            title="Select PFS image to unpack",
            filetypes=[
                ("PFS images", "*.ffpfs *.ffpfsc"),
                ("All files", "*.*"),
            ]
        )
        if not path:
            return
        p = Path(path)
        self.source_var.set(str(p))
        self.unpack_mode_var.set(True)
        if not self.output_var.get():
            self.output_var.set(str(p.parent))
        self.add_source_to_queue()

    def browse_output_folder(self):
        p = filedialog.askdirectory(title="Select output folder")
        if p:
            self.output_var.set(p)
            if not self.temp_var.get():
                self.temp_var.set(str(Path(p) / "_ffpfsc_temp"))
            save_settings({"output_folder": p})
            self.update_command_preview()

    def browse_temp_folder(self):
        p = filedialog.askdirectory(title="Select temp folder")
        if p:
            tp = str(Path(p) / "_ffpfsc_temp")
            self.temp_var.set(tp)
            save_settings({"temp_folder": tp})
            self.update_command_preview()

    def preview_light(self, p: Path):
        self.game_name_var.set(f"Name: {guess_game_name(p)}")
        self.title_var.set(f"Title ID: {parse_title_id(p)}")
        self.source_detail_var.set(f"Source: {p}")
        self.orig_var.set("Original Size: click Scan / Add")
        self.files_var.set("Files: click Scan / Add")
        self.load_art(find_artwork(p))
        self.update_command_preview()

    ART_PX = 96          # the cover in the details pane

    def load_art(self, art):
        try:
            art_key = (str(art), Path(str(art)).stat().st_mtime_ns) if art else None
        except OSError:
            art_key = None
        if art_key == getattr(self, "_loaded_art_key", object()):
            return
        self._loaded_art_key = art_key
        photo = None
        if Image and ImageTk and art_key:
            try:
                img = Image.open(art).convert("RGBA")
                import ui_kit
                art_px = round(self.ART_PX * ui_kit.SCALE)     # device pixels
                img.thumbnail((art_px, art_px), Image.LANCZOS)
                try:
                    from PIL import ImageChops, ImageDraw
                    mask = Image.new("L", img.size, 0)
                    ImageDraw.Draw(mask).rounded_rectangle(
                        [0, 0, img.size[0] - 1, img.size[1] - 1], radius=max(10, art_px // 7), fill=255)
                    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
                except Exception:
                    pass
                photo = ImageTk.PhotoImage(img)
            except Exception:
                photo = None
        self.art_img = photo   # hold a reference; Tk drops unreferenced images
        self.art_label.set_photo(photo)

    @staticmethod
    def _clean_path_str(raw: str) -> str:
        """Strip whitespace and surrounding quotes Windows sometimes adds."""
        s = raw.strip()
        if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
            s = s[1:-1].strip()
        return s

    def _classify_extracted_payload(self, extracted_root: Path, archive_name: str) -> tuple[str, list[Path]]:
        pfs_images = find_files_by_suffix(extracted_root, PFS_IMAGE_SUFFIXES)
        if pfs_images:
            self.log("INFO", f"Archive payload detected: {len(pfs_images)} PFS image(s) for extraction")
            return "pfs", pfs_images

        disk_images = find_files_by_suffix(extracted_root, DISK_IMAGE_SUFFIXES)
        if disk_images:
            self.log("INFO", f"Archive payload detected: {len(disk_images)} disk image(s) for compression")
            return "disk", disk_images

        games = find_game_folders(extracted_root)
        if games:
            self.log("INFO", f"Archive payload detected: {len(games)} PS5 game folder(s)")
            return "game", games

        if is_game_folder(extracted_root):
            self.log("INFO", "Archive payload detected: PS5 game folder")
            return "game", [extracted_root]

        packages = find_files_by_suffix(extracted_root, {".pkg"})
        if packages:
            ps4 = [x for x in packages if _ps4pkg_module().is_ps4_package(x)]
            if ps4:
                others = len(packages) - len(ps4)
                self.log("INFO", f"Archive payload detected: {len(ps4)} PS4 package(s)"
                                 + (f"; {others} other package(s) stay in the extraction" if others else ""))
                return "ps4", [extracted_root]
            self.log("INFO", f"Archive payload detected: {len(packages)} package(s) (.pkg)")
            return "pkg", packages

        # nested archive: say so instead of a vague 'no payload'
        inner_archives = find_files_by_suffix(extracted_root, {".zip", ".rar", ".7z"})
        if inner_archives:
            names = ", ".join(sorted({a.name for a in inner_archives})[:5])
            raise RuntimeError(
                f"{archive_name} contains another archive ({names}), not a game.\n\n"
                "Nested archives are not unpacked automatically - extract the inner "
                "archive yourself first, then add the resulting game folder or disk image."
            )

        raise RuntimeError(
            f"No supported payload found after extracting {archive_name}.\n\n"
            "Expected one of these inside the archive:\n"
            "  • PFS images (.ffpfs / .ffpfsc) for extraction\n"
            "  • Disk images (.exfat / .ffpkg) for compression\n"
            "  • A PS5 game folder containing sce_sys/ and eboot.bin\n"
            "  • A package (.pkg)"
        )

    @staticmethod
    def _payload_key(root, path) -> str:
        """The game's path relative to the archive root, stable across unpacks."""
        try:
            return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
        except (OSError, ValueError):
            return Path(path).name

    def _item_from_payload_path(self, kind: str, path: Path) -> GameItem:
        if kind == "ps4":
            return self._ps4_item_for(path)
        if kind == "pfs":
            return GameItem.from_pfs_image(path)
        if kind == "pkg":
            return GameItem.from_chain(path, to="ffpfsc")      # only its source fields are used
        if kind == "disk":
            return GameItem.from_exfat(path)
        return GameItem(path)

    def _split_backport_packages(self, root: Path, packages: list) -> tuple[list, list]:
        """A backport package goes next to the output; alone in the archive it is the job."""
        ol = _organize_module()
        ports = [p for p in packages if ol.name_role(p, root) == "backport"]
        if not ports or len(ports) == len(packages):
            return list(packages), []
        return [p for p in packages if p not in ports], ports

    def _release_extras(self, root: Path, game_paths) -> list:
        """Everything that holds no game; only OS clutter stays behind."""
        games = []
        for g in game_paths:
            try:
                games.append(Path(g).resolve())
            except OSError:
                pass
        out = []

        def visit(d: Path) -> None:
            try:
                children = sorted(d.iterdir(), key=lambda q: q.name.lower())
            except OSError:
                return
            for c in children:
                if is_fs_junk_name(c.name) or c.is_symlink():
                    continue
                try:
                    rc = c.resolve()
                except OSError:
                    continue
                if rc in games:
                    continue
                if c.is_dir() and any(rc in g.parents for g in games):
                    visit(c)
                else:
                    out.append(c)
        visit(Path(root))
        return out

    @staticmethod
    def _download_extras(item) -> list:
        """Minus what an earlier unpack of the archive added."""
        rel = getattr(item, "bundle_release", None)
        if not rel:
            return []
        root = os.path.realpath(rel) + os.sep
        beside = os.path.realpath(Path(rel).parent)      # a folder next to it with the game's title id
        return [Path(x) for x in (getattr(item, "bundle_siblings", None) or [])
                if os.path.realpath(x).startswith(root)
                or (os.path.realpath(Path(x).parent) == beside and Path(x).is_dir())]

    def _sibling_extras(self, parent: Path, game_paths) -> list:
        """Copied, never moved: the source is not touched."""
        ex = set()
        for g in game_paths:
            try:
                ex.add(str(Path(g).resolve()))
            except Exception:
                pass
        out = []
        try:
            for child in sorted(Path(parent).iterdir()):
                try:
                    rp = str(child.resolve())
                except Exception:
                    continue
                if rp in ex:
                    continue                                       # the game folder itself
                if any(g == rp or g.startswith(rp + os.sep) for g in ex):
                    continue                                       # a wrapper that holds the game
                if is_fs_junk_name(child.name):
                    continue
                if child.is_file() and child.suffix.lower() in EXTRA_JUNK_EXTS:
                    continue                                       # side-car notes / nfo / cover images
                out.append(child)
        except Exception:
            pass
        return out

    def _copy_item_payload(self, target: GameItem, source: GameItem) -> None:
        # fPKG and chain jobs keep their recipe whatever the archive held
        keep_op = getattr(target, "operation", "pack") in ("fpkg-build", "chain")
        target.path         = source.path
        target.archive_path = source.archive_path
        target.operation    = target.operation if keep_op else source.operation
        target.name         = source.name
        target.title_id     = source.title_id
        target.size         = source.size
        target.files        = source.files
        target.artwork      = source.artwork
        if source.artwork and not art_cache_file(getattr(target, "art_key", "") or ""):
            key = art_source_key(target)
            if store_art(key, source.artwork):
                target.art_key = key
        target.status       = source.status
        # now a real folder packed in place: the re-gate sizes only image+spool (~2.3x)
        target.source_kind    = getattr(source, "source_kind", "inplace")
        target.extracted_size = getattr(source, "extracted_size", source.size)
        # only pack jobs act on this; the fPKG path never injects the emu
        try:
            target.ampr_emu = is_apr_game(target.path)
        except Exception:
            target.ampr_emu = False

    def _launch_scan(self, fn):
        """Counts scans in flight so START can't re-add the source; decremented once in finally."""
        with self._scan_lock:
            self._scan_in_flight += 1
        def _runner():
            try:
                fn()
            finally:
                with self._scan_lock:
                    self._scan_in_flight = max(0, self._scan_in_flight - 1)
        threading.Thread(target=_runner, daemon=True).start()

    def add_source_to_queue(self):
        src_str = self._clean_path_str(self.source_var.get())
        if not src_str:
            self.pending_start = False
            self._pending_fpkg_identity = None
            messagebox.showerror("Nothing selected",
                                  "Enter or browse to a game folder, archive, disk image, or PFS image (.ffpfs/.ffpfsc).")
            return
        src = Path(src_str)
        if not src.exists():
            self.pending_start = False
            self._pending_fpkg_identity = None
            messagebox.showerror("Path not found",
                                  tr("This path does not exist:\n{src}", src=src))
            return
        # one-shot flag: consume it here so it never leaks into the next drop
        _unpack_folder = bool(self.unpack_mode_var.get())
        self.unpack_mode_var.set(False)

        # PS4 packages: sorted into the library
        if not _unpack_folder and _is_ps4_source(src):
            out = (self.output_var.get() or "").strip()
            if not out:
                self.pending_start = False
                self.open_job_dialog(init_src=str(src)); return
            item = self._ps4_item_for(src, output_path=out)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f"PS4 packages queued: {src.name} -> {out}.  Press Start to run.")
            return

        # .ffpfs: a re-pack source, read in place, no extraction
        if src.is_file() and src.suffix.lower() == ".ffpfs":
            item = GameItem.from_exfat(src)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f".ffpfs image queued for (re)packing: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                tr(".ffpfs image queued for packing: {name}", name=src.name),
                                "Ready", 0, 0, "00:00", " - ", " - ", side=True)
            return

        # .ffpfsc: unpack via MkPFS, or straight to fPKG when .pkg is remembered
        if src.is_file() and src.suffix.lower() == ".ffpfsc":
            if self.output_format_var.get() == "pkg":
                item = self._fpkg_item_for(src, dict(self.fpkg_defaults),
                                           output_path=(self.output_var.get() or "").strip() or None)
                if item is None:
                    self.pending_start = False
                    return
                self.queue.append(item)
                self.update_queue_box(select_item=item)
                self.log("OK", f".ffpfsc image queued as an fPKG build (remembered format .pkg): "
                               f"{src.name}  [{format_size(item.size)}]")
                self.status_update("Ready", tr(".ffpfsc queued for fPKG build: {name}", name=src.name),
                                    "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                return
            item = GameItem.from_pfs_image(src)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f".ffpfsc image queued for extraction: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                tr(".ffpfsc image queued for extraction: {name}", name=src.name),
                                "Ready", 0, 0, "00:00", " - ", " - ", side=True)
            return

        # .exfat / .ffpkg go straight to the backend
        if src.is_file() and src.suffix.lower() in DISK_IMAGE_SUFFIXES:
            item = GameItem.from_exfat(src)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            label = "exFAT image" if src.suffix.lower() == ".exfat" else "ffpkg image"
            self.log("OK", f"{label} queued: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                f"{label} queued: {src.name}",
                                "Ready", 0, 0, "00:00", " - ", " - ", side=True)
            return

        # single archive: placeholder now, extracted on its turn
        if src.is_file() and src.suffix.lower() in (".zip", ".rar", ".7z"):
            # many-volume headers take seconds on a slow drive, so read on the scan thread
            self.status_update("Scanning", tr("Reading archive headers: {name}…", name=src.name),
                                "Scanning Files", 0, 0, "00:00", " - ", " - ", side=True)

            _ps4_out = (self.output_var.get() or "").strip()
            _ps4_pw = self._candidate_passwords()

            def _read_archive(a=src):
                try:
                    info = ArchiveExtractor.ps4_archive_info(a, _ps4_pw)
                    if info is not None:
                        # PS4 packages inside: a sorting job, named from the first package
                        self.scan_q.put(("archive", self._ps4_archive_item(a, info, output_path=_ps4_out or None)))
                        return
                    self.scan_q.put(("archive", GameItem.from_archive(a)))
                except Exception as e:
                    self.scan_q.put(("error", tr("Could not read {name}: {e}", name=a.name, e=e)))
            self._launch_scan(_read_archive)
            return

        # folder of PFS images to unpack
        if src.is_dir() and _unpack_folder:
            self.status_update("Scanning", tr("Scanning {name} for PFS images…", name=src.name),
                                "Scanning Files", 0, 0, "00:00", " - ", " - ", side=True)
            self.log("INFO", f"Scanning folder for .ffpfs/.ffpfsc images: {src}")

            def _scan_pfs_folder(p=src):
                try:
                    images = find_files_by_suffix(p, PFS_IMAGE_SUFFIXES)
                    if not images:
                        self.scan_q.put(("error", tr("No .ffpfs or .ffpfsc files found in:\n{p}", p=p)))
                    elif len(images) == 1:
                        self.scan_q.put(("ok", GameItem.from_pfs_image(images[0])))
                    else:
                        self.scan_q.put(("pfs_found", images))
                except Exception as e:
                    self.scan_q.put(("error", tr("PFS scan error: {e}", e=e)))
            self._launch_scan(_scan_pfs_folder)
            return

        if is_game_folder(src):
            warns = validate_game_structure(src)
            if warns:
                msg = "\n".join(f"• {w}" for w in warns)
                if not ask_confirm(
                    "This may not be a game folder",
                    tr("Potential issues detected:\n\n{msg}\n\nExpected: sce_sys/param.json and eboot.bin", msg=msg),
                    "Add anyway"
                ):
                    self.pending_start = False
                    self._pending_fpkg_identity = None
                    return
            self.status_update("Scanning", f"Reading {src.name}…",
                                "Scanning Files", 0, 0, "00:00", " - ", " - ", side=True)
            def _scan_single(p=src):
                try:
                    self.scan_q.put(("ok", GameItem(p)))
                except Exception as e:
                    self.scan_q.put(("error", str(e)))
            self._launch_scan(_scan_single)
            return

        # parent or unknown folder: game folders and loose archives
        self.status_update("Scanning", f"Scanning {src.name}…",
                            "Scanning Files", 0, 0, "00:00", " - ", " - ", side=True)
        self.log("INFO", f"Scanning folder: {src}")
        cand_pw = self._candidate_passwords()   # built on the Tk thread, for archive peeking

        def _scan_folder(p=src, cand_pw=cand_pw, detect_patch=self.auto_integrate_patch_var.get()):
            try:
                # bundle: one game plus extras, packed into a recreated copy of this folder
                game, siblings, all_games, patch = detect_game_bundle(p, cand_pw, self.log, detect_patch)
                # a lone file game bundles too (folder name to output); a game subfolder doesn't
                naked_file_game = (game is not None
                                   and game.is_file()
                                   and not siblings and not patch)
                if game is not None and (siblings or patch or naked_file_game):
                    if naked_file_game:
                        extra = tr("single file game '{name}' - folder name mirrored to output", name=game.name)
                    else:
                        extra = tr("1 game + {siblings} extra file(s)", siblings=len(siblings))
                        if patch:
                            extra += f" + patch '{patch.name}' (integrated)"
                    self.log("OK", f"Folder bundle: {extra} - "
                                   "the folder will be recreated at the destination.")
                    self.scan_q.put(("ok", GameItem.from_bundle(p, game, siblings, patch)))
                    return
                # library: one bundle per subfolder that holds a game
                lib = scan_parent_for_bundles(p, cand_pw, self.log, detect_patch)
                if lib:
                    self.log("OK", f"Library scan: {len(lib)} game folder(s) found - each will be "
                                   "mirrored at the destination.")
                    self.scan_q.put(("bundles", lib))
                    return

                self.log("INFO", "Looking for PS5 game folders…")
                games = find_game_folders(p)
                if games:
                    self.log("INFO", f"Found {len(games)} game folder(s)")
                    if len(games) == 1:
                        try:
                            self.scan_q.put(("ok", GameItem(games[0])))
                        except Exception as e:
                            self.scan_q.put(("error", str(e)))
                    else:
                        self.scan_q.put(("multi_found", games))
                    return

                # no game folders: disk images and archives, one level deep
                self.log("INFO", "No game folders found - scanning for disk images and archives…")
                image_files = []   # .exfat and .ffpkg
                archives_raw = []  # every volume, .partN.rar included
                try:
                    for f in p.iterdir():
                        if not f.is_file():
                            continue
                        # exFAT AppleDouble sidecars like '._Game.rar' keep the real suffix. lovely
                        if is_fs_junk_name(f.name):
                            continue
                        suffix = f.suffix.lower()
                        if suffix in DISK_IMAGE_SUFFIXES:
                            image_files.append(f)
                            label = "exFAT" if suffix == ".exfat" else "ffpkg"
                            self.log("INFO", f"  Found {label} image: {f.name}")
                        elif suffix in (".zip", ".rar", ".7z"):
                            archives_raw.append(f)
                except Exception as e:
                    self.log("WARN", f"Could not list folder contents: {e}")

                image_files.sort(key=lambda f: f.name.lower())
                # a .partN.rar set is one game: key every part by its first volume
                archives_raw.sort(key=lambda f: f.name.lower())
                seen, archives = set(), []
                for f in archives_raw:
                    first = ArchiveExtractor._first_volume(f)
                    if first in seen:
                        continue
                    seen.add(first)
                    archives.append(first)
                    self.log("INFO", f"  Found archive: {first.name}")

                if image_files:
                    self.log("INFO", f"Found {len(image_files)} disk image(s) - queuing directly (no extraction needed)")
                    if len(image_files) == 1:
                        self.scan_q.put(("ok", GameItem.from_exfat(image_files[0])))
                    else:
                        self.scan_q.put(("exfat_found", image_files))
                    return

                if archives:
                    self.log("INFO", f"Found {len(archives)} archive(s) - queuing for extraction")
                    self.scan_q.put(("archives_found", archives))
                    return

                # split .7z/.zip volumes aren't recombined; tell the user instead of 'nothing found'
                split_parts = []
                try:
                    for f in p.iterdir():
                        n = f.name.lower()
                        if f.is_file() and (re.search(r"\.7z\.\d{2,}$", n)
                                            or re.search(r"\.zip\.\d{2,}$", n)
                                            or re.search(r"\.z\d{2,}$", n)):
                            split_parts.append(f.name)
                except Exception:
                    pass
                if split_parts:
                    sample = ", ".join(sorted(split_parts)[:4])
                    self.scan_q.put((
                        "error",
                        tr("Found split-archive parts in:\n{p}\n\n  {sample}\n\nSplit .7z / .zip volumes are not "
                          "recombined automatically. Recombine them into a single .7z, .zip or .rar first (e.g. "
                          "with 7-Zip or Keka), then add that file.\n(Multi-part RAR - .partN.rar or .rNN - is "
                          "supported directly.)", p=p, sample=sample)
                    ))
                    return

                self.log("WARN", f"No games, disk images, or archives found in {p}")
                self.scan_q.put((
                    "error",
                    tr("Nothing found in:\n{p}\n\nExpected either:\n  • Game folders containing sce_sys/ and "
                      "eboot.bin\n  • Disk images (.exfat or .ffpkg)\n  • Archive files (.zip / .rar / .7z)", p=p)
                ))
            except Exception as e:
                self.log("ERROR", f"Folder scan crashed: {e}")
                self.scan_q.put(("error", tr("Scan error: {e}", e=e)))

        self._launch_scan(_scan_folder)

    def _archive_set_size(self, archive: Path) -> int:
        """Archives are usually stored, so this is about the extracted size."""
        try:
            base = re.sub(
                r'(\.part\d+\.rar|\.r\d{2,}|\.7z\.\d+|\.zip\.\d+|\.z\d+|\.\d{3}|\.rar|\.zip|\.7z)$',
                '', archive.name, flags=re.I)
            total = 0
            for p in archive.parent.iterdir():
                if p.is_file() and p.name.startswith(base):
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
            return total or archive.stat().st_size
        except Exception:
            try:
                return archive.stat().st_size
            except Exception:
                return 0

    def _mark_no_spotlight(self, *dirs) -> None:
        """macOS: a .metadata_never_index marker keeps Spotlight out."""
        if sys.platform != "darwin":
            return
        for d in dirs:
            if not d:
                continue
            try:
                p = Path(d)
                p.mkdir(parents=True, exist_ok=True)
                marker = p / ".metadata_never_index"
                if not marker.exists():
                    marker.touch()
            except Exception:
                pass

    def _warm_drive_types(self) -> None:
        """Background SSD/HDD probes, so diskutil/PowerShell never blocks the UI."""
        probes = [self.output_var.get().strip(), self.source_var.get().strip()]
        probes += [str(d) for d in self._temp_pool_dirs()]   # primary temp + extra pool drives
        for p in probes:
            if not p:
                continue
            threading.Thread(target=lambda pp=Path(p): get_drive_type(pp), daemon=True).start()

    def _start_keep_awake(self) -> None:
        """Checks the toggle each cycle, so a Settings change needs no restart."""
        self._keepawake_stop = threading.Event()
        self._keepawake_announced = False
        self._keepawake_thread = threading.Thread(target=self._keep_awake_loop, daemon=True)
        self._keepawake_thread.start()

    @staticmethod
    def _dev_of(path):
        if not path:
            return None
        try:
            p = Path(path)
            d = p if p.is_dir() else p.parent
            return os.stat(str(d)).st_dev
        except OSError:
            return None

    def _temp_pool_dirs(self):
        """Primary temp first, then the extra pool dirs; one per device."""
        out, seen = [], set()
        primary = self.temp_var.get().strip()
        cands = ([primary] if primary else []) + [str(p).strip() for p in getattr(self, "temp_pool", [])]
        for i, raw in enumerate(cands):
            if not raw:
                continue
            d = Path(raw)
            try:
                if i > 0 and not d.is_dir():
                    continue   # skip a missing EXTRA pool dir (unplugged drive, typo)
                dev = _drive_cache_key(d)
            except Exception:
                continue
            if dev in seen:
                continue
            seen.add(dev)
            out.append(d)
        return out

    def _keepalive_dirs(self):
        """Skips confirmed SSDs; WD Elements externals report 'Unknown', so those stay."""
        seen, out = set(), []
        raws = []
        for v in (self.source_var, self.output_var):
            try:
                raws.append((v.get() or "").strip())
            except Exception:
                pass
        raws += [str(d) for d in self._temp_pool_dirs()]
        for raw in raws:
            if not raw:
                continue
            p = Path(raw)
            d = p if p.is_dir() else p.parent
            try:
                if not d.is_dir():
                    continue
                if drive_type_cached(d) == "SSD":
                    continue  # never bother a confirmed SSD
                dev = os.stat(str(d)).st_dev
            except OSError:
                continue
            if dev in seen:
                continue
            seen.add(dev)
            out.append((d, dev))
        return out

    def _busy_devices(self):
        """Drives the job is streaming on, the pinger skips them; empty means ping all."""
        w = getattr(self, "worker", None)
        if w is None or not getattr(w, "is_alive", lambda: False)():
            return set()
        phase = getattr(w, "phase", "") or ""
        item = getattr(self, "_active_item", None)
        out = self.output_var.get().strip()
        src = getattr(item, "path", None) if item else None
        arch = getattr(item, "archive_path", None) if item else None
        br = getattr(item, "_build_root", None) if item else None   # extracted-source dir
        bt = getattr(item, "_build_temp", None) if item else None   # inner-image dir
        busy = set()
        def add(*paths):
            for p in paths:
                dev = self._dev_of(p)
                if dev is not None:
                    busy.add(dev)
        if phase in ("Scanning Files", "Reading Game", "Creating Temp PFS"):
            # pass 1; for a plain folder _build_root is an unused extract dir, not busy
            add((br or src) if arch else src, bt)
        elif phase in ("Compressing", "Writing Final Image"):
            add(bt, out)                 # pass 2: read inner image, write .ffpfsc
        elif phase == "Extracting":
            add(arch or src, br)         # archive extraction: read archive, write extract dir
        elif phase == "Verifying Output":
            add(out)                     # verify: read .ffpfsc
        return busy

    def _job_active(self) -> bool:
        """Outside a run the external HDDs may sleep; the pinger asks this."""
        try:
            if getattr(self, "_batch_running", False):
                return True
            w = getattr(self, "worker", None)
            if w is not None and w.is_alive():
                return True
            p = getattr(self, "current_process", None)
            if p is not None and p.poll() is None:
                return True
            if getattr(self, "pending_start", False):
                return True
        except Exception:
            pass
        return False

    def _keep_awake_loop(self) -> None:
        while not self._keepawake_stop.is_set():
            active = False
            try:
                active = bool(self.keep_drives_awake_var.get()) and self._job_active()
            except Exception:
                pass
            if active:
                # 8 s, under WD IntelliPark's park timer: heads stay loaded between games
                try:
                    interval = max(3, min(15, int(load_settings().get("keep_awake_interval", 8))))
                except Exception:
                    interval = 8
                # skip drives the job is hammering, a flushed write there only forces a seek
                busy = self._busy_devices()
                pinged = 0
                for d, dev in self._keepalive_dirs():
                    if dev in busy:
                        continue
                    poke_drive_keepalive(d)
                    pinged += 1
                if pinged and not self._keepawake_announced:
                    self.log("INFO", f"Keep-awake: holding idle drive(s) spun-up every "
                                     f"{interval}s for this run (skipping the busy one).")
                    self._keepawake_announced = True
            else:
                self._keepawake_announced = False
            self._keepawake_stop.wait(interval if active else 5)

    def _same_drive_rw_allowed(self, path) -> bool:
        """Free on an SSD, painful on an HDD."""
        mode = (load_settings().get("same_drive_rw", "auto") or "auto").lower()
        if mode == "always":
            return True
        if mode == "never":
            return False
        try:
            # Unknown is never cached, so a miss probes again
            dt = drive_type_cached(Path(path))
            if dt == "Unknown":
                dt = get_drive_type(Path(path))
            try:
                k = _drive_cache_key(Path(path))
                if not hasattr(self, "_drive_type_logged"):
                    self._drive_type_logged = set()
                if k not in self._drive_type_logged:
                    self._drive_type_logged.add(k)
                    self.log("INFO", f"Temp drive detected as {dt} - same-drive read+write "
                                     f"{'allowed (kept on this drive)' if dt == 'SSD' else 'avoided (split to output)'}.")
            except Exception:
                pass
            return dt == "SSD"
        except Exception:
            return False

    def _archive_extract_pct(self, item) -> float:
        """A copy job unpacking onto its output drive only renames later; across drives it's half."""
        if getattr(item, "operation", "") != "copy":
            return ARCHIVE_EXTRACT_OVERALL_PCT
        root, out = getattr(item, "_build_root", None), self._job_output_dir(item)
        try:
            if root is not None and out is not None and same_drive(Path(str(root)).parent, out):
                return ARCHIVE_EXTRACT_RENAME_PCT
        except Exception:
            pass
        return ARCHIVE_EXTRACT_COPY_PCT

    def _planned(self, item):
        """Other jobs are planned on a copy, so looking never moves one; cached 10 s."""
        if item is self._running_item() or getattr(item, "_from_archive", False):
            return item
        cache = self.__dict__.setdefault("_plan_cache", {})
        hit = cache.get(id(item))
        now = time.monotonic()
        if hit is not None and hit[0] is item and now - hit[1] < 10:
            return hit[2]
        probe = copy.copy(item)
        try:
            self._resolve_extract_root(probe, quiet=True)
        except Exception:
            probe = item
        cache[id(item)] = (item, now, probe)
        return probe

    def _resolve_extract_root(self, item, quiet: bool = False) -> Path:
        """Sets _build_root (archive extract dir) and _build_temp (--temp-dir, inner image)."""
        archive = getattr(item, "archive_path", None)
        # space gate: .ffpfsc reserves a realistic size, .ffpfs the full one
        try:
            _pj = getattr(item, "output_compressed", None)
            item._output_compressed = bool(self.output_compressed_var.get() if _pj is None else _pj)
            # an fPKG is Kraken-packed per file, so the compressed estimate fits .pkg too
        except Exception:
            item._output_compressed = True
        temp_base = self.temp_var.get().strip()
        if not temp_base:
            anchor = archive.parent if archive else Path.home()
            temp_base = str(anchor / "_ffpfsc_temp")
            self.temp_var.set(temp_base)
        temp_base_p = Path(temp_base)
        temp_root = temp_base_p / "_extracted"

        def set_temp():
            item._build_root = temp_root
            item._build_temp = temp_base_p
            item._image_only_on_temp = False
            item._extract_on_pool = False
        set_temp()   # default: whole scratch on the temp drive

        # resolved several times per item, so log each decision once
        def _plog(level, msg):
            if quiet or getattr(item, "_last_placement_log", None) == msg:
                return
            item._last_placement_log = msg
            self.log(level, msg)

        mode = self.drive_mode_var.get() if getattr(self, "drive_mode_var", None) else "auto"
        _jo = self._job_output_dir(item)
        out_str = str(_jo) if _jo else ""
        size = _build_size_of(item)
        size_is_estimate = False
        # encrypted headers report size 0; game archives barely shrink, so ~1.05x on-disk
        if size == 0 and getattr(item, "archive_path", None):
            on_disk = int(getattr(item, "size", 0) or 0)
            if on_disk > 0:
                size = int(on_disk * 1.05)
                size_is_estimate = True
        factor = _peak_factor_for(item)
        if not out_str:
            return temp_root

        out_dir = Path(out_str)
        spread_root = out_dir / "_ffpfsc_extract"
        spread_temp = out_dir / "_ffpfsc_temp"

        def set_spread():
            item._build_root = spread_root
            item._build_temp = spread_temp
            item._image_only_on_temp = False
            item._extract_on_pool = False
        try:
            probe_out = out_dir if out_dir.exists() else out_dir.parent
            out_is_source = bool(archive) and same_drive(probe_out, archive.parent)
        except Exception:
            probe_out, out_is_source = out_dir, False

        same_to = same_drive(temp_base_p, out_dir)
        # same-drive rw is free on an SSD, so no padding; the pass-2 assert is the backstop
        same_rw_ok = self._same_drive_rw_allowed(temp_base_p)
        item._same_drive_ok = bool(same_rw_ok)
        # .pkg chain: image and finished file share one drive, no spool check in pass 2
        pkg_chain = (getattr(item, "operation", "") == "chain" and chain_needs_unpack(item)
                     and chain_source_kind(item) == "pkg")
        same_pad = same_to and (pkg_chain or not same_rw_ok)
        full_need  = estimate_peak_space_needed(size, factor, same_pad)  # src+image+spool on temp
        image_need = estimate_image_space_needed(size)                   # just the inner image on temp
        out_full   = estimate_peak_space_needed(size, factor, True)      # everything on the output drive
        # split: output holds the extracted source (archives) plus the final container
        comp_out = bool(getattr(item, "_output_compressed", True))
        known_out = int(getattr(item, "size", 0) or 0) if getattr(item, "source_kind", "") == "archive" else 0
        src_on_out = size if archive else 0
        out_split_need = int(src_on_out) + estimate_output_space_needed(size, comp_out, known_out)
        temp_free = get_free_space(temp_base_p)
        out_free  = get_free_space(probe_out)
        szs = (format_size(size) + " est.") if size_is_estimate else (format_size(size) if size else "?")
        contention = " (output is the source drive: read+write contention, slower)" if out_is_source else ""

        # disk images are single-pass: no inner image, only the output needs space
        if _item_is_single_pass(item):
            output_need = estimate_output_space_needed(size, comp_out, 0)
            set_spread()  # the backend still wants a temp dir
            if size == 0 or out_free >= output_need:
                if size > 0:
                    _plog("INFO", f"Auto: {item.name} (~{szs}): disk image → single-pass "
                                  f"to {out_dir} (~{format_size(output_need)} needed, "
                                  f"{format_size(out_free)} free){contention}.")
            else:
                _plog("WARN", f"Auto: {item.name} (~{szs}): disk image needs "
                              f"~{format_size(output_need)} on output drive, only "
                              f"{format_size(out_free)} free - the space gate will skip/abort it.")
            return spread_root

        if mode == "temp":
            if size and temp_free < image_need:
                _plog("WARN", f"Drive 'temp': {item.name} image needs ~{format_size(image_need)} on temp, "
                                  f"only {format_size(temp_free)} free - the space gate will handle it.")
            return temp_root
        if mode == "spread":
            set_spread()
            _plog("INFO", f"Drive 'spread': building on the output drive {out_dir}{contention}.")
            return spread_root
        # auto: fast-temp candidates with their free space
        pool = []
        for d in self._temp_pool_dirs():
            try:
                pool.append((d, get_free_space(d)))
            except Exception:
                pass
        if not pool:
            pool = [(temp_base_p, temp_free)]
        is_archive_pre = getattr(item, "source_kind", "") == "archive"

        def _set(image_dir, extract_dir, image_only, on_pool):
            item._build_temp = Path(image_dir)
            item._build_root = Path(extract_dir)
            item._image_only_on_temp = image_only
            item._extract_on_pool = on_pool

        def _scratch(d):
            """Extra pool drives get a '_ffpfsc_temp' subfolder, never the drive root."""
            d = Path(d)
            return d if d == temp_base_p else d / "_ffpfsc_temp"

        def _is_ssd(d):
            # speed only: keep the image on real flash, not a roomy HDD pool entry
            try:
                dt = drive_type_cached(Path(d))
                if dt == "Unknown":
                    dt = get_drive_type(Path(d))
                return dt == "SSD"
            except Exception:
                return False

        # 1) all on one fast drive where same-drive rw is OK; not on a size estimate
        fit_full = [(d, f) for d, f in pool if f >= full_need and self._same_drive_rw_allowed(d)]
        if size > 0 and fit_full and not size_is_estimate:
            ssd_full = [(d, f) for d, f in fit_full if _is_ssd(d)]
            best = max(ssd_full or fit_full, key=lambda t: t[1])[0]
            _set(_scratch(best), _scratch(best) / "_extracted", False, False)
            if _drive_cache_key(best) != _drive_cache_key(temp_base_p):
                _plog("INFO", f"Auto: {item.name} (~{szs}): whole scratch on pool drive {best}.")
            return item._build_root
        # 2) archives: image on one SSD, source on another; not on an estimate either
        if size > 0 and is_archive_pre and not size_is_estimate and not pkg_chain:
            img_fit = [(d, f) for d, f in pool if f >= image_need and _is_ssd(d)]
            if img_fit:
                img_dir = min(img_fit, key=lambda t: t[1])[0]    # smallest SSD that fits the image
                img_dev = _drive_cache_key(img_dir)
                ext_fit = [(d, f) for d, f in pool
                           if _is_ssd(d) and _drive_cache_key(d) != img_dev and f >= int(size)]
                if ext_fit:
                    ext_dir = max(ext_fit, key=lambda t: t[1])[0]  # most-free OTHER SSD
                    _set(_scratch(img_dir), _scratch(ext_dir) / "_extracted", True, True)
                    _plog("INFO", f"Auto: {item.name} (~{szs}): extract source → {ext_dir}; "
                                     f"inner image → {img_dir}; final → {out_dir}. Pass 1 stays SSD↔SSD.")
                    return item._build_root
        # 3) split: image on a fast drive, source on the output drive, no same-drive rw
        img_fit = [(d, f) for d, f in pool if f >= image_need]
        if size > 0 and img_fit and out_free >= out_split_need and not pkg_chain:
            ssd_img = [(d, f) for d, f in img_fit if _is_ssd(d)]
            img_dir = max(ssd_img or img_fit, key=lambda t: t[1])[0]  # prefer an SSD for the image
            _set(_scratch(img_dir), spread_root, True, False)   # source extracts to the big output drive
            _plog("INFO", f"Auto: {item.name} (~{szs}): 1) extract source → output drive "
                             f"({out_dir}); 2) build inner image → {temp_drive_label(img_dir)} "
                             f"({img_dir}); spool auto-routed. No same-drive read+write.")
            return spread_root
        # 4) all on the output drive: slower, but it completes
        if out_free >= out_full:
            set_spread()
            why = "too big for the temp drive(s)" if size > 0 else "unknown size - using the larger drive for safety"
            _plog("INFO", f"Auto: {item.name} (~{szs}) {why} → building on {out_dir}"
                             f"{contention or ' (mechanical drive, slower, but it completes)'}.")
            return spread_root
        # 5) nothing fits: stay on temp, the space gate skips it with real numbers
        _plog("WARN", f"Auto: {item.name} (~{szs}) fits neither the temp drive(s) (~{format_size(image_need)}) "
                          f"nor the output drive (~{format_size(out_full)}) - the space gate will skip/abort it.")
        return temp_root

    def _scratch_parent_roots(self, item=None) -> set:
        """A folder the user named '_extracted' anywhere else is not ours."""
        cands = []
        try:
            cands.append(self.temp_var.get().strip())
            cands.append(self.output_var.get().strip())
        except Exception:
            pass
        try:
            # every configured entry: scratch from an older setup stays ours
            pool = list(self._temp_pool_dirs()) + [str(x) for x in (getattr(self, "temp_pool", None) or [])]
            for d in pool:
                cands.append(str(d))
                cands.append(str(Path(d) / "_ffpfsc_temp"))
        except Exception:
            pass
        if item is not None:
            for attr in ("output_path", "_build_temp", "_build_root"):
                v = getattr(item, attr, None)
                if v:
                    cands.append(str(v))
        roots = set()
        for c in cands:
            if not c:
                continue
            try:
                roots.add(Path(c).resolve())
            except Exception:
                pass
        return roots

    def _extract_dir_for_item(self, item):
        """The item's unpack dir under an app-made '_extracted' or '_ffpfsc_extract', else None."""
        try:
            src = Path(getattr(item, "path", "") or "").resolve()
        except Exception:
            return None
        roots = self._scratch_parent_roots(item)
        for parent in src.parents:
            if parent.name in ("_extracted", "_ffpfsc_extract"):
                if parent not in roots and parent.parent not in roots:
                    return None
                try:
                    return parent / src.relative_to(parent).parts[0]
                except Exception:
                    return None
        return None

    @staticmethod
    def _run_took_files(worker) -> bool:
        """The backend says so only after a file is gone, so a crash in between says nothing."""
        return bool(worker is not None and (getattr(worker, "consumed", False)
                                            or "--stage-in-place" in (getattr(worker, "cmd", None) or [])))

    def _mark_source_taken_in(self, item, cmd) -> None:
        """Saved first, so a crash mid-run unpacks again instead of using a gutted copy."""
        taken = "--stage-in-place" in (cmd or [])
        if bool(getattr(item, "source_taken_in", False)) != taken:
            item.source_taken_in = taken          # per run: only this run's command counts
            if taken and not (self._save_queue() and self._queue_marks_taken_in(item)):
                # unrecorded, a crash would leave a copy that looks whole: free nothing then
                item.source_taken_in = False
                cmd.remove("--stage-in-place")
                self.log("WARN", "Could not save the queue: this run keeps the unpacked copy whole.")

    @staticmethod
    def _queue_marks_taken_in(item) -> bool:
        """Read back from disk, not assumed."""
        try:
            src = str(getattr(item, "path", "") or "")
            return any(d.get("source_taken_in") is True and str(d.get("path") or "") == src
                       for d in (load_settings().get("queue") or []) if isinstance(d, dict))
        except Exception:
            return False

    def _consumable_extract(self, item) -> bool:
        """The backend may free files only when no other job reads this unpack."""
        own = self._extract_dir_for_item(item)
        if own is None or not getattr(item, "origin_archive", None):
            return False
        return own not in self._open_extracts(exclude=item)

    def _open_jobs(self, exclude=None):
        """Waiting, running, failed with a kept copy, or done with extras still missing."""
        for j in self.queue:
            if j is exclude or getattr(j, "archive_path", None):
                continue
            st = getattr(j, "status", "")
            kept = getattr(j, "kept_extract", False)
            if (st == "Done" and not kept) or (st in self._RETRYABLE and not kept):
                continue
            yield j

    def _open_extracts(self, exclude=None) -> set:
        """Several games of one archive share one copy."""
        return {r for r in (self._extract_dir_for_item(j) for j in self._open_jobs(exclude)) if r is not None}

    def _extract_cleanup_parts(self, item, keep_own_extras: bool = False) -> list:
        """Spares what other jobs still read; mid-job also the job's own extras."""
        root = self._extract_dir_for_item(item)
        if root is None:
            return []
        keep = set()
        jobs = list(self._open_jobs(exclude=item)) + ([item] if keep_own_extras else [])
        for j in jobs:
            srcs = list(getattr(j, "bundle_siblings", None) or [])
            if j is not item:
                srcs.insert(0, getattr(j, "path", None))
            for src in srcs:
                try:
                    rp = Path(str(src)).resolve() if src else None
                except OSError:
                    rp = None
                if rp is not None and (rp == root or root in rp.parents):
                    keep.add(rp)
        return removable_parts(root, keep) if keep else [root]

    def _reclaim_temp_base_inline(self, tp: str) -> None:
        tp = (tp or "").strip()
        if not tp:
            return
        base = Path(tp)
        for name in ("_patch_game", "_patch_files"):
            try:
                shutil.rmtree(str(base / name), ignore_errors=True)
            except Exception:
                pass
        try:
            ex = base / "_extracted"
            if ex.is_dir() and not any(ex.iterdir()):
                ex.rmdir()
        except Exception:
            pass
        # only after the last job; mid-batch the temp base gets reused
        try:
            if (not self.queue and base.name == "_ffpfsc_temp"
                    and base.is_dir() and not any(base.iterdir())):
                base.rmdir()
                self.log("INFO", "Removed the now-empty temp folder.")
        except Exception:
            pass

    def _cleanup_item_extract(self, item) -> None:
        """Spares what other jobs still read; the rmtree runs on a thread."""
        d = self._extract_dir_for_item(item)
        parts = self._extract_cleanup_parts(item)
        _tp = (self.temp_var.get() or "").strip()   # snapshot on the main thread
        def _work(d=d, tp=_tp):
            try:
                sz = sum(_remove_scratch(p) for p in parts)
                if sz:
                    self.log("INFO", f"Cleaned {format_size(sz)} extracted source from {d.parent.name}.")
                if d is not None and d.parent.name == "_ffpfsc_extract":
                    try:
                        if not any(d.parent.iterdir()):
                            d.parent.rmdir()
                    except Exception:
                        pass
            except Exception:
                pass
            # also for jobs with no extracted dir
            self._reclaim_temp_base_inline(tp)
        self._run_cleanup(_work)

    def _free_source_after_pass1(self, item) -> None:
        """On [PASS1-DONE]: pass 2 and an OOM retry only need the inner image."""
        if item is None or getattr(item, "_source_freed", False):
            return
        own = self._extract_dir_for_item(item)
        if own is None:
            return  # user's own folder, never delete
        item._source_freed = True
        parts = self._extract_cleanup_parts(item, keep_own_extras=True)   # not what other jobs still read, nor its own extras
        def _work(d=own):
            try:
                if parts:
                    sz = sum(_remove_scratch(p) for p in parts)
                    self.log("INFO", f"Freed {format_size(sz)} extracted source after pass 1 "
                                     f"(no longer needed for compression).")
                    if d.parent.name == "_ffpfsc_extract":
                        try:
                            if not any(d.parent.iterdir()):
                                d.parent.rmdir()
                        except Exception:
                            pass
            except Exception:
                pass
        self._run_cleanup(_work)

    def _cleanup_inner_image(self, item) -> None:
        """Kept until now so an OOM retry could resume pass 2."""
        if item is None:
            return
        inner = getattr(item, "_inner_image", None)
        if not inner:
            return
        try:
            item._inner_image = None
        except Exception:
            pass
        if not _is_inner_image_path(inner):
            self.log("WARN", f"Not removed, not an inner image: {inner}")
            return
        def _work(p=Path(str(inner))):
            try:
                if p.is_file() and not p.is_symlink():
                    sz = p.stat().st_size
                    p.unlink()
                    if sz:
                        self.log("INFO", f"Removed {format_size(sz)} inner image (pass-1 cache).")
                par = p.parent
                if par.name == "_ffpfsc_inner":
                    try:
                        if not any(par.iterdir()):
                            par.rmdir()
                    except Exception:
                        pass
            except Exception:
                pass
        self._run_cleanup(_work)

    def _candidate_passwords(self, item=None) -> list[str]:
        """Order: the explicit password, the job's own, then the global list; no dupes."""
        cands: list[str] = []
        explicit = self.password_var.get().strip()
        if explicit:
            cands.append(explicit)
        if item is not None:
            override = (getattr(item, "password", None) or "").strip()
            if override:
                cands.append(override)
        for p in self.archive_passwords:
            p = (p or "").strip()
            if p:
                cands.append(p)
        seen: set[str] = set()
        out: list[str] = []
        for p in cands:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _extract_queued_item(self, item):
        """Runs on a thread, then calls start() again."""
        archive = item.archive_path
        extract_root = self._resolve_extract_root(item)
        # keep Spotlight off the scratch, it fights us for the same (often spinning) drive
        self._mark_no_spotlight(extract_root, getattr(item, "_build_temp", None))

        self._active_item = item  # terminal handlers clean this one even if the queue changed
        item.status = "Extracting"
        self.cancel_requested = False
        self.extract_cancel_event.clear()
        self.update_queue_box()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        self.log("INFO", f"Extracting archive: {archive.name}")
        self._begin_job_progress(item)
        self.status_update("Extracting", f"Unpacking {archive.name}…  0%",
                            "Extracting", 0, 0, "00:00", " - ", " - ", job=item)

        _last_pct = [-1]
        _share = self._archive_extract_pct(item)
        _t0 = time.monotonic()
        # header size, else the archive itself; game data hardly shrinks
        _unpacked = int(getattr(item, "extracted_size", 0) or 0) or int(archive_set_ondisk_size(archive) or 0)

        def _progress(pct, filename):
            if pct - _last_pct[0] >= 1 or pct >= 100:
                _last_pct[0] = pct
                secs = time.monotonic() - _t0
                speed, eta = unpack_rate(pct, secs, _unpacked)
                self.status_update("Extracting",
                                    f"Unpacking {archive.name}…  {pct}%",
                                    "Extracting", pct, pct * _share / 100.0,
                                    format_duration(secs), speed, eta, job=item)

        candidate_passwords = self._candidate_passwords(item)

        def worker():
            try:
                extracted_root = ArchiveExtractor.extract_with_passwords(
                    archive, extract_root,
                    candidate_passwords,
                    log_fn=self.log, progress_fn=_progress,
                    cancel_event=self.extract_cancel_event
                )
                if self.cancel_requested:
                    raise ArchiveExtractionCancelled("Archive extraction cancelled by user.")
                kind, paths = self._classify_extracted_payload(extracted_root, archive.name)
                item.archive_content = self._content_of_payload(kind, paths) or getattr(item, "archive_content", "")
                if getattr(item, "auto_output", False):
                    self._auto_resolve_unpacked(item, kind, paths)
                if getattr(item, "content_kind", "") == ORGANIZE_TARGET:
                    item.origin_archive = str(archive)
                    item.origin_extracted_size = int(getattr(item, "extracted_size", 0) or 0)
                    item.path, item.archive_path = Path(extracted_root), None
                    item.source_kind, item.copy_mode = "inplace", "move"
                    item._from_archive = True
                    try:
                        item.size = item.extracted_size = int(get_folder_size(Path(extracted_root)))
                    except Exception:
                        pass
                    self._extract_q.put(("ok", (item, [])))
                    return
                if kind == "ps4":
                    item.operation = "copy"           # sorted into the library, whatever the job said
                if kind == "pkg" and getattr(item, "operation", "") != "chain":
                    item.chain_to = ("pkg" if getattr(item, "operation", "") == "fpkg-build" else
                                     "ffpfs" if getattr(item, "output_compressed", True) is False else "ffpfsc")
                    item.operation = "chain"
                if kind == "pkg":
                    paths, _ports = self._split_backport_packages(Path(extracted_root), paths)
                    _want = getattr(item, "origin_payload", None)       # a retry keeps its package
                    _back = [x for x in _ports if self._payload_key(extracted_root, x) == _want]
                    paths, _ports = _back + paths, [x for x in _ports if x not in _back]
                    for _bp in _ports:
                        self.log("INFO", f"{_bp.name} is a backport: it goes next to the output, not into a job of its own")
                payload_items = [self._item_from_payload_path(kind, path) for path in paths]
                for pi, path in zip(payload_items, paths):
                    pi.origin_payload = self._payload_key(extracted_root, path)
                # Unpacked again for one game of the archive (a retry): that game is this job.
                want = getattr(item, "origin_payload", None)
                item._unpacked_again = bool(want)
                if want:
                    hit = next((i for i, pi in enumerate(payload_items) if pi.origin_payload == want), None)
                    if hit is None:
                        raise RuntimeError(f"{archive.name} no longer holds {want}.")
                    payload_items.insert(0, payload_items.pop(hit))
                    paths = [paths[hit]] + [x for i, x in enumerate(paths) if i != hit]
                primary = payload_items[0]
                item.origin_archive = str(archive)
                item.origin_payload = primary.origin_payload
                item.origin_extracted_size = int(getattr(item, "extracted_size", 0) or 0)
                self._copy_item_payload(item, primary)
                if kind == "ps4":
                    item.content_kind = "ps4"
                    item.copy_mode = "move"   # the app's own extraction (see _ps4_copy_mode)
                    item.ps4_count = getattr(primary, "ps4_count", 0)
                    if getattr(primary, "display_name", None):
                        item.display_name = primary.display_name
                if kind == "pkg":
                    self._probe_pkg_content(item)
                try:
                    if self._take_game_name(item, self._game_identity(item)):
                        self._names_dirty = True
                except Exception:
                    pass
                # progress goes after the extract slice; underscore attrs aren't saved with the queue
                item._from_archive = True
                if len(payload_items) > 1 and not want:
                    self._group_games_of_archive(item, payload_items[1:])
                # extras beside the game in the archive (an '[ ALL DLC ]' wrapper) go along
                planned = self._download_extras(item)
                if kind == "pkg":
                    # a package release: backports, checksums and the rest all go along
                    try:
                        item.bundle_siblings = planned + self._release_extras(Path(extracted_root), paths)
                    except Exception:
                        item.bundle_siblings = planned
                elif kind == "game" and not (len(paths) == 1
                        and Path(paths[0]).resolve() == Path(extracted_root).resolve()):
                    try:
                        item.bundle_siblings = planned + self._sibling_extras(Path(primary.path).parent, paths)
                    except Exception:
                        item.bundle_siblings = planned
                self._extract_q.put(("ok", (item, payload_items[1:])))
            except ArchiveExtractionCancelled as exc:
                self.log("WARN", str(exc))
                self._extract_q.put(("cancelled", str(exc)))
            except Exception as exc:
                self.log("ERROR", f"Extraction failed: {exc}")
                self._extract_q.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _patch_needs_prepare(self, item) -> bool:
        """.zip/.rar patches are left to the backend's --patch; only .7z gets unpacked here."""
        if getattr(item, "operation", "") != "patch":
            return False
        try:
            gp = Path(getattr(item, "path", "") or "")
            if gp.is_file() and gp.suffix.lower() in (".zip", ".rar", ".7z"):
                return True
            ps = getattr(item, "patch_source", None)
            if ps and Path(ps).suffix.lower() == ".7z":
                return True
        except Exception:
            pass
        return False

    def _patch_prepare_failed(self, item, msg: str):
        self.log("ERROR", f"Patch prepare failed: {msg}")
        self.status_update("Failed", tr("Patch prepare failed: {msg}", msg=msg), "Failed", 0, 0, "00:00", " - ", " - ")
        # failed items stay, only successful ones leave
        self._retire_failed(item, "Failed")
        if self._batch_running:
            self._batch_failed += 1
            self._update_batch_counter()
            self.update_queue_box()
            if self._has_pending():
                self.root.after(600, self._batch_auto_start)
            else:
                self._batch_running = False
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                if self._batch_total > 1:
                    self._show_batch_complete()
        else:
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self.update_queue_box()

    def _prepare_patch_item(self, item):
        """Background thread; Tk calls go through the main loop."""
        self._active_item = item
        item.status = "Extracting"
        self.cancel_requested = False
        self.extract_cancel_event.clear()
        self.update_queue_box()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        temp_base = self.temp_var.get().strip() or str(Path(item.path).parent / "_ffpfsc_temp")
        self.temp_var.set(temp_base)
        pw = self._candidate_passwords(item)
        game = Path(item.path)
        patch = Path(item.patch_source) if getattr(item, "patch_source", None) else None
        ARCH = (".zip", ".rar", ".7z")

        def work():
            try:
                gp = game
                inplace = bool(getattr(item, "patch_inplace", False))
                if game.is_file() and game.suffix.lower() in ARCH:
                    self.log("INFO", f"Extracting patch game archive: {game.name}")
                    self.status_update("Extracting", tr("Unpacking game: {name}…", name=game.name),
                                       "Extracting", 0, 0, " - ", " - ", " - ")
                    gp = ArchiveExtractor.extract_with_passwords(
                        game, Path(temp_base) / "_patch_game", pw,
                        log_fn=self.log, cancel_event=self.extract_cancel_event)
                    inplace = True  # throwaway temp copy, so overlay in place
                pp = patch
                if patch is not None and patch.suffix.lower() == ".7z":
                    self.log("INFO", f"Extracting .7z patch: {patch.name}")
                    self.status_update("Extracting", tr("Unpacking patch: {name}…", name=patch.name),
                                       "Extracting", 0, 0, " - ", " - ", " - ")
                    pp = ArchiveExtractor.extract_with_passwords(
                        patch, Path(temp_base) / "_patch_files", pw,
                        log_fn=self.log, cancel_event=self.extract_cancel_event)
                if self.cancel_requested:
                    raise ArchiveExtractionCancelled("Cancelled by user.")

                def _done():
                    item.path = Path(gp)
                    if pp is not None:
                        item.patch_source = Path(pp)
                    item.patch_inplace = inplace
                    item.status = "Queued"
                    self.update_queue_box()
                    if self._batch_running:
                        self._batch_auto_start()
                    else:
                        self.start()
                self.root.after(0, _done)
            except ArchiveExtractionCancelled as e:
                self.root.after(0, lambda m=str(e): self._patch_prepare_failed(item, m))
            except Exception as e:
                self.root.after(0, lambda m=str(e): self._patch_prepare_failed(item, m))
        threading.Thread(target=work, daemon=True).start()

    def _queue_sel_idx(self) -> int | None:
        sel = self.queue_listbox.curselection()
        return int(sel[0]) if sel else None

    def _on_queue_select(self, _event=None):
        idx = self._queue_sel_idx()
        if idx is not None and idx < len(self.queue):
            self.update_game_details(self.queue[idx])

    def _resolve_archive_password(self, item) -> None:
        """Only when no saved password opened the header (extracted_size still 0)."""
        if (getattr(item, "source_kind", "") != "archive"
                or getattr(item, "extracted_size", 0) > 0
                or not getattr(item, "archive_path", None)):
            return
        arc = item.archive_path
        if getattr(item, "archive_problem", ""):
            # Broken, not locked: no password reads past a damaged or missing part.
            self._log_archive_problem(item)
            return
        if not getattr(item, "header_locked", False):
            # the header opened, only the size looked odd: asking for a password would be wrong
            self.log("INFO", f"'{arc.name}': the archive opens, but its size could not be trusted; "
                             f"the drive routing estimates it.")
            return
        cands0 = self._candidate_passwords(item)
        if cands0:
            state, sz, problem = ArchiveExtractor.probe_header_state(arc, cands0)
            if state == "open":            # a password added since the job was created
                item.header_locked = False
                item.extracted_size = ArchiveExtractor.plausible_extracted_size(sz, getattr(item, "size", 0))
                return
            if state == "damaged":
                item.header_locked, item.archive_problem = False, problem
                self._log_archive_problem(item)
                return
        attempts = 0
        while attempts < 3:
            dlg = ArchivePasswordPrompt(self, arc.name)
            try:
                self.root.wait_window(dlg)
            except Exception:
                return
            pw = (dlg.password or "").strip()
            if not pw:
                return
            attempts += 1
            try:
                state, sz, problem = ArchiveExtractor.probe_header_state(arc, [pw])
            except Exception:
                state, sz, problem = "locked", 0, ""
            if state == "damaged":
                # the password got past the check, the archive itself is broken
                item.header_locked, item.archive_problem, item.password = False, problem, pw
                self._log_archive_problem(item)
                return
            opened = state == "open"
            if opened:
                item.header_locked = False
                item.extracted_size = ArchiveExtractor.plausible_extracted_size(sz, getattr(item, "size", 0))
                item.password = pw
                try:
                    if pw not in (self.archive_passwords or []):
                        self.archive_passwords.append(pw)
                        save_settings({"archive_passwords": list(self.archive_passwords)})
                except Exception:
                    pass
                self.log("OK", f"Unlocked '{arc.name}' - " + (
                    f"size {format_size(item.extracted_size)} read for routing." if item.extracted_size
                    else "its size could not be trusted; the drive routing estimates it."))
                return
            self.log("WARN", f"Password did not unlock '{arc.name}'.")
            if not ask_confirm(
                "Wrong password",
                tr("That password did not unlock '{name}'.", name=arc.name), "Try again", "Give up"):
                return
        self.log("WARN", f"Gave up on the password for '{arc.name}' - routing will use the size estimate.")

    def _log_archive_problem(self, item) -> None:
        arc = getattr(item, "archive_path", None)
        self.log("WARN", f"'{Path(str(arc)).name if arc else item.name}' cannot be read: {item.archive_problem}. "
                         f"No password helps here; get the damaged or missing part again, or remove the job.")

    def _on_queue_double_click(self, event=None):
        """The running job and finished ones aren't edited, they only refresh details."""
        # nearest(event.y): the listbox selection may lag behind the click
        idx = None
        try:
            if event is not None:
                idx = self.queue_listbox.nearest(event.y)
        except Exception:
            idx = None
        if idx is None:
            idx = self._queue_sel_idx()
        if idx is None or idx >= len(self.queue):
            return "break"
        self._edit_job(self.queue[idx])
        return "break"

    def _edit_job(self, item) -> None:
        if item not in self.queue:
            return
        idx = self.queue.index(item)
        # never the running job, its paths are in use
        if item is self._running_item():
            self.log("WARN", "Cannot edit the currently running job.")
            return
        status = (getattr(item, "status", "") or "").lower()
        if status in ("failed", "skipped", "cancelled", "done"):
            # the extracted copy may be gone: the editor then shows the archive it came from
            self._rearm_from_archive(item)

        if getattr(item, "operation", "") == "ps4-keystone":
            self._show_view("keystone")
            self._keystone_view.edit(item)
            return
        if getattr(item, "ps5_repair", False):
            self._show_view("ps5repair")
            self._ps5_view.edit(item)
            return
        try:
            JobDialog(self, item=item)
        except Exception as e:
            self.log("ERROR", f"Could not open the editor: {e}")


    # fPKG build: identity comes from param.json at build time, these are fallbacks
    FPKG_FILE_SOURCES = (".zip", ".rar", ".7z", ".exfat", ".ffpkg", ".ffpfs", ".ffpfsc")
    _TITLE_ID_RE = re.compile(r"^[A-Z]{4}[0-9]{5}$")

    def _fpkg_params_of(self, item) -> dict:
        lvl = getattr(item, "fpkg_level", None)
        try:
            lvl = int(lvl) if lvl is not None else int(self.compression_level_var.get())
        except Exception:
            lvl = int(self.compression_level_var.get())
        return {
            "content_id": getattr(item, "fpkg_content_id", "") or "",
            "title_id":   getattr(item, "fpkg_title_id", "") or "",
            "title":      getattr(item, "fpkg_title", "") or "",
            "version":    getattr(item, "fpkg_version", "01.000.000") or "01.000.000",
            "inner":      getattr(item, "fpkg_inner_mode", "kraken") or "kraken",
            "backend":    getattr(item, "fpkg_kraken_backend", "builtin") or "builtin",
            "level":      max(-4, min(9, lvl)),     # -4..-1 = Kraken fast preset, 0..9 = normal
            "dll":        getattr(item, "fpkg_pubtools_dll", "") or "",
            "retail_normalize": bool(getattr(item, "fpkg_retail_normalize", True)),
            "hdr_flag":         _hdr_mode(getattr(item, "fpkg_hdr_flag", "auto")),
            "regen_playgo":     bool(getattr(item, "fpkg_regen_playgo", False)),
            "fake_sign":        bool(getattr(item, "fpkg_fake_sign", True)),
            "backport_target":  getattr(item, "backport_target", None),
            "backport_libs":    getattr(item, "backport_libs_root", None) or "",
        }

    # title id in a release name, also right before '_' ('-PPSA99999_v01.020')
    _ARCHIVE_TID = re.compile(r"(?<![A-Za-z0-9])(PPSA\d{5}|CUSA\d{5})(?!\d)", re.I)

    def _group_games_of_archive(self, item, extras) -> None:
        """One output folder: the game the archive names, else the biggest."""
        games = [item] + list(extras)
        for k, g in enumerate(games, start=1):
            g.archive_games, g.archive_index = len(games), k
        arc = Path(str(getattr(item, "origin_archive", "") or ""))
        m = next((x for x in (self._ARCHIVE_TID.search(n) for n in (arc.name, arc.parent.name)) if x), None)
        tid = (m.group(1) if m else str(getattr(item, "archive_title_id", "") or "")).upper()
        idents = [(g, self._game_identity(g) or {}) for g in games]
        named = [(g, i) for g, i in idents if tid and str(i.get("title_id") or "").upper() == tid]
        if len(named) != 1:
            named = [max(idents, key=lambda gi: int(getattr(gi[0], "extracted_size", 0)
                                                    or getattr(gi[0], "size", 0) or 0))]
        main_ident = named[0][1]
        if main_ident.get("title") or main_ident.get("title_id"):
            folder = organized_names(main_ident, "")[0]
            for g in games:
                g.organize_folder = folder

    def _adopt_games_of_archive(self, item, extras) -> None:
        """Retry unpack: siblings waiting on the same archive run from this copy, no second jobs."""
        origin = getattr(item, "origin_archive", None)
        jobs = {getattr(j, "origin_payload", None): j for j in self.queue
                if j is not item and origin and getattr(j, "origin_archive", None) == origin
                and getattr(j, "origin_payload", None)}
        for extra in extras:
            job = jobs.get(getattr(extra, "origin_payload", None))
            if (job is not None and getattr(job, "archive_path", None) and job is not self._active_item
                    and getattr(job, "status", "") not in self._TERMINAL_STATUSES):
                self._copy_item_payload(job, extra)
                job.kept_extract = False
                self.log("INFO", f"{getattr(job, 'display_name', None) or job.name}: runs from the same unpack.")

    def _as_chain_job(self, extra, tpl) -> None:
        """A patch belongs to one game and is not copied; for .pkg only the compression is."""
        extra.operation = "chain"
        extra.chain_to = getattr(tpl, "chain_to", None) or "ffpfsc"
        extra.archive_content = getattr(tpl, "archive_content", "") or getattr(extra, "archive_content", "")
        extra.chain_sign = bool(getattr(tpl, "chain_sign", False))
        extra.chain_lz4 = bool(getattr(tpl, "chain_lz4", False))
        extra.lz4_profile = getattr(tpl, "lz4_profile", None)
        extra.chain_lz4_unpack = bool(getattr(tpl, "chain_lz4_unpack", False))
        extra.patch_source = None
        extra.backport_target = getattr(tpl, "backport_target", None)
        extra.backport_libs_root = getattr(tpl, "backport_libs_root", None)
        extra.after_source = getattr(tpl, "after_source", None)
        extra.after_move_to = getattr(tpl, "after_move_to", None)
        extra.source_root = getattr(tpl, "source_root", None)
        extra.output_path = getattr(tpl, "output_path", None)
        extra.output_compressed = extra.chain_to != "ffpfs"
        extra.compression_level = getattr(tpl, "compression_level", None)
        if getattr(tpl, "auto_organize", None) is not None:
            extra.auto_organize = tpl.auto_organize
        if extra.chain_to == "pkg":
            self._apply_fpkg_params(extra, self._fpkg_compression_of(tpl))

    def _fpkg_compression_of(self, item) -> dict:
        """Identity is never shared, each game's param.json gives it at build time."""
        p = self._fpkg_params_of(item)
        return {k: p[k] for k in ("inner", "backend", "level", "dll",
                                  "retail_normalize", "hdr_flag", "regen_playgo", "fake_sign")}

    def _apply_fpkg_params(self, item, params: dict) -> None:
        item.fpkg_content_id     = str(params.get("content_id", "") or "").strip().upper()
        item.fpkg_title_id       = str(params.get("title_id", "") or "").strip().upper()
        item.fpkg_title          = str(params.get("title", "") or "").strip()
        item.fpkg_version        = str(params.get("version", "") or "").strip() or "01.000.000"
        item.fpkg_inner_mode     = params.get("inner") if params.get("inner") in ("none", "zlib", "kraken") else "kraken"
        item.fpkg_kraken_backend = params.get("backend") if params.get("backend") in ("builtin", "publishingtools") else "builtin"
        _lvl = params.get("level")
        try:
            item.fpkg_level = max(-4, min(9, int(_lvl) if _lvl is not None else 7))
        except Exception:
            item.fpkg_level = 7
        item.fpkg_retail_normalize = bool(params.get("retail_normalize", True))
        item.fpkg_hdr_flag         = _hdr_mode(params.get("hdr_flag", "auto"))
        item.fpkg_regen_playgo     = bool(params.get("regen_playgo", False))
        item.fpkg_fake_sign        = bool(params.get("fake_sign", True))
        bt = params.get("backport_target")
        item.backport_target = bt if is_backport_target(bt) else None
        blr = str(params.get("backport_libs", "") or "").strip()
        item.backport_libs_root = blr or None
        dll = str(params.get("dll", "") or "").strip()
        if item.fpkg_kraken_backend == "publishingtools" and sys.platform != "win32":
            # LibProsperoPkg hard-fails off 64-bit Windows; old jobs fall back to built-in
            try:
                self.log("WARN", f"{getattr(item, 'display_name', None) or item.name}: the Publishing Tools backend "
                                 f"is Windows-only - building with the built-in Kraken encoder instead.")
            except Exception:
                pass
            item.fpkg_kraken_backend = "builtin"
        if item.fpkg_kraken_backend == "publishingtools" and not dll:
            dll = (self.pubtools_dll_var.get() or "").strip()
        item.fpkg_pubtools_dll = dll if item.fpkg_kraken_backend == "publishingtools" else ""
        if item.fpkg_title_id and not self._TITLE_ID_RE.match(str(getattr(item, "title_id", "") or "")):
            item.title_id = item.fpkg_title_id

    def _as_fpkg_job(self, item, params: dict, identity: dict | None = None):
        """In place, keeps size, files, artwork and bundle tags; identity beats params."""
        p = dict(params)
        if identity:
            p.update({k: v for k, v in identity.items() if v})
        item.operation = "fpkg-build"
        item.output_compressed = True          # not meaningful for .pkg; marks the snapshot as taken
        self._apply_fpkg_params(item, p)
        # the fPKG builder has no patch overlay, so warn and drop the patch
        ps = getattr(item, "patch_source", None)
        if ps:
            try:
                self.log("WARN", f"{getattr(item, 'display_name', None) or item.name}: the detected patch "
                                 f"'{Path(str(ps)).name}' is NOT integrated into an fPKG build - the .pkg is built "
                                 f"from the base game only. Patch it separately.")
            except Exception:
                pass
            item.patch_source = None
        # .ffpfsc unwraps to ~1.7x its size (ratio ~0.59); reserve 2x
        try:
            _p = Path(str(getattr(item, "path", "") or ""))
            if _p.is_file() and _p.suffix.lower() == ".ffpfsc":
                item.extracted_size = int((getattr(item, "size", 0) or 0) * 2)
        except Exception:
            pass
        return item

    def _take_pending_fpkg_identity(self, item) -> dict | None:
        """Goes to exactly the item made from that folder (by resolved path), then dropped."""
        pend = getattr(self, "_pending_fpkg_identity", None)
        if not pend:
            return None
        target, ident = pend
        try:
            same = Path(str(getattr(item, "path", "") or "")).resolve() == Path(str(target)).resolve()
        except Exception:
            same = False
        if not same:
            return None
        self._pending_fpkg_identity = None
        return ident

    def _fpkg_item_for(self, src: Path, params: dict, *, output_path=None, parent=None):
        """None, after an error dialog, for anything it can't build from."""
        src = Path(src)
        suf = src.suffix.lower() if src.is_file() else ""
        try:
            if src.is_file() and suf in (".zip", ".rar", ".7z"):
                base = GameItem.from_archive(src)
            elif src.is_file() and suf in (".ffpfsc", ".ffpfs", ".exfat", ".ffpkg"):
                base = GameItem.from_exfat(src)  # the backend unwraps it
            elif src.is_dir():
                base = GameItem(src)
            else:
                base = None
        except Exception as e:
            messagebox.showerror("Build failed", tr("Could not classify the source:\n{e}", e=e),
                                 parent=parent or self.root)
            return None
        if base is None:
            messagebox.showerror("Unsupported source",
                                 tr("Not a valid fPKG source:\n{src}\n\nUse a game folder, an archive, or a .ffpfsc / .ffpfs / "
                                   ".exfat / .ffpkg image.", src=src),
                                 parent=parent or self.root)
            return None
        self._as_fpkg_job(base, params)
        base.output_path = Path(output_path) if output_path else None
        return base

    def _copy_item_for(self, src, *, output_path=None, mode="organize",
                       auto_organize=None, parent=None):
        """The source already has the target format, so the job just copies."""
        src = Path(src)
        if not (src.is_file() and src.suffix.lower() in (".ffpfsc", ".ffpfs", ".pkg")):
            messagebox.showerror("Wrong type",
                                 tr("Copy needs a .ffpfsc, .ffpfs or .pkg file:\n{src}", src=src),
                                 parent=parent or self.root)
            return None
        try:
            base = GameItem.from_exfat(src)
        except Exception as e:
            messagebox.showerror("Copy failed", tr("Could not classify the source:\n{e}", e=e),
                                 parent=parent or self.root)
            return None
        base.operation = "copy"
        base.copy_mode = mode
        base.output_compressed = (src.suffix.lower() == ".ffpfsc")   # marks the snapshot as taken
        base.output_path = Path(output_path) if output_path else None
        if auto_organize is not None:
            base.auto_organize = bool(auto_organize)
        base.name = src.stem
        return base

    @staticmethod
    def _ps4_copy_mode(item) -> str:
        """an archive's extraction is moved; own packages are copied unless After-job is Delete"""
        if getattr(item, "origin_archive", None) or getattr(item, "_from_archive", False):
            return "move"
        return "move" if (getattr(item, "after_source", None) or "keep") == "delete" else "keep"

    @staticmethod
    def _is_archive_path(p: Path) -> bool:
        n = p.name.lower()
        return p.is_file() and (p.suffix.lower() in (".zip", ".rar", ".7z") or bool(re.search(r"\.r\d{2,}$", n)))

    def _organize_item_for(self, src, *, output_path=None):
        """copy job that sorts src into the library as '<Title> [ID] [vX]'. Tk-free"""
        item = GameItem.from_chain(Path(src), to="ffpfsc", output_path=output_path)
        item.operation = "copy"
        item.content_kind = ORGANIZE_TARGET
        item.chain_to = ORGANIZE_TARGET
        item.copy_mode = "move" if getattr(item, "archive_path", None) else "keep"
        return item

    def _pkg_role(self, path) -> str:
        """'addon' (update or add-on), 'game' or 'unknown'. reads the headers only"""
        path = Path(path)
        _bd = str(backend_base_dir())
        if _bd not in sys.path:
            sys.path.insert(0, _bd)
        import organize_lib
        # its own name or its folder's says update, patch or backport
        if organize_lib._name_role(path.stem) or any(organize_lib._name_role(d.name) for d in path.parents[:2]):
            return "addon"
        role = package_role(path)
        if not role and _ps4pkg_module().is_ps4_package(path):
            try:  # PS4: game or update by its SFO
                kind = _ps4pkg_module().read_identity(path).kind
                role = "game" if kind == "game" else "addon" if kind in ("update", "dlc") else ""
            except Exception:
                role = ""
        return role or "unknown"

    def _release_planner(self, root, passwords):
        """one game per download folder takes the rest of that folder along. add worker"""
        root = Path(root)

        def plan(sources):
            return plan_releases(root, sources, passwords=passwords, pkg_role=self._pkg_role, log_fn=self.log)
        return plan

    @staticmethod
    def _make_organize(item):
        item.operation = "copy"
        item.content_kind = ORGANIZE_TARGET
        item.chain_to = ORGANIZE_TARGET
        item.copy_mode = "move" if getattr(item, "archive_path", None) else "keep"
        return item

    RECENT_OUTPUTS = 8

    @staticmethod
    def _recent_outputs(platform: str) -> list[str]:
        try:
            rec = load_settings().get("recent_outputs") or {}
            return [str(x) for x in rec.get(platform) or [] if x][:App.RECENT_OUTPUTS]
        except Exception:
            return []

    @staticmethod
    def _remember_output(platform: str, folder: str) -> None:
        folder = (folder or "").strip()
        if not folder:
            return
        try:
            rec = dict(load_settings().get("recent_outputs") or {})
            rec[platform] = ([folder] + [x for x in rec.get(platform) or [] if x != folder])[:App.RECENT_OUTPUTS]
            save_settings({"recent_outputs": rec})
        except Exception:
            pass

    def _ps4_route(self, src, output_path=None, passwords=None, to: str = "pkg"):
        """the PS4 job for src, or None when it is not PS4 (the dialog's job applies)"""
        src = Path(src)
        if self._is_archive_path(src):
            info = ArchiveExtractor.ps4_archive_info(src, passwords)
            return self._ps4_archive_item(src, info, output_path=output_path) if info is not None else None
        if not _is_ps4_source(src):
            return None
        if to == "folder" and src.is_file():
            dest = Path(output_path or src.parent) / f"{sanitize_filename(src.stem)} [extracted]"
            return GameItem.from_fpkg_extract(src, output_path=str(dest))
        return self._ps4_item_for(src, output_path=output_path)

    def _auto_item_for(self, src, table, build, *, output_path=None, passwords=None):
        """Automatic output: Settings picks it by source kind, PS4 packages always go to the library"""
        src = Path(src)
        content = ""
        if self._is_archive_path(src):
            info = ArchiveExtractor.ps4_archive_info(src, passwords)
            if info is not None:
                return self._ps4_archive_item(src, info, output_path=output_path)
            content = ArchiveExtractor.content_kind(src, passwords)
            kind = content or "folder"
        elif _is_ps4_source(src):
            return self._ps4_item_for(src, output_path=output_path)
        elif src.is_dir():
            kind = "folder"
        else:
            kind = chain_source_kind(types.SimpleNamespace(path=src))
        to = auto_target(kind, table)
        it = build(src, "ffpfsc" if to == ORGANIZE_TARGET else to)
        if to == ORGANIZE_TARGET:
            self._make_organize(it)
        it.auto_output = True
        if content:
            it.archive_content = content
        return it

    @staticmethod
    def _content_of_payload(kind: str, paths) -> str:
        if kind == "ps4":
            return ""
        return {"game": "folder", "pkg": "pkg"}.get(kind) or (Path(paths[0]).suffix.lower().lstrip(".") if paths else "")

    def _auto_resolve_unpacked(self, item, kind: str, paths) -> None:
        """what came out of the archive wins, names read at add time can lie. extract worker"""
        content = self._content_of_payload(kind, paths)
        if not content:
            return
        item.archive_content = content
        to = auto_target(content, (load_settings() or {}).get("auto_actions"))
        if to == ORGANIZE_TARGET:
            if getattr(item, "content_kind", "") != ORGANIZE_TARGET:
                self._make_organize(item)
                self.log("INFO", tr("{name} holds {what}: Auto organizes it into the library",
                                    name=Path(str(item.archive_path or item.path)).name, what=source_label(item)))
            return
        if getattr(item, "content_kind", "") == ORGANIZE_TARGET:
            item.content_kind, item.copy_mode = "", None
        before = getattr(item, "chain_to", None)
        if before != to:
            self.log("INFO", tr("{name} holds {what}: Auto builds {to}",
                                name=Path(str(item.archive_path or item.path)).name, what=source_label(item),
                                to=CHAIN_TARGET_LABEL.get(to, to)))
        item.operation, item.chain_to = "chain", to
        item.chain_sign = bool(getattr(item, "auto_sign", item.chain_sign)) and to != "pkg"
        item.chain_lz4 = (bool(getattr(item, "auto_lz4", item.chain_lz4)) and to in ultra_core.LZ4_TARGETS
                          and not getattr(item, "chain_lz4_unpack", False))
        item.lz4_profile = getattr(item, "auto_lz4_profile", item.lz4_profile) if item.chain_lz4 else None
        if to == "pkg":
            if before != "pkg":
                self._apply_fpkg_params(item, self.fpkg_defaults)
        elif to != "ffpfsc":
            item.compression_level = None

    def _ps4_archive_item(self, archive, info: dict, *, output_path=None):
        item = GameItem.from_archive(Path(archive))
        item.operation = "copy"
        item.content_kind = "ps4"
        item.copy_mode = "move"
        item.output_path = Path(output_path) if output_path else None
        item.ps4_count = len(info.get("packages") or [])
        ident = info.get("ident")
        if ident is not None:
            item.archive_title, item.archive_title_id = ident.title, ident.title_id
            item.archive_version = ident.version
            self._take_game_name(item, {"title": ident.title, "title_id": ident.title_id})
        return item

    @staticmethod
    def _ps4_keystone_state(path) -> dict | None:
        """{'fingerprint', 'default', 'valid'} or None when unreadable. about 0.1 s"""
        try:
            _bd = str(backend_base_dir())
            if _bd not in sys.path:
                sys.path.insert(0, _bd)
            import fpkg as _fpkg
            return _fpkg.ps4_keystone_info(Path(path))
        except Exception:
            return None

    @staticmethod
    def _ps4_default_keystone(path) -> bool | None:
        """default keystone (all-zero passcode) or a damaged one: retail saves show as corrupted"""
        got = App._ps4_keystone_state(path)
        return None if got is None else bool(got["default"] or not got.get("valid", True))

    @staticmethod
    def _ps4_version_problem(path) -> dict | None:
        """update merged in but the app version stayed the game's: the console offers the update"""
        try:
            return _ps4pkg_module().version_problem(Path(path))
        except Exception:
            return None

    def _ps4_item_for(self, src, *, output_path=None, info=None):
        src = Path(src)
        if self._is_archive_path(src):
            info = info or ArchiveExtractor.ps4_archive_info(src, self._candidate_passwords()) or {"packages": []}
            return self._ps4_archive_item(src, info, output_path=output_path)
        item = GameItem.from_exfat(src) if src.is_file() else GameItem(src)
        item.operation = "copy"
        item.content_kind = "ps4"
        item.copy_mode = "keep"            # the command decides, see _ps4_copy_mode
        item.output_path = Path(output_path) if output_path else None
        m = _ps4pkg_module()
        pkgs = [src] if src.is_file() else sorted(x for x in src.rglob("*.pkg")
                                                 if x.is_file() and not is_fs_junk_name(x.name))
        idents = []
        for x in pkgs:
            try:
                idents.append(m.read_identity(x))
            except Exception:
                pass
        main = next((i for i in idents if i.kind == "game"), None) or (idents[0] if idents else None)
        if main:
            item.title_id = main.title_id
            item.display_name = main.title or src.stem
        item.ps4_count = len(pkgs)
        if src.is_file() and main is not None and main.kind == "game":
            item.ps4_default_keystone = self._ps4_default_keystone(src)
            vp = self._ps4_version_problem(src)
            item.ps4_version_problem = bool(vp and vp["named"])
        return item

    # auto-organize
    def _auto_organize_on(self, item) -> bool:
        v = getattr(item, "auto_organize", None)
        return bool(self.auto_organize_var.get()) if v is None else bool(v)

    def _game_identity(self, item) -> dict | None:
        """title, title id and version from the source's param.json, nothing gets unpacked"""
        cached = getattr(item, "_identity", None)
        if cached:
            return cached
        if getattr(item, "archive_path", None) and not getattr(item, "path", None):
            if int(getattr(item, "archive_games", 0) or 0) > 1:
                return None  # one game of several: known after the unpack
            return self._archive_identity(item)
        p = getattr(item, "path", None)
        if not p:
            return None
        p = Path(str(p))
        # remember the miss by path+mtime, else every preview refresh spawns a backend on the UI thread
        try:
            _miss_key = (str(p), p.stat().st_mtime_ns)
        except OSError:
            _miss_key = None
        if _miss_key is not None and getattr(item, "_identity_miss", None) == _miss_key:
            return None
        ident = None
        try:
            if p.is_dir():
                pj = p / "sce_sys" / "param.json"
                has_pj = pj.is_file()
                title = guess_game_name(p) if has_pj else ""
                tid = parse_title_id(p, [pj] if has_pj else None)
                tid = "" if tid in ("Unknown", "") else tid
                ver = guess_game_version(p)
                if title or tid:
                    ident = {"title": title, "title_id": tid, "version": ver}
            elif p.is_file() and p.suffix.lower() in (".ffpfs", ".ffpfsc", ".exfat", ".ffpkg", ".pkg"):
                ident = self._read_image_metadata(p)
        except Exception as e:
            self.log("WARN", f"Auto-organize: could not read the game's metadata from {p.name}: {e}")
            ident = None
        if ident and "fw" not in ident:
            ident["fw"] = self._source_fw(p)
        if ident:
            if not ident.get("title_id"):
                _t = parse_title_id(p)
                ident["title_id"] = "" if _t in ("Unknown", "") else _t
            if not ident.get("version"):
                _m = re.search(r"\bv?(\d{1,2}\.\d{2,3}(?:\.\d{2,3})?)\b", p.name)
                ident["version"] = _m.group(1) if _m else ""
            try:
                item._identity = ident
            except Exception:
                pass
        elif _miss_key is not None:
            try:
                item._identity_miss = _miss_key
            except Exception:
                pass
        return ident

    def _archive_identity(self, item, passwords=None) -> dict | None:
        """param.json read out of the archive alone (ZIP, RAR, 7z that is not solid)"""
        if getattr(item, "archive_title", "") or getattr(item, "archive_title_id", ""):
            return {"title": item.archive_title, "title_id": item.archive_title_id,
                    "version": getattr(item, "archive_version", ""), "fw": ""}
        arc = Path(str(item.archive_path))
        try:
            key = (str(arc), arc.stat().st_mtime_ns)
        except OSError:
            return None
        if getattr(item, "_archive_ident_miss", None) == key:
            return None
        if passwords is None:
            passwords = self._candidate_passwords(item)
        ident = None
        try:
            data = ArchiveExtractor.read_game_param(arc, passwords)
            ident = ident_from_param_bytes(data) if data else None
        except Exception:
            ident = None
        if not ident:
            item._archive_ident_miss = key
            return None
        item.archive_title, item.archive_title_id = ident["title"], ident["title_id"]
        item.archive_version = ident.get("version", "")
        return dict(ident, fw="")

    def _name_jobs_from_games(self) -> None:
        todo = [it for it in self.queue if not getattr(it, "_name_probed", False)]
        if not todo:
            return
        for it in todo:
            it._name_probed = True
        pw = {id(it): self._candidate_passwords(it) for it in todo
              if getattr(it, "archive_path", None) and not getattr(it, "path", None)}
        online = bool(self.online_names_var.get())          # a Tk variable: read it here

        def work():
            changed = False
            for it in todo:
                try:
                    if id(it) in pw:
                        ident = self._archive_identity(it, pw[id(it)])
                        if ident is None:
                            info = ArchiveExtractor.ps4_archive_info(Path(str(it.archive_path)), pw[id(it)])
                            i4 = info.get("ident") if info else None
                            ident = ({"title": i4.title, "title_id": i4.title_id} if i4 is not None
                                     else self._listed_title(it, online) or ident_from_folder_name(it.archive_path))
                    else:
                        ident = self._game_identity(it)
                except Exception:
                    ident = None
                if self._take_game_name(it, ident):
                    changed = True
                try:
                    if self._fetch_art(it, pw.get(id(it))):
                        changed = True
                except Exception:
                    pass
            if changed:
                self._names_dirty = True
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _listed_title(item, enabled: bool) -> dict | None:
        arc = Path(str(getattr(item, "archive_path", "") or ""))
        if not enabled or not str(getattr(item, "archive_path", "") or "").strip():
            return None
        try:
            tdb = _title_db_module()
            tid = tdb.title_id_in(arc.name, arc.parent.name)
            name = tdb.lookup(tid, Path(APP_DIR) / "titles") if tid else None
        except Exception:
            return None
        return {"title": name, "title_id": tid} if name else None

    def _job_art(self, item):
        """cached copy first, else the folder's icon, cached so it outlives the folder"""
        f = art_cache_file(getattr(item, "art_key", "") or "") or art_cache_file(art_source_key(item))
        if f is not None:
            return f
        art = getattr(item, "artwork", None)
        if art and Path(str(art)).is_file():
            key = art_source_key(item)
            if store_art(key, art):
                item.art_key = key
                return art_cache_file(key)
            return art
        return None

    def _fetch_art(self, item, passwords=None) -> bool:
        """off the main thread. decodes only icon0.png, never the whole source"""
        key = art_source_key(item)
        if not key or art_cache_file(getattr(item, "art_key", "") or "") or art_cache_file(key):
            if art_cache_file(key) and not getattr(item, "art_key", ""):
                item.art_key = key
            return False
        data = None
        arc = getattr(item, "archive_path", None)
        p = Path(str(getattr(item, "path", "") or "")) if getattr(item, "path", None) else None
        if arc and p is None:
            data = ArchiveExtractor.read_shallowest(Path(str(arc)), "sce_sys/icon0.png", passwords or [])
            if data is None:
                info = ArchiveExtractor.ps4_archive_info(Path(str(arc)), passwords or [])
                if info and info.get("packages"):
                    data = self._ps4_icon_from_archive(Path(str(arc)), info["packages"][0], passwords or [])
        elif p is not None and p.is_dir():
            data = find_artwork(p)
        elif p is not None and p.is_file():
            data = self._icon_of_container(p)
        if data and store_art(key, data):
            item.art_key = key
            return True
        return False

    def _icon_of_container(self, p: Path):
        suf = p.suffix.lower()
        if suf == ".pkg" and _is_ps4_source(p):
            return _ps4pkg_module().read_icon(p)
        if suf not in (".ffpfs", ".ffpfsc", ".pkg"):
            return None
        tmp = Path(tempfile.mkdtemp(prefix="ffpfsc_icon_"))
        try:
            mfile = tmp / "members.txt"
            mfile.write_text("sce_sys/icon0.png\n", encoding="utf-8")
            dest = tmp / "out"
            if suf == ".pkg":
                backend = backend_base_dir()
                if str(backend) not in sys.path:
                    sys.path.insert(0, str(backend))
                import fpkg as _fpkg
                _fpkg.extract_members(p, dest, mfile, on_line=lambda _l: None)
            else:
                subprocess.run(self._backend_cmd("--extract-from", str(p), "--dest", str(dest),
                                                 "--members-file", str(mfile)),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            f = dest / "sce_sys" / "icon0.png"
            return f.read_bytes() if f.is_file() else None
        except Exception:
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _ps4_icon_from_archive(archive: Path, member: str, passwords):
        m = _ps4pkg_module()
        try:
            head = ArchiveExtractor.read_member_prefix(archive, member, 0x1000, passwords) or b""
            h = m.header_from_bytes(head)
            need = h.entry_table_offset + 32 * h.entry_count
            if not 0 < h.entry_count < 10000 or need > 32 << 20:
                return None
            table = ArchiveExtractor.read_member_prefix(archive, member, need, passwords) or b""
            import struct as _st
            for i in range(h.entry_count):
                eid, _fn, _f1, _f2, off, size = _st.unpack_from(">IIIIII", table, h.entry_table_offset + i * 32)
                if eid == m.ENTRY_ICON0_PNG and off + size <= 32 << 20:
                    data = ArchiveExtractor.read_member_prefix(archive, member, off + size, passwords) or b""
                    return data[off:off + size] if len(data) >= off + size else None
        except Exception:
            return None
        return None

    @staticmethod
    def _take_game_name(item, ident) -> bool:
        """label becomes 'Title [PPSA01234]', trademark signs dropped. True when it changed"""
        title = canonical_game_title((ident or {}).get("title") or "") if ident else ""
        tid = str((ident or {}).get("title_id") or "").strip().upper() if ident else ""
        if not title:
            return False
        label = f"{title} [{tid}]" if tid and tid.lower() not in title.lower() else title
        if getattr(item, "display_name", None) == label:
            return False
        item.display_name = label
        return True

    @staticmethod
    def _ident_from_param_json(pj: Path) -> dict | None:
        try:
            return ident_from_param_bytes(Path(pj).read_bytes())
        except OSError:
            return None

    def _extract_image_member(self, image: Path, member: str, dest: Path) -> Path | None:
        mfile = Path(dest) / "members.txt"
        mfile.write_text(member + "\n", encoding="utf-8")
        out = Path(dest) / "out"
        cmd = self._backend_cmd("--extract-from", str(image), "--dest", str(out), "--members-file", str(mfile))
        subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        got = out / member
        return got if got.is_file() else None

    def _read_image_metadata(self, p: Path) -> dict | None:
        suf = p.suffix.lower()
        if suf in (".ffpfs", ".ffpfsc"):
            tmp = Path(tempfile.mkdtemp(prefix="ffpfsc_ident_"))
            try:
                mfile = tmp / "members.txt"
                mfile.write_text("sce_sys/param.json\n", encoding="utf-8")
                dest = tmp / "out"
                cmd = self._backend_cmd("--extract-from", str(p), "--dest", str(dest), "--members-file", str(mfile))
                subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
                pj = dest / "sce_sys" / "param.json"
                return self._ident_from_param_json(pj) if pj.is_file() else None
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if suf == ".pkg" and _is_ps4_source(p):
            return self._ps4_identity(p)
        if suf == ".pkg":
            tmp = Path(tempfile.mkdtemp(prefix="ffpfsc_pkg_ident_"))
            try:
                mfile = tmp / "members.txt"
                mfile.write_text("sce_sys/param.json\n", encoding="utf-8")
                dest = tmp / "out"
                # through the backend's fpkg so a frozen build finds the tool
                backend = backend_base_dir()
                if str(backend) not in sys.path:
                    sys.path.insert(0, str(backend))
                try:
                    import fpkg as _fpkg
                    _fpkg.extract_members(p, dest, mfile, on_line=lambda _l: None)
                except Exception as e:
                    self.log("WARN", f"Auto-organize: could not read param.json out of {p.name}: {e}")
                    return None
                pj = dest / "sce_sys" / "param.json"
                return self._ident_from_param_json(pj) if pj.is_file() else None
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        backend = backend_base_dir()
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        from mkpfs.game_metadata import read_game_metadata   # reads only the metadata file
        m = read_game_metadata(p)
        title = param_title(getattr(m, "param", None) or {}) or (getattr(m, "game_title", "") or "").strip()
        tid = (getattr(m, "title_id", "") or "").strip()
        ver = (getattr(m, "version", "") or "").strip()
        if tid in ("-", " - ", "Unknown"):
            tid = ""
        if not (title or tid):
            return None
        return {"title": title, "title_id": tid.upper(), "version": ver}

    def _ps4_identity(self, path):
        try:
            i = _ps4pkg_module().read_identity(path)
            return {"title": i.title, "title_id": i.title_id, "version": i.version}
        except Exception:
            return None

    def _source_fw(self, p: Path) -> str:
        """firmware from eboot.bin's SDK version, '' when unreadable (encrypted eboot, disk image)"""
        try:
            if _is_ps4_source(p):
                return ""                       # PS5 firmware tags do not apply to PS4 content
            if p.is_dir():
                bp = _backport_module()
                words = bp.sdk_words_of_file(p / "eboot.bin")
                return bp.sdk_firmware(words[0]) if words else ""
            if p.suffix.lower() in (".ffpfs", ".ffpfsc", ".pkg"):
                r = subprocess.run(self._backend_cmd("--sdk-of", str(p)), capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=120)
                for line in (r.stdout or "").splitlines():
                    if line.startswith("SDK_JSON: "):
                        return str(json.loads(line[len("SDK_JSON: "):]).get("fw") or "")
        except Exception:
            pass
        return ""

    @staticmethod
    def _param_bytes_of(path: Path, passwords) -> bytes | None:
        try:
            if path.is_dir():
                hits = sorted(path.rglob("sce_sys/param.json"), key=lambda q: len(q.parts))
                return hits[0].read_bytes() if hits else None
            if path.suffix.lower() in (".zip", ".rar") or re.match(r"^\.r\d{2,}$", path.suffix.lower()):
                return ArchiveExtractor.read_game_param(path, passwords)
        except Exception:
            return None
        return None

    def _patch_fit(self, source: Path, patch: Path) -> tuple[str, str]:
        """other title id: refuse. backport built for another version: warn. backend checks again"""
        pw = [p.strip() for p in (load_settings().get("archive_passwords") or []) if str(p).strip()]
        try:
            g = json.loads(self._param_bytes_of(source, pw) or b"{}")
            pt = json.loads(self._param_bytes_of(patch, pw) or b"{}")
        except Exception:
            return "ok", ""
        gtid, ptid = str(g.get("titleId") or "").upper(), str(pt.get("titleId") or "").upper()
        if gtid and ptid and gtid != ptid:
            return "refuse", tr("This patch is for {ptid}, but the game is {gtid}.", ptid=ptid, gtid=gtid)

        def num(v):
            try:
                t = str(v).strip()
                return int(t, 16) if t.lower().startswith("0x") else int(t)
            except Exception:
                return None
        gsdk, psdk = num(g.get("sdkVersion")), num(pt.get("sdkVersion"))
        gver, pver = str(g.get("contentVersion") or ""), str(pt.get("contentVersion") or "")
        if gsdk and psdk and psdk < gsdk and gver and pver and gver != pver:
            return "warn", (tr("This patch is a backport made for version {pver}, but the game is version {gver}. A "
                              "backport usually works only with the version it was made for.", pver=pver, gver=gver))
        return "ok", ""

    def _patch_fw(self, patch: Path) -> str | None:
        """None: no eboot.bin in the patch (game keeps its own). '': one we can't read"""
        bp = _backport_module()
        try:
            if patch.is_dir():
                hits = sorted(patch.rglob("eboot.bin"), key=lambda q: len(q.parts))
                if not hits:
                    return None
                words = bp.sdk_words_of_file(hits[0])
                return bp.sdk_firmware(words[0]) if words else ""
            if patch.suffix.lower() == ".zip":
                with zipfile.ZipFile(patch) as z:
                    names = sorted((n for n in z.namelist() if n.rsplit("/", 1)[-1] == "eboot.bin"),
                                   key=lambda n: n.count("/"))
                    if not names:
                        return None
                    info = z.getinfo(names[0])
                    with z.open(info) as f:
                        def read(offset, n, f=f):
                            f.seek(offset)
                            return f.read(n)
                        words = bp.sdk_words_from_reader(read, info.file_size)
                    return bp.sdk_firmware(words[0]) if words else ""
            if patch.suffix.lower() == ".rar" or re.match(r"^\.r\d{2,}$", patch.suffix.lower()):
                cache = self.__dict__.setdefault("_patch_fw_cache", {})
                key = (str(patch), patch.stat().st_mtime_ns)
                if key not in cache:
                    pw = [p.strip() for p in (load_settings().get("archive_passwords") or []) if str(p).strip()]
                    names = ArchiveExtractor.list_members(patch, pw)
                    if not any(n.rsplit("/", 1)[-1] == "eboot.bin" for n in names):
                        cache[key] = None
                    else:
                        data = ArchiveExtractor.read_shallowest(patch, "eboot.bin", pw)
                        words = bp.sdk_words_from_reader(lambda o, n, d=data: d[o:o + n], len(data)) if data else None
                        cache[key] = bp.sdk_firmware(words[0]) if words else ""
                return cache[key]
        except Exception:
            return ""
        return ""                              # .7z: not read before the build

    def _job_fw(self, item, source_fw: str) -> str:
        """firmware the OUTPUT needs, '' when unknown. never a guess"""
        bp = _backport_module()
        fw = source_fw or ""
        patch = getattr(item, "patch_source", None)
        if patch:
            pfw = self._patch_fw(Path(str(patch)))
            if pfw is not None:
                fw = pfw
        bt = getattr(item, "backport_target", None)
        if fw and is_backport_target(bt):
            target = bp.sdk_firmware(bp.SDK_TARGETS["10.xx"][0]) if bt == "10.xx" else bt
            try:
                if bp._fw_key(target) < bp._fw_key(fw):
                    fw = target
            except ValueError:
                pass
        return fw

    def _organized_layout(self, item, out: Path, ext: str):
        ident = self._game_identity(item)
        if not ident:
            if not getattr(item, "_organize_warned", False):
                try:
                    item._organize_warned = True
                except Exception:
                    pass
                self.log("WARN", f"Auto-organize: no param.json readable for {getattr(item, 'display_name', None) or item.name}"
                                 f" - naming from the source name instead.")
            return None, None
        ident = dict(ident, fw=self._job_fw(item, ident.get("fw", "")))
        folder, fname = organized_names(ident, ext, item)
        return out / (getattr(item, "organize_folder", None) or folder), fname

    _FW_TAG = re.compile(r"\s*\[fw[\d.x]+\]$", re.I)
    OUTPUT_EXISTS_CHOICES = ("skip", "ask", "overwrite", "keep")
    OUTPUT_EXISTS_LABELS = {"skip": "Skip", "ask": "Ask", "overwrite": "Overwrite", "keep": "Keep both"}

    def _predicted_output(self, item) -> Path | None:
        """where the job will write, or None when we can't know yet"""
        if getattr(item, "content_kind", "") in ("ps4", ORGANIZE_TARGET):
            return None
        op = getattr(item, "operation", "pack")
        if op == "fpkg-extract":
            _od = self._job_output_dir(item)
            if not _od:
                return None
            folder = self._fpkg_extract_folder(item, _od)
            return folder if self._folder_taken(folder) else None
        if op == "chain":
            to = getattr(item, "chain_to", None) or "ffpfsc"
            if to == "folder":
                src = Path(str(getattr(item, "path", "") or ""))
                _od = self._job_output_dir(item)
                if _od and src.is_dir() and (getattr(item, "chain_lz4", False) or getattr(item, "chain_lz4_unpack", False)):
                    folder = _od / f"{sanitize_filename(src.name)} {'[LZ4]' if getattr(item, 'chain_lz4', False) else '[unpacked]'}"
                    return folder if self._folder_taken(folder) else None
                if not _od or not src.is_file():
                    return None                   # a folder changed in place writes nothing new
                folder = _od if _od.name.endswith(" [extracted]") else _od / f"{sanitize_filename(src.stem)} [extracted]"
                return folder if self._folder_taken(folder) else None
        elif op == "pack":
            try:
                _ip = Path(getattr(item, "path", "") or "")
                pfs_family = _ip.is_dir() or _ip.suffix.lower() == ".ffpfs"
            except Exception:
                pfs_family = False
            comp = getattr(item, "output_compressed", None)
            if comp is None:
                comp = self.output_compressed_var.get()
            to = "ffpfs" if (pfs_family and not comp) else "ffpfsc"
        elif op == "fpkg-build":
            to = "pkg"
        else:
            return None
        if to not in ("ffpfs", "ffpfsc", "pkg", "exfat"):
            return None
        out = self._job_output_dir(item)
        if out is None:
            return None
        if out.suffix.lower() in (".ffpfs", ".ffpfsc", ".pkg", ".exfat"):
            return out
        ext = "." + to
        unread_archive = bool(getattr(item, "archive_path", None)) and not getattr(item, "path", None)
        sub = getattr(item, "bundle_subfolder", None)
        if self._auto_organize_on(item) and self._game_identity(item):
            base, name = self._organized_layout(item, out, ext)
            if base is not None:
                return base / name
        if unread_archive or to == "pkg":
            return None
        base = self._mirror_base(item, out, sub) if sub else out
        try:
            return base / descriptive_ffpfsc_name(item, ext=ext)
        except Exception:
            return None

    def _backport_ceiling(self, item) -> str | None:
        bt = getattr(item, "backport_target", None) if item is not None else None
        if not is_backport_target(bt):
            return None
        try:
            bp = _backport_module()
            return bp.sdk_firmware(bp.SDK_TARGETS["10.xx"][0]) if bt == "10.xx" else str(bt)
        except Exception:
            return None

    @staticmethod
    def _fpkg_extract_folder(item, out: Path) -> Path:
        """always its own '<package> [extracted]' folder, never straight into a library folder"""
        out = Path(out)
        if not out.name.endswith(" [extracted]"):
            out = out / f"{sanitize_filename(Path(item.path).stem)} [extracted]"
        return out

    @staticmethod
    def _folder_taken(path: Path) -> bool:
        try:
            return Path(path).is_dir() and any(not is_fs_junk_name(q.name) for q in Path(path).iterdir())
        except OSError:
            return False

    @staticmethod
    def _is_own_source(item, path) -> bool:
        for src in (getattr(item, "path", None), getattr(item, "archive_path", None)):
            try:
                if src and path and Path(str(src)).is_file() and os.path.samefile(str(src), str(path)):
                    return True
            except OSError:
                pass
        return False

    def _existing_output(self, target: Path, item=None) -> Path | None:
        """a PS5 PKG Tools rebuild next to its own source is no conflict"""
        try:
            if item is not None and getattr(item, "ps5_repair", False) and self._is_own_source(item, target):
                return None
            if target.exists():
                return target
            parent = target.parent
            if not parent.is_dir():
                return None
            known_fw = bool(self._FW_TAG.search(target.stem))
            if not known_fw and getattr(item, "patch_source", None):
                return None          # a patch may change the firmware (a backport): never guess
            want = self._FW_TAG.sub("", target.stem)
            ceiling = None if known_fw else self._backport_ceiling(item)
            for f in parent.iterdir():
                if f.suffix.lower() != target.suffix.lower() or self._FW_TAG.sub("", f.stem) != want:
                    continue
                tag = re.search(r"\[fw([\d.x]+)\]$", f.stem, re.I)
                if known_fw and tag:
                    continue                     # another firmware: another build
                if ceiling and tag:
                    try:
                        bp = _backport_module()
                        if bp._fw_key(tag.group(1)) > bp._fw_key(ceiling):
                            continue             # the build before the backport
                    except ValueError:
                        pass
                return f
        except OSError:
            return None
        return None

    def _output_rule(self) -> str:
        try:
            v = self.output_exists_var.get()
        except Exception:
            v = "skip"
        return v if v in self.OUTPUT_EXISTS_CHOICES else "skip"

    def _check_existing_outputs(self) -> bool:
        """with Ask, one window lists the known conflicts and its answer rules this run. False on Cancel"""
        self._output_policy = None if self._output_rule() == "ask" else self._output_rule()
        conflicts = []
        for it in self.queue:
            if getattr(it, "status", "") in self._TERMINAL_STATUSES:
                continue
            it._replace_output = False
            it._keep_both = False
            it._output_checked = False
            if self._output_policy:
                continue
            try:
                target = self._predicted_output(it)
            except Exception:
                target = None
            hit = self._existing_output(target, it) if target is not None else None
            if hit is not None:
                conflicts.append((it, hit))
        if not conflicts:
            return True
        choice = self._ask_existing_outputs(conflicts)
        if choice == "cancel":
            self.log("INFO", "Start cancelled: outputs already there.")
            return False
        self._output_policy = choice
        return True

    def _ask_existing_outputs(self, conflicts) -> str:
        dlg = OutputExistsDialog(self.root, [(getattr(it, "display_name", None) or it.name, hit)
                                             for it, hit in conflicts])
        try:
            self.root.wait_window(dlg)
        except Exception:
            return "cancel"
        return dlg.choice or "cancel"

    def _apply_output_choice(self, item, hit: Path, choice: str) -> None:
        name = getattr(item, "display_name", None) or item.name
        if choice == "overwrite" and self._is_own_source(item, hit):
            choice = "keep"                      # never build over the file the job reads
        if choice == "overwrite":
            item._replace_output = True
            self.log("INFO", f"{name}: replaces {hit}.")
        elif choice == "keep":
            item._keep_both = True
            self.log("INFO", f"{name}: {hit.name} is already there; the new build gets a numbered name beside it.")


    def _late_output_check(self, item) -> str:
        """last look once the archive is unpacked: proceed, skip or cancel"""
        if (getattr(item, "_output_checked", False) or getattr(item, "_replace_output", False)
                or getattr(item, "_keep_both", False)):
            return "proceed"
        try:
            target = self._predicted_output(item)
        except Exception:
            target = None
        if target is None:
            return "proceed"  # asked again once the archive is unpacked
        item._output_checked = True
        hit = self._existing_output(target, item)
        if hit is None:
            return "proceed"
        rule = self._output_rule()
        choice = (getattr(self, "_output_policy", None) or (None if rule == "ask" else rule)
                  or self._ask_existing_outputs([(item, hit)]))
        if choice == "cancel":
            return "cancel"
        if not getattr(self, "_output_policy", None):
            self._output_policy = choice          # the rest of this batch follows it
        if choice in ("overwrite", "keep"):
            self._apply_output_choice(item, hit, choice)
            return "proceed"
        return "skip"

    @staticmethod
    def _free_output_name(path: Path) -> Path:
        """first free ' (2)', ' (3)' that still fits the ShadowMount name limit, title shortened if needed"""
        stem, ext = path.stem, path.suffix
        i = stem.find(" [")
        title, tags = (stem[:i], stem[i:]) if i > 0 else (stem, "")
        n = 2
        while n < 1000:
            t = title
            while len(f"{t} ({n}){tags}{ext}".encode("utf-8")) > SHADOWMOUNT_NAME_LIMIT and len(t) > 4:
                t = t[:-1].rstrip()
            cand = path.with_name(f"{t} ({n}){tags}{ext}")
            if not cand.exists():
                return cand
            n += 1
        return path

    def _keep_both_name(self, item, out: Path) -> Path:
        if getattr(item, "_keep_both", False) and out.suffix.lower() in (".ffpfs", ".ffpfsc") and out.exists():
            new = self._free_output_name(out)
            self.log("INFO", f"Keep both: {out.name} is already there, the new build is {new.name}.")
            return new
        return out

    def _skip_late(self, item, hit_note: str = "") -> None:
        """not counted as a failure, its scratch is reclaimed and the batch goes on"""
        self._cleanup_after_failure(item)
        hit, exact = None, False
        try:
            predicted = self._predicted_output(item)
            hit = self._existing_output(predicted, item) if predicted is not None else None
            exact = hit is not None and Path(hit) == Path(predicted)
        except Exception:
            pass

        def finish(done: bool):
            if self._batch_running:
                if done:
                    self._batch_done += 1
                else:
                    self._batch_total = max(0, self._batch_total - 1)
            self._skip_late_advance()
        self._settle_existing(item, hit, hit_note, then=finish, exact=exact)

    def _skip_late_advance(self) -> None:
        if self._batch_running:
            self._update_batch_counter()
            self.update_queue_box()
            if self._has_pending():
                self.root.after(600, self._batch_auto_start)
            else:
                self._batch_running = False
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self._update_batch_counter()
        else:
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self.update_queue_box()

    def _mirror_base(self, item, out: Path, sub: str) -> Path:
        """mirror the source folder name, unless that IS the source folder (output == its parent)"""
        target = out / sanitize_filename(sub)
        try:
            src_dir = getattr(item, "bundle_dir", None)
            if src_dir and target.resolve() == Path(str(src_dir)).resolve():
                if not getattr(item, "_mirror_warned", False):
                    item._mirror_warned = True
                    self.log("INFO", f"Output folder is the source's parent - not mirroring '{sub}' into the "
                                     f"source folder itself; writing to {out}.")
                return out
        except Exception:
            pass
        return target

    def _finalize_pkg_name(self, item, pkg_path: Path) -> Path:
        """the tool names it after the content id, give it the library name from build_command"""
        want = getattr(item, "_organized_pkg_name", None)
        pkg_path = Path(pkg_path)
        if not want or pkg_path.name == want:
            return pkg_path
        target = pkg_path.with_name(want)
        try:
            if target.exists() and getattr(item, "_replace_output", False):
                os.replace(pkg_path, target)
                self.log("INFO", f"Auto-organize: {want} replaced with the new build.")
                return target
            if target.exists():
                # never replace a file that is already there, keep both
                stem, ext = os.path.splitext(want)
                n = 2
                while target.exists():
                    target = pkg_path.with_name(f"{stem} ({n}){ext}")
                    n += 1
                self.log("WARN", f"Auto-organize: {want} already exists - keeping both, "
                                 f"the new build is named {target.name}")
            pkg_path.rename(target)
            self.log("INFO", f"Auto-organize: renamed {pkg_path.name} → {target.name}")
            return target
        except Exception as e:
            self.log("WARN", f"Auto-organize: could not rename {pkg_path.name} → {want}: {e}")
            return pkg_path

    # queue management
    def queue_move_up(self):
        idx = self._queue_sel_idx()
        if idx is None or idx == 0:
            return
        self.move_job(idx, idx - 1)

    def queue_move_down(self):
        idx = self._queue_sel_idx()
        if idx is None or idx >= len(self.queue) - 1:
            return
        self.move_job(idx, idx + 2)

    def move_job(self, src: int, dst: int) -> None:
        """src goes before the job now at dst (len = the end). the running job may move too"""
        if not (0 <= src < len(self.queue)):
            return
        dst = max(0, min(len(self.queue), dst))
        if dst in (src, src + 1):
            return
        item = self.queue.pop(src)
        self.queue.insert(dst - 1 if dst > src else dst, item)
        self.update_queue_box(select_item=item)

    def run_job_next(self, item) -> None:
        if item not in self.queue or getattr(item, "status", "") in self._TERMINAL_STATUSES:
            return
        running = self._running_item()
        first = next((i for i, it in enumerate(self.queue)
                      if it is not running and it is not item
                      and getattr(it, "status", "") not in self._TERMINAL_STATUSES), None)
        if first is not None and first < self.queue.index(item):
            self.move_job(self.queue.index(item), first)
            self.log("INFO", f"{getattr(item, 'display_name', None) or item.name} runs next.")

    def _marked_items(self) -> list:
        try:
            rows = self.queue_listbox.marked_rows()
        except Exception:
            return []
        return [self.queue[i] for i in rows if 0 <= i < len(self.queue)]

    def queue_remove_selected(self):
        """the running job stays. removing a job deletes the copy it kept from its archive"""
        if self._panels.stack:
            return  # Backspace in an open editor must not remove the job
        rows = [i for i in self.queue_listbox.marked_rows() if 0 <= i < len(self.queue)]
        if not rows or not self.queue:
            return
        items = [self.queue[i] for i in rows]
        running = self._running_item()
        if running is not None and running in items:
            if len(items) == 1:
                messagebox.showwarning("In Progress",
                                       "This job is running.\n"
                                       "Cancel it first to remove it.")
                return
            items = [it for it in items if it is not running]
            self.log("INFO", "The running job stays in the queue; cancel it first to remove it.")
        kept = [it for it in items if getattr(it, "kept_extract", False)]
        if kept:
            n = len(kept)
            text = (tr("This job keeps what it extracted from its archive, for a retry. Removing it deletes that copy.")
                    if len(items) == 1 else
                    plural(n, "1 of these jobs keeps what it extracted from its archive, for a retry. "
                              "Removing deletes that copy.",
                           "{n} of these jobs keep what they extracted from their archives, for a retry. "
                           "Removing deletes those copies."))
            if not ask_confirm("Remove jobs" if len(items) > 1 else "Remove the job?", text,
                               "Remove", danger=True):
                return
        after = [it for it in self.queue[rows[-1] + 1:] if it not in items]
        before = [it for it in self.queue[:rows[0]] if it not in items]
        next_item = after[0] if after else (before[-1] if before else None)
        for it in items:
            self._drop_kept_extract(it)
            self.queue.remove(it)
        if len(items) > 1:
            self.log("INFO", f"Removed {len(items)} jobs from the queue.")
        self.update_queue_box(select_item=next_item)

    def remove_first(self):
        if self.queue and not self._batch_running:
            self.queue.pop(0)
        self.update_queue_box()

    def clear_queue(self):
        """like Clear all, minus the kept-copy question for callers that already asked"""
        if self._batch_running:
            ok = ask_confirm("Clear the queue?",
                             tr("A job is running. Remove the waiting jobs? The running job finishes normally."),
                             "Remove them", danger=True)
            if not ok:
                return
            run = self._running_item()
            for it in self.queue:
                if it is not run:
                    self._drop_kept_extract(it)
            self.queue[:] = [run] if run is not None else []
        else:
            for it in self.queue:
                self._drop_kept_extract(it)
            self.queue.clear()
        self._queue_missing_saved = []   # a deliberate clear also drops parked entries
        self._refresh_parked()
        self.update_queue_box()

    # GameItem fields holding a Path, everything else is JSON-safe
    _QUEUE_PATH_FIELDS = ("path", "archive_path", "bundle_dir", "patch_source", "output_path")

    @staticmethod
    def _parked_source(d: dict):
        return d.get("archive_path") or d.get("path") or d.get("origin_archive")

    @staticmethod
    def _parked_back(d: dict) -> bool:
        return any(x and Path(str(x)).exists() for x in (d.get("archive_path"), d.get("path"), d.get("origin_archive")))

    def _recheck_parked(self):
        """every 20 s, off the main thread: a share that doesn't answer must not freeze the window"""
        parked = list(getattr(self, "_queue_missing_saved", None) or [])

        def look():
            back = False
            try:
                back = any(self._parked_back(d) for d in parked)
            except OSError as e:
                try:
                    self.root.after(0, lambda e=e: self.log("WARN", f"Could not look for parked jobs: {e}"))
                except (tk.TclError, RuntimeError):
                    pass
            finally:
                try:
                    self.root.after(0, lambda: done(back))
                except (tk.TclError, RuntimeError):
                    pass

        def done(back):
            try:
                if back and not self._batch_running and parked == list(self._queue_missing_saved or []):
                    self._queue_missing_saved = []
                    self._restore_queue(saved=parked, quiet=True)
            except Exception as e:
                self.log("WARN", f"Could not look for parked jobs: {e}")
            try:
                self.root.after(20000, self._recheck_parked)
            except tk.TclError:
                pass
        if parked:
            threading.Thread(target=look, daemon=True).start()
        else:
            done(False)

    def _refresh_parked(self):
        n = len(getattr(self, "_queue_missing_saved", None) or [])
        btn = getattr(self, "_parked_btn", None)
        if btn is None:
            return
        if n:
            btn.configure(text=plural(n, "1 job waits for its drive", "{n} jobs wait for their drive"))
            btn.grid(row=1, column=0, sticky="w", pady=(4, 0))
        else:
            btn.grid_remove()

    def _show_parked(self):
        parked = list(getattr(self, "_queue_missing_saved", None) or [])
        if not parked:
            return
        names = "\n".join("•  " + str(d.get("display_name") or d.get("name") or self._parked_source(d) or "?")
                          + (f"  ({self._parked_source(d)})" if self._parked_source(d) else "")
                          for d in parked[:10])
        if ask_confirm("Jobs waiting for a drive",
                       tr("Their source is on a drive that is not connected. They come back into the queue by "
                          "themselves when it is.") + "\n\n" + names,
                       "Forget them", "Keep waiting", danger=True):
            self._queue_missing_saved = []
            self._save_queue()
            self._refresh_parked()
            self.log("INFO", f"Forgot {len(parked)} parked job(s).")

    def _save_queue(self):
        """gated until _restore_queue ran, so the first empty render can't clobber the saved queue"""
        if not getattr(self, "_queue_restored", False):
            return False
        try:
            items = []
            for it in self.queue:
                d = {}
                for k, v in vars(it).items():
                    if k.startswith("_") or k == "artwork":
                        continue
                    if isinstance(v, Path):
                        d[k] = str(v)
                    elif k == "bundle_siblings":
                        d[k] = [str(s) for s in (v or [])]
                    elif v is None or isinstance(v, (str, int, float, bool)):
                        d[k] = v
                items.append(d)
            items += list(getattr(self, "_queue_missing_saved", None) or [])
            return save_settings({"queue": items})
        except Exception:
            return False

    def _restore_queue(self, saved=None, quiet: bool = False):
        missing = []  # source not reachable right now
        if saved is None:
            try:
                saved = load_settings().get("queue") or []
            except Exception:
                saved = []
        restored = skipped = 0
        for d in saved:
            try:
                if not isinstance(d, dict):
                    skipped += 1
                    continue
                obj = GameItem.__new__(GameItem)
                for k, v in d.items():
                    if k in self._QUEUE_PATH_FIELDS and v:
                        setattr(obj, k, Path(v))
                    elif k == "bundle_siblings":
                        setattr(obj, k, [Path(s) for s in (v or [])])
                    else:
                        setattr(obj, k, v)
                _src = getattr(obj, "path", None)
                _org = getattr(obj, "origin_archive", None)
                # a done job that kept its unpack keeps pointing at it, so restart cleanups spare the extras
                _holds = getattr(obj, "status", "") == "Done" and getattr(obj, "kept_extract", False)
                # files were taken out of the unpack: start at the archive again.
                # no archive: park it, never run from the incomplete copy
                _taken = (getattr(obj, "source_taken_in", False) and getattr(obj, "status", "") != "Done"
                          and not getattr(obj, "archive_path", None))
                _rearm = bool(_taken and _org and Path(_org).exists())
                if _taken and not _rearm:
                    skipped += 1
                    missing.append(d)
                    continue
                obj.source_taken_in = False
                if (_org and not _holds and not getattr(obj, "archive_path", None) and _src
                        and (_rearm or not Path(str(_src)).exists()) and Path(_org).exists()):
                    obj.archive_path, obj.path = Path(_org), None
                    obj.source_kind, obj.files, obj.bundle_siblings = "archive", 0, self._download_extras(obj)
                    obj.extracted_size = int(getattr(obj, "origin_extracted_size", 0) or 0)
                    try:
                        obj.size = archive_set_ondisk_size(Path(_org))
                    except Exception:
                        pass
                    if getattr(obj, "status", "") in ("Failed", "Cancelled", "Skipped"):
                        obj.status = "Pending Extract"
                probe = getattr(obj, "archive_path", None) or getattr(obj, "path", None)
                # A finished job is a record: it stays listed when its source is gone.
                if getattr(obj, "status", "") != "Done" and (not probe or not Path(probe).exists()):
                    # drive unplugged or share down: keep the entry so the next save doesn't lose it
                    skipped += 1
                    missing.append(d)
                    continue
                # a crashed run left emu files in the user's own folder: restore it now
                if getattr(obj, "ampr_injected", None) or getattr(obj, "ampr_index_path", None):
                    try:
                        self._ampr_cleanup(obj)
                    except Exception:
                        pass
                obj.artwork = None   # the cover comes back from the art cache (art_key)
                # old queues have no format snapshot, they were .ffpfsc jobs, not today's default
                if getattr(obj, "operation", "pack") == "pack" and getattr(obj, "output_compressed", None) is None:
                    obj.output_compressed = True
                if getattr(obj, "status", "") in ("Running", "Extracting", "Patching"):
                    obj.status = "Pending Extract" if getattr(obj, "archive_path", None) else "Queued"
                self.queue.append(obj)
                restored += 1
            except Exception:
                skipped += 1
        self._queue_missing_saved = missing   # re-persisted by _save_queue, untouched
        # extras a crashed run set aside beside a user's game folder go back in
        for obj in list(self.queue):
            try:
                g = Path(str(getattr(obj, "path", "") or ""))
                aside = g.parent / f".{g.name}.ffpfsc-extras"
                if g.is_dir() and aside.is_dir():
                    self._return_extras_aside(types.SimpleNamespace(extras_aside=f"{aside} -> {g}"))
                    self.log("INFO", f"Extras an interrupted run had set aside are back in {g}.")
            except Exception:
                pass
        self._queue_restored = True   # from here on, queue mutations persist
        if quiet:
            if restored:
                self.log("OK", f"{restored} parked job(s) are back: their drive is connected again.")
                self.update_queue_box()
                self._save_queue()
            else:
                self._refresh_parked()
            return
        # prune covers no job uses, parked jobs keep theirs
        _keep = {getattr(o, "art_key", "") for o in self.queue} | {str(d.get("art_key") or "") for d in missing}
        threading.Thread(target=prune_art_cache, args=(_keep,), daemon=True).start()
        if self.queue:
            self.update_queue_box()
        self._refresh_parked()
        self.root.after(20000, self._recheck_parked)
        if restored:
            self.log("INFO", f"Restored {restored} item(s) from the saved queue.")
        if getattr(self, "_cleared_libs_setting", ""):
            self.log("INFO", f"Settings: the patched libraries folder was your firmware libraries folder "
                             f"({self._cleared_libs_setting}), which holds the original libraries; cleared.")
        if missing:
            names = ", ".join(str(m.get("display_name") or m.get("name") or m.get("path") or "?")
                              for m in missing[:5])
            self.log("WARN", f"{len(missing)} saved queue item(s) sit on a drive that is not mounted "
                             f"right now; they stay parked until it returns: {names}")
        if getattr(self, "_settings_locked", False):
            self.log("ERROR", f"{SETTINGS_FILE} is in use by another program and could not be read. This session "
                              "runs on defaults and saves nothing (settings, queue, history stay as they are); "
                              "close the other program and restart the app.")
        if ultra_core._SETTINGS_CORRUPT_COPY:
            self.log("WARN", f"settings.json could not be read and was kept as "
                             f"{ultra_core._SETTINGS_CORRUPT_COPY.name}; the profile was re-created with defaults.")
        if skipped:
            self.log("WARN", f"Saved queue: {skipped} item(s) skipped (source path missing/invalid).")

    def _probe_archive_contents(self) -> None:
        """member names only, so the source says '.pkg' or 'Folder' instead of '.rar'"""
        if getattr(self, "_content_probe_busy", False):
            return
        todo = [it for it in self.queue if getattr(it, "archive_path", None)
                and not getattr(it, "archive_content", "") and not getattr(it, "_content_probed", False)
                and getattr(it, "content_kind", "") != "ps4"]
        if not todo:
            return
        for it in todo:
            it._content_probed = True
        self._content_probe_busy = True
        pw = self._candidate_passwords()
        found: list = []
        done = threading.Event()

        # paths and passwords taken now: a job that starts meanwhile clears archive_path
        jobs = [(it, Path(it.archive_path), [x for x in [getattr(it, "password", None) or ""] if x] + pw)
                for it in todo]

        def work():
            try:
                for it, arc, cands in jobs:
                    c = ArchiveExtractor.content_kind(arc, cands)
                    if c:
                        found.append((it, c))
            finally:
                done.set()

        def poll():
            if not done.is_set():
                self.root.after(200, poll); return
            self._content_probe_busy = False
            changed = False
            for it, c in found:
                if not getattr(it, "archive_content", ""):
                    it.archive_content, changed = c, True
            if changed:
                self.update_queue_box()
        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, poll)

    def update_queue_box(self, select_item=None):
        # snapshot the global Output onto each job at add time, so it keeps its own
        _gout = (self.output_var.get() or "").strip()
        try:
            _fmt = self.output_format_var.get()
        except Exception:
            _fmt = "ffpfsc"
        for _it in self.queue:
            _op = getattr(_it, "operation", "pack")
            if _gout and getattr(_it, "output_path", None) is None and _op in ("pack", "unpack", "chain"):
                _it.output_path = Path(_gout)
            # format is per job. with .pkg remembered, a fresh pack job becomes an fPKG build
            if _op == "pack" and getattr(_it, "output_compressed", None) is None:
                if _fmt == "pkg":
                    self._as_fpkg_job(_it, dict(self.fpkg_defaults),
                                      identity=self._take_pending_fpkg_identity(_it))
                _it.output_compressed = (_fmt != "ffpfs")
            if _op in ("pack", "fpkg-build", "chain") and getattr(_it, "auto_organize", None) is None:
                _it.auto_organize = bool(self.auto_organize_var.get())
        self._save_queue()
        self._probe_archive_contents()
        # a plain refresh keeps the focus by job key, so a reorder doesn't jump to another job
        keep_marked = select_item is None
        if select_item is None:
            _fk = self.queue_listbox.focus_key()
            select_item = next((it for it in self.queue if id(it) == _fk), None) if _fk is not None else None
        if select_item is None:
            prev_idx  = self._queue_sel_idx()
            select_item = (self.queue[prev_idx]
                           if prev_idx is not None and prev_idx < len(self.queue)
                           else None)

        self._sync_primary_action()
        if not self.queue:
            self.queue_listbox.set_rows([])
            self.queue_total_var.set("No jobs yet")
            self._details_item = None
            try:
                self._nav["queue"].configure(badge=None)
                self._card_show(False)
            except Exception:
                pass
            return

        total = sum(getattr(x, "size", 0) or 0 for x in self.queue)
        try:
            self._name_jobs_from_games()
        except Exception:
            pass
        rows = []
        _run_item = self._running_item()
        for i, item in enumerate(self.queue):
            # remember the name as added, after the unpack item.name becomes the extracted stem
            if not getattr(item, "display_name", None):
                item.display_name = (getattr(item, "bundle_subfolder", None)
                                     or getattr(item, "name", "") or item.title_id)
            prefix = "▶ " if item is _run_item else f"{i + 1}. "
            opn = getattr(item, "operation", "pack")
            badge = {"unpack": "CONVERT", "patch": "PATCH ",
                     "fake-sign": "SIGN   ",
                     "ps4-keystone": "KEY    ",
                     "fpkg-extract": "fPKG-EX",
                     "fpkg-build":   "fPKG-BD",
                     "copy":         "COPY   ",
                     "chain":        ("→ " + CHAIN_TARGET_LABEL.get(getattr(item, "chain_to", ""), "?")).ljust(7)
                     }.get(opn, "PACK   ")
            if opn == "chain":
                _ch = ", ".join(chain_changes(item)) or "no changes"
                _sz = (f"~{format_size(display_size(item))}" if shows_extracted_size(item)
                       else format_size(getattr(item, "size", 0) or 0))
                detail = f"{_ch}  ·  {_sz}"
            elif opn in ("fake-sign", "ps4-keystone"):
                detail = "in place"
            elif opn == "patch":
                detail = "overwrite" if getattr(item, "patch_overwrite", False) else "→ [patched]"
            elif opn == "fpkg-extract":
                detail = format_size(getattr(item, "size", 0) or 0) + " → /app0"
            elif opn == "fpkg-build":
                _b = (getattr(item, "fpkg_kraken_backend", "builtin") or "builtin")
                _m = (getattr(item, "fpkg_inner_mode", "none") or "none")
                detail = f"→ .pkg  ({_m}/{_b})"
            elif opn == "copy":
                _mode = {"move": "move", "organize": "move on the same drive, copy across drives"}.get(
                    getattr(item, "copy_mode", None), "copy, the source stays")
                detail = f"{format_size(getattr(item, 'size', 0) or 0)}  ·  {_mode}"
            else:
                # archives store the packed size, show the unpacked one with ~ as an estimate
                detail = (f"~{format_size(display_size(item))} unpacked"
                          if shows_extracted_size(item) else format_size(item.size))
            disp = getattr(item, "display_name", None) or item.name
            line = f"{prefix}{badge}  {item.title_id}  {disp}  [{detail}]  {item.status}"
            running = item is _run_item
            st = str(getattr(item, "status", "") or "")
            state = ("running" if running else "done" if st == "Done" else "failed" if st == "Failed"
                     else "skipped" if st in ("Skipped", "Cancelled")
                     else "waiting" if st == "Extracting" else "queued")
            if shows_extracted_size(item):
                size_txt = f"~{format_size(display_size(item))}"
            elif getattr(item, "size", 0):
                size_txt = format_size(item.size)
            else:
                size_txt = ""
            sub = "  →  ".join(tr(p) for p in self._job_recipe(item))
            if size_txt:
                sub += f"   ·   {size_txt}"
            _after = {"trash": tr("source to the {TRASH_NAME}", TRASH_NAME=TRASH_NAME),
                      "move": tr("source moves"), "delete": tr("deletes the source")}.get(
                          getattr(item, "after_source", None) or "keep")
            if _after and st not in ("Done",):
                sub += f"   ·   {_after}"
            if getattr(item, "ps4_default_keystone", None) and not getattr(item, "ps4_keystone", None):
                sub += "   ·   ⚠ " + tr("default keystone")
            tid = shown_title_id(item)
            if tid and tid not in str(disp):
                sub = f"{tid}   ·   {sub}"
            chip = (f"{int(self._cur_job_pct)}%" if running else
                    {"Pending": "Queued", "Pending Extract": "Queued"}.get(st, st or "Queued"))
            rows.append({"key": id(item), "text": line, "title": str(disp), "subtitle": sub, "state": state, "chip": chip,
                         "group": (str(item.origin_archive) if int(getattr(item, "archive_games", 0) or 0) > 1
                                   and getattr(item, "origin_archive", None) else None),
                         "progress": (max(0.0, min(1.0, self._cur_job_pct / 100.0)) if running else None)})

        n = len(self.queue)
        self.queue_total_var.set(plural(n, "1 job  ·  {size}", "{n} jobs  ·  {size}", size=format_size(total)))
        try:
            self._nav["queue"].configure(badge=n)
        except Exception:
            pass

        try:
            sel = self.queue.index(select_item) if select_item in self.queue else 0
        except (ValueError, TypeError):
            sel = 0
        self.queue_listbox.set_rows(rows, selected=sel, keep_marked=keep_marked)
        self.queue_listbox.see(sel)

        # 'is' is safe here: _details_item holds a real reference, so no id reuse
        sel_item = self.queue[sel]
        if sel_item is not self._details_item:
            self.update_game_details(sel_item)
        elif len(self.queue_listbox.marked_rows()) > 1:
            self._card_show(True)

    def update_game_details(self, item):
        self._details_item = item   # record before any call that might raise
        self._sync_progress_box()
        self.game_name_var.set(f"Name: {getattr(item, 'display_name', None) or item.name}")
        mode = {"unpack": "Convert", "patch": "Integrate patch",
                "fake-sign": "Fake sign", "ps4-keystone": "PS4 PKG Tools",
                "fpkg-extract": "Extract fPKG",
                "fpkg-build":   "Build fPKG"}.get(getattr(item, "operation", "pack"), "Pack")
        if getattr(item, "operation", "pack") == "chain":
            mode = chain_summary(item)
        self.title_var.set(f"Title ID: {item.title_id}  |  Mode: {mode}")
        self.source_detail_var.set(f"Source: {item.path}")
        if shows_extracted_size(item):
            self.orig_var.set(f"Original Size: ~{format_size(item.extracted_size)} unpacked  "
                              f"({format_size(item.size)} packed)")
        else:
            self.orig_var.set(f"Original Size: {format_size(item.size)}")
        self.files_var.set(f"Files: {item.files:,}")
        self.load_art(self._job_art(item))
        self._refresh_space_for_item(item)
        self.update_command_preview()
        self._fill_job_card(item)

    def _fill_job_card(self, item):
        try:
            self._card_show(True)
            disp = getattr(item, "display_name", None) or item.name
            self.card_title_var.set(str(disp))
            ver = ""
            try:
                src = getattr(item, "path", None)
                if src and Path(str(src)).exists():
                    ver = guess_game_version(Path(str(src)))
            except Exception:
                ver = ""
            size_txt = (tr("~{size} unpacked", size=format_size(item.extracted_size)) if shows_extracted_size(item)
                        else format_size(getattr(item, "size", 0) or 0))
            files = getattr(item, "files", 0) or 0
            meta = [x for x in (shown_title_id(item), f"v{ver}" if ver else "", size_txt,
                                plural(files, "1 file", "{n} files", n=i18n.number(f"{files:,}")) if files else "") if x]
            self.card_meta_var.set("   ·   ".join(meta))
            out = self._job_output_dir(item)
            self.card_target_var.set(f"→  {out}" if out else tr("→  no output folder set"))
            self._card_chips.set_parts(self._job_recipe(item, detail=True))
            self._fill_card_info(item)
        except Exception:
            pass

    _CARD_INFO_KEYS = ("Status", "Source", "Changes", "Compression", "Drives", "Space", "After")

    def _sync_retry_btn(self, item) -> None:
        try:
            self.retry_btn.configure(state="normal" if getattr(item, "status", "") in self._RETRYABLE
                                     and item in self.queue else "disabled")
        except Exception:
            pass

    def _fill_card_info(self, item):
        self._sync_retry_btn(item)
        rows = getattr(self, "_card_info_rows", None)
        if not rows or item is None:
            return
        info = self._card_info_text(item)
        for key, (var, k, v) in rows.items():
            text = info.get(key, "")
            if var.get() != text:
                var.set(text)
            if text and not v.winfo_manager():
                k.grid(); v.grid()
            elif not text and v.winfo_manager():
                k.grid_remove(); v.grid_remove()

    def _card_info_text(self, item) -> dict:
        info = {}
        status = str(getattr(item, "status", "") or "Queued")
        if getattr(self, "_batch_running", False) and item is getattr(self, "_active_item", None):
            status = "Running"
        status = {"Pending Extract": "Queued; the archive is extracted when its turn comes",
                  "Pending": "Queued"}.get(status, status)
        note = str(getattr(item, "status_note", "") or "").strip()
        if note and status in ("Failed", "Skipped", "Cancelled", "Done"):
            status = f"{status}: {note}"
        if getattr(item, "kept_extract", False):
            status += "; the extracted copy is kept, so Start goes on from it (removing the job deletes it)"
        if getattr(item, "archive_problem", "") and getattr(item, "archive_path", None):
            status += tr("; the archive cannot be read: {archive_problem}", archive_problem=item.archive_problem)
        info["Status"] = status
        src = getattr(item, "archive_path", None) or getattr(item, "path", None)
        if src:
            info["Source"] = str(src)
        if int(getattr(item, "archive_games", 0) or 0) > 1 and getattr(item, "origin_archive", None):
            info["Archive"] = (f"{Path(str(item.origin_archive)).name}, game "
                               f"{getattr(item, 'archive_index', '?')} of {item.archive_games}")
        changes = []
        patch = getattr(item, "patch_source", None)
        if patch:
            changes.append(tr("Integrate the patch from {patch}", patch=patch))
        bt = getattr(item, "backport_target", None)
        if bt:
            libs = getattr(item, "backport_libs_root", None) or (self.backport_libs_var.get() or "").strip()
            fw = (self.fw_libs_var.get() or "").strip()
            line = tr("Backport to {bt}", bt=bt)
            line += tr("; patched libraries from {libs}", libs=libs) if libs else "; no patched libraries set"
            line += "; the functions are checked against your firmware folder" if fw else \
                    "; no function check (no firmware folder in Settings)"
            changes.append(line)
        out = self._job_recipe_parts(item)[-1] if self._job_recipe_parts(item) else ""
        if getattr(item, "chain_sign", False) or getattr(item, "operation", "") == "fake-sign":
            changes.append("Fake-sign the executables")
        if getattr(item, "chain_lz4_unpack", False):
            changes.insert(0, tr("LZ4 packs unpacked back to plain files"))
        if getattr(item, "chain_lz4", False):
            prof = getattr(item, "lz4_profile", None)
            changes.append(tr("LZ4 asset packs, profile {p}", p=Path(str(prof)).name) if prof
                           else tr("LZ4 asset packs, careful default profile"))
        elif out == ".pkg":
            changes.append("Executables signed by the package builder")
        info["Changes"] = "\n".join(changes) if changes else "None"
        if out == ".ffpfsc":
            try:
                cores = int(self.cpu_count_var.get())
            except (TypeError, ValueError):
                cores = 0
            info["Compression"] = (tr(".ffpfsc, zlib level {v}, 64 KiB blocks, {v2}", v=self._ffpfsc_level(item), v2=tr('cores by game size') if cores == 0 else tr('{n} cores', n=cores)))
        elif out == ".pkg":
            p = self._fpkg_params_of(item)
            speed = tr("level {n}", n=int(p.get('level', 0)))
            try:
                cores = int(self.cpu_count_var.get())
            except (TypeError, ValueError):
                cores = 0
            info["Compression"] = (tr(".pkg, Kraken {speed} on {v}, codec layer {v2}; retail fixes {v3}, HDR {v4}", speed=speed, v=tr('all cores') if cores <= 0 else tr('{n} cores', n=cores), v2=p.get('inner', 'kraken'), v3=tr('on') if p.get('retail_normalize', True) else tr('off'), v4=p.get('hdr_flag', 'auto')))
        elif out == ".ffpfs":
            info["Compression"] = tr(".ffpfs, uncompressed")
        work = getattr(item, "_build_temp", None) or (self.temp_var.get() or "").strip()
        out_dir = self._job_output_dir(item)
        if getattr(item, "operation", "") == "copy" and out_dir:
            # nothing is built: an archive is unpacked on the output drive, then moved in
            arc = getattr(item, "archive_path", None) and not getattr(item, "path", None)
            info["Drives"] = (tr("unpacks and writes on {v} ({out_dir})", v=_drive_name(out_dir), out_dir=out_dir) if arc
                              else tr("writes to {v} ({out_dir})", v=_drive_name(out_dir), out_dir=out_dir))
        elif out_dir and getattr(item, "operation", "") in ("chain", "pack", "fpkg-build", "patch"):
            # the router's pick, not the temp setting
            plan = self._planned(item)
            bits = []
            root = getattr(plan, "_build_root", None)
            if getattr(item, "archive_path", None) and not getattr(item, "path", None) and root:
                bits.append(tr("unpacks on {v} ({root})", v=_drive_name(Path(str(root))), root=root))
            img = getattr(plan, "_build_temp", None)
            if img and not _item_is_single_pass(plan):
                bits.append(tr("builds on {v} ({img})", v=_drive_name(Path(str(img))), img=img))
            bits.append(tr("writes to {v} ({out_dir})", v=_drive_name(out_dir), out_dir=out_dir))
            info["Drives"] = ", ".join(bits)
        elif work or out_dir:
            bits = []
            if work:
                bits.append(tr("works on {v} ({work})", v=_drive_name(Path(str(work))), work=work))
            if out_dir:
                bits.append(tr("writes to {v} ({out_dir})", v=_drive_name(out_dir), out_dir=out_dir))
            info["Drives"] = ", ".join(bits)
        space = (self.temp_space_var.get() or "").strip()
        if space and not space.endswith(" -"):
            info["Space"] = space.replace("  |  ", " · ")
        aj = _after_job_module()
        act = getattr(item, "after_source", None) or aj.KEEP
        if (act in aj.ACTIONS and act != aj.KEEP
                and (getattr(item, "operation", "") not in ("fake-sign", "copy", "ps4-keystone")
                     or getattr(item, "content_kind", "") in ("ps4", ORGANIZE_TARGET))):
            text = aj.DONE_TEXT[act].format(dest=getattr(item, "after_move_to", None) or "a folder")
            info["After"] = text[0].upper() + text[1:] + ", once the job is Done"
        return info

    def _probe_pkg_content(self, item) -> int:
        """game size from the package's own directory, nothing is decoded"""
        if getattr(item, "operation", "") != "chain" or chain_source_kind(item) != "pkg":
            return 0
        if getattr(item, "pkg_content_size", 0):
            item.extracted_size = max(int(item.pkg_content_size), int(getattr(item, "extracted_size", 0) or 0))
            return int(item.pkg_content_size)
        try:
            backend = backend_base_dir()
            if str(backend) not in sys.path:
                sys.path.insert(0, str(backend))
            import fpkg as _fpkg
            doc = _fpkg.list_inner(Path(str(item.path)))
            _files = [e for e in doc.get("entries", []) if e.get("type") == "file"]
            _sizes = [int(e.get("size") or 0) for e in _files]
            content = sum(_sizes)
            item.pkg_largest_file = max(_sizes, default=0)
            item.pkg_kept_bytes = sum(int(e.get("size") or 0) for e in _files if pkg_file_stays(e))
        except Exception as e:
            self.log("WARN", f"Could not read the size of the game in {Path(str(item.path)).name}: {e}")
            return 0
        if content > 0:
            item.pkg_content_size = content
            item.extracted_size = max(content, int(getattr(item, "extracted_size", 0) or 0))
        return content

    def _refresh_space_for_item(self, item=None):
        if item is None:
            item = self._shown_or_next()
        if item is None or getattr(item, "size", 0) == 0:
            self.temp_space_var.set("Temp Needed: - ")
            return
        if getattr(item, "operation", "pack") == "copy":
            out_dir = self._job_output_dir(item)
            tp = self.temp_var.get().strip()
            try:
                needs = _space_requirements(item, Path(tp) if tp else out_dir, out_dir) if out_dir else []
                if not needs:
                    self.temp_space_var.set("Same drive: renamed or cloned, no extra space")
                else:
                    ok = all(get_free_space(d) >= n for _l, d, n in needs)
                    self.temp_space_var.set("  |  ".join(tr("{label}: ~{need}, {free} free", label=tr(label), need=format_size(n),
                                                            free=format_size(get_free_space(d))) for label, d, n in needs)
                                            + "  |  " + tr("fits" if ok else "LOW, may not fit"))
            except Exception:
                self.temp_space_var.set(tr("Needs: ~{size}", size=format_size(_build_size_of(item))))
            return
        if getattr(item, "operation", "pack") == "unpack":
            try:
                op = self.output_var.get().strip()
                out_dir = Path(op) if op else None
                out_free = get_free_space(out_dir) if out_dir else 0
                self.temp_space_var.set(
                    tr("Extract Needs: ~{size}+  |  Out Free: {free}", size=format_size(item.size), free=format_size(out_free))
                )
            except Exception:
                self.temp_space_var.set(tr("Extract Needs: ~{size}+", size=format_size(item.size)))
            return
        out_dir = self._job_output_dir(item)
        if out_dir is not None and getattr(item, "operation", "pack") in ("chain", "pack", "fpkg-build", "patch"):
            # same numbers the space gate checks at start, on the drives the router picks
            try:
                plan = self._planned(item)
                img = Path(str(getattr(plan, "_build_temp", None) or self.temp_var.get().strip() or out_dir))
                needs = _space_requirements(plan, img, out_dir)
                if needs:
                    ok = all(get_free_space(d) >= n for _l, d, n in needs)
                    short = {"Temp drive (inner image)": "Image", "Inner image drive": "Image",
                             "Extracted source drive": "Unpacked game", "Output drive": "Output",
                             "Temp drive (whole scratch)": "Unpack and image",
                             "Output drive (archive unpacked)": "Output",
                             "Output drive (copy on the same drive)": "Copy"}
                    # needs on the same drive share one row, labels joined by +
                    _lab = lambda label: " + ".join(dict.fromkeys(tr(short.get(x, x)) for x in label.split(" + ")))
                    self.temp_space_var.set(
                        "  |  ".join(tr("{label} on {drive} ~{need}, {free} free", label=_lab(label),
                                        drive=_drive_colon(Path(str(d))), need=format_size(n),
                                        free=format_size(get_free_space(d))) for label, d, n in needs)
                        + "  |  " + tr("fits" if ok else "LOW, may not fit"))
                    return
            except Exception:
                pass
        try:
            tp = self.temp_var.get().strip()
            temp_dir = Path(tp) if tp else None
            out_dir  = self._job_output_dir(item)
            if temp_dir is None:
                self.temp_space_var.set(tr("Peak Needed: ~{size}", size=format_size(int(_build_size_of(item) * _peak_factor_for(item)))))
                return
            same   = same_drive(temp_dir, out_dir) if out_dir else True
            size   = _build_size_of(item)
            factor = _peak_factor_for(item)
            free   = get_free_space(temp_dir)
            out_free = get_free_space(out_dir) if out_dir else 0
            image_need = estimate_image_space_needed(size)     # just the inner image on the SSD
            need_label = "Temp image"
            if getattr(item, "operation", "pack") == "chain" and chain_needs_unpack(item):
                # the unpacked game and the image both sit on the temp drive
                image_need = estimate_peak_space_needed(size, factor, False)
                need_label = "Temp peak"
            out_full   = estimate_peak_space_needed(size, factor, True)
            # fits if the SSD holds the image (the spool goes to the output drive) or the output holds it all
            ok = (size > 0 and free >= image_need) or (out_dir is not None and out_free >= out_full)
            flag = tr("fits" if ok else "LOW, may not fit")
            self.temp_space_var.set(tr("{label}: ~{need}  |  Temp Free: {free}  |  Out Free: {out_free}  |  {flag}",
                                       label=tr(need_label), need=format_size(image_need), free=format_size(free),
                                       out_free=format_size(out_free), flag=flag))
        except Exception:
            self.temp_space_var.set(tr("Peak Needed: ~{size}", size=format_size(item.size * 2.2)))

    def _update_format_label(self):
        comp = self.output_compressed_var.get()
        try:
            self.format_hint_var.set("Compressed (.ffpfsc, smaller)" if comp
                                     else "Uncompressed (.ffpfs, faster)")
        except Exception:
            pass

    def _on_format_toggle(self):
        self._update_format_label()
        for fn in ("update_queue_box", "update_command_preview"):
            try:
                getattr(self, fn)()
            except Exception:
                pass
        self.log("INFO", "Output format set to "
                 + ("compressed .ffpfsc (smaller)" if self.output_compressed_var.get()
                    else "uncompressed .ffpfs (faster to build and mount; full size)."))

    def _shown_or_next(self):
        it = getattr(self, "_details_item", None)
        if it is not None and it in self.queue:
            return it
        return self._running_item() or self._next_pending() or (self.queue[0] if self.queue else None)

    def update_command_preview(self):
        item = self._shown_or_next()
        if item and getattr(item, "archive_path", None):
            self.command_label.configure(
                text=tr("{name} - archive will be extracted before compression starts.", name=item.name))
            return
        src = self.source_var.get().strip()
        if not item and src and Path(src).exists():
            p = Path(src)
            pycmd = get_backend_python_command() or ["python"]
            is_unpack = p.suffix.lower() == ".ffpfsc" or self.unpack_mode_var.get()
            if getattr(sys, "frozen", False):
                cmd = pycmd + [str(p), self.output_var.get().strip() or str(p.parent), "--overwrite"]
            else:
                cmd = pycmd + ["-u", str(backend_base_dir() / "cli.py"), str(p),
                               self.output_var.get().strip() or str(p.parent), "--overwrite"]
            if is_unpack:
                cmd.append("--unpack")
        elif item:
            try:
                cmd, _, _, _ = self.build_command(item)
            except Exception:
                self.command_label.configure(text="Select output and temp folder to preview command.")
                return
        else:
            self.command_label.configure(text="Select source, output, and temp folder to preview command.")
            return
        self.command_label.configure(text=" ".join(f'"{x}"' if " " in x else x for x in cmd))

    def build_command(self, item):
        _job_out = getattr(item, "output_path", None)
        out = Path(str(_job_out)) if _job_out else Path(self.output_var.get().strip())
        # full library name, not just the title id: two jobs with one title id would overwrite each other
        op = getattr(item, "operation", "pack")
        # .ffpfs only for folders and .ffpfs sources, disk images always end up compressed
        try:
            _ip = Path(getattr(item, "path", "") or "")
            src_pfs_family = _ip.is_dir() or _ip.suffix.lower() == ".ffpfs"
        except Exception:
            src_pfs_family = False
        _comp = getattr(item, "output_compressed", None)
        if _comp is None:
            _comp = self.output_compressed_var.get()
        self._cmd_uncompressed = (op == "pack") and src_pfs_family and not _comp
        out_ext = ".ffpfs" if self._cmd_uncompressed else ".ffpfsc"
        explicit_file = out.suffix.lower() in (".ffpfsc", ".ffpfs")
        sub = getattr(item, "bundle_subfolder", None)
        if op == "pack" and not explicit_file:
            # auto-organize, else a bundle mirrors its folder (never INTO the source), else straight in
            base = out
            organized_name = None
            if self._auto_organize_on(item):
                _b, organized_name = self._organized_layout(item, out, out_ext)
                if _b is not None:
                    base = _b
                elif sub:
                    base = self._mirror_base(item, out, sub)
            elif sub:
                base = self._mirror_base(item, out, sub)
            try:
                out = self._keep_both_name(item, base / (organized_name or descriptive_ffpfsc_name(item, ext=out_ext)))
                if getattr(item, "_name_was_truncated", False):
                    self.log("WARN", f"Output filename shortened to fit the {SHADOWMOUNT_NAME_LIMIT}-"
                                     f"byte ShadowMount limit: {out.name}")
                elif getattr(item, "_name_fluff_stripped", False):
                    self.log("INFO", f"Dropped edition suffix to fit the {SHADOWMOUNT_NAME_LIMIT}-"
                                     f"byte ShadowMount limit: {out.name}")
            except Exception:
                out = base  # backend names <title_id><ext> inside
        temp = Path(self.temp_var.get().strip())
        backend = backend_base_dir()
        cli_py = backend / "cli.py"
        pycmd = get_backend_python_command()
        if not pycmd:
            raise RuntimeError("Python was not found. Install Python, or run the app from source.")

        # chain job: named like the old jobs did, so the library looks the same
        if op == "chain":
            to = getattr(item, "chain_to", None) or "ffpfsc"
            src = Path(item.path)
            item._organized_pkg_name = None
            if to == "folder":
                if src.is_dir() and getattr(item, "chain_lz4", False):
                    out = out / f"{sanitize_filename(src.name)} [LZ4]"   # the packs never go into the source
                elif src.is_dir() and getattr(item, "chain_lz4_unpack", False):
                    out = out / f"{sanitize_filename(src.name)} [unpacked]"
                elif src.is_dir():
                    out = src                                   # changes in place; nothing is written elsewhere
                elif not out.name.endswith(" [extracted]"):
                    out = out / f"{sanitize_filename(src.stem)} [extracted]"
                if not src.is_dir() and getattr(item, "_keep_both", False) and self._folder_taken(out):
                    n = 2
                    while (out.parent / f"{out.name} ({n})").exists():
                        n += 1
                    out = out.parent / f"{out.name} ({n})"
                run_dir = out
            elif to == "pkg":
                if not explicit_file and (self._auto_organize_on(item) or sub):
                    if self._auto_organize_on(item):
                        _b, _pkg_name = self._organized_layout(item, out, ".pkg")
                        if _b is not None:
                            out = _b
                            item._organized_pkg_name = _pkg_name
                        elif sub:
                            out = self._mirror_base(item, out, sub)
                    elif sub:
                        out = self._mirror_base(item, out, sub)
                run_dir = out
            else:
                ext = {"ffpfs": ".ffpfs", "exfat": ".exfat"}.get(to, ".ffpfsc")
                if not explicit_file:
                    base = out
                    organized_name = None
                    if self._auto_organize_on(item):
                        _b, organized_name = self._organized_layout(item, out, ext)
                        if _b is not None:
                            base = _b
                        elif sub:
                            base = self._mirror_base(item, out, sub)
                    elif sub:
                        base = self._mirror_base(item, out, sub)
                    try:
                        out = self._keep_both_name(item, base / (organized_name or descriptive_ffpfsc_name(item, ext=ext)))
                    except Exception:
                        out = base
                run_dir = out.parent if out.suffix.lower() in (".ffpfsc", ".ffpfs", ".exfat") else out
            self._cmd_uncompressed = (to == "ffpfs")
            try:
                (out if to in ("folder", "pkg") or not out.suffix else out.parent).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            # OOM retry after pass 1: compress what pass 1 left, the unpacked copy is gone by then
            _resume = getattr(item, "_resume_inner", None)
            _resuming = to == "ffpfsc" and bool(_resume) and Path(str(_resume)).is_file()
            if _resuming:
                src = Path(str(_resume))
            head = (pycmd + [str(src), str(out)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), str(src), str(out)])
            cmd = head + ["--to", to]
            # a plain copy keeps the source, unless the job deletes it anyway: then just move
            cmd += ["--copy-mode", "move" if getattr(item, "after_source", None) == "delete" else "keep"]
            if to in ("pkg", "ffpfsc") and not _resuming and self._consumable_extract(item):
                cmd.append("--stage-in-place")      # our own unpacked copy: taken in as it goes
            if getattr(item, "chain_rebuild", False) and not _resuming:
                cmd.append("--rebuild")
            if getattr(item, "ps5_standalone", False) and not _resuming:
                cmd.append("--ps5-standalone")
            if getattr(item, "ps5_trophies", False) and not _resuming:
                cmd.append("--ps5-trophies")
            _ps = getattr(item, "patch_source", None)
            if _ps and not _resuming:
                cmd += ["--patch", str(_ps)]
            if not _resuming:
                cmd += self._backport_args(item)
            if getattr(item, "chain_sign", False) and not _resuming:
                cmd.append("--sign")
            if getattr(item, "chain_lz4_unpack", False) and not _resuming:
                cmd.append("--lz4-unpack")
            if getattr(item, "chain_lz4", False) and not _resuming:
                cmd.append("--lz4")
                if getattr(item, "lz4_profile", None):
                    cmd += ["--lz4-profile", str(item.lz4_profile)]
            if getattr(item, "_build_temp", None) is None:
                try:
                    self._resolve_extract_root(item)
                except Exception:
                    pass
            bt = getattr(item, "_build_temp", None)
            tstr = str(Path(bt)) if bt else str(temp)
            if tstr:
                cmd += ["--temp-dir", tstr]
            if to in ("ffpfs", "ffpfsc", "pkg") or getattr(item, "chain_lz4", False):
                # CPU cores: mkpfs workers for images, Kraken for .pkg, LZ4 for the packs
                _cpu = getattr(item, "_cpu_retry_override", None)
                if _cpu is None:
                    _cpu = self.cpu_count_var.get()
                if _cpu:
                    cmd += ["--cpu-count", str(_cpu)]
            if to in ("ffpfs", "ffpfsc"):
                _cl = self._ffpfsc_level(item)
                if _cl != 7:
                    cmd += ["--compression-level", str(_cl)]
                _bs = self.block_size_var.get()
                if _bs and _bs != "auto":
                    cmd += ["--block-size", _bs]
                if self.verify_output_var.get():
                    cmd.append("--verify")
                out_root = str(_job_out) if _job_out else self.output_var.get().strip()
                if out_root:
                    cmd += ["--spool-fallback-dir", out_root]
            if to == "pkg":
                cmd += ["--fpkg-inner", str(getattr(item, "fpkg_inner_mode", "kraken") or "kraken"),
                        "--fpkg-kraken-backend", str(getattr(item, "fpkg_kraken_backend", "builtin") or "builtin")]
                if not getattr(item, "fpkg_retail_normalize", True):
                    cmd += ["--fpkg-no-retail-normalize"]
                _hdr = _hdr_mode(getattr(item, "fpkg_hdr_flag", "auto"))
                if _hdr != "auto":
                    cmd += ["--fpkg-hdr-flag", _hdr]
                if getattr(item, "fpkg_regen_playgo", False):
                    cmd += ["--fpkg-regen-playgo"]
                if getattr(item, "fpkg_keystone", None):
                    cmd += ["--fpkg-keystone", str(item.fpkg_keystone)]
                for flag, attr in (("--content-id", "fpkg_content_id"), ("--title-id", "fpkg_title_id"),
                                   ("--fpkg-title", "fpkg_title"), ("--fpkg-version", "fpkg_version")):
                    _v = str(getattr(item, attr, "") or "").strip()
                    if _v and not (attr == "fpkg_version" and _v == "01.000.000"):
                        cmd += [flag, _v]
                _lvl = getattr(item, "fpkg_level", None)
                cmd += ["--compression-level", str(int(self._pkg_level_default() if _lvl is None else _lvl))]
            if self.verbose_var.get():
                cmd.append("--verbose")
            # an earlier folder is replaced only when the start's "already exists" rule said so
            if not (to == "folder" and not src.is_dir() and self._folder_taken(out)
                    and not getattr(item, "_replace_output", False)):
                cmd.append("--overwrite")
            return cmd, backend, run_dir, temp

        # PS4 keystone: packages are changed in place
        if op == "ps4-keystone":
            head = pycmd if getattr(sys, "frozen", False) else pycmd + ["-u", str(cli_py)]
            cmd = head + ["--ps4-keystone-set", str(item.path)]
            if getattr(item, "ps4_keystone", None):
                cmd += ["--ps4-keystone", str(item.ps4_keystone)]
            if getattr(item, "ps4_fix_version", False):
                cmd.append("--ps4-fix-version")
            for u in (getattr(item, "ps4_rebind", "") or "").splitlines():
                if u.strip():
                    cmd += ["--ps4-rebind", u.strip()]
            return cmd, backend, Path(item.path).parent, temp

        if op == "fake-sign":
            head = pycmd if getattr(sys, "frozen", False) else pycmd + ["-u", str(cli_py)]
            cmd = head + ["--fake-sign", str(item.path)]
            return cmd, backend, Path(item.path), temp

        # copy job, no re-encode. same drive: rename, else a chunked copy
        if op == "copy" and getattr(item, "content_kind", "") == ORGANIZE_TARGET:
            try:
                out.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            head = (pycmd + ["placeholder", str(out)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), "placeholder", str(out)])
            if not str(getattr(item, "path", "") or "").strip():      # Path("") is the working folder
                raise RuntimeError("This Organize job has no source.")
            cmd = head + ["--organize-into", str(item.path),
                          "--copy-mode", self._ps4_copy_mode(item),
                          "--if-exists", self.output_exists_var.get() or "skip"]
            return cmd, backend, out, temp
        if op == "copy" and getattr(item, "content_kind", "") == "ps4":
            try:
                out.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            head = (pycmd + ["placeholder", str(out)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), "placeholder", str(out)])
            cmd = head + ["--ps4-sort", str(item.path),
                          "--copy-mode", self._ps4_copy_mode(item),
                          "--if-exists", self.output_exists_var.get() or "skip"]
            if getattr(item, "ps4_keystone", None):
                cmd += ["--ps4-keystone", str(item.ps4_keystone)]
            return cmd, backend, out, temp
        if op == "copy":
            src = Path(item.path)
            out_ext = src.suffix.lower()
            explicit_file = out.suffix.lower() in (".ffpfsc", ".ffpfs", ".pkg")
            copy_name = None
            base = out
            if not explicit_file:
                if self._auto_organize_on(item):
                    _b, _nm = self._organized_layout(item, out, out_ext)
                    if _b is not None:
                        base = _b
                        copy_name = _nm
                    elif sub:
                        base = self._mirror_base(item, out, sub)
                elif sub:
                    base = self._mirror_base(item, out, sub)
                out = base
            else:
                copy_name = out.name
                out = out.parent
            try:
                out.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            head = (pycmd + ["placeholder", str(out)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), "placeholder", str(out)])
            cmd = head + ["--copy", str(src)]
            if copy_name:
                cmd += ["--copy-name", copy_name]
            cmd += ["--copy-mode", getattr(item, "copy_mode", None) or "keep"]
            return cmd, backend, out, temp

        if op == "fpkg-extract":
            # never dump /app0 straight into a library folder, it gets its own [extracted] folder
            out = self._fpkg_extract_folder(item, out)
            # the backend replaces an earlier extract only with --overwrite, and only once the new one is complete
            _taken = self._folder_taken(out)
            if _taken and getattr(item, "_keep_both", False):
                n = 2
                while (out.parent / f"{out.name} ({n})").exists():
                    n += 1
                out = out.parent / f"{out.name} ({n})"
            head = (pycmd + ["placeholder", str(out)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), "placeholder", str(out)])
            cmd = head + ["--fpkg-extract", str(item.path)]
            if _taken and getattr(item, "_replace_output", False):
                cmd.append("--overwrite")
            try:
                out.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return cmd, backend, out, temp

        if op == "fpkg-build":
            # like a pack: the tool names the .pkg by content id, _finalize_pkg_name renames it later
            item._organized_pkg_name = None
            if out.suffix.lower() != ".pkg":
                if self._auto_organize_on(item):
                    _b, _pkg_name = self._organized_layout(item, out, ".pkg")
                    if _b is not None:
                        out = _b
                        item._organized_pkg_name = _pkg_name
                    elif sub:
                        out = self._mirror_base(item, out, sub)
                elif sub:
                    out = self._mirror_base(item, out, sub)
            head = (pycmd + ["placeholder", str(out)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), "placeholder", str(out)])
            cmd = head + [
                "--fpkg-build", str(item.path),
                "--fpkg-inner", str(getattr(item, "fpkg_inner_mode", "kraken") or "kraken"),
                "--fpkg-kraken-backend", str(getattr(item, "fpkg_kraken_backend", "builtin") or "builtin"),
            ]
            if self._consumable_extract(item):
                cmd.append("--stage-in-place")  # our own unpacked copy, taken in as it goes
            # only non-default retail switches go out, the backend defaults are the known-good set
            if not getattr(item, "fpkg_retail_normalize", True):
                cmd += ["--fpkg-no-retail-normalize"]
            _hdr = _hdr_mode(getattr(item, "fpkg_hdr_flag", "auto"))
            if _hdr != "auto":
                cmd += ["--fpkg-hdr-flag", _hdr]
            if getattr(item, "fpkg_regen_playgo", False):
                cmd += ["--fpkg-regen-playgo"]
            if getattr(item, "fpkg_keystone", None):
                cmd += ["--fpkg-keystone", str(item.fpkg_keystone)]
            if not getattr(item, "fpkg_fake_sign", True):
                cmd += ["--fpkg-no-fake-sign"]
            # backport lowers SDK words before the C# tool signs and packs, in place on a folder
            cmd += self._backport_args(item)
            # identity fields are fallbacks, the backend reads param.json first
            for flag, attr in (("--content-id", "fpkg_content_id"), ("--title-id", "fpkg_title_id"),
                               ("--fpkg-title", "fpkg_title"), ("--fpkg-version", "fpkg_version")):
                _v = str(getattr(item, attr, "") or "").strip()
                if _v and not (attr == "fpkg_version" and _v == "01.000.000"):
                    cmd += [flag, _v]
            _dll = getattr(item, "fpkg_pubtools_dll", "") or ""
            if _dll:
                cmd += ["--fpkg-pubtools-dll", str(_dll)]
            bt = getattr(item, "_build_temp", None)
            tstr = str(Path(bt)) if bt else str(temp)
            if tstr:
                cmd += ["--temp-dir", tstr]
            # Kraken: 7 normal, -4 fast (0..9 are all the same). never the tuning-bar level
            _lvl = getattr(item, "fpkg_level", None)
            if _lvl is None:
                _lvl = self._pkg_level_default()
            cmd += ["--compression-level", str(int(_lvl))]
            try:
                out.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return cmd, backend, out, temp

        if op == "patch":
            game = Path(item.path)
            patch_src = getattr(item, "patch_source", None)
            if not patch_src:
                raise RuntimeError("Patch job has no patch source.")
            if getattr(item, "patch_overwrite", False) and game.suffix.lower() == ".ffpfsc":
                pout = game
            else:
                stem = game.stem if game.suffix.lower() == ".ffpfsc" else game.name
                stem = re.sub(r"\s*\[patched\]\s*$", "", stem, flags=re.I)
                tag = " [patched].ffpfsc"
                budget = max(8, SHADOWMOUNT_NAME_LIMIT - len(tag.encode("utf-8")))
                pout = out / (_truncate_to_bytes(sanitize_filename(stem), budget).rstrip() + tag)
            try:
                pout.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            head = (pycmd + [str(game), str(pout)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), str(game), str(pout)])
            cmd = head + ["--patch", str(patch_src), "--overwrite"]
            if getattr(item, "patch_inplace", False):
                cmd.append("--patch-inplace")
            bt = getattr(item, "_build_temp", None)
            tstr = str(Path(bt)) if bt else str(temp)
            if tstr:
                cmd += ["--temp-dir", tstr]
            _cl = self._ffpfsc_level(item)
            if _cl != 7:
                cmd += ["--compression-level", str(_cl)]
            _cpu = getattr(item, "_cpu_retry_override", None)
            if _cpu is None:
                _cpu = self.cpu_count_var.get()
            if _cpu:
                cmd += ["--cpu-count", str(_cpu)]
            _bs = self.block_size_var.get()
            if _bs and _bs != "auto":
                cmd += ["--block-size", _bs]
            if self.verify_output_var.get():
                cmd.append("--verify")
            # a big patched image may spill its pass-2 spool onto the output drive
            cmd += ["--spool-fallback-dir", str(pout.parent)]
            if self.verbose_var.get():
                cmd.append("--verbose")
            return cmd, backend, pout.parent, temp

        # OOM resume: compress the built inner image, the name still comes from the item
        _resume = getattr(item, "_resume_inner", None)
        _resume_ok = bool(_resume) and Path(str(_resume)).is_file()
        _src = str(_resume) if _resume_ok else str(item.path)
        if getattr(sys, "frozen", False):
            cmd = pycmd + [_src, str(out)]
        else:
            cmd = pycmd + ["-u", str(cli_py), _src, str(out)]
        if getattr(item, "operation", "pack") == "unpack":
            if out.exists() and out.is_dir():
                out = out / f"{item.path.stem}_extracted"
                if getattr(sys, "frozen", False):
                    cmd = pycmd + [str(item.path), str(out)]
                else:
                    cmd = pycmd + ["-u", str(cli_py), str(item.path), str(out)]
            cmd += ["--unpack", "--overwrite"]
            # decompress one level: stop at the first nested image
            if getattr(item, "unwrap", True) is False:
                cmd.append("--no-unwrap")
            return cmd, backend, out, temp
        if getattr(self, "_cmd_uncompressed", False):
            cmd.append("--no-compress")   # emit uncompressed .ffpfs (skip pass-2)
        if self.batch_var.get():
            cmd.append("--batch")
        if self.keep_pfs_var.get():
            cmd.append("--keep-pfs")
        if self.verify_output_var.get():
            cmd.append("--verify")
        # MkPFS tuning
        comp_level = self._ffpfsc_level(item)
        if comp_level != 7:
            cmd += ["--compression-level", str(comp_level)]
        # an OOM retry pins a lower core count, it beats the setting
        cpu = getattr(item, "_cpu_retry_override", None)
        if cpu is None:
            cpu = self.cpu_count_var.get()
        if cpu:
            cmd += ["--cpu-count", str(cpu)]
        block_size = self.block_size_var.get()
        if block_size and block_size != "auto":
            cmd += ["--block-size", block_size]
        if self.verbose_var.get():
            cmd.append("--verbose")
        # scratch (inner image + spool) goes on the drive this run was placed on
        patch_src = getattr(item, "patch_source", None)
        is_patch_job = (bool(patch_src) and self.auto_integrate_patch_var.get()
                        and bool(getattr(item, "path", None)) and item.path.is_dir()
                        and not _resume_ok)   # a resume only re-compresses the inner image
        if getattr(item, "_build_temp", None) is None:
            try:
                self._resolve_extract_root(item)
            except Exception:
                pass
        build_temp = getattr(item, "_build_temp", None)
        if build_temp is not None:
            temp = Path(build_temp)
        temp_str = str(temp) if str(temp) else ""
        if temp_str:
            cmd += ["--temp-dir", temp_str]
        # the spool may spill to the output root, the inner image stays on the fast SSD
        out_root = str(_job_out) if _job_out else self.output_var.get().strip()
        if out_root:
            cmd += ["--spool-fallback-dir", out_root]
        if is_patch_job:
            cmd += ["--patch", str(patch_src)]
        if (self.build_via_exfat_var.get() and not is_patch_job and not _resume_ok
                and getattr(item, "operation", "pack") == "pack"
                and getattr(item, "path", None) and Path(item.path).is_dir()):
            cmd.append("--via-exfat")
        # not for AMPR: those are signed before ampr_emu.index is built, re-signing breaks it
        if (self.fake_sign_before_pack_var.get() and not is_patch_job and not _resume_ok
                and getattr(item, "operation", "pack") == "pack"
                and not getattr(item, "ampr_emu", False)):
            cmd.append("--fake-sign-first")
        # backport lowers SDK words in place on the folder source and adds the fakelib folder
        if (not is_patch_job and not _resume_ok
                and getattr(item, "operation", "pack") == "pack"
                and getattr(item, "path", None) and Path(item.path).is_dir()):
            cmd += self._backport_args(item)
        cmd.append("--overwrite")
        return cmd, backend, out if out.suffix.lower() not in (".ffpfsc", ".ffpfs") else out.parent, temp

    def _run_cleanup(self, work) -> None:
        """a counter, not a flag: the batch waits for every running rmtree before reading free space"""
        with self._cleanup_lock:
            self._cleanup_inflight += 1
        def _wrapped():
            try:
                work()
            finally:
                with self._cleanup_lock:
                    self._cleanup_inflight = max(0, self._cleanup_inflight - 1)
        threading.Thread(target=_wrapped, daemon=True).start()

    # AMPR / APR (PlayGo)
    def _ampr_folder(self):
        try:
            p = self.ampr_var.get().strip()
        except Exception:
            p = ""
        return Path(p) if p and Path(p).is_dir() else None

    def _ensure_ampr_folder(self) -> bool:
        if self._ampr_folder():
            return True
        result = [False]
        win = MessageWindow(self.root)
        win.geometry("560x400")
        win.title("AMPR Emu Files Needed")
        win.resizable(False, False)
        ctk.CTkLabel(win, text="AMPR emu folder not set",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=WHITE).pack(padx=28, pady=(22, 4))
        ctk.CTkLabel(win,
                     text="This APR (PlayGo) game needs two emu files to boot after compression:\n"
                          "  • libSceAmpr.sprx\n"
                          "  • libScePlayGo.sprx\n\n"
                          "Point to the folder that contains both. They are copied into a\n"
                          "fakelib/ folder inside the game before packing, and an\n"
                          "ampr_emu.index is built. (Stored in Settings - asked only once.)",
                     font=ctk.CTkFont(size=12), text_color=MUTED, justify="left").pack(padx=28, pady=(0, 14))
        path_var = tk.StringVar(value="")
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=28, pady=(0, 16))
        ctk.CTkEntry(row, textvariable=path_var, width=300,
                     placeholder_text="Folder containing libSceAmpr.sprx…").pack(side="left", padx=(0, 8))
        def _browse():
            from tkinter import filedialog
            chosen = filedialog.askdirectory(title="Select AMPR Emu Folder")
            if chosen:
                path_var.set(chosen)
        self._button(row, "Browse", _browse, width=80, height=32).pack(side="left")
        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(pady=(0, 22))
        def _confirm():
            p = path_var.get().strip()
            if p:
                self.ampr_var.set(p)
                save_settings({"ampr_folder": p})
                result[0] = True
            win.destroy()
        self._button(btns, "Confirm & Continue", _confirm, green=True, width=190, height=36).pack(side="left", padx=(0, 10))
        self._button(btns, "Skip (no AMPR)", win.destroy, width=140, height=36).pack(side="left")
        for seq in ("<Return>", "<KP_Enter>"):
            win.bind(seq, lambda e: _confirm())
        win.grab_set()
        self.root.wait_window(win)
        return result[0]

    def _inject_ampr_files(self, item) -> None:
        """remembers what it injected so a user's own folder can be cleaned up afterwards"""
        ampr_dir = self._ampr_folder()
        if not ampr_dir or not item.path or not getattr(item, "ampr_emu", False):
            return
        target_dir = Path(item.path) / "fakelib"
        target_dir.mkdir(exist_ok=True)
        item._ampr_injected = []
        item.ampr_injected = []        # persisted twin: survives a crash or force-quit
        for fname in AMPR_SPRX_FILES:
            src, dst = ampr_dir / fname, target_dir / fname
            if not src.exists():
                self.log("WARN", f"AMPR: {fname} not found in {ampr_dir}")
                continue
            if dst.exists():
                self.log("INFO", f"AMPR: {fname} already present - skipping injection")
                continue
            try:
                shutil.copy2(src, dst)
                item._ampr_injected.append(dst)
                item.ampr_injected.append(str(dst))
                self.log("INFO", f"AMPR: injected {fname}")
            except Exception as exc:
                self.log("WARN", f"AMPR: failed to inject {fname}: {exc}")

    def _build_ampr_index(self, item) -> None:
        """AMPRIDX3, byte-exact: header <8sIIQQQII>, records <IIQq>, FNV-1a-64 slots <QII>"""
        if not getattr(item, "ampr_emu", False) or not item.path or not Path(item.path).is_dir():
            return
        import struct as _struct
        root       = Path(item.path).resolve()
        output     = root / "ampr_emu.index"
        output_tmp = output.with_suffix(output.suffix + ".tmp")

        def _key(p):
            # runtime key: UTF-8, backslash to slash, only ASCII A-Z folded. str.lower() breaks the hash
            raw = p.replace("\\", "/").encode("utf-8")
            return bytes((b + 0x20) if 0x41 <= b <= 0x5A else b for b in raw)

        def _fnv(p):
            h = 1469598103934665603
            for b in _key(p):
                h ^= b
                h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
            return h or 1

        def _make_slots(rows):
            n = 2
            while n < len(rows) * 2:
                n <<= 1
            table = [(0, 0, 0)] * n
            mask = n - 1
            for i, (_, _, path) in enumerate(rows):
                h = _fnv(path)
                pos = h & mask
                dup = False
                while table[pos][1] != 0:
                    if table[pos][0] == h:
                        oh, oi, of_ = table[pos]
                        table[pos] = (oh, oi, of_ | 1)
                        dup = True
                    pos = (pos + 1) & mask
                table[pos] = (h, i + 1, 1 if dup else 0)
            return table

        def _write(rows):
            rec_s = _struct.Struct("<IIQq")
            slt_s = _struct.Struct("<QII")
            hdr_s = _struct.Struct("<8sIIQQQII")
            rows = sorted(rows, key=lambda r: _key(r[2]))
            blob = bytearray()
            recs = bytearray()
            for sz, mt, path in rows:
                enc = path.encode("utf-8") + b"\0"
                recs += rec_s.pack(len(blob), len(enc) - 1, sz, mt)
                blob += enc
            table = _make_slots(rows)
            p_end = hdr_s.size + len(recs) + len(blob)
            h_off = (p_end + (slt_s.size - 1)) & ~(slt_s.size - 1)
            with output_tmp.open("wb") as f:
                f.write(hdr_s.pack(b"AMPRIDX3", 3, rec_s.size, len(rows),
                                   len(blob), h_off, slt_s.size, len(table)))
                f.write(recs)
                f.write(blob)
                f.write(b"\0" * (h_off - p_end))
                for h, ip1, fl in table:
                    f.write(slt_s.pack(h, ip1, fl))
            output_tmp.replace(output)

        out_r, tmp_r = output.resolve(), output_tmp.resolve()
        _SKIP = {_key("/app0/ampr_emu.index"), _key("/app0/ampr_emu.index.tmp"),
                 _key("/app0/ampr_commands.bin"), _key("/app0/apr_emu.log")}   # emulator trace/log
        # OS junk is stripped before packing, so it stays out of the index too (same rule as MkPFS)
        try:
            _bk = backend_base_dir()
            if str(_bk) not in sys.path:
                sys.path.insert(0, str(_bk))
            from mkpfs.utils import is_ignored_name as _is_junk_name
        except Exception:
            def _is_junk_name(n: str) -> bool:
                return n in (".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX") or n.startswith("._")
        seen, rows = {}, []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted((d for d in dirnames if not _is_junk_name(d)), key=str.lower)
            filenames = sorted((f for f in filenames if not _is_junk_name(f)), key=str.lower)
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    if not fpath.is_file() or fpath.resolve() in (out_r, tmp_r):
                        continue
                    ipath = "/app0/" + fpath.relative_to(root).as_posix()
                    ikey = _key(ipath)
                    if ikey in _SKIP or ikey in seen:
                        continue
                    seen[ikey] = ipath
                    st = fpath.stat()
                    rows.append((st.st_size, int(st.st_mtime), ipath))
                except Exception as exc:
                    self.log("WARN", f"AMPR index: skipping {fpath.name}: {exc}")
        try:
            _write(rows)
            item._ampr_index_path = output
            item.ampr_index_path = str(output)   # persisted twin for crash recovery
            self.log("INFO", f"AMPR: built index → ampr_emu.index  ({len(rows):,} files)")
        except Exception as exc:
            try:
                output_tmp.unlink(missing_ok=True)   # never leave a half-written .tmp to be packed
            except Exception:
                pass
            self.log("WARN", f"AMPR: index build failed: {exc}")

    def _fake_sign_folder_inproc(self, folder: Path) -> None:
        """in-process on purpose: signing must finish before the index is built"""
        backend = backend_base_dir()
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        try:
            import fake_sign as _fs
        except Exception as e:
            self.log("ERROR", f"Fake-sign before AMPR failed to load: {e}")
            return
        self.log("INFO", f"Fake-signing {folder.name} before building the AMPR index…")
        _fs.fake_sign_tree(str(folder), log=lambda m: self.log("INFO", m))

    def _prepare_ampr(self, item) -> None:
        if not getattr(item, "ampr_emu", False) or not getattr(item, "path", None):
            return
        game = Path(item.path)
        self.log("INFO", f"AMPR: {item.name} is a PlayGo/APR title - preparing emu files.")
        # releases often ship the emu already, then there is nothing to inject
        shipped = [f for f in AMPR_SPRX_FILES if (game / "fakelib" / f).is_file()]
        shipped_index = (game / "ampr_emu.index").is_file()
        if len(shipped) == len(AMPR_SPRX_FILES):
            self.log("INFO", "AMPR: the game ships its own fakelib/ emu files"
                             + (" and ampr_emu.index" if shipped_index else "") + " - no emu folder needed.")
            want_sign = bool(self.fake_sign_before_pack_var.get()) and game.is_dir()
            if want_sign and shipped_index and not getattr(item, "_from_archive", False):
                # signing would stale the shipped index, and the user's own folder is never rewritten
                self.log("WARN", "AMPR: fake-sign-before-pack skipped for this game - it ships an "
                                 "ampr_emu.index that must match its files, and the source is your own folder.")
                want_sign = False
            if want_sign:
                self._fake_sign_folder_inproc(game)
            if want_sign or not shipped_index:
                self.log("INFO", "AMPR: rebuilding ampr_emu.index after fake-signing (AMPRIDX3)." if shipped_index
                                 else "AMPR: no ampr_emu.index shipped - building one (AMPRIDX3).")
                self._build_ampr_index(item)
            else:
                self.log("INFO", "AMPR: keeping the shipped ampr_emu.index as-is.")
            return
        if shipped:
            missing = [f for f in AMPR_SPRX_FILES if f not in shipped]
            self.log("INFO", f"AMPR: the game ships {', '.join(shipped)} but not {', '.join(missing)} - "
                             f"the emu folder supplies the rest.")
        if not self._ensure_ampr_folder():
            self.log("WARN", "AMPR: no emu folder set - packing WITHOUT AMPR support; this "
                             "APR title may not boot until you set the folder in Settings.")
            return
        # sign first so the index records the final sizes. the shims come signed already
        if self.fake_sign_before_pack_var.get() and Path(item.path).is_dir():
            self._fake_sign_folder_inproc(Path(item.path))
        self._inject_ampr_files(item)
        self._build_ampr_index(item)

    def _ampr_cleanup(self, item) -> None:
        """restores a user's own folder. archive unpacks live in temp and go with the teardown"""
        if item is None or getattr(item, "_from_archive", False):
            return
        injected = getattr(item, "_ampr_injected", None) or getattr(item, "ampr_injected", None)
        idx = getattr(item, "_ampr_index_path", None) or getattr(item, "ampr_index_path", None)
        if not injected and not idx:
            return
        for p in (injected or []):
            try:
                Path(p).unlink()
            except Exception:
                pass
        try:
            fl = Path(item.path) / "fakelib"
            if fl.is_dir() and not any(fl.iterdir()):
                fl.rmdir()
        except Exception:
            pass
        if idx:
            try:
                Path(idx).unlink()
            except Exception:
                pass
        item._ampr_injected = []
        item._ampr_index_path = None
        item.ampr_injected = []
        item.ampr_index_path = None
        self.log("INFO", f"AMPR: cleaned injected emu files from {Path(item.path).name}.")

    def _oom_retry(self, item) -> bool:
        """requeue with one fewer mkpfs worker"""
        MAX_RETRIES = 2
        tries = getattr(item, "_oom_retries", 0)
        prev = getattr(item, "_cpu_retry_override", None)
        base = self.cpu_count_var.get()
        # AUTO (0) is unknown here and the backend capped it, so 1 is the only safe step down
        current = prev if prev is not None else (base if (base and base > 0) else None)
        new_cpu = max(1, (current - 1) if current is not None else 1)
        if tries >= MAX_RETRIES or (current is not None and new_cpu >= current):
            self.log("ERROR", f"Still out of memory at {new_cpu} core(s) - giving up on "
                              f"{item.name}. Try a lower compression level or smaller block size.")
            return False
        item._cpu_retry_override = new_cpu
        item._oom_retries = tries + 1
        # OOM usually hits pass 2: resume from the inner image and skip pass 1
        inner = getattr(item, "_inner_image", None)
        if not (inner and Path(str(inner)).is_file()) and self._run_took_files(self.worker):
            # pass 1 had already taken files out of the unpack: a rebuild from it would miss them
            self.log("ERROR", f"Out of memory in pass 1 after part of the unpacked copy was taken in - "
                              f"no automatic retry for {item.name}; Retry unpacks it again.")
            return False
        if inner and Path(str(inner)).is_file():
            item._resume_inner = str(inner)
            self.log("INFO", "Resuming from the already-built inner image - pass 1 is skipped "
                             "on this retry (compression only).")
        item.status = "Pending"
        # reclaim the partial scratch but KEEP the extracted source and the inner image for the retry
        self._cleanup_after_failure(item, keep_source=True)
        if item not in self.queue:
            self.queue.append(item)
        self._run_next = item
        self._active_item = item
        self.update_queue_box()
        self.log("WARN", f"Out of memory - retrying {item.name} with {new_cpu} CPU core(s) "
                         f"(attempt {tries + 1}/{MAX_RETRIES}).")
        self.status_update("Retrying", tr("Out of memory - retrying with {new_cpu} core(s)…", new_cpu=new_cpu),
                           "Retrying", 0, 0, " - ", " - ", " - ")
        self._batch_running = True   # keep the loop alive even for a single-game run
        self.root.after(800, self._batch_auto_start)
        return True

    def _cleanup_after_failure(self, item, keep_source: bool = False) -> None:
        """mkpfs tmp* dirs and this job's unpack, on the build drive, the temp folder and the output spool"""
        self._ampr_cleanup(item)  # restore a user's folder we put emu files in
        roots, seen = [], set()
        _jo = self._job_output_dir(item)
        out_str = str(_jo) if _jo else ""
        out_spool = (str(Path(out_str) / "_ffpfsc_temp") if out_str else None)
        # a build killed in pass 2 leaves <name>.ffpfsc.partial (and .tmp) in the output
        _w = getattr(self, "worker", None)
        _partial_dir = Path(str(getattr(_w, "output_dir", "") or "")) if _w is not None else None
        _partial_since = float(getattr(_w, "start_time", 0) or 0) - 2.0
        _kept_build = str(getattr(_w, "kept_build", "") or "") if _w is not None else ""
        try:
            _kept_build = str(Path(_kept_build).resolve()) if _kept_build else ""   # the backend prints it resolved
        except OSError:
            pass
        _stage_parent = None
        try:
            _src = Path(str(getattr(item, "path", "") or ""))
            if getattr(item, "operation", "") == "fpkg-build" and _src.is_dir():
                _stage_parent = _src.parent
        except Exception:
            _stage_parent = None
        _bt = getattr(item, "_build_temp", None)
        _tv = self.temp_var.get().strip()
        # the backend keeps its own scratch in <temp>/_ffpfsc_temp (chain-*, tmp*)
        _scr = [str(Path(c) / "_ffpfsc_temp") for c in (_bt, _tv) if c and Path(c).name != "_ffpfsc_temp"]
        for cand in (_bt, _tv, out_spool, *_scr):
            if not cand:
                continue
            r = Path(cand)
            try:
                key = str(r.resolve())
            except Exception:
                key = str(r)
            if key not in seen and r.exists():
                seen.add(key)
                roots.append(r)
        if not roots and self._extract_dir_for_item(item) is None and _partial_dir is None \
                and _stage_parent is None:
            return

        _tp = (self.temp_var.get() or "").strip()   # snapshot on the main thread
        _own = self._extract_dir_for_item(item)
        _own_parts = [] if keep_source else self._extract_cleanup_parts(item)
        _temp_roots = set()
        for c in (_bt, _tv, *_scr):
            try:
                if c:
                    _temp_roots.add(str(Path(c).resolve()))
            except OSError:
                pass
        def _work(tp=_tp):
            freed = 0
            for root in roots:
                try:
                    for p in root.iterdir():
                        if p.is_dir() and (is_app_tmp_dir_in(p) or p.name.startswith("ffpfsc_spool_")
                                           or (root.name == "_ffpfsc_temp" and p.name.startswith("chain-")
                                               and not p.name.startswith("chain-extras-"))   # undelivered extras stay
                                           or (not keep_source and p.name == "_ffpfsc_inner")):
                            sz = get_folder_size(p)
                            shutil.rmtree(str(p), ignore_errors=True)
                            freed += sz
                except Exception:
                    pass
            # the output's _ffpfsc_temp goes once only OS markers are left, the temp folder itself stays
            try:
                if out_spool and Path(out_spool).is_dir() and str(Path(out_spool).resolve()) not in _temp_roots:
                    junk = list(Path(out_spool).iterdir())
                    if all(q.is_file() and (is_fs_junk_name(q.name) or q.name == ".metadata_never_index") for q in junk):
                        for q in junk:
                            q.unlink(missing_ok=True)
                        os.rmdir(out_spool)          # fails when a new job put its folder in meanwhile
            except OSError:
                pass
            # fPKG tool's hard-link mirror (.ffpfsc-stage-<id>): rmtree only drops links, sources stay
            if _stage_parent is not None and _stage_parent.is_dir():
                try:
                    for p in _stage_parent.iterdir():
                        if p.is_dir() and p.name.startswith(".ffpfsc-stage-") \
                                and p.stat().st_mtime >= _partial_since:
                            shutil.rmtree(str(p), ignore_errors=True)
                except Exception:
                    pass
            # hidden work folder of a cut-short extract: .<name>.partial-XXXXXXXX or .new-XXXXXXXX
            if _partial_dir is not None:
                for d in {_partial_dir, _partial_dir.parent}:
                    try:
                        for p in d.iterdir():
                            if (p.is_dir() and not p.is_symlink() and p.name.startswith(".")
                                    and re.search(r"\.(partial|new)-[a-z0-9_]{8}$", p.name)
                                    and p.stat().st_mtime >= _partial_since):
                                sz = get_folder_size(p)
                                shutil.rmtree(str(p), ignore_errors=True)
                                freed += sz
                    except OSError:
                        pass
            if _partial_dir is not None and _partial_dir.is_dir():
                # a cut-short copy leaves .<name>.copy-<hex>.copy-tmp beside its target, maybe in a title folder
                try:
                    _lvl = [_partial_dir]
                    for _depth in range(3):
                        _next = []
                        for d in _lvl:
                            for p in d.iterdir():
                                if (p.name.startswith(".") and re.search(r"\.copy-[0-9a-f]{8}\.copy-tmp$", p.name)
                                        and p.stat().st_mtime >= _partial_since):
                                    sz = get_folder_size(p) if p.is_dir() else p.stat().st_size
                                    shutil.rmtree(str(p), ignore_errors=True) if p.is_dir() else p.unlink()
                                    freed += sz
                                elif p.is_dir() and not p.is_symlink() and _depth < 2:
                                    _next.append(p)
                        _lvl = _next
                except Exception:
                    pass
                try:
                    for p in _partial_dir.iterdir():
                        n = p.name.lower()
                        if _kept_build and str(p.resolve()) in (_kept_build, _kept_build + ".tmp"):
                            continue          # finished, only its rename failed: it stays for the user

                        if p.is_file() and (n.endswith(".partial") or n.endswith(".partial.tmp")) \
                                and p.stat().st_mtime >= _partial_since:
                            sz = p.stat().st_size
                            p.unlink()
                            freed += sz
                except Exception:
                    pass
            if not keep_source:
                try:
                    own = _own   # temp/_extracted OR output/_ffpfsc_extract
                    if own is not None and _own_parts:
                        freed += sum(_remove_scratch(p) for p in _own_parts)
                        if own.parent.name == "_ffpfsc_extract":
                            try:
                                if not any(own.parent.iterdir()):
                                    own.parent.rmdir()
                            except Exception:
                                pass
                except Exception:
                    pass
            if freed:
                self.log("INFO", f"Cleaned {format_size(freed)} of scratch from the failed/skipped run.")
            self._reclaim_temp_base_inline(tp)

        self._run_cleanup(_work)

    # auto-clear temp
    @staticmethod
    def _backup_is_users(d: Path) -> bool:
        """holds a user's originals: its record names a folder outside our scratch, even one on an absent drive"""
        try:
            pb = _patch_backup_module()
            for rec in d.glob("*/restore.jsonl"):
                game, _ops = pb._entries(rec.parent)
                if game and "_ffpfsc_temp" not in Path(game).parts and "_extracted" not in Path(game).parts:
                    return True
            return False
        except Exception:
            return True                           # unsure: keep it

    def _restore_count(self, step: int) -> None:
        """touched by the main thread and cleanup threads alike, hence the lock"""
        with self._cleanup_lock:
            self._restores_inflight = max(0, self._restores_inflight + step)

    @staticmethod
    def _patch_backup_dirs(bases) -> set:
        """patch backups hold a user's original game files: no sweep or Clear Temp may delete them"""
        out: set = set()
        for base in bases:
            if not base:
                continue
            b = Path(base)
            for root in {b if b.name == "_ffpfsc_temp" else b / "_ffpfsc_temp"}:
                try:
                    for d in root.glob("patch-backup-*"):
                        if d.is_dir() and not d.is_symlink() and App._backup_is_users(d):
                            out.add(d.resolve())
                except OSError:
                    pass
        return out

    def _recover_patch_backups(self, bases) -> set:
        """puts a crashed run's backups back into the user's folders, keeps what fails. no Tk"""
        held: set = set()
        try:
            pb = _patch_backup_module()
        except Exception:
            return held
        for base in bases:
            if not base:
                continue
            for root in {Path(base) if Path(base).name == "_ffpfsc_temp" else Path(base) / "_ffpfsc_temp"}:
                try:
                    cands = [d for d in root.glob("patch-backup-*/*") if (d / "restore.jsonl").is_file()]
                except OSError:
                    continue
                for d in cands:
                    try:
                        game, _ops = pb._entries(d)
                    except Exception:
                        game = None
                    if not game or not Path(game).is_dir() or "_ffpfsc_temp" in Path(game).parts \
                            or "_extracted" in Path(game).parts:
                        continue  # our own copy, or the folder is gone
                    try:
                        n = pb.restore_originals(d, Path(game))
                        self.log("INFO", f"An interrupted patch job: its patch was taken back out of {game} "
                                         f"({n} change(s) undone).")
                        shutil.rmtree(d.parent, ignore_errors=True)
                    except Exception as e:
                        self.log("ERROR", f"An interrupted patch job left originals for {game} in {d}, and they "
                                          f"could not be put back ({e}). They stay there.")
                        try:
                            held.add(d.parent.resolve())
                        except OSError:
                            held.add(d.parent)
        return held

    def _offer_startup_sweep(self):
        if getattr(self, "_browser_only", False):
            return  # browser-only launch: no prompt on the hidden window
        NAMES = ("_extracted", "_ffpfsc_extract", "_ffpfsc_temp", "_ffpfsc_inner")
        keep = self._open_extracts()          # copies queued jobs still read
        # Tk variables are read on the main thread only; the scan thread gets a snapshot.
        bases0 = [self.temp_var.get().strip(), self.output_var.get().strip()]
        try:
            bases0 += [str(p) for p in self._temp_pool_dirs()]   # extra scratch SSDs too
        except Exception:
            pass

        def _scan():
            targets, seen = [], set()
            bases = list(bases0)
            # crashed patch on a user's folder: originals go back first,
            # never offer to delete a backup that could not be restored
            try:
                self._recover_patch_backups(bases0)
            finally:
                self._restore_count(-1)   # raised before the thread
            keep.update(self._patch_backup_dirs(bases0))   # whatever could not be restored stays
            # every output in the history is a folder we wrote to, its parent may hold an old _ffpfsc_temp
            try:
                hist = load_history() or []
                hist_bases: set[str] = set()
                for h in hist[-100:]:
                    out = str((h or {}).get("output") or "").strip()
                    if not out:
                        continue
                    op = Path(out)
                    # temp may sit next to the .ffpfsc or one level up (per-title folder)
                    for cand in (op, op.parent):
                        try:
                            s = str(cand.resolve())
                        except Exception:
                            s = str(cand)
                        hist_bases.add(s)
                bases += sorted(hist_bases)
            except Exception:
                pass
            keep.update(self._patch_backup_dirs(bases))     # the history's drives too
            base_seen: set[str] = set()
            for base_str in bases:
                if not base_str:
                    continue
                try:
                    bk = str(Path(base_str).resolve())
                except Exception:
                    bk = base_str
                if bk in base_seen:
                    continue
                base_seen.add(bk)
                base = Path(base_str)
                if not base.exists():
                    continue
                try:
                    for p in base.iterdir():
                        if p.is_dir() and (is_app_tmp_dir_in(p) or p.name in NAMES):
                            for q in removable_parts(p, keep):
                                try:
                                    key = str(q.resolve())
                                except Exception:
                                    key = str(q)
                                if key not in seen:
                                    seen.add(key)
                                    targets.append(q)
                except Exception:
                    continue
            total = 0
            for p in targets:
                try:
                    total += get_folder_size(p)
                except Exception:
                    pass
            if targets and total >= 64 * 1024**2 and not self._job_active():
                self.root.after(0, lambda: self._prompt_startup_sweep(targets, total))

        self._restore_count(+1)          # the scan restores interrupted patches first: Start waits
        threading.Thread(target=_scan, daemon=True).start()

    def _prompt_startup_sweep(self, targets, total):
        try:
            if self._job_active():
                self.log("INFO", "Startup sweep skipped - a job is already working in those folders.")
                return
            n = len(targets)
            listing = "\n".join(f"  • {p}" for p in targets[:8]) + ("\n  • …" if n > 8 else "")
            msg = (tr("Found ~{total} of leftover working data from a previous run in {n} "
                     "folder(s):\n\n{listing}\n\nDelete it now to reclaim the space?", total=format_size(total), n=n, listing=listing))
            if not ask_confirm("Reclaim leftover temp space?", msg, "Delete", danger=True):
                return

            def _work():
                freed = 0
                for p in targets:
                    if self._job_active():   # a job started while the prompt was open
                        break
                    try:
                        sz = get_folder_size(p)
                        shutil.rmtree(str(p), ignore_errors=True)
                        freed += sz
                    except Exception:
                        pass
                if freed:
                    self.log("OK", f"Startup sweep: reclaimed {format_size(freed)} of leftover scratch.")
            self._run_cleanup(_work)
            self.log("INFO", "Reclaiming leftover scratch in the background…")
        except Exception:
            pass

    def _auto_clear_temp(self, done_item=None):
        """after a successful build, spares what a queued job still reads"""
        tp = self.temp_var.get().strip()
        if not tp:
            return
        temp_dir = Path(tp)
        if not temp_dir.exists():
            return
        freed = 0
        errors = 0
        keep = self._open_extracts(exclude=done_item)
        for p in [q for c in list(temp_dir.iterdir()) for q in removable_parts(c, keep)]:
            # only our own working data, never anything else a user keeps in the temp folder
            if not (is_app_tmp_dir_in(p)
                    or p.name in ("_extracted", "_patch_game", "_patch_files", "_ffpfsc_inner")):
                continue
            try:
                sz = get_folder_size(p) if p.is_dir() else (p.stat().st_size if p.is_file() else 0)
                if p.is_dir():
                    shutil.rmtree(str(p), ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                freed += sz
            except Exception:
                errors += 1
        msg = tr("Auto-cleared temp: freed {freed}", freed=format_size(freed))
        if errors:
            msg += tr(" ({errors} item(s) could not be removed)", errors=errors)
        self.log("OK", msg)

    # batch auto-advance
    def _update_batch_counter(self):
        if not self._batch_running:
            self.batch_counter_var.set("")
            return
        current = self._batch_done + self._batch_failed + 1
        extra = "".join(f"  ·  {n} {w}" for n, w in ((self._batch_done, "done"), (self._batch_failed, "failed")) if n)
        frac, eta = getattr(self, "_all_frac", None), getattr(self, "_all_eta", None)
        if frac is not None:
            extra += tr("  ·  {v} % of all", v=int(frac * 100)) + (f", {self._fmt_left(eta)}" if eta is not None else "")
        line = f"Job {current} of {self._batch_total}{extra}"
        if getattr(self, "_pause_requested", False):
            line += "  ·  pauses after this job"
        if self.batch_counter_var.get() != line:
            self.batch_counter_var.set(line)

    def _job_output_dir(self, item) -> Path | None:
        """the job's own output snapshot first: space and placement must check THIS drive"""
        op = getattr(item, "output_path", None)
        if op:
            try:
                return Path(str(op))
            except Exception:
                pass
        g = (self.output_var.get() or "").strip()
        return Path(g) if g else None

    def _note_space_skip(self, item, short) -> None:
        name = getattr(item, "display_name", None) or item.name
        if short is None:
            item._skip_reason = "not enough free space"
            self.log("WARN", f"Low space: {name} skipped (policy: skip).")
            return
        label, folder, need, free = short
        kind = "output" if label.startswith("Output") else "temp"
        drive = _drive_name(folder)
        item._skip_reason = tr("{kind} drive full: {drive} needs ~{need}, {free} free", kind=kind, drive=drive, need=format_size(need), free=format_size(free))
        self.log("WARN", f"{kind.capitalize()} drive full: {name} skipped, {drive} needs ~{format_size(need)} "
                         f"and has {format_size(free)} free (policy: skip).")
        to = getattr(item, "chain_to", None) or {"fpkg-build": "pkg", "copy": "copy"}.get(
            getattr(item, "operation", "pack")) or ("ffpfsc" if getattr(item, "output_compressed", True) else "ffpfs")
        what = (" for the copy" if to in ("copy", "organize") else " for the folder" if to == "folder"
                else tr(" for the .{to}", to=to)) if kind == "output" else ""
        line = (tr("{name} was skipped. {v} needs about {need}{what} and has {free} free.", name=name, v=drive[:1].upper() + drive[1:], need=format_size(need), what=what, free=format_size(free)))
        win = getattr(self, "_space_skip_win", None)
        try:
            if win is None or not win.winfo_exists():
                win = self._space_skip_win = SpaceSkipWindow(self.root)
            win.add(line, kind)
        except tk.TclError:
            pass

    def _space_gate(self, item, out_dir):
        """place the run by its REAL footprint, then go/skip/cancel against that drive"""
        op = getattr(item, "operation", "pack")
        # unpack, fPKG extract and fake-sign build no image, no space gate needed
        if op in ("unpack", "fake-sign", "fpkg-extract", "ps4-keystone"):
            return "proceed"
        # a chain to a folder writes no inner image either
        if op == "chain" and getattr(item, "chain_to", None) == "folder":
            return "proceed"
        out_dir = Path(out_dir)
        item._skip_reason = ""
        self._probe_pkg_content(item)          # a .pkg holds more than its file size
        try:
            self._resolve_extract_root(item)   # the single place that picks the build drive
        except Exception as e:
            self.log("WARN", f"Placement failed ({e}); using temp.")
        temp_dir = Path(getattr(item, "_build_temp", None)
                        or (self.temp_var.get().strip() or str(Path.home())))
        try:
            ok = _space_preflight_ok(item, temp_dir, out_dir)
        except Exception as e:
            self.log("WARN", f"Space pre-check skipped: {e}")
            return "proceed"

        def _ask():
            diag = SpaceDiagnosticsDialog(self.root, item, temp_dir, out_dir)
            self.root.wait_window(diag)
            choice = getattr(diag, "choice", "proceed" if diag.proceed else "cancel")
            if choice == "skip":
                self._note_space_skip(item, space_shortfall(item, temp_dir, out_dir))
            return choice

        if ok:
            return _ask() if self.show_space_dialog_var.get() else "proceed"
        policy = (load_settings().get("low_space_policy", "ask") or "ask").lower()
        if policy == "auto":
            self.log("WARN", f"Low space - proceeding anyway (policy: auto): {item.name}")
            return "proceed"
        if policy == "skip":
            self._note_space_skip(item, space_shortfall(item, temp_dir, out_dir))
            return "skip"
        return _ask()

    _TERMINAL_STATUSES = ("Done", "Failed", "Skipped", "Cancelled")

    def _has_pending(self) -> bool:
        return any(getattr(it, "status", "") not in self._TERMINAL_STATUSES for it in self.queue)

    def _running_item(self):
        it = getattr(self, "_active_item", None)
        return it if (self._batch_running and it is not None and it in self.queue) else None

    def _next_pending(self):
        """the current job (re-entry after its unpack), else Retry's pick, else the first one waiting"""
        act = getattr(self, "_active_item", None)
        if (self._batch_running and act is not None and act in self.queue
                and getattr(act, "status", "") not in self._TERMINAL_STATUSES):
            return act
        pref = getattr(self, "_run_next", None)
        if pref is not None and pref in self.queue and getattr(pref, "status", "") not in self._TERMINAL_STATUSES:
            return pref
        return next((it for it in self.queue if getattr(it, "status", "") not in self._TERMINAL_STATUSES), None)

    def _pick_next(self):
        item = self._next_pending()
        if item is not None:
            self._active_item = item
            if getattr(self, "_run_next", None) is item:
                self._run_next = None
            self.__dict__.setdefault("_job_t0", {}).setdefault(id(item), time.time())
            if getattr(self, "_active_since", (None, 0))[0] != id(item):
                self._active_since = (id(item), time.time())     # kept over the unpack's re-entry
        return item

    def _extract_is_complete(self, item) -> bool:
        """a job cancelled mid-extract has only half a copy"""
        own = self._extract_dir_for_item(item)
        if own is None or not own.exists() or getattr(item, "archive_path", None):
            return False
        src = getattr(item, "path", None)
        return bool(src) and Path(str(src)).exists()

    _RETRYABLE = ("Failed", "Cancelled", "Skipped")

    def _retry_failed(self):
        """skipped and stopped jobs stay as they are"""
        if self._batch_running:
            return
        n = 0
        for it in [x for x in self.queue if getattr(x, "status", "") == "Failed"]:
            self._rearm_from_archive(it)
            if self._copy_lacks_files(it):
                continue
            it.status = "Pending Extract" if getattr(it, "archive_path", None) else "Queued"
            it.status_note = ""
            n += 1
        if n:
            self.update_queue_box()
            self.log("INFO", f"Retry failed: {n} job(s) run again.")
            self.start(rearm_failed=False)

    def _retry_selected(self):
        item = getattr(self, "_details_item", None)
        if item is not None and item in self.queue:
            self._retry_job(item)

    def _retry_job(self, item) -> None:
        """from the kept copy, else from the archive. joins the waiting jobs while the queue runs"""
        if item not in self.queue or getattr(item, "status", "") not in self._RETRYABLE:
            return
        self._rearm_from_archive(item)
        if self._copy_lacks_files(item):
            self.update_queue_box(select_item=item)
            return
        item.status = "Pending Extract" if getattr(item, "archive_path", None) else "Queued"
        item.status_note = ""
        name = getattr(item, "display_name", None) or item.name
        if self._batch_running:
            self._run_next = item
            self.log("INFO", f"Retry: {name} runs after the current job.")
            self.update_queue_box(select_item=item)
            return
        self._run_next = item                  # runs next; the job keeps its place in the list
        self.update_queue_box(select_item=item)
        self.log("INFO", f"Retry: {name}.")
        self.start(rearm_failed=False)

    @staticmethod
    def _source_key(path) -> str:
        """a RAR set's first part, else the resolved path. case-insensitive on purpose"""
        if not path:
            return ""
        try:
            p = Path(str(path))
            if p.is_file() and p.suffix.lower() in (".rar",):
                p = ArchiveExtractor._first_volume(p)
            return str(p.resolve()).lower()
        except OSError:
            return str(path).lower()

    _TITLE_ID_IN_NAME = re.compile(r"\b(PPSA\d{5}|CUSA\d{5}|UP\d{4}|EP\d{4})\b", re.I)

    @classmethod
    def _title_id_from_path(cls, path) -> str:
        """PPSA/CUSA id from the name or any parent folder, upper-case, '' if none"""
        for part in reversed(Path(str(path) if path else "").parts):
            m = cls._TITLE_ID_IN_NAME.search(part or "")
            if m:
                return m.group(1).upper()
        return ""

    @staticmethod
    def _item_title_id(item) -> str:
        tid = str(getattr(item, "title_id", "") or getattr(item, "archive_title_id", "") or "").strip().upper()
        return tid if tid.startswith(("PPSA", "CUSA")) else ""

    def _known_signatures(self, include_history: bool = True) -> tuple[set[str], set[str]]:
        """what Rescan matches against: (resolved paths, title ids)"""
        paths: set[str] = set()
        tids: set[str] = set()
        for it in self.queue:
            for cand in (getattr(it, "archive_path", None), getattr(it, "origin_archive", None),
                         getattr(it, "path", None)):
                k = self._source_key(cand)
                if k:
                    paths.add(k)
                tid = self._title_id_from_path(cand)
                if tid:
                    tids.add(tid)
            # path and archive_path are None after a done run's cleanup, the labels still carry the id
            for label in (getattr(it, "display_name", None), getattr(it, "name", None),
                          getattr(it, "archive_title", None)):
                tid = self._title_id_from_path(label)
                if tid:
                    tids.add(tid)
            tid = self._item_title_id(it)
            if tid:
                tids.add(tid)
        try:
            for row in (load_history() or []) if include_history else []:
                for field in ("source", "origin_archive", "input", "output"):
                    k = self._source_key(row.get(field))
                    if k:
                        paths.add(k)
                    tid = self._title_id_from_path(row.get(field))
                    if tid:
                        tids.add(tid)
                tid = str(row.get("title_id") or "").strip().upper()
                if tid.startswith(("PPSA", "CUSA")):
                    tids.add(tid)
        except Exception:
            pass
        return paths, tids

    @staticmethod
    def _output_already_has(out_root, tid: str) -> bool:
        """cheap: one iterdir at the root, plus the children of plain folders"""
        if not (out_root and tid):
            return False
        try:
            root = Path(out_root)
            if not root.is_dir():
                return False
            tag = f"[{tid}]".lower()
            for p in root.iterdir():
                if tag in p.name.lower():
                    return True
                if p.is_dir() and not p.suffix:
                    try:
                        for q in p.iterdir():
                            if tag in q.name.lower() and q.suffix.lower() in (".ffpfs", ".ffpfsc", ".pkg", ".exfat"):
                                return True
                    except OSError:
                        continue
        except OSError:
            return False
        return False

    def rescan_last_source(self) -> None:
        """reuses the last Add job's settings, no editor. runs on a worker"""
        settings = load_settings()
        last_dir = native_path((settings.get("last_source_dir") or "").strip())
        last = (settings.get("last_source") or "").strip()
        if not (last_dir or last):
            messagebox.showinfo("Rescan", "No source to rescan yet. Add a job once, then Rescan "
                                           "finds new downloads in that folder.")
            return
        # a folder source is a single game, so Rescan looks at its parent
        folder = Path(last_dir) if last_dir else (Path(last).parent if Path(last).is_file() else Path(last).parent)
        if not folder.is_dir():
            messagebox.showerror("Rescan", tr("The last source folder is gone:\n{folder}", folder=folder))
            return
        tpl = (settings.get("rescan_template") or {})
        if not tpl.get("to"):
            messagebox.showinfo("Rescan", "Add a job once with the output and the changes you want, "
                                           "then Rescan reuses those for new sources in the same folder.")
            return

        cleanup = tpl.get("after_source") in ("trash", "move", "delete")
        # with a clean-up chosen, known games are queued anyway so their sources get cleaned up
        known_paths, known_tids = self._known_signatures(include_history=not cleanup)
        out_root = None if cleanup else ((tpl.get("output") or "").strip() or None)
        self.log("INFO", f"Rescan: looking for new sources in {folder}…")
        self.status_update("Scanning", f"Rescan: reading {folder}…", "Scanning Files",
                           0, 0, "00:00", " - ", " - ", side=True)

        passwords = self._candidate_passwords()

        def work():
            try:
                found = find_job_sources(folder)
            except Exception as e:
                self.scan_q.put(("rescan-error", str(e)))
                return
            # plan on everything found, so an add-on never shows up as new once its game is in
            try:
                pairs, _addons = plan_releases(folder, found, passwords=passwords, pkg_role=self._pkg_role)
            except Exception:
                pairs = [(p, None) for p in found]
            bundles = {str(p): b for p, b in pairs if b}
            new, skipped = [], 0
            for p, _b in pairs:
                if self._source_key(p) in known_paths:
                    skipped += 1
                    continue
                tid = self._title_id_from_path(p)
                if tid and (tid in known_tids or self._output_already_has(out_root, tid)):
                    skipped += 1
                    continue
                new.append(p)
            self.scan_q.put(("rescan-found", {"folder": str(folder), "sources": new, "tpl": tpl,
                                              "scanned": len(found), "skipped": skipped,
                                              "bundles": {k: v for k, v in bundles.items()
                                                          if k in {str(x) for x in new}}}))
        self._launch_scan(work)

    def _rescan_make(self, tpl: dict):
        """a make(source) like JobDialog._add, built from the saved Add job settings"""
        to = str(tpl.get("to") or "ffpfsc")
        out = str(tpl.get("output") or "")
        sign = bool(tpl.get("sign"))
        target = tpl.get("backport_target") or None
        pkg_params = tpl.get("pkg_params") or {}
        ff_level = int(tpl.get("ff_level") or 7)
        organize = bool(tpl.get("organize"))
        after = tpl.get("after_source") if tpl.get("after_source") in _after_job_module().ACTIONS else "keep"
        after_dir = tpl.get("after_move_to") or None
        root = str(tpl.get("_rescan_root") or "") or None

        table = auto_actions((load_settings() or {}).get("auto_actions")) if to == AUTO_TARGET else None
        passwords = self._candidate_passwords() if to == AUTO_TARGET else None

        def build(src, to):
            it = GameItem.from_chain(src, to=to, output_path=out or None, sign=sign and to != "pkg",
                                     patch_source=None, backport_target=target,
                                     backport_libs_root=None)
            if to == "pkg":
                self._apply_fpkg_params(it, pkg_params or self.fpkg_defaults)
            it.compression_level = ff_level if to == "ffpfsc" else None
            it.auto_organize = organize
            return it

        def make(src):
            if to == ORGANIZE_TARGET:
                it = self._organize_item_for(src, output_path=out or None)
            elif to == AUTO_TARGET:
                it = self._auto_item_for(src, table, build, output_path=out or None, passwords=passwords)
                it.auto_sign = sign
            else:
                it = build(src, to)
            it.after_source, it.after_move_to = after, after_dir
            it.source_root = root
            return it
        return make

    def _add_jobs_async(self, sources, make, plan=None) -> None:
        """built on a worker, handed to the main loop one by one. Start waits for the last"""
        if not sources:
            return
        st = self.__dict__.setdefault("_add_state", {"total": 0, "done": 0, "made": [], "errors": 0})
        st["total"] += len(sources)
        with self._scan_lock:
            self._scan_in_flight += 1
        q = self.__dict__.setdefault("_add_q", queue.Queue())

        def work():
            try:
                pairs = [(s, None) for s in sources]
                if plan is not None:
                    try:
                        pairs, addons = plan(list(sources))
                        if addons:
                            q.put(("addons", None, addons, ""))
                    except Exception as e:
                        q.put(("note", None, None, tr("Could not sort the sources by download folder ({e}); "
                                                      "each source gets a job of its own.", e=e)))
                for s, bundle in pairs:
                    try:
                        it = make(s)
                        if bundle:
                            it.bundle_siblings = [Path(e) for e in bundle["extras"]]
                            it.bundle_release = bundle["release"]
                        q.put(("item", it, s, ""))
                    except Exception as e:
                        q.put(("error", None, s, str(e)))
            finally:
                q.put(("done", None, None, ""))
        threading.Thread(target=work, daemon=True).start()
        self._show_add_progress()

    def _estimate_left(self, left_bytes: int, cur_bytes: int, cur_pct: float, cur_item,
                       cur_left: float | None = None) -> float | None:
        """Seconds the whole run still needs."""
        now = time.time()
        if cur_left is not None:
            fin_s, fin_b = getattr(self, "_batch_fin_secs", 0.0), getattr(self, "_batch_fin_bytes", 0)
            t0 = (getattr(self, "_job_t0", {}) or {}).get(id(cur_item)) if cur_item is not None else None
            if fin_b > 0 and fin_s > 0:
                pace = fin_s / fin_b
            elif t0 and cur_bytes > 0:
                pace = ((now - t0) + cur_left) / cur_bytes
            else:
                pace = 0.0
            return max(0.0, cur_left + left_bytes * pace)
        if now - getattr(self, "_batch_t0", now) < 90:
            return None
        rest = left_bytes + cur_bytes * max(0.0, 1.0 - cur_pct / 100.0)
        fin_s, fin_b = getattr(self, "_batch_fin_secs", 0.0), getattr(self, "_batch_fin_bytes", 0)
        if fin_b > 0 and fin_s > 0:
            pace = fin_s / fin_b
        else:
            t0 = (getattr(self, "_job_t0", {}) or {}).get(id(cur_item)) if cur_item is not None else None
            done = cur_bytes * cur_pct / 100.0
            if not t0 or cur_pct < 2 or done <= 0:
                return None
            pace = (now - t0) / done
        return max(0.0, rest * pace)

    def _job_phases(self, item) -> list:
        op = getattr(item, "operation", "pack")
        arc = bool(getattr(item, "archive_path", None) or getattr(item, "origin_archive", None))
        if op == "copy":
            return ["Extracting"] if arc else ["Writing Final Image"]
        if op in ("unpack", "fpkg-extract"):
            return ["Extracting"]
        to = (getattr(item, "chain_to", None) or "ffpfsc") if op == "chain" else "ffpfsc"
        if to == "folder":
            return ["Extracting"]
        if to == "ffpfs" or (op == "pack" and getattr(item, "output_compressed", True) is False):
            return ["Extracting", "Creating Temp PFS"]
        return ["Extracting", "Creating Temp PFS", "Compressing"]

    _STEP_WIDTHS = {"Extracting": 28, "Creating Temp PFS": 24, "Compressing": 34, "Writing Final Image": 97}

    def _job_time_left(self, item, stage, stage_pct, eta, size) -> float | None:
        """running step's ETA plus later steps at their learned speed (learned on steps of 30 s or more)"""
        now = time.time()
        clock = getattr(self, "_stage_clock", None)
        if clock is None or clock[0] is not item or clock[1] != stage:
            if clock is not None and clock[0] is item and clock[1] in LONG_STEPS and size:
                took = now - clock[2]
                if took >= 30:
                    old = self._stage_rates.get(clock[1])
                    new = size / took
                    self._stage_rates[clock[1]] = new if not old else 0.5 * old + 0.5 * new
                    try:
                        save_settings({"stage_rates": dict(self._stage_rates)})
                    except Exception:
                        pass
            clock = self._stage_clock = (item, stage, now)
        elapsed = now - clock[2]
        left = eta_seconds(eta)
        if left is None and stage_pct >= 2 and elapsed >= 10:
            left = elapsed * (100.0 - stage_pct) / stage_pct
        return job_time_left(self._job_phases(item), stage, left, elapsed, size, self._stage_rates,
                             self._STEP_WIDTHS)

    @staticmethod
    def _fmt_left(secs: float) -> str:
        """rounded so the number doesn't twitch on every update"""
        if secs < 90:
            return "about a minute left"
        mins = int(round(secs / 60.0))
        if mins < 60:
            return tr("about {mins} min left", mins=mins)
        mins = int(round(mins / 5.0)) * 5                  # five-minute steps beyond an hour
        h, m = divmod(mins, 60)
        return tr("about {h} h {m:02d} min left", h=h, m=m) if m else f"about {h} h left"

    def _all_jobs_line(self) -> str:
        frac = getattr(self, "_all_frac", None)
        if frac is None:
            return ""
        eta = getattr(self, "_all_eta", None)
        left = self._fmt_left(eta) if eta is not None else "estimating the time left…"
        return tr("All jobs  {v} %   ·   {left}", v=int(frac * 100), left=left)

    def _show_all_progress(self) -> None:
        try:
            frac = getattr(self, "_all_frac", None)
            if not self._batch_running or frac is None:
                if self._all_box.winfo_manager():
                    self._all_box.grid_remove()
                return
            eta = getattr(self, "_all_eta", None)
            left = tr("All jobs  {v} %", v=int(frac * 100))
            right = self._fmt_left(eta) if eta is not None else "estimating the time left…"
            if self._all_left_var.get() != left:
                self._all_left_var.set(left)
            if self._all_right_var.get() != right:
                self._all_right_var.set(right)
            self._all_bar.set(frac)
            if not self._all_box.winfo_manager():
                self._all_box.grid()
        except Exception:
            pass

    def _show_add_progress(self) -> None:
        self._show_all_progress()
        st = getattr(self, "_add_state", None)
        try:
            if not st or st["total"] == 0:
                self._add_box.grid_remove()
                return
            n, total = st["done"], st["total"]
            last = st["made"][-1] if st["made"] else None
            name = (getattr(last, "display_name", None) or getattr(last, "name", "")) if last else ""
            note = reading_note()
            self._add_label_var.set(tr("Adding jobs…  {n} of {total}", n=n, total=total)
                                    + (note or (f"   ·   {name}" if name else "")))
            self._add_bar.set(n / total if total else 0)
            self._add_box.grid()
        except Exception:
            pass

    def _drain_add_q(self) -> None:
        """main thread: takes in what the add worker has built so far"""
        q = getattr(self, "_add_q", None)
        st = getattr(self, "_add_state", None)
        if q is None or st is None:
            self._show_add_progress()
            return
        added = False
        while True:
            try:
                kind, it, src, err = q.get_nowait()
            except queue.Empty:
                break
            if kind == "item":
                self.queue.append(it)
                if self._batch_running:        # a job added while the queue runs is part of this run
                    self.__dict__.setdefault("_batch_items", []).append(it)
                    self._batch_total += 1
                    self._update_batch_counter()
                st["made"].append(it)
                st["done"] += 1
                added = True
                if getattr(it, "ps4_default_keystone", None) and not getattr(it, "ps4_keystone", None):
                    self.log("WARN", tr("{name}: the package has the default keystone, so saves of the retail "
                                        "game will show as corrupted. Tools › PS4 PKG Tools replaces it with the "
                                        "keystone exported from one of your saves.",
                                        name=it.display_name or it.name))
                if getattr(it, "ps4_version_problem", False):
                    self.log("WARN", tr("{name}: an update is merged into this full game but its app version stayed "
                                        "the game's, so the console will offer the official update. Tools › PS4 "
                                        "PKG Tools sets the version.", name=it.display_name or it.name))
                try:
                    if getattr(it, "archive_path", None):
                        self._resolve_archive_password(it)
                except Exception:
                    pass
            elif kind == "error":
                st["done"] += 1
                st["errors"] += 1
                self.log("WARN", f"Could not add {Path(str(src)).name}: {err}")
            elif kind == "addons":
                st["done"] += len(src)
                names = ", ".join(Path(str(x)).name for x in src[:6]) + (" …" if len(src) > 6 else "")
                self.log("INFO", tr("{n} add-on(s) go along with their game instead of a job of their own: {names}",
                                    n=len(src), names=names))
            elif kind == "note":
                self.log("WARN", err)
            else:
                with self._scan_lock:
                    self._scan_in_flight = max(0, self._scan_in_flight - 1)
                if st["done"] >= st["total"]:
                    made = st["made"]
                    if made:
                        what = chain_summary(made[0]) + (f" × {len(made)}" if len(made) > 1 else "")
                        self.log("OK", f"Queued: {what} - {made[0].display_name or made[0].name}"
                                       + (f" (+{len(made) - 1} more)" if len(made) > 1 else "")
                                       + ".  Press Start to run.")
                    self._add_state = {"total": 0, "done": 0, "made": [], "errors": 0}
                    if self.pending_start and self._scan_in_flight == 0:
                        self.pending_start = False
                        self.root.after(50, self.start)
        if added:
            self.update_queue_box(select_item=st["made"][-1] if st["made"] else None)
        self._show_add_progress()

    def _release_failed_copies(self, item) -> bool:
        """next job doesn't fit: drop copies failed jobs kept. a copy kept after a cancel is the user's"""
        kept = [it for it in self.queue
                if it is not item and getattr(it, "status", "") == "Failed" and getattr(it, "kept_extract", False)]
        if not kept or getattr(item, "operation", "pack") in ("unpack", "fake-sign", "fpkg-extract", "ps4-keystone"):
            return False
        try:
            self._probe_pkg_content(item)
            self._resolve_extract_root(item)
            od = self._job_output_dir(item) or Path(self.output_var.get().strip())
            temp_dir = Path(getattr(item, "_build_temp", None) or (self.temp_var.get().strip() or str(Path.home())))
            if _space_preflight_ok(item, temp_dir, Path(od)):
                return False
        except Exception:
            return False
        for it in kept:
            it.kept_extract = False
            it.status_note = (it.status_note + " " if it.status_note else "") + \
                "Its extracted copy was deleted to make room for the next job."
            self.log("INFO", f"Deleted the extracted copy of the failed job {getattr(it, 'display_name', None) or it.name}: "
                             f"{item.name} needs the space. A retry unpacks its archive again.")
            self._cleanup_item_extract(it)
        self.update_queue_box()
        return True

    def _drop_kept_extract(self, item) -> None:
        if getattr(item, "kept_extract", False):
            item.kept_extract = False
            self._cleanup_item_extract(item)

    def _due_checks(self, item) -> bool:
        """both start paths come through here: start() and _batch_auto_start()"""
        if not self._refresh_archive_set(item):
            return False
        due = self._late_output_check(item)
        if due == "cancel":
            self._batch_running = False
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Start cancelled.", "Ready", 0, 0, "00:00", " - ", " - ")
            return False
        if due == "skip":
            self._ensure_batch_started()        # the jobs after this one still run
            self._skip_late(item)
            return False
        return True

    def _refresh_archive_set(self, item) -> bool:
        """re-read when the set changed since add or the unpacked size is still unknown"""
        arc = getattr(item, "archive_path", None)
        if not arc or getattr(item, "kept_extract", False):
            return True
        first = Path(arc)
        name = getattr(item, "display_name", None) or item.name
        parts = archive_set_parts(first)
        on_disk = sum((p.stat().st_size for p in parts if p.exists()), 0)
        old_size = int(getattr(item, "size", 0) or 0)
        changed = bool(on_disk) and on_disk != old_size
        if changed:
            self.log("INFO", f"{name}: the archive set is now {len(parts)} part(s), {format_size(on_disk)} on disk "
                             f"(it was {format_size(old_size)} when the job was added).")
            item.size = on_disk
        if not changed and int(getattr(item, "extracted_size", 0) or 0) > 0 and not getattr(item, "archive_problem", ""):
            return True
        self.status_update("Reading", tr("Reading the headers of {name}…", name=first.name), "Scanning Files", 0, 0,
                           "00:00", " - ", " - ", side=True)
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        saved = [p.strip() for p in (load_settings().get("archive_passwords") or []) if str(p).strip()]
        cands = ([item.password] if getattr(item, "password", None) else []) + saved
        try:
            state, hdr, problem = ArchiveExtractor.probe_header_state(first, cands)
        except Exception as e:
            state, hdr, problem = "unknown", 0, str(e)
        if state == "open":
            item.header_locked, item.archive_problem = False, ""
            real = ArchiveExtractor.plausible_extracted_size(hdr, on_disk or old_size)
            if real and real != int(getattr(item, "extracted_size", 0) or 0):
                self.log("INFO", f"{name}: unpacks to {format_size(real)} (read from the archive's headers).")
                item.extracted_size = real
            self.update_queue_box()
            return True
        if state == "locked":
            item.header_locked = True            # the extraction step asks for the password
            return True
        if state == "damaged":
            item.archive_problem = problem
            note = tr("The archive cannot be read: {problem}", problem=problem)
            self._ensure_batch_started()        # the jobs after this one still run
            self._retire_failed(item, "Failed", note)
            self.log("ERROR", f"{name}: not started. {note}. Once the set is complete, Retry runs it.")
            self._batch_failed += 1
            self._update_batch_counter()
            self.update_queue_box()
            if self._has_pending():
                self.root.after(600, self._batch_auto_start)
            else:
                self._batch_running = False
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self.status_update("Ready", tr("Not started: {note}", note=note), "Ready", 0, 0, "00:00", " - ", " - ")
                self._queue_finished()
            return False
        return True                              # unknown: the extraction step reports what it finds

    def _copy_lacks_files(self, item) -> bool:
        """files were taken out and the archive is gone: stay Failed rather than build half a game"""
        if not getattr(item, "source_taken_in", False) or getattr(item, "archive_path", None):
            return False
        item.status = "Failed"
        item.status_note = ("The unpacked copy lacks files the last run took in, and its archive is not "
                            "there to unpack it again. Bring the archive back, then Retry.")
        self.log("WARN", f"{getattr(item, 'display_name', None) or item.name}: {item.status_note}")
        return True

    def _rearm_from_archive(self, item) -> bool:
        """the unpacked copy is gone: point back at the archive so the next run unpacks again"""
        origin = getattr(item, "origin_archive", None)
        if not origin or getattr(item, "archive_path", None):
            return False
        src = getattr(item, "path", None)
        try:
            # never run from a scratch copy, a failed run may be deleting it right now.
            # only a copy kept after a cancel or a source outside the scratch is reused
            if src and Path(str(src)).exists() and (self._extract_dir_for_item(item) is None
                                                     or getattr(item, "kept_extract", False)):
                return False
            if not Path(origin).exists():
                return False
        except OSError:
            return False
        arc = Path(origin)
        item.archive_path = arc
        item.path = None
        item.source_kind = "archive"
        item.files = 0
        item.bundle_siblings = self._download_extras(item)
        try:
            item.size = archive_set_ondisk_size(arc)
        except Exception:
            pass
        item.extracted_size = int(getattr(item, "origin_extracted_size", 0) or 0)
        item.pkg_content_size = 0           # read again from the package the archive yields
        item.pkg_largest_file = item.pkg_kept_bytes = 0
        self.log("INFO", f"{getattr(item, 'display_name', None) or item.name}: the extracted copy is gone; "
                         f"the job starts again from {arc.name}.")
        return True

    def _retire_failed(self, item, status: str = "Failed", reason: str = "") -> None:
        """failed jobs stay in the queue, only successful ones leave"""
        try:
            item.status = status
            item.status_note = " ".join(str(reason or "").split())[:300]
            if item not in self.queue:          # popped by an older path: back at the end
                self.queue.append(item)
        except Exception:
            pass

    def _batch_auto_start(self):
        """rechecks disk space before each game"""
        # a cancel in the 600 ms advance gap: cancel_requested is reset below, don't drop it
        if self.cancel_requested or self.extract_cancel_event.is_set():
            self._batch_running = False
            self.cancel_requested = False
            self.extract_cancel_event.clear()
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Queue stopped.", "Ready", 0, 0, "00:00", " - ", " - ")
            self.log("WARN", "Queue stopped by the user.")
            return
        # wait for reclaims or the gate sees a full drive. cap 10 min, exFAT rmtree takes its sweet time
        if getattr(self, "_restores_inflight", 0):
            self.root.after(500, self._batch_auto_start)      # a patch restore has no time limit
            return
        if getattr(self, "_cleanup_inflight", 0) > 0:
            self._cleanup_wait_ticks += 1
            if self._cleanup_wait_ticks <= 1200:  # 1200 * 500 ms = 10 min
                if self._cleanup_wait_ticks == 1:
                    self.status_update("Cleaning up", "Reclaiming temp space before the next game…",
                                        "Cleaning", 0, 0, " - ", " - ", " - ")
                self.root.after(500, self._batch_auto_start)
                return
            self.log("WARN", "Cleanup still running past the wait cap - continuing; the space gate decides.")
        self._cleanup_wait_ticks = 0
        # the running job coming back here (after its unpack or an OOM retry) is not the next job
        if (getattr(self, "_pause_requested", False) and self._next_pending() is not None
                and self._next_pending() is not getattr(self, "_active_item", None)):
            self._pause_queue()
            return
        if self._next_pending() is None:
            self._batch_running = False
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.update_queue_box()
            self._queue_finished()
            if self._batch_total > 1:
                self._show_batch_complete()
            return
        _before = getattr(self, "_active_item", None)
        item = self._pick_next()
        self._follow_next_job(_before, item)
        if not self._due_checks(item):
            return
        if self._release_failed_copies(item):
            self.root.after(500, self._batch_auto_start)   # waits for the reclaim, then gates again
            return

        # space gate: a big game that fits the HDD goes there instead of being skipped
        try:
            od = self._job_output_dir(item)
            op = str(od) if od else ""
            if op and getattr(item, "operation", "pack") != "unpack":
                gate = self._space_gate(item, od)
                bt = getattr(item, "_build_temp", None)
                if bt is not None:
                    _sz = _build_size_of(item)
                    if getattr(item, "_image_only_on_temp", False):
                        need = estimate_image_space_needed(_sz)
                        kind = tr("image on {v} (spool auto-routed)", v=temp_drive_label(Path(bt)))
                    else:
                        need = estimate_peak_space_needed(_sz, _peak_factor_for(item), same_drive(Path(bt), od))
                        kind = "full scratch"
                    self.log("INFO", f"Space check - {item.name}: {kind} on {bt} | "
                                     f"need ~{format_size(need)} | free {format_size(get_free_space(bt))} | {gate}")
                if gate == "cancel":
                    self._batch_running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self._update_batch_counter()
                    return
                if gate == "skip":
                    self._cleanup_after_failure(item)
                    self._retire_failed(item, "Skipped", getattr(item, "_skip_reason", ""))
                    self._batch_failed += 1
                    self._update_batch_counter()
                    self.update_queue_box()
                    self.root.after(600, self._batch_auto_start)
                    return
        except Exception as e:
            self.log("WARN", f"Space pre-check skipped: {e}")

        # archive game or .7z patch: resolve to folders first, then re-enter
        if self._patch_needs_prepare(item):
            self._prepare_patch_item(item)
            return
        if getattr(item, "archive_path", None):
            self._extract_queued_item(item)
            return
        _late = self._late_output_check(item)
        if _late == "cancel":
            self._cleanup_after_failure(item)
            self._batch_running = False
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            return
        if _late == "skip":
            self._skip_late(item)
            return
        # keep Spotlight off our temp, the user's source folder stays indexable
        self._mark_no_spotlight(getattr(item, "_build_temp", None))
        # AMPR emu files and index go into the game folder before packing. pack only
        if getattr(item, "ampr_emu", False) and getattr(item, "operation", "pack") == "pack":
            self._prepare_ampr(item)
        try:
            cmd, cwd, out_dir, temp_dir = self.build_command(item)
        except Exception as e:
            self.log("ERROR", f"Auto-advance build_command failed: {e}")
            self._batch_running = False
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            return
        item.status = "Running"
        item.status_note = ""
        item.kept_extract = False
        self.update_queue_box()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        current = self._batch_done + self._batch_failed + 1
        self._update_batch_counter()
        self.header_status_var.set(
            f"v{APP_VERSION}  |  Game {current}/{self._batch_total}  |  ✓{self._batch_done} ✗{self._batch_failed}"
        )
        _floor = self._archive_extract_pct(item) if getattr(item, "_from_archive", False) else 0
        if not getattr(item, "_from_archive", False):
            self._begin_job_progress(item)
        elif getattr(item, "_resume_inner", None):
            self._begin_job_progress(item, _floor)        # a resume: compression only, after the unpack share
        self.status_update(
            f"Game {current}/{self._batch_total}",
            f"Starting: {item.name}",
            "Scanning Files", 0, _floor, "00:00", " - ", " - ", job=item
        )
        self.log("INFO", f"── Batch auto-advance: game {current}/{self._batch_total} - {item.name}")
        self.cancel_requested = False
        self._active_item = item
        self._mark_source_taken_in(item, cmd)
        self.worker = CLIWorker(self, item, cmd, cwd, out_dir, temp_dir)
        self.worker.start()

    # after-job actions
    _ARCHIVE_NAME_RE = re.compile(r"\.(part\d+\.rar|rar|zip|7z|r\d{2,}|z\d{2}|7z\.\d{3}|zip\.\d{3}|\d{3})$", re.I)

    @staticmethod
    def _uniq_paths(paths) -> list:
        uniq, seen = [], set()
        for p in paths:
            key = os.path.realpath(str(p))
            if key not in seen:
                seen.add(key)
                uniq.append(p)
        return uniq

    def _after_game_sources(self, item) -> list:
        """the game the job started from, every archive part. never our extracted copy"""
        arch = getattr(item, "archive_path", None) or getattr(item, "origin_archive", None)
        if arch:
            return self._uniq_paths(archive_set_parts(Path(arch)))
        if getattr(item, "path", None) and not getattr(item, "_from_archive", False):
            return [Path(item.path)]
        return []

    def _after_patch_sources(self, item) -> list:
        patch = getattr(item, "patch_source", None)
        if not patch:
            return []
        pp = Path(patch)
        return self._uniq_paths(archive_set_parts(pp) if (pp.is_file() and self._ARCHIVE_NAME_RE.search(pp.name))
                                else [pp])

    def _after_sources(self, item) -> list:
        return self._uniq_paths(self._after_game_sources(item) + self._after_patch_sources(item))

    def _after_protected(self, item, dest=None) -> list:
        """never moved or removed as a whole: the add folder, output, destination and our own"""
        out = [str(ultra_core.APP_DIR), getattr(item, "source_root", None), dest]
        try:
            out += [self.temp_var.get().strip(), self.output_var.get().strip(),
                    str(self._job_output_dir(item) or ""), (load_settings().get("last_source_dir") or "").strip()]
        except Exception:
            pass
        out += self._app_folders()
        return [x for x in out if x]

    def _app_folders(self) -> list:
        """our profile plus the scratch under temp, temp pool and output. a source never overlaps them"""
        roots = []
        try:
            roots += [self.temp_var.get().strip(), self.output_var.get().strip()]
            roots += [str(d) for d in self._temp_pool_dirs()] + [str(x) for x in (getattr(self, "temp_pool", None) or [])]
        except Exception:
            pass
        out = [str(ultra_core.APP_DIR)]
        for r in roots:
            if r:
                out += [str(Path(r) / sub) for sub in ("_extracted", "_ffpfsc_temp", "_ffpfsc_extract")]
        t = (self.temp_var.get() or "").strip()
        if t and not os.path.ismount(t):
            out.append(t)
        return out

    def _after_job_plan(self, item, worker):
        """(action, sources, destination, reason to leave them) for a Done job"""
        aj = _after_job_module()
        act = getattr(item, "after_source", None) or aj.KEEP
        if act not in aj.ACTIONS or act == aj.KEEP:
            return aj.KEEP, [], None, None
        ps4 = getattr(item, "content_kind", "") in ("ps4", ORGANIZE_TARGET)   # library jobs (see _ps4_copy_mode)
        if getattr(item, "operation", "") in ("fake-sign", "copy", "ps4-keystone") and not ps4:
            return aj.KEEP, [], None, None          # these work on the source themselves
        if ps4 and act == aj.DELETE and not getattr(item, "origin_archive", None):
            return aj.KEEP, [], None, None          # the packages were moved (see _ps4_copy_mode)
        if getattr(worker, "_is_copy", False) and act == aj.DELETE and not ps4:
            return aj.KEEP, [], None, None          # the copy already moved it (--copy-mode move)
        dest = (getattr(item, "after_move_to", None) or None) if act == aj.MOVE else None
        if act == aj.MOVE and not dest:
            return act, [], None, "no destination folder is set"
        if getattr(worker, "validate_failed", False):
            return act, [], dest, "the package's checklist reported failures"
        out = (getattr(worker, "output_path", "") or "").strip('"')
        try:
            ok = bool(out) and (Path(out).is_dir() or Path(out).stat().st_size > 0)
        except OSError:
            ok = False
        if not ok:
            return act, [], dest, "its output was not found on disk"
        others = []
        for it in self.queue:
            if it is item or getattr(it, "status", "") == "Done" or getattr(it, "_output_there", False):
                continue
            others += self._after_sources(it)
            if getattr(it, "path", None):
                others.append(Path(it.path))
        game, patch = self._after_game_sources(item), self._after_patch_sources(item)
        keep_dirs = self._after_protected(item, dest)
        rel = None
        if game:
            title = re.sub(r"\s*\[[^\]]*\]", "", str(getattr(item, "archive_title", "") or
                                                    getattr(item, "display_name", "") or "")).strip()
            if getattr(item, "bundle_release", None):
                # a game with add-ons: its download folder, and only while it holds what was planned
                rel = self._bundle_release(item, game, keep_dirs)
            else:
                rel = aj.release_folder(game, list_sources=find_job_sources, title_id=self._item_title_id(item) or "",
                                        title=title, protected=keep_dirs, may_be=getattr(item, "source_root", None))
        whole = rel is not None and act in (aj.TRASH, aj.MOVE)
        if (rel is not None and act == aj.DELETE and getattr(item, "bundle_release", None)
                and getattr(worker, "extras_copied", False) and not getattr(worker, "extras_left", None)):
            whole = True                         # the add-ons reached the output: the folder goes
        targets = ([rel] if whole else game) + [p for p in patch
                                                if not (whole and str(os.path.realpath(p)).startswith(
                                                    str(os.path.realpath(rel)) + os.sep))]
        if whole:                                # a folder beside the release that went along goes too
            targets += [s for s in self._download_extras(item) if s.parent == Path(rel).parent and s.exists()]
        item._after_info = {"release": rel, "whole": whole, "keep_dirs": keep_dirs}
        return act, targets, dest, aj.refusal(targets, output=out, dest=dest, protected=self._app_folders(),
                                              others=others)

    def _bundle_release(self, item, game, keep_dirs):
        """only when every game part sits directly in it and it is none of our or the job's folders"""
        rel = getattr(item, "bundle_release", None)
        if not rel or not game:
            return None
        aj = _after_job_module()
        folder = Path(rel)
        try:
            if not folder.is_dir() or {os.path.realpath(Path(g).parent) for g in game} != {os.path.realpath(folder)}:
                return None
        except OSError:
            return None
        picked = getattr(item, "source_root", None)       # the folder the job was added from may go
        prot = [q for q in keep_dirs if q and (not picked or os.path.realpath(q) != os.path.realpath(picked))]
        if aj._is_protected_dir(folder, prot):
            return None
        # only while it holds what was planned: a download that arrived since stays put
        known = {os.path.realpath(g) for g in game} | {os.path.realpath(x) for x in self._download_extras(item)}
        try:
            for e in folder.iterdir():
                # a hidden folder may hold anything: then the folder stays
                if (os.path.realpath(e) in known or is_fs_junk_name(e.name)
                        or (e.name.startswith(".") and not e.is_dir())
                        or (e.is_file() and e.suffix.lower() in EXTRA_JUNK_EXTS)):
                    continue
                return None
        except OSError:
            return None
        return folder

    def _run_after_job(self, item, worker, then) -> None:
        """a move across drives runs on a worker, the queue waits so they don't share the drive"""
        aj = _after_job_module()
        try:
            act, srcs, dest, why = self._after_job_plan(item, worker)
        except Exception as e:
            act, srcs, dest, why = "error", [], None, str(e)
        item._after_result = ("keep", "")
        if act == aj.KEEP:
            then(); return
        name = getattr(item, "display_name", None) or item.name
        origin = getattr(item, "origin_archive", None)
        if why and origin and any(j is not item and getattr(j, "origin_archive", None) == origin
                                  and getattr(j, "status", "") != "Done" for j in self.queue):
            self.log("INFO", f"After the job: the archive of {name} stays until the other games from it are done.")
            then(); return
        if why and why.startswith("another job in the queue still needs"):
            # one source, several jobs (a .ffpfsc and a .pkg, say): the last one decides
            item._after_result = ("deferred", why)
            self.log("INFO", f"After the job: the source of {name} stays for the next job from it; "
                             "the last one decides what happens to it.")
            then(); return
        if why:
            item._after_result = ("refused", why)
            self.log("WARN", f"After the job: the source of {name} stays where it is: {why}.")
            then(); return
        size = aj.size_of(srcs)
        names = ", ".join(p.name for p in srcs)
        info = getattr(item, "_after_info", None) or {}
        what = "the source folder" if info.get("whole") else "the source"
        doing = {aj.TRASH: f"moving {what} to the {TRASH_NAME}", aj.MOVE: f"moving {what} to {dest}",
                 aj.DELETE: "deleting the source"}[act]
        self.log("INFO", f"After the job: {doing}: {names} ({format_size(size)}).")
        results = queue.Queue()
        last = [-10]

        def progress(done, total):
            pct = int(done * 100 / total) if total else 100
            if pct >= last[0] + 10:
                last[0] = pct - pct % 10
                results.put(("PROGRESS", tr("Moving the source: {v}%", v=last[0])))

        def work():
            try:
                where = aj.apply(act, srcs, dest=dest, on_progress=progress)
                if act == aj.TRASH:
                    msg = (tr("Moved {what} of {name} to the {TRASH_NAME} ({size}; it frees the space once the "
                             "{TRASH_NAME} is emptied): {names}.", what=what, name=name, TRASH_NAME=TRASH_NAME, size=format_size(size), names=names))
                    item._after_result = ("done", tr("{what} was moved to the {TRASH_NAME}.", what=what, TRASH_NAME=TRASH_NAME))
                elif act == aj.MOVE:
                    msg = f"Moved {what} of {name} to {dest}: " + ", ".join(Path(w).name for w in where) + "."
                    item._after_result = ("done", tr("{what} was moved to {dest}.", what=what, dest=dest))
                else:
                    msg = tr("Deleted the source of {name} ({size} freed): {names}.", name=name, size=format_size(size), names=names)
                    item._after_result = ("done", "the source was deleted.")
                results.put(("SUCCESS", msg))
                # a Delete also takes the release folder when only notes, checksums or pictures are left
                keep_dirs = info.get("keep_dirs") or []
                left = {Path(t).parent for t in srcs}
                rel = info.get("release")
                if act == aj.DELETE and rel is not None and aj.sweep_sidecars(rel, keep_dirs):
                    results.put(("INFO", tr("Removed the release folder {name}: only notes, checksums or pictures were left in it.", name=rel.name)))
                    left.add(Path(rel).parent)
                gone = aj.prune_empty_dirs(left, keep_dirs)
                if gone:
                    results.put(("INFO", "Removed empty folder(s): " + ", ".join(str(g) for g in gone) + "."))
            except Exception as e:
                item._after_result = ("failed", str(e))
                results.put(("WARN", tr("After the job: {name}: {e}. Whatever was not handled stays where it is.", name=name, e=e)))
            results.put(None)

        def poll():
            try:
                while True:
                    m = results.get_nowait()
                    if m is None:
                        self._after_busy = False
                        self.update_queue_box()
                        then()
                        return
                    self.log(*m)
            except queue.Empty:
                self.root.after(200, poll)

        self._after_busy = True
        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, poll)

    def _settle_existing(self, item, hit, hit_note: str = "", then=None, exact: bool = True) -> None:
        """Move/Trash/Delete run now and it is Done, Keep skips. a title+version hit never costs the source"""
        then = then or (lambda done: None)
        name = getattr(item, "display_name", None) or item.name
        item._output_there = True
        note = tr("Its output is already there: {hit}", hit=hit) if hit else (hit_note or "Its output is already there.")
        self._retire_failed(item, "Skipped", note)
        act = getattr(item, "after_source", None) or "keep"
        if hit is not None and act != "keep" and not exact:
            note = tr("A build of the same title and version is there ({name}); the source stays",
                      name=Path(hit).name)
            self._retire_failed(item, "Skipped", note)
            self.log("INFO", f"{name}: skipped; {note}.")
            then(False); return
        if hit is None or act == "keep":
            self.log("INFO", f"{name}: skipped, {note[0].lower() + note[1:]}")
            then(False); return

        def after():
            res = getattr(item, "_after_result", None) or ("failed", "")
            if res[0] == "done":
                self._retire_failed(item, "Done", tr("Its output was already there ({name}); {v}", name=Path(hit).name, v=res[1]))
                self.log("SUCCESS", f"{name}: its output was already there; {res[1]} Marked Done.")
                if self.auto_remove_done_var.get() and item in self.queue:
                    self.queue.remove(item)
                self.update_queue_box()
                then(True)
            else:
                self._retire_failed(item, "Skipped", tr("{note}; the source stays: {v}", note=note, v=res[1]))
                self.update_queue_box()
                then(False)
        import types as _t
        self._run_after_job(item, _t.SimpleNamespace(output_path=str(hit), _is_copy=False, validate_failed=False), after)

    def _publish_patch_backup(self, item, worker, then) -> None:
        """moves the originals the patch replaced beside the output, on a worker. the queue waits"""
        staged = self._staged_patch_backup(worker)
        out = (getattr(worker, "output_path", "") or "").strip('"') if worker is not None else ""
        if staged is None or not out:
            then(); return
        out_p = Path(out)
        dest_dir = out_p.parent
        dest = dest_dir / staged.name
        n = 2
        while dest.exists():
            dest = dest_dir / f"{staged.name} ({n})"
            n += 1
        results = queue.Queue()

        def work():
            try:
                # a patch that only added files: no originals to keep
                if not any(q.is_file() for q in (staged / "app0").rglob("*")):
                    shutil.rmtree(staged.parent if staged.parent.name.startswith("patch-backup-") else staged,
                                  ignore_errors=True)
                else:
                    (staged / "restore.jsonl").unlink(missing_ok=True)     # the app's own record stays behind
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(staged), str(dest))
                    if staged.parent.name.startswith("patch-backup-"):
                        shutil.rmtree(staged.parent, ignore_errors=True)
                    results.put(("INFO", tr("Kept the original files the patch replaced, with a README, in {dest}.", dest=dest)))
            except Exception as e:
                results.put(("WARN", tr("The original files the patch replaced could not be moved beside the output ({e}); they "
                                       "are still in {staged}.", e=e, staged=staged)))
            results.put(None)

        def poll():
            try:
                while True:
                    m = results.get_nowait()
                    if m is None:
                        then(); return
                    self.log(*m)
            except queue.Empty:
                self.root.after(200, poll)
        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, poll)

    @staticmethod
    def _staged_patch_backup(worker) -> Path | None:
        """only an absolute folder in patch-backup-* under our _ffpfsc_temp, anything else stays untouched"""
        raw = str(getattr(worker, "patch_backup", "") or "").strip() if worker is not None else ""
        if not raw:
            return None
        p = Path(raw)
        if (not p.is_absolute() or not p.parent.name.startswith("patch-backup-")
                or "_ffpfsc_temp" not in p.parts or not p.is_dir()):
            return None
        return p

    def _return_extras_aside(self, worker) -> None:
        """a hard kill (Windows) skipped the cleanup: put the set-aside extras back, (2) on a clash"""
        raw = str(getattr(worker, "extras_aside", "") or "") if worker is not None else ""
        if " -> " not in raw:
            return
        aside, game = (Path(x.strip()) for x in raw.split(" -> ", 1))
        if not (aside.name.startswith(".") and aside.name.endswith(".ffpfsc-extras")
                and aside.parent == game.parent and aside.is_dir() and game.is_dir()):
            return
        for p in sorted(aside.iterdir()):
            target = game / p.name
            if target.exists():
                stem, ext = (p.stem, p.suffix) if p.is_file() else (p.name, "")
                n = 2
                while (game / f"{stem} ({n}){ext}").exists():
                    n += 1
                target = game / f"{stem} ({n}){ext}"
                self.log("WARN", f"'{p.name}' is in {game} again; the set-aside one goes back as '{target.name}'.")
            try:
                os.rename(p, target)
            except OSError as e:
                self.log("WARN", f"Could not move '{p.name}' back into {game}: {e}. It is in {aside}.")
        try:
            aside.rmdir()
        except OSError:
            pass

    def _restore_then_drop_patch_backup(self, item, worker) -> None:
        """what to restore is decided here on the main thread, the copying runs in the background"""
        p = self._staged_patch_backup(worker)
        if p is None:
            return
        restore_root = self._patch_restore_root(item, p)
        name = getattr(item, "name", "") if item is not None else ""
        if worker is not None:
            worker.patch_backup = ""

        self._restore_count(+1)

        def _work(p=p, root=restore_root, name=name):
            try:
                _restore(p, root, name)
            finally:
                self._restore_count(-1)

        def _restore(p, root, name):
            if root is not None:
                try:
                    n = _patch_backup_module().restore_originals(p, root)
                    if n:
                        self.log("INFO", f"{name}: the job did not finish; the patch was taken back out of "
                                         f"{root} ({n} change(s) undone).")
                except Exception as e:
                    self.log("ERROR", f"{name}: the patch could not be taken back out of the source folder "
                                      f"({e}). The originals stay in {p}.")
                    return                      # the backup stays: it holds the only originals
            shutil.rmtree(p.parent, ignore_errors=True)
        self._run_cleanup(_work)

    def _patch_restore_root(self, item, p: Path):
        if item is None or not (p / "restore.jsonl").is_file() or self._extract_dir_for_item(item) is not None:
            return None
        try:
            root, _ops = _patch_backup_module()._entries(p)
            src = Path(str(getattr(item, "path", "") or "")).resolve()
        except Exception:
            return None
        if not root or not (Path(root) == src or src in Path(root).parents):
            return None
        return Path(root)

    def _notify_job(self, item, ok: bool, detail: str = "") -> None:
        if self.notify_var.get() != "job" or item is None:
            return
        name = getattr(item, "display_name", None) or item.name
        _after_job_module().notify(name, "Done" if ok else f"Failed: {detail}"[:180])

    def _queue_finished(self) -> None:
        """the run ended on its own, not by Stop"""
        aj = _after_job_module()
        total, done, fail = self._batch_total, self._batch_done, self._batch_failed
        if self.notify_var.get() == "queue" or (self.notify_var.get() == "job" and total > 1):
            aj.notify("PS5 UltraPack", tr("Queue finished: {done} of {total} done", done=done, total=total) + (f", {fail} failed" if fail else ""))
        act = self.after_queue_var.get()
        if act not in ("sleep", "quit"):
            return
        old = getattr(self, "_countdown", None)
        if old is not None:
            try:
                old.destroy()
            except Exception:
                pass
        self.log("INFO", f"Queue finished: {'sleep' if act == 'sleep' else 'quit'} in {CountdownWindow.SECONDS} s "
                         f"(Settings › General › When the queue is done).")
        self._countdown = CountdownWindow(self.root, act, on_go=lambda: self._after_queue_go(act),
                                          on_cancel=lambda: self.log("INFO", "Cancelled: the app stays open."
                                                                     if act == "quit" else "Cancelled: no sleep."))

    def _after_queue_go(self, act: str) -> None:
        self._countdown = None
        if act == "quit":
            self._on_close()
            return
        def work():
            try:
                _after_job_module().sleep_now()
            except Exception as e:
                self.log("ERROR", f"The computer did not go to sleep: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _show_batch_complete(self):
        total = self._batch_total
        done  = self._batch_done
        fail  = self._batch_failed
        self.batch_counter_var.set(
            tr("Queue finished  ·  {done} of {total} done", done=done, total=total) + (tr("  ·  {n} failed", n=fail) if fail else "")
        )
        if fail == 0:
            self.log("SUCCESS", f"Queue finished - all {total} job(s) done.")
        else:
            self.log("WARN", f"Queue finished - {done} of {total} job(s) done, {fail} failed.")
        if self.after_queue_var.get() not in ("sleep", "quit"):   # a modal box would hold them up
            left = [it for it in (getattr(self, "_batch_items", None) or [])
                    if it in self.queue and getattr(it, "status", "") in ("Failed", "Skipped", "Cancelled")]
            QueueReportWindow(self.root, done, total, left, retry=self._retry_failed)

    def _ensure_batch_started(self):
        """only for a fresh run, safe to call twice"""
        if not self._batch_running:
            cd = getattr(self, "_countdown", None)
            if cd is not None:               # a new run: no sleep or quit after the last one
                self._countdown = None
                try:
                    cd.destroy()
                except Exception:
                    pass
            # Only the jobs that will run count: done and kept failed jobs are listed too.
            todo = [it for it in self.queue if getattr(it, "status", "") not in self._TERMINAL_STATUSES]
            self._batch_total   = len(todo)
            self._batch_done    = 0
            self._batch_failed  = 0
            self._batch_running = self._batch_total > 0
            # weighted by size: a 187 GB game moves the bar more than a 35 GB one
            try:
                self._batch_sizes = [max(0, int(display_size(it) or 0)) for it in todo]
            except Exception:
                self._batch_sizes = []
            self._batch_items = list(todo)   # by job, not by place: the list can be reordered
            # the finished jobs' pace is the best guess for the rest
            self._batch_t0 = time.time()
            self._batch_fin_secs, self._batch_fin_bytes = 0.0, 0
            self._job_t0 = {}
            self._all_frac, self._all_eta = None, None
        self._update_batch_counter()

    # start / cancel
    def start(self, rearm_failed: bool = True):
        """a fresh Start also re-runs failed, skipped and cancelled jobs. Retry passes False"""
        if getattr(self, "_restores_inflight", 0) and not self._batch_running:
            # a patch is still being undone in a source folder: wait, or the run reads a half-patched folder
            if not getattr(self, "_start_waiting", False):
                self._start_waiting = True
                self.status_update("Restoring", "Putting a patched source folder back first…", "Cleaning",
                                   0, 0, " - ", " - ", " - ", side=True)

                def _again():
                    self._start_waiting = False
                    self.start(rearm_failed=rearm_failed)
                self.root.after(500, _again)
            return
        if getattr(self, "_start_waiting", False):
            return                                # the waiting start runs it
        if not self._batch_running and (getattr(self, "_add_state", None) or {}).get("total"):
            self.pending_start = True
            self.status_update("Adding", "Adding the jobs, then starting…", "Scanning Files",
                               0, 0, "00:00", " - ", " - ", side=True)
            self.log("INFO", "Start: the queue starts as soon as every new job has been added.")
            return
        if not self.output_var.get().strip():
            messagebox.showerror("Missing output", "Select an output folder.")
            return
        if not self.temp_var.get().strip():
            self.temp_var.set(str(Path(self.output_var.get()) / "_ffpfsc_temp"))
        if not self.queue:
            # an async scan from Add may still be running, re-adding would queue a duplicate
            if getattr(self, "_scan_in_flight", 0) > 0:
                self.pending_start = True
                self.status_update("Scanning", "Finishing the source scan, then starting…",
                                    "Scanning Files", 0, 0, "00:00", " - ", " - ")
                return
            # don't fall back to the last source, it isn't shown anywhere
            self.log("INFO", "Nothing to start - add a game folder, archive or image first.")
            self.status_update("Idle", "Queue is empty.", "", 0, 0, "00:00", " - ", " - ")
            return

        # a fresh Start re-arms failed jobs. within a run they stay skipped or we'd loop forever
        if not self._batch_running and rearm_failed is not False:
            requeued = 0
            for it in self.queue:
                if getattr(it, "status", "") in ("Failed", "Skipped", "Cancelled"):
                    self._rearm_from_archive(it)
                    if self._copy_lacks_files(it):
                        continue
                    it.status = "Pending Extract" if getattr(it, "archive_path", None) else "Queued"
                    requeued += 1
            if requeued:
                self.log("INFO", f"Retrying {requeued} previously failed/skipped job(s).")
                self.status_update("Starting", plural(requeued, "Start runs 1 failed, skipped or stopped job again.",
                                                      "Start runs {n} failed, skipped or stopped jobs again."),
                                   "Starting", 0, 0, " - ", " - ", " - ", side=True)
                self.update_queue_box()

        # Outputs that are already there: one question for all, before anything is unpacked.
        if not self._batch_running and not self._check_existing_outputs():
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            return

        if self._next_pending() is None:
            self.log("INFO", "Nothing to start - every job in the queue has run. Clear completed removes "
                             "the finished ones; Retry runs a failed one again." if self.queue
                     else "Nothing to start - the queue is empty.")
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            return
        item = self._pick_next()

        # the archive set may have grown since add (download still running), read it again
        if not self._due_checks(item):
            return

        # space gate first: judge an archive by its header size, not after it filled the SSD
        gate = self._space_gate(item, self._job_output_dir(item) or Path(self.output_var.get().strip()))
        if gate == "cancel":
            if self._batch_running:
                self._batch_running = False
            # start() may run after extraction with the buttons still busy, reset them
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Drive check cancelled.", "Ready", 0, 0, "00:00", " - ", " - ")
            return
        if gate == "skip":
            self._cleanup_after_failure(item)
            if self._batch_running:
                self._retire_failed(item, "Skipped", getattr(item, "_skip_reason", ""))
                self._batch_failed += 1
                self._update_batch_counter()
                self.update_queue_box()
                self.root.after(600, self._batch_auto_start)
            else:
                # single run: reset the buttons the extract step set, or the UI looks frozen
                item.status = "Skipped"
                item.status_note = getattr(item, "_skip_reason", "")
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self.update_queue_box()
                self.status_update("Ready", f"Skipped: {item.status_note or 'not enough free space'}", "Ready",
                                   0, 0, "00:00", " - ", " - ")
            return

        if self._patch_needs_prepare(item):
            self._ensure_batch_started()   # mark the batch running BEFORE the early return
            self._prepare_patch_item(item)
            return
        if getattr(item, "archive_path", None):
            self._ensure_batch_started()  # a first-job unpack failure moves on instead of aborting
            self._extract_queued_item(item)
            return

        _late = self._late_output_check(item)
        if _late == "cancel":
            self._cleanup_after_failure(item)
            self._batch_running = False
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Start cancelled.", "Ready", 0, 0, "00:00", " - ", " - ")
            return
        if _late == "skip":
            self._skip_late(item)
            return
        # keep Spotlight off our temp, the user's source folder stays indexable
        self._mark_no_spotlight(getattr(item, "_build_temp", None))
        # AMPR emu goes into packs only, an fPKG installs natively and has no use for it
        if getattr(item, "ampr_emu", False) and getattr(item, "operation", "pack") == "pack":
            self._prepare_ampr(item)
        try:
            cmd, cwd, out_dir, temp_dir = self.build_command(item)
        except Exception as e:
            messagebox.showerror("Cannot start", str(e))
            return

        # Stale temp data warning
        try:
            if getattr(item, "operation", "pack") != "unpack" and temp_dir.exists():
                # count all leftovers, but never the current job's source under <temp>/_extracted
                cur_src = None
                try:
                    ex_root = (temp_dir / "_extracted").resolve()
                    src = Path(getattr(item, "path", "") or "").resolve()
                    if ex_root in src.parents:
                        cur_src = ex_root / src.relative_to(ex_root).parts[0]
                except Exception:
                    cur_src = None
                stale_items = []
                for p in temp_dir.iterdir():
                    if p.name == "_extracted" and p.is_dir():
                        for child in p.iterdir():
                            if cur_src is None or child.resolve() != cur_src:
                                stale_items.append(child)
                    elif is_app_tmp_dir_in(p):
                        stale_items.append(p)
                if stale_items:
                    stale_size = sum(folder_size(p) for p in stale_items)
                    if stale_size > 1024 * 1024 * 1024:
                        keep_running = ask_confirm(
                            "Old temporary data found",
                            tr("Found {stale_size} of old temporary data in:\n{temp_dir}\n\nStart anyway, or stop and "
                               "clean it first with Clean temp.", stale_size=format_size(stale_size), temp_dir=temp_dir),
                            "Start anyway", "Stop")
                        if not keep_running:
                            return
        except Exception:
            pass

        self._ensure_batch_started()

        self._last_cmd_str = " ".join(cmd)
        self.cancel_requested = False
        item.status = "Running"
        item.status_note = ""
        item.kept_extract = False
        self.update_queue_box()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        label = f"Game 1/{self._batch_total}" if self._batch_total > 1 else "Starting"
        _floor = self._archive_extract_pct(item) if getattr(item, "_from_archive", False) else 0
        if not getattr(item, "_from_archive", False):
            self._begin_job_progress(item)
        elif getattr(item, "_resume_inner", None):
            self._begin_job_progress(item, _floor)        # a resume: compression only, after the unpack share
        self.status_update(label, "Launching backend.", "Starting", 0, _floor, "00:00", " - ", " - ", job=item)
        self._active_item = item
        self._mark_source_taken_in(item, cmd)
        self.worker = CLIWorker(self, item, cmd, cwd, out_dir, temp_dir)
        self.worker.start()

    def cancel(self):
        self.cancel_requested = True
        self.extract_cancel_event.set()
        # repaint now, or the queued worker updates mask it and Cancel looks dead
        try:
            self.cancel_btn.configure(state="disabled")
            self.stop_btn.configure(state="disabled")
            self.big_status_var.set("Cancelling…")
            self.big_detail_var.set("Stopping the current job and reclaiming temp space…")
            self._status_state = "Cancelling"
            self.footer_var.set("● Cancelling…")
            self.header_status_var.set(f"v{APP_VERSION}  |  Cancelling…")
            self.root.update_idletasks()
        except Exception:
            pass
        _kill_process_tree(self.current_process)
        self.status_update("Cancelling", "Cancel requested - stopping…", "Cancelling", 0, 0, " - ", " - ", " - ")

    def status_update(self, title, detail, stage, stage_pct, overall_pct, elapsed, speed, eta,
                      job=None, side=False):
        """job: its bar never moves backward. side: footer only while a job runs, never resets its bars"""
        if stage == "Creating Temp PFS" and stage_pct >= 100:
            stage_pct = 99
        self.status_q.put((title, detail, stage, stage_pct, overall_pct, elapsed, speed, eta, job, side))

    def _begin_job_progress(self, item, floor: float = 0.0):
        self._job_peak_item, self._job_peak = item, float(floor)

    def log(self, tag, msg):
        self.log_q.put((tag, msg))

    def finish(self, success, msg, last_cmd=""):
        self.done_q.put((success, msg, last_cmd))

    def add_history(self, item, output, final_size, elapsed):
        saved = item.size - final_size if item.size and final_size else 0
        pct = saved / item.size * 100 if item.size else 0
        hist = load_history()
        hist.append({
            "date": now_datetime(),
            "name": item.name,
            "title_id": item.title_id,
            "original": item.size,
            "final": final_size,
            "saved": saved,
            "pct": pct,
            "elapsed": elapsed,
            "output": output,
        })
        save_history(hist)
        rating, _ = compression_rating(pct)
        self.saved_var.set(f"Saved: {format_size(saved)}")
        self.ratio_var.set(f"Compression: {pct:.2f}%")
        self.rating_var.set(f"Rating: {rating}")
        self.refresh_history()
        self.refresh_statistics()

    # tools
    def clear_temp_files(self):
        """only our own working data: temp, extra temp drives and a package job's build folder in the output"""
        tp = self.temp_var.get().strip()
        if not tp:
            messagebox.showerror("Clean temp", "No temp folder is set.")
            return
        roots = [Path(tp)]
        try:
            roots += [Path(x) for x in self._temp_pool_dirs()]
        except Exception:
            pass
        out = (self.output_var.get() or "").strip()
        # only our own scratch folders, temp might point at a drive root or a shared folder
        def _ours(p: Path) -> bool:
            return p.is_dir() and (p.name in ("_extracted", "_ffpfsc_extract", "_ffpfsc_inner",
                                              "_patch_game", "_patch_files", "_ffpfsc_temp")
                                   or is_app_tmp_dir_in(p) or p.name.startswith("ffpfsc_spool_"))
        if self._job_active() or self._restores_inflight or getattr(self, "_cleanup_inflight", 0):
            messagebox.showinfo("Clean temp", "A job or a cleanup is working in the temp folder. Clean it once "
                                              "the queue has stopped and the cleanup is done.")
            return
        keep = self._open_extracts() | self._patch_backup_dirs([str(r) for r in roots])
        targets, seen, unread = [], set(), []
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for q in (x for c in root.iterdir() if _ours(c) for x in removable_parts(c, keep)):
                    if os.path.realpath(q) not in seen:
                        seen.add(os.path.realpath(q)); targets.append(q)
            except OSError as e:
                unread.append(f"{root}: {e}")
        if out and (Path(out) / "_ffpfsc_temp").is_dir():          # a package build stopped half-way
            for q in removable_parts(Path(out) / "_ffpfsc_temp", keep):
                if os.path.realpath(q) not in seen:
                    seen.add(os.path.realpath(q)); targets.append(q)
        for u in unread:
            self.log("WARN", f"Clean temp: cannot read {u}")
        size = sum(get_folder_size(p) for p in targets)
        if not targets or size == 0:
            messagebox.showinfo("Clean temp", "No working data of this app in the temp folders.")
            return
        listing = "\n".join(f"  • {p}" for p in targets[:8]) + ("\n  • …" if len(targets) > 8 else "")
        if not ask_confirm("Clean temp?",
                           tr("Delete this app's working data ({size}):\n{listing}\n\nOther files in these "
                              "folders are left alone.", size=format_size(size), listing=listing),
                           "Delete", danger=True):
            return
        try:
            for p in targets:
                shutil.rmtree(str(p), ignore_errors=True)
            messagebox.showinfo("Clean temp", tr("Temp cleaned. Freed {size}.", size=format_size(size)))
            self.log("OK", f"Clean temp: {len(targets)} folder(s), {format_size(size)} freed")
        except Exception as e:
            messagebox.showerror("Clean temp", tr("Could not clean the temp folder:\n{e}", e=e))

    def export_diagnostics(self):
        zip_path = export_diagnostic_zip(last_cmd=self._last_cmd_str)
        if zip_path and zip_path.exists():
            ok = ask_confirm(
                "Diagnostic package",
                tr("Diagnostic ZIP saved to:\n{zip_path}", zip_path=zip_path), REVEAL_LABEL, "Close"
            )
            if ok:
                open_path(APP_DIR)
        else:
            messagebox.showerror("Error", "Failed to create diagnostic ZIP.")

    # history and statistics
    def refresh_history(self):
        self.history_box.configure(state="normal")
        self.history_box.delete("1.0", "end")
        hist = load_history()
        if not hist:
            self.history_box.insert("end", "No compressions recorded yet.\n")
            self.history_box.configure(state="disabled")
            return
        header = f"{'Date':<20} {'Game':<35} {'Original':>10} {'Output':>10} {'Saved':>10} {'%':>6}  Rating\n"
        self.history_box.insert("end", header)
        self.history_box.insert("end", "─" * len(header) + "\n")
        for entry in reversed(hist[-50:]):
            orig = format_size(entry.get("original", 0))
            final = format_size(entry.get("final", 0))
            saved = format_size(entry.get("saved", 0))
            pct = entry.get("pct", 0)
            rating, _ = compression_rating(pct)
            name = entry.get("name", "Unknown")[:34]
            date = entry.get("date", "")[:19]
            line = f"{date:<20} {name:<35} {orig:>10} {final:>10} {saved:>10} {pct:>5.1f}%  {rating}\n"
            self.history_box.insert("end", line)
        self.history_box.configure(state="disabled")

    def refresh_statistics(self):
        self.stats_box.configure(state="normal")
        self.stats_box.delete("1.0", "end")
        hist = load_history()

        total_games = len(hist)
        total_original = sum(e.get("original", 0) for e in hist)
        total_final = sum(e.get("final", 0) for e in hist)
        total_saved = sum(e.get("saved", 0) for e in hist)
        avg_pct = (sum(e.get("pct", 0) for e in hist) / total_games) if total_games else 0

        lines = [
            f"  Games Compressed:      {total_games}",
            f"  Total Original Size:   {format_size(total_original)}",
            f"  Total Output Size:     {format_size(total_final)}",
            f"  Total Space Saved:     {format_size(total_saved)}",
            f"  Average Compression:   {avg_pct:.1f}%",
            "",
        ]

        if hist:
            best = max(hist, key=lambda e: e.get("pct", 0))
            lines.append(f"  Best Compression:      {best.get('name','?')[:40]}  ({best.get('pct',0):.1f}%)")
            worst = min(hist, key=lambda e: e.get("pct", 0))
            lines.append(f"  Worst Compression:     {worst.get('name','?')[:40]}  ({worst.get('pct',0):.1f}%)")

        for line in lines:
            self.stats_box.insert("end", line + "\n")
        self.stats_box.configure(state="disabled")

    # sound and summary
    def play_complete_sound(self, success=True):
        try:
            want = (success and self.sound_complete_var.get()) or \
                   (not success and self.sound_error_var.get())
            if not want:
                return
            if winsound:
                winsound.MessageBeep(winsound.MB_ICONASTERISK if success else winsound.MB_ICONHAND)
            elif sys.platform == "darwin":
                snd = "/System/Library/Sounds/Glass.aiff" if success else "/System/Library/Sounds/Basso.aiff"
                subprocess.Popen(["afplay", snd],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def show_summary_popup(self):
        try:
            report = FINAL_REPORT_FILE.read_text(encoding="utf-8", errors="replace")
        except Exception:
            report = tr("Job complete.")
        if f"{tr('Compression rating')}:" in report:
            report += "\n\n" + tr("Note: compression does not change FPS or graphics. If the rating is poor, "
                                   "keep the original uncompressed folder instead.")
        self._last_result_text = report
        SummaryDialog(self.root, report, on_show=self.open_output_folder)

    def copy_last_result(self):
        text = getattr(self, "_last_result_text", "")
        if not text:
            try:
                text = FINAL_REPORT_FILE.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            messagebox.showinfo("Copied", "Compression result copied to clipboard.")

    def open_raw_log(self):
        ensure_app_dir()
        RAW_LOG_FILE.touch(exist_ok=True)
        open_path(RAW_LOG_FILE)

    def open_output_folder(self):
        # a bundle lands in its own subfolder, so prefer the real result path
        try:
            out = getattr(self.worker, "output_path", "") if getattr(self, "worker", None) else ""
            if out and Path(out).exists():
                target = Path(out).parent if Path(out).is_file() else Path(out)
                open_path(str(target))
                return
        except Exception:
            pass
        p = self.output_var.get()
        if p and Path(p).exists():
            open_path(p)

    def clear_logs(self):
        self.log_box.delete("1.0", "end")
        self.visible_log_lines = 0

    def update_stages_display(self, current_stage, pct):
        full_names   = [s[0] for s in _STAGE_DEFS]
        current_idx  = full_names.index(current_stage) if current_stage in full_names else -1
        # pack and unpack use disjoint stages, so only tick what was actually entered
        sp = getattr(self.worker, "stage_progress", {}) if getattr(self, "worker", None) else {}
        steps = []
        verify_on = bool(getattr(self, "verify_output_var", None) and self.verify_output_var.get())
        if current_idx >= 0:
            for i, (full, short) in enumerate(_STAGE_DEFS):
                if i == current_idx:
                    dp = min(int(pct), 99) if full == "Creating Temp PFS" else int(pct)
                    steps.append((short, "current", dp))
                elif i < current_idx:
                    if sp.get(full, 0) > 0:
                        steps.append((short, "done", 0))
                elif full == "Extracting" or (full == "Verifying Output" and not verify_on):
                    continue
                else:
                    steps.append((short, "pending", 0))
        try:
            self._steps.set_steps(steps)
        except Exception:
            pass

    # poll loop
    def _tick_elapsed(self):
        """keeps elapsed ticking even when the backend prints nothing"""
        w = getattr(self, "worker", None)
        if w and w.is_alive() and w.start_time:
            self.elapsed_var.set(f"Elapsed: {format_duration(time.time() - w.start_time)}")

    def _update_ram_meter(self):
        """plain under 70 %, amber under 85 %, red above"""
        try:
            import psutil
            mem = psutil.virtual_memory()
            avail_gb = mem.available / 1024**3
            total_gb = mem.total / 1024**3
            pct = mem.percent
            tok = "faint" if pct < 70 else "warning" if pct < 85 else "danger"
            self.ram_var.set(tr("RAM: {avail} of {total} GB free", avail=i18n.number(f"{avail_gb:.1f}"),
                                  total=f"{total_gb:.0f}"))
            try:
                self._ram_label.configure(fg=self.kit.c(tok))
            except Exception:
                pass
        except Exception:
            # no psutil: hide the meter
            try:
                self.ram_var.set("")
            except Exception:
                pass

    def _poll(self):
        try:
            self._poll_inner()
        except Exception as e:
            # last resort, the poll loop must never die
            try:
                self.log("ERROR", f"[_poll crash - loop kept alive] {e}")
            except Exception:
                pass
        self.root.after(200, self._poll)

    def _poll_inner(self):
        self._tick_elapsed()
        # RAM meter every 10th tick, about 2 s
        self._ram_tick = getattr(self, "_ram_tick", 0) + 1
        if self._ram_tick >= 10:
            self._ram_tick = 0
            self._update_ram_meter()
        try:
            self._tick_space_status()
            self._sync_run_ui()
            self._drain_add_q()
            if getattr(self, "_names_dirty", False):
                self._names_dirty = False
                _sel = getattr(self, "_details_item", None)
                if len(self.queue_listbox.marked_rows()) > 1:
                    self.update_queue_box()     # several jobs marked: new names keep them marked
                else:
                    self.update_queue_box(select_item=_sel)     # the job in the details stays
                if _sel is not None and _sel in self.queue:
                    self._fill_job_card(_sel)
                    self.load_art(self._job_art(_sel))
            self._card_tick = getattr(self, "_card_tick", 0) + 1
            if self._card_tick >= 5 and getattr(self, "_inspector_open", False):
                self._card_tick = 0
                item = getattr(self, "_details_item", None)
                if item is not None and (item in self.queue or item is getattr(self, "_active_item", None)):
                    self._fill_card_info(item)
        except Exception:
            pass
        try:
            while True:
                status, payload = self.scan_q.get_nowait()
                if status == "rescan-found":
                    folder = payload["folder"]; sources = payload["sources"]; tpl = payload["tpl"]
                    scanned, skipped = payload.get("scanned", 0), payload.get("skipped", 0)
                    tail = (tr(" ({skipped} already queued, done or in the output folder)", skipped=skipped) if skipped else "")
                    if not sources:
                        msg = (tr("Nothing new in {folder}{tail}.", folder=folder, tail=tail) if scanned
                               else tr("Rescan found no sources in {folder}.", folder=folder))
                        self.log("INFO", msg)
                        self.status_update("Ready", msg, "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                    else:
                        _act = tpl.get("after_source")
                        _clean = {"delete": " Their sources are deleted when done.",
                                  "trash": " Their sources go to the Trash when done.",
                                  "move": " Their sources are moved when done."}.get(_act, "")
                        self.log("OK", f"Rescan: {len(sources)} new source(s) in {folder}{tail}.{_clean}")
                        if _clean:
                            self.status_update("Ready", tr("Rescan: {n} new jobs. After the job: {what}.", n=len(sources),
                                                           what=tr(_after_job_module().LABELS.get(_act, _act))),
                                               "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        _bmap = payload.get("bundles") or {}
                        self._add_jobs_async(list(sources), self._rescan_make(dict(tpl, _rescan_root=folder)),
                                             plan=lambda srcs, _b=_bmap: ([(x, _b.get(str(x))) for x in srcs], []))
                    continue
                if status == "rescan-error":
                    self.log("ERROR", f"Rescan failed: {payload}")
                    self.status_update("Error", tr("Rescan failed: {payload}", payload=payload), "Error", 0, 0, "00:00", " - ", " - ", side=True)
                    continue
                if status == "archive":
                    item = payload
                    self.queue.append(item)
                    self.update_queue_box(select_item=item)
                    self.log("OK", f"Archive queued: {item.archive_path.name}  [{format_size(item.size)}]")
                    self.status_update("Ready",
                                        tr("Archive queued - will extract when compression starts: {name}", name=item.archive_path.name),
                                        "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                    # encrypted header: ask for the password now so routing knows the real size
                    self._resolve_archive_password(item)
                    self.update_queue_box(select_item=item)
                    if self.pending_start:
                        self.pending_start = False
                        self.start()
                elif status == "ok":
                    item = payload
                    self.queue.append(item)
                    self.update_queue_box(select_item=item)  # applies the remembered format, may turn it into fPKG
                    self.status_update("Ready", tr("{title_id} added to queue.", title_id=item.title_id),
                                        "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                    _as = "  → fPKG (.pkg)" if getattr(item, "operation", "pack") == "fpkg-build" else ""
                    self.log("OK", f"Added {item.title_id} | {item.name} | {format_size(item.size)}{_as}")
                    if self.pending_start:
                        self.pending_start = False
                        self.start()

                elif status == "bundles":
                    items: list = payload
                    count = len(items)
                    preview = "\n".join(f"  • {getattr(it, 'bundle_subfolder', it.name)}" for it in items[:12])
                    if count > 12:
                        preview += f"\n  … and {count - 12} more"
                    proceed = count == 1 or ask_confirm(
                        "Add these games?",
                        tr("Found {count} game folder(s):\n\n{preview}\n\nEach keeps its own folder at the destination, "
                           "with its extras next to the .ffpfsc.", count=count, preview=preview),
                        plural(count, "Add 1 job", "Add {n} jobs"))
                    if proceed:
                        for it in items:
                            self.queue.append(it)
                        self.update_queue_box(select_item=items[0] if items else None)
                        self.log("OK", f"Queued {count} game folder(s) - each mirrored at the destination.")
                        self.status_update("Ready", tr("{count} game(s) added to queue.", count=count),
                                            "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "Adding the jobs was cancelled.", "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        self.pending_start = False

                elif status == "multi_found":
                    games: list = payload
                    count = len(games)
                    preview = "\n".join(f"  • {g.name}" for g in games[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = ask_confirm(
                        "Add these games?",
                        tr("Found {count} PS5 game folder(s):\n\n{preview}", count=count, preview=preview),
                        plural(count, "Add 1 job", "Add {n} jobs"))
                    if ok:
                        self.log("INFO", f"Queuing {count} games…")
                        self.status_update("Scanning", tr("Adding {count} games to queue…", count=count),
                                            "Scanning Files", 0, 0, "00:00", " - ", " - ", side=True)
                        def _add_all(paths=games):
                            for gpath in paths:
                                try:
                                    self.scan_q.put(("ok", GameItem(gpath)))
                                except Exception as e:
                                    self.log("ERROR", f"Skipped {gpath.name}: {e}")
                        threading.Thread(target=_add_all, daemon=True).start()
                    else:
                        self.status_update("Ready", "Adding the jobs was cancelled.", "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        self.pending_start = False

                elif status == "exfat_found":
                    image_list: list = payload
                    count = len(image_list)
                    preview = "\n".join(f"  • {f.name}" for f in image_list[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = ask_confirm(
                        "Add these disk images?",
                        tr("Found {count} disk image(s) (.exfat / .ffpkg):\n\n{preview}\n\nEach image is compressed "
                           "directly, without unpacking.", count=count, preview=preview),
                        plural(count, "Add 1 job", "Add {n} jobs"))
                    if ok:
                        for img in image_list:
                            item = GameItem.from_exfat(img)
                            self.queue.append(item)
                            lbl = "exFAT" if img.suffix.lower() == ".exfat" else "ffpkg"
                            self.log("OK", f"{lbl} image queued: {img.name}")
                        self.update_queue_box()
                        self.status_update("Ready", tr("{count} disk image(s) added to queue.", count=count),
                                            "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "Image add cancelled.", "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        self.pending_start = False

                elif status == "archives_found":
                    archives: list = payload
                    count = len(archives)
                    preview = "\n".join(f"  • {a.name}" for a in archives[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = ask_confirm(
                        "Add these archives?",
                        tr("Found {count} archive file(s):\n\n{preview}\n\nEach archive is unpacked when its turn "
                           "comes.", count=count, preview=preview),
                        plural(count, "Add 1 job", "Add {n} jobs"))
                    if ok:
                        added = []
                        for arc in archives:
                            item = GameItem.from_archive(arc)
                            self.queue.append(item)
                            added.append(item)
                            self.log("OK", f"Archive queued: {arc.name}")
                        self.update_queue_box()
                        # unreadable headers: ask for passwords now so routing sees the real unpacked size
                        for it in added:
                            self._resolve_archive_password(it)
                        self.update_queue_box()
                        self.status_update("Ready", tr("{count} archive(s) added to queue.", count=count),
                                            "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "Archive add cancelled.", "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        self.pending_start = False

                elif status == "pfs_found":
                    images: list = payload
                    count = len(images)
                    preview = "\n".join(f"  • {f.name}" for f in images[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = ask_confirm(
                        "Unpack these images?",
                        tr("Found {count} .ffpfs/.ffpfsc image(s):\n\n{preview}", count=count, preview=preview),
                        plural(count, "Add 1 unpack job", "Add {n} unpack jobs"))
                    if ok:
                        for image in images:
                            item = GameItem.from_pfs_image(image)
                            self.queue.append(item)
                            self.log("OK", f"PFS image queued for extraction: {image.name}")
                        self.update_queue_box()
                        self.status_update("Ready", tr("{count} PFS image(s) added to queue.", count=count),
                                            "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "PFS image add cancelled.", "Ready", 0, 0, "00:00", " - ", " - ", side=True)
                        self.pending_start = False

                elif status == "cancelled":
                    self.pending_start = False
                    self._pending_fpkg_identity = None
                    self._batch_running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.status_update("Ready", str(payload), "Ready", 0, 0, "00:00", " - ", " - ")

                else:  # "error"
                    self.pending_start = False
                    self._pending_fpkg_identity = None   # the item it was meant for never landed
                    messagebox.showerror("Scan failed", str(payload))
        except queue.Empty:
            pass

        # read yview BEFORE inserting, afterwards Tk always reports < 1.0 and we never follow again
        try:
            was_at_bottom = self.log_box._textbox.yview()[1] >= 0.98
        except Exception:
            try:
                was_at_bottom = self.log_box.yview()[1] >= 0.98
            except Exception:
                was_at_bottom = True

        processed = 0
        try:
            t = self.log_box._textbox
            while processed < 50:
                tag, msg = self.log_q.get_nowait()
                line = f"[{now_time()}] [{tag}] {msg}\n"
                t.insert("end", line, (tag,))
                self.visible_log_lines += 1
                processed += 1
        except (queue.Empty, AttributeError):
            if processed == 0:
                try:
                    while processed < 50:
                        tag, msg = self.log_q.get_nowait()
                        self.log_box.insert("end", f"[{now_time()}] [{tag}] {msg}\n")
                        self.visible_log_lines += 1
                        processed += 1
                except queue.Empty:
                    pass
        if processed:
            if self.visible_log_lines > 1500:
                try:
                    self.log_box._textbox.delete("1.0", "300.0")
                except Exception:
                    self.log_box.delete("1.0", "300.0")
                self.visible_log_lines -= 300

            # follow only if the user was at the bottom, don't yank them away from older lines
            if was_at_bottom:
                try:
                    self.log_box._textbox.see("end")
                except Exception:
                    try:
                        self.log_box.see("end")
                    except Exception:
                        pass

        try:
            while True:
                title, detail, stage, stage_pct, overall_pct, elapsed, speed, eta, job, side = self.status_q.get_nowait()
                if side and self._batch_running and self.queue:
                    # Side message during a running job: the job's own status stays.
                    self._status_state = str(title)
                    self.footer_var.set(f"● {title}: {detail}" if detail else f"● {title}")
                    continue
                if job is not None:
                    # One job's progress only ever grows (a retry starts a new peak).
                    if job is not getattr(self, "_job_peak_item", None):
                        self._job_peak_item, self._job_peak = job, 0.0
                    self._job_peak = max(self._job_peak, overall_pct)
                    overall_pct = self._job_peak
                self.big_status_var.set(title)
                self.big_detail_var.set(detail)
                # lower bar is the current step (stage_pct), whole-game progress feeds the queue bar
                _ri = self._running_item()
                _gname = ((getattr(_ri, "display_name", None) or _ri.name) if _ri is not None else "") or ""
                self.cur_game_var.set(f"CURRENT STEP  ·  {_gname}" if _gname else "CURRENT STEP")
                self.stage_title_var.set(stage or title)
                self.stage_detail_var.set(detail)
                self.stage_pct_var.set(f"{int(stage_pct)}%")
                self.stage_bar.set(max(0, min(1, stage_pct / 100)))
                # queue bar is size-weighted, falls back to counting games
                _total = max(1, getattr(self, "_batch_total", 1))
                _done  = getattr(self, "_batch_done", 0) + getattr(self, "_batch_failed", 0)
                # by job, not by list position (the list can be reordered)
                _items = getattr(self, "_batch_items", None) or []
                def _sz(it):
                    try:
                        return max(0, int(display_size(it) or 0))
                    except Exception:
                        return 0
                _tot_bytes = sum(_sz(it) for it in _items)
                if _tot_bytes > 0:
                    _done_bytes = sum(_sz(it) for it in _items if it is not _ri
                                      and getattr(it, "status", "") in self._TERMINAL_STATUSES)
                    _cur_bytes = _sz(_ri) if _ri in _items else 0
                    _qfrac = max(0.0, min(1.0, (_done_bytes + _cur_bytes * overall_pct / 100.0) / _tot_bytes))
                else:
                    _qfrac = max(0.0, min(1.0, (_done + overall_pct / 100.0) / _total))
                if self._batch_running:
                    self._all_frac = _qfrac
                    try:
                        _left = sum(_sz(it) for it in _items if it is not _ri
                                    and getattr(it, "status", "") not in self._TERMINAL_STATUSES)
                        _cur = _sz(_ri) if _ri is not None else 0
                        _jl = (self._job_time_left(_ri, stage, stage_pct, eta, _cur)
                               if _ri is not None and job in (None, _ri) else None)
                        self._all_eta = self._estimate_left(_left, _cur, overall_pct, _ri, cur_left=_jl)
                        if _jl is not None:
                            eta = humanize_eta(f"{int(_jl)}s")      # Left: the job's, not the step's
                    except Exception:
                        self._all_eta = None
                    self._update_batch_counter()
                self.overall_pct_var.set(f"{int(_qfrac * 100)}%")
                self.overall_bar.set(_qfrac)
                self.overall_title_var.set(
                    tr("QUEUE  ·  Job {n} of {total}", n=min(_done + 1, _total), total=_total) if _total > 1 else "QUEUE")
                self.speed_var.set(f"Speed: {speed}")
                self.elapsed_var.set(f"Elapsed: {elapsed}")
                self.eta_var.set(f"ETA: {eta}")
                self.header_status_var.set(f"v{APP_VERSION}  |  Stage: {stage}")
                self._status_state = str(title)       # English: the dot's colour is read from it
                self.footer_var.set(f"● {title}")
                self.update_stages_display(stage, stage_pct)
                self._cur_job_pct = overall_pct
                if _ri is not None:
                    try:
                        self.queue_listbox.update_row(self.queue.index(_ri), progress=max(0.0, min(1.0, overall_pct / 100.0)),
                                                      chip=f"{int(overall_pct)}%")
                    except Exception:
                        pass
        except queue.Empty:
            pass

        try:
            status, payload = self._extract_q.get_nowait()
            if status == "ok":
                if isinstance(payload, tuple):
                    item, extra_items = payload
                else:
                    item, extra_items = payload, []
                if extra_items and getattr(item, "_unpacked_again", False):
                    self._adopt_games_of_archive(item, extra_items)
                    extra_items = []
                if extra_items and getattr(item, "operation", "pack") == "chain":
                    for extra in extra_items:
                        self._as_chain_job(extra, item)
                elif extra_items and getattr(item, "operation", "pack") == "fpkg-build":
                    # extra games of an fPKG archive share only the compression settings, identity is per game
                    _tpl = self._fpkg_compression_of(item)
                    for extra in extra_items:
                        self._as_fpkg_job(extra, _tpl)
                        extra.output_path = getattr(item, "output_path", None)
                if extra_items:
                    for extra in extra_items:
                        # every game knows the archive: After-job waits for the last one, a retry can unpack again
                        extra.origin_archive = getattr(item, "origin_archive", None)
                        extra.origin_extracted_size = getattr(item, "origin_extracted_size", 0)
                        for attr in ("after_source", "after_move_to", "source_root"):
                            if getattr(extra, attr, None) is None and getattr(item, attr, None) is not None:
                                setattr(extra, attr, getattr(item, attr))
                    try:
                        idx = self.queue.index(item)
                    except ValueError:
                        idx = 0
                    for offset, extra in enumerate(extra_items, start=1):
                        self.queue.insert(idx + offset, extra)
                        if self._batch_running:        # each game of the archive counts in this run
                            self.__dict__.setdefault("_batch_items", []).append(extra)
                            self._batch_total += 1
                    self._update_batch_counter()
                    self.log("OK", f"Queued {len(extra_items)} additional payload item(s) from the archive")
                # changed in place, clear the cache so the details refresh fully
                self._details_item   = None
                self._loaded_art_key = None
                self.update_queue_box(select_item=item)
                self.log("OK", f"Extraction complete: {item.name}  [{format_size(item.size)}]")
                self.start()
            elif status == "cancelled":
                _ci = self._active_item
                if _ci is not None and _ci in self.queue:
                    self._cleanup_after_failure(_ci)
                    _ci.status = "Cancelled"
                self._batch_running = False
                self.pending_start = False
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self.update_queue_box()
                self.status_update("Ready", str(payload), "Ready", 0, 0, "00:00", " - ", " - ")
                self.log("WARN", str(payload))
            else:
                failed_item = self._active_item
                if failed_item is not None:
                    self._cleanup_after_failure(failed_item)
                    self._retire_failed(failed_item, "Failed")
                    self._batch_failed += 1
                self.update_queue_box()
                self.log("ERROR", f"Extraction failed: {payload}")
                if self._batch_running and self._has_pending():
                    self.log("WARN", "Continuing batch with the next item after extraction failure.")
                    self.root.after(600, self._batch_auto_start)
                else:
                    self._batch_running = False
                    self.pending_start = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self._update_batch_counter()
                    self._queue_finished()
                    if self._batch_total > 1:
                        self._show_batch_complete()
                    elif self.after_queue_var.get() not in ("sleep", "quit"):
                        messagebox.showerror("Extraction Failed", str(payload))
        except queue.Empty:
            pass

        try:
            success, msg, last_cmd = self.done_q.get_nowait()
            self._last_cmd_str = last_cmd

            completed_item = self._active_item
            if completed_item is not None and completed_item in self.queue:
                completed_item.status = "Done" if success else "Failed"
            if completed_item is not None:
                # the copy lacks files only after a failed run that took some in
                completed_item.source_taken_in = bool(not success and self._run_took_files(self.worker))

            if success:
                self._batch_done += 1
                if completed_item is not None:      # its pace feeds the estimate for the rest
                    _t0 = (getattr(self, "_job_t0", {}) or {}).pop(id(completed_item), None)
                    if _t0:
                        try:
                            self._batch_fin_secs = getattr(self, "_batch_fin_secs", 0.0) + (time.time() - _t0)
                            self._batch_fin_bytes = getattr(self, "_batch_fin_bytes", 0) + max(0, int(display_size(completed_item) or 0))
                        except Exception:
                            pass
                self.status_update("Complete", msg, "Complete", 100, 100, " - ", " - ", " - ")
                self.log("SUCCESS", msg)
                self.play_complete_sound(True)

                _final_sz = getattr(self.worker, "final_size", 0) if self.worker else 0
                completed_operation = getattr(completed_item, "operation", "pack") if completed_item else "pack"
                # history only for jobs that write a packed image
                if completed_operation not in ("unpack", "fake-sign", "fpkg-extract", "copy", "ps4-keystone"):
                    # main thread only: add_history touches Tk widgets
                    try:
                        _w = self.worker
                        self.add_history(
                            completed_item,
                            getattr(_w, "output_path", "") if _w else "",
                            _final_sz,
                            (time.time() - _w.start_time) if (_w and getattr(_w, "start_time", None)) else 0.0,
                        )
                    except Exception as e:
                        self.log("WARN", f"Could not record history: {e}")

                _done_worker = self.worker
                self._return_extras_aside(_done_worker)    # whatever the backend could not move back itself
                # extras that missed the output keep the unpack, nothing of a release gets lost
                _left = list(getattr(_done_worker, "extras_left", None) or [])
                _not_all_out = bool(_left)          # the after-job rule then leaves the source alone
                if _left and completed_item is not None and self._extract_dir_for_item(completed_item):
                    completed_item.kept_extract = True
                    self.log("WARN", f"{', '.join(_left)} did not come along to the output; the unpack stays in "
                                     f"{self._extract_dir_for_item(completed_item)}. Removing the job from the "
                                     "queue deletes it.")
                else:
                    _left = []
                if self.auto_clear_temp_var.get() and not _left:
                    self._auto_clear_temp(completed_item)
                # also catches the spread-mode unpack on the output drive, _auto_clear_temp only sees temp
                if completed_item is not None:
                    if not _left:
                        self._cleanup_item_extract(completed_item)
                    self._cleanup_inner_image(completed_item)
                    self._ampr_cleanup(completed_item)   # restore a direct source folder
                    self._notify_job(completed_item, True)
                    if self.auto_remove_done_var.get() and not _left:
                        if completed_item in self.queue:
                            self.queue.remove(completed_item)
                    else:
                        self._retire_failed(completed_item, "Done")

                def _advance():
                    if self._batch_running and self._has_pending():
                        self.update_queue_box()
                        self.root.after(600, self._batch_auto_start)
                    else:
                        self._batch_running = False
                        self.start_btn.configure(state="normal")
                        self.cancel_btn.configure(state="disabled")
                        self.update_queue_box()
                        self._update_batch_counter()
                        quitting = self.after_queue_var.get() in ("sleep", "quit")
                        self._queue_finished()
                        if self._batch_total > 1:
                            self._show_batch_complete()
                        else:
                            if self.open_output_var.get():
                                self.open_output_folder()
                            if self.summary_popup_var.get() and not quitting:
                                self.show_summary_popup()

                def _after_backup():
                    if completed_item is not None and _not_all_out:
                        self.log("WARN", f"{completed_item.name}: the after-job rule is not applied, "
                                         "because not everything reached the output.")
                        _advance()
                    elif completed_item is not None:
                        self._run_after_job(completed_item, _done_worker, _advance)
                    else:
                        _advance()
                self._publish_patch_backup(completed_item, _done_worker, _after_backup)

            elif self.cancel_requested or self.extract_cancel_event.is_set():
                # a user cancel arrives as a failed result; count it as cancelled, not failed
                self._batch_running = False
                self.cancel_requested = False
                self.extract_cancel_event.clear()
                self._return_extras_aside(self.worker)
                self._restore_then_drop_patch_backup(completed_item, self.worker)
                if completed_item is not None:
                    keep = self._extract_is_complete(completed_item) and not self._run_took_files(self.worker)
                    self._cleanup_after_failure(completed_item, keep_source=keep)
                    self._cleanup_inner_image(completed_item)   # no resume after a cancel
                    self._retire_failed(completed_item, "Cancelled")
                    completed_item.kept_extract = keep
                    if keep:
                        own = self._extract_dir_for_item(completed_item)
                        self.log("INFO", f"Kept the extracted copy in {own} on {_drive_colon(own)} Start runs the job "
                                         f"from it again, and removing the job from the queue deletes it.")
                self.update_queue_box()
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self._update_batch_counter()
                self.status_update("Ready", "Cancelled by user.", "Ready", 0, 0, "00:00", " - ", " - ")
                self.log("WARN", "Cancelled by user.")
            else:
                # OOM killed: retry the same game with one core less instead of failing
                if (self.worker is not None and getattr(self.worker, "oom_killed", False)
                        and completed_item is not None and self._oom_retry(completed_item)):
                    # the retry waits for this: a resume needs the patched image, a rebuild the clean folder
                    self._return_extras_aside(self.worker)
                    self._restore_then_drop_patch_backup(completed_item, self.worker)
                    return
                self._batch_failed += 1
                self.status_update("Failed", msg, "Failed", 0, 0, " - ", " - ", " - ")
                self.log("ERROR", msg)
                self.play_complete_sound(False)
                self._return_extras_aside(self.worker)
                self._restore_then_drop_patch_backup(completed_item, self.worker)
                self._notify_job(completed_item, False, msg)
                if completed_item is not None:
                    # keep a finished unpack so Retry skips the archive; _release_failed_copies frees it if needed
                    keep = self._extract_is_complete(completed_item) and not self._run_took_files(self.worker)
                    self._cleanup_after_failure(completed_item, keep_source=keep)
                    self._cleanup_inner_image(completed_item)
                    self._retire_failed(completed_item, "Failed", msg)
                    completed_item.kept_extract = keep
                    if keep:
                        own = self._extract_dir_for_item(completed_item)
                        self.log("INFO", f"Kept the extracted copy in {own} on {_drive_colon(own)} Edit or Retry "
                                         f"runs the job from it again, and removing the job deletes it.")
                self.update_queue_box()
                if self._batch_running and self._has_pending():
                    self.log("WARN", "Continuing batch with the next item after failure.")
                    self.root.after(600, self._batch_auto_start)
                else:
                    self._batch_running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self._update_batch_counter()
                    self._queue_finished()
                    if self._batch_total > 1:
                        self._show_batch_complete()
                    else:
                        log_lines = get_last_log_lines(50)
                        _fi = completed_item
                        ErrorDialog(self.root, msg, last_cmd, log_lines,
                                    operation=getattr(_fi, "operation", "pack") if _fi else "pack",
                                    on_edit=(lambda it=_fi: self._edit_job(it)) if _fi is not None else None,
                                    on_retry=(lambda it=_fi: self._retry_job(it)) if _fi is not None else None)
        except queue.Empty:
            pass


if _HAS_DND:
    class _CTkDnD(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    _CTkDnD = ctk.CTk


def _wire_open_document(root, app):
    """double-clicked .ffpfsc/.ffpfs/.pkg opens in Look inside; macOS needs argv_emulation off for it"""
    def _on_open(*paths):
        files = [p for p in paths if str(p).lower().endswith((".ffpfsc", ".ffpfs", ".pkg"))]
        if not files:
            return
        try:
            root.deiconify()
            root.lift()
        except Exception:
            pass
        for f in files:
            try:
                app.open_pfs_browser(f)
            except Exception as e:
                try:
                    app.log("ERROR", f"Could not open {f} in Look inside: {e}")
                except Exception:
                    pass

    if sys.platform != "darwin":
        args = [a for a in sys.argv[1:] if os.path.isfile(a)]
        if args:
            root.after(800, lambda: _on_open(*args))
        return
    try:
        root.createcommand("::tk::mac::OpenDocument", _on_open)
    except Exception:
        return


def main():
    ensure_app_dir()
    root = _CTkDnD()
    app = App(root)
    _wire_open_document(root, app)
    root.mainloop()


if __name__ == "__main__":
    main()
