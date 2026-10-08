
from __future__ import annotations

import os
import re
import sys
import time
import json
import queue
import zipfile
import threading
import subprocess
import tempfile
import signal
import shutil
import struct
import types
import webbrowser
import multiprocessing
from pathlib import Path


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
        # 'fork' avoids re-exec'ing the frozen app per worker (which would re-launch
        # the GUI = fork bomb). It is only safe here because this --mkpfs-internal
        # process is pure compute (zlib/file IO) and has NOT initialized any macOS
        # framework (Cocoa/CoreFoundation) — fork() after that is unsafe on macOS.
        # KEEP this branch framework-free: do NOT import tkinter/customtkinter/PIL
        # (or anything that pulls them in) before the pool runs.
        try:
            multiprocessing.set_start_method("fork", force=True)
        except RuntimeError:
            pass

    backend_dir = _bundled_backend_dir()
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    if sys.argv[1] == "--mkpfs-internal":
        from mkpfs.cli import cli_mkpfs_main

        sys.exit(cli_mkpfs_main(sys.argv[2:]))

    import runpy

    cli_py = backend_dir / "cli.py"
    sys.argv = [str(cli_py)] + sys.argv[2:]
    runpy.run_path(str(cli_py), run_name="__main__")
    sys.exit(0)


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
APP_VERSION = "2.2.1"
# For archive sources, the GUI extraction occupies the first slice of a game's overall
# progress; the worker's pack progress is compressed into the remaining tail so the
# whole-game percentage stays monotonic across extraction → pack (see CLIWorker._set_stage
# and the extraction status_update calls).
ARCHIVE_EXTRACT_OVERALL_PCT = 25
# A copy job (PS4 library, Organize) unpacks its archive on the output drive: what follows is
# a rename, so the unpack is nearly the whole job. Across drives a copy of the same size follows.
ARCHIVE_EXTRACT_RENAME_PCT = 97
ARCHIVE_EXTRACT_COPY_PCT = 50


def unpack_rate(pct: float, secs: float, total_bytes: int) -> tuple[str, str]:
    """(speed, time left) of an archive unpack at *pct* after *secs*: the time left from the
    pace so far, the speed from the share of *total_bytes* done. '—' until there is enough
    to go on (2 % and 3 s), and for a speed without a known size."""
    if pct < 2 or secs < 3 or pct >= 100:
        return "—", "—"
    left = humanize_eta(f"{int(secs * (100 - pct) / pct)}s")
    if total_bytes <= 0:
        return "—", left
    rate = total_bytes * pct / 100.0 / secs
    return (f"{rate / 1e9:.2f} GB/s" if rate >= 1e9 else f"{rate / 1e6:.1f} MB/s"), left
BACKEND_NAME = "bizkut/ps5-ffpfs-cli"
MKPFS_NAME    = "MkPFS"
MKPFS_VERSION = "1.0.0"

# The Tk-free core (settings, naming, drive/space logic, archive extraction, GameItem)
# lives in ultra_core.py; every name it defines is re-exported here.
# The core sits next to this file; make sure it is importable however this module is
# loaded (run as a script, frozen, or exec'd by a test from another directory).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import ultra_core  # noqa: E402
from ultra_core import *  # noqa: E402,F401,F403
from ui_kit import (Kit, IconButton, IconView, ArtView, ProgressBar, QueueList, TransportControl, unpack_touchpad_delta,  # noqa: E402
                    Chips, StepStrip, LogText, Tile, RoundBox, ctk_pair, apply_ctk_theme, PALETTE)


def _backport_module():
    """backend/backport.py, imported in-process for its read-only helpers (targets,
    SDK words, the firmware folder checks). Jobs still run it through the CLI."""
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import backport
    return backport


def _ps4pkg_module():
    """backend/ps4pkg.py: PS4 package identity (Tk-free)."""
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import ps4pkg
    return ps4pkg


def _is_ps4_source(p) -> bool:
    """A PS4 package, or a folder (not a game folder) whose packages are all PS4 ones."""
    try:
        p = Path(p)
        m = _ps4pkg_module()
        if p.is_file():
            return p.suffix.lower() == ".pkg" and m.is_ps4_package(p)
        if not p.is_dir() or is_game_folder(p):
            return False
        pkgs = [x for x in p.rglob("*.pkg") if x.is_file() and not is_fs_junk_name(x.name)]
        return bool(pkgs) and all(m.is_ps4_package(x) for x in pkgs)
    except Exception:
        return False


def _after_job_module():
    """backend/after_job.py: what happens to a job's source once it is done (Tk-free)."""
    _bd = str(backend_base_dir())
    if _bd not in sys.path:
        sys.path.insert(0, _bd)
    import after_job
    return after_job


def _drive_name(path: Path) -> str:
    """The drive a path lives on, by its volume name: 'SAMSUNG', or 'the system drive'."""
    parts = Path(path).parts
    if len(parts) > 2 and parts[1] == "Volumes":
        return parts[2]
    return "the system drive"


def _sane_patched_libs(libs: str, fw_root: str) -> str:
    """The patched libraries setting, or '' when it points at the firmware libraries folder
    (or into it): that folder holds the ORIGINAL libraries, which never go into fakelib/."""
    try:
        return "" if (libs and _backport_module().patched_libs_problem(libs, fw_root)) else libs
    except Exception:
        return libs


def _firmware_folder_status(raw: str) -> str:
    """One line under the firmware libraries setting: what the app found there."""
    if not raw:
        return "Not set: backports use the three public targets, and Check has nothing to compare with."
    root = Path(raw)
    if not root.is_dir():
        return "This folder does not exist."
    try:
        folders = _backport_module().firmware_folders(root)
    except Exception as e:
        return f"Could not read the folder: {e}"
    if folders:
        names = list(folders)
        span = names[0] if len(names) == 1 else f"{names[0]} … {names[-1]}"
        return f"{len(names)} firmware folder(s) found: {span}."
    if any(root.rglob("*.sprx")):
        return "No firmware subfolders: the libraries here count for every target. Name a subfolder per firmware."
    return "No firmware subfolders and no .sprx libraries found."


def _wrapped_line_count(font, text: str, width: int) -> int:
    """How many lines *text* takes in *font* when a Tk label wraps it at *width* pixels
    (breaks between words, like Tk)."""
    lines = 0
    for paragraph in str(text).split("\n"):
        lines += 1
        used, space = 0, font.measure(" ")
        for word in paragraph.split():
            w = font.measure(word)
            if used and used + space + w > width:
                lines += 1
                used = w
            else:
                used = used + (space if used else 0) + w
    return max(1, lines)


def _augment_path_for_gui() -> None:
    """macOS/Linux GUI apps launched via Finder/Dock inherit only a minimal
    PATH (/usr/bin:/bin:/usr/sbin:/sbin), so Homebrew / MacPorts tools such as
    7z and unrar are invisible to shutil.which() and subprocess. Prepend the
    common install dirs so the RAR / 7z CLI fallbacks can be located."""
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

# Each constant is a (light_mode, dark_mode) tuple taken from the ui_kit palette, so the
# CTk dialogs and the main window share one set of colours. CTk picks the right value on
# set_appearance_mode(); the main window's Tk widgets are recoloured by the kit.
BLACK   = ctk_pair("surface")         # dialog background
PANEL   = ctk_pair("surface2")        # group box / panel
CARD    = ctk_pair("control")         # entry / inner card
CARD2   = ctk_pair("control")         # inner field / label fill
BTN     = ctk_pair("btn")             # a normal button, the same as the main window's
BTN_HOVER = ctk_pair("btn_hover")
BTN_BORDER = ctk_pair("btn_border")   # a hairline in light mode, the fill colour in dark
BORDER  = ctk_pair("border")          # panel border
BORDER2 = ctk_pair("border_strong")   # entry / button border
ACCENT  = ctk_pair("accent_fill")     # the one primary action per window (PS5 blue)
ACCENT_HOVER = ctk_pair("accent_hover")
ON_ACCENT = "#ffffff"                 # text on ACCENT
HOVER   = ctk_pair("control_hover")   # hover of a normal button
SUCCESS = ctk_pair("success")
YELLOW  = ctk_pair("warning")
RED     = ctk_pair("danger")
DANGER_HOVER = ctk_pair("danger_hover")
WHITE   = ctk_pair("text")            # primary text (dark text in light mode)
MUTED   = ctk_pair("muted")           # secondary text
MONO_FONT = "Menlo"                   # ships with macOS (Consolas does not)
# Shortcuts: Command on macOS, Control elsewhere. ACCEL is what a menu shows (Tk on macOS
# turns "Command-N" into a real key equivalent), SHORTCUT what a tooltip says.
IS_MAC = sys.platform == "darwin"
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
# The dialogs use the main window's type scale (see ui_kit.Fonts): logical pixels, which
# is also what CTkFont sizes are. If a CTk scaling is ever set, set it before any window
# exists: changing it pins every open CTk window to its current size for a second.


# ─── Panels and message windows ────────────────────────────────────────────────

def set_window_appearance(win, mode: str) -> None:
    """Give a window's title bar the app's light or dark look; macOS draws it after the
    system setting otherwise (Tk's unsupported MacWindowStyle, present in Tk 8.6.11+)."""
    try:
        win.tk.call("::tk::unsupported::MacWindowStyle", "appearance", win._w,
                    "aqua" if str(mode).lower() == "light" else "darkaqua")
    except tk.TclError:
        pass


class ScrollFrame(ctk.CTkScrollableFrame):
    """A CTkScrollableFrame whose scrollbar shows only while the content is taller than
    the frame; CustomTkinter keeps it on screen even when there is nothing to scroll.

    The decision compares the content's requested height with the canvas height, once the
    layout has settled. Deciding from the canvas's scroll fractions instead made the bar
    blink: they arrive re-entrantly while the frame re-lays out, from a scroll region that
    is briefly far too large, and hiding the bar re-lays it out again."""

    SETTLE_MS = 60

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sb_shown = True
        self._sb_job = None
        self._parent_canvas.bind("<Configure>", lambda e: self._queue_sb_check(), add="+")
        self.bind("<Configure>", lambda e: self._queue_sb_check(), add="+")
        # CustomTkinter binds only <MouseWheel>; Tk 9 on macOS sends a trackpad swipe as
        # <TouchpadScroll> (pixel deltas), so without this the pages scroll with a wheel but
        # not with two fingers. Same containment rule as CTk's own handler.
        try:
            self.bind_all("<TouchpadScroll>", self._touchpad_all, add=True)
        except tk.TclError:
            pass                                   # Tk 8.6: no such event

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
    """Shows the dialogs as panels over the main window's content area; the sidebar and the
    status bar stay visible. Panels stack: the newest is shown, closing it shows the one
    below. While any is open the sidebar is disabled, and Escape closes the top panel."""

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
            self.app._set_nav_enabled(False)
        self.frame.after_idle(self._layout)

    def pop(self, panel):
        if panel not in self.stack:
            return
        was_top = self.stack[-1] is panel
        self.stack.remove(panel)
        if not self.stack:
            self.frame.place_forget()
            self.app._set_nav_enabled(True)
        elif was_top:
            self._layout()

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
        m = self.MARGIN
        slot = top._slot
        if top.LARGE:
            max_w = getattr(top, "MAX_W", self.LARGE_MAX_W)
            slot.place(relx=0.5, y=m, anchor="n", width=min(max_w, W - 2 * m), height=H - 2 * m)
        else:
            pw, ph = top.preferred_size()
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
    """A dialog shown as a panel of the main window instead of a separate window. It takes
    the CTkToplevel calls the dialog classes make: title() is kept, geometry("WxH") sets the
    panel's preferred size, the window-only calls (transient, grab_set, resizable, …) do
    nothing, and Return/Escape bindings go through the panel host to the top panel.
    Callers that wait for a result keep using root.wait_window(dialog)."""

    LARGE = False          # large panels fill the content area; small ones keep their size
    host = None            # the App's PanelHost

    def __init__(self, master=None, **kw):
        host = EmbeddedDialog.host
        # Each panel sits in its own tk holder that goes away with it: CustomTkinter hooks
        # the configure() of a CTk widget's tk master, and a hook left behind by a closed
        # panel would break recolouring the shared backdrop.
        self._slot = host.app.kit.frame(host.frame, bg="bg")
        super().__init__(self._slot, fg_color=BLACK, corner_radius=12, border_width=1, border_color=BORDER2)
        self.pack(fill="both", expand=True)
        self._title, self._pref, self._close_cb, self._keys, self._gone = "", (560, 420), None, {}, False
        self._close_btn = IconButton(self, host.app.kit, icon="x", command=self.request_close, variant="ghost",
                                     height=26, width=26, padx=0, icon_size=14, bg="surface", tooltip="Close  (Esc)")
        self._close_btn.place(relx=1.0, x=-12, y=12, anchor="ne")
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

    # Window-only calls: there is no window of its own to manage.
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


class MessageWindow(ctk.CTkToplevel):
    """A message or a single question in a small window of its own: a job's result, an
    error report, the space check before a start, a password or folder prompt. Work
    surfaces (Add job, Look inside, Settings) are panels of the main window instead.

    The window uses the main window's colours and opens centred over it, in the upper
    third like a macOS alert. The heading inside the window names it, so the title bar
    stays empty. Escape runs the close handler. It waits until the main window is on
    screen, and a grab asked for before that is applied once it shows."""

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
            w, h = self._size or (self.winfo_reqwidth(), self.winfo_reqheight())
            x, y = self._position(owner, w, h)
            super().geometry(f"{w}x{h}+{x}+{y}")
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


class FirstRunWizard(EmbeddedDialog):
    LARGE = True
    MAX_W = 700            # a readable column, not the full width of a wide window

    def __init__(self, parent):
        super().__init__(parent)
        self.title(f"{APP_NAME} — First Run Setup")
        self.geometry("620x400")
        self.resizable(False, False)
        self.grab_set()
        self.configure(fg_color=BLACK)

        self.step = 0
        self.temp_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.result = {}

        self._build()
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

        self.step_var = tk.StringVar(value="Step 1 of 4")
        ctk.CTkLabel(nav, textvariable=self.step_var, text_color=MUTED).grid(row=0, column=1)

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _show_step(self, n):
        self.step = n
        self.step_var.set(f"Step {n + 1} of 4")
        self.back_btn.configure(state="normal" if n > 0 else "disabled")
        self.next_btn.configure(text="Finish" if n == 3 else "Next →")
        self._clear_body()
        ctk.CTkFrame(self.body, height=8, fg_color=PANEL).pack(fill="x")          # top padding of the box

        if n == 0:
            self.header.configure(text="Choose a temp folder")
            self.sub.configure(text="Put it on a fast SSD or NVMe drive: a mechanical HDD slows large games down.")
            ctk.CTkLabel(self.body, text="Temp folder", text_color=WHITE).pack(anchor="w", padx=14, pady=(6, 4))
            row = ctk.CTkFrame(self.body, fg_color=PANEL)
            row.pack(fill="x", padx=14)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkEntry(row, textvariable=self.temp_path, fg_color=CARD, border_color=BORDER2, text_color=WHITE).grid(row=0, column=0, sticky="ew", padx=(0, 8))
            ctk.CTkButton(row, text="Browse", width=80, fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                           command=self._browse_temp, border_width=1, border_color=BTN_BORDER).grid(row=0, column=1)

        elif n == 1:
            self.header.configure(text="Choose an output folder")
            self.sub.configure(text="Finished .ffpfsc files go here. An external drive works, and so does the temp drive.")
            ctk.CTkLabel(self.body, text="Output folder", text_color=WHITE).pack(anchor="w", padx=14, pady=(6, 4))
            row = ctk.CTkFrame(self.body, fg_color=PANEL)
            row.pack(fill="x", padx=14)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkEntry(row, textvariable=self.output_path, fg_color=CARD, border_color=BORDER2, text_color=WHITE).grid(row=0, column=0, sticky="ew", padx=(0, 8))
            ctk.CTkButton(row, text="Browse", width=80, fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
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
                # The SSD/HDD probe may shell out (diskutil / PowerShell) for seconds, so
                # it runs off the UI thread and fills in this label — plus the HDD
                # warning — when done (see _probe_temp_drive_type).
                lines.append(f"Temp drive:    {format_size(tfree)} free  ·  type: detecting…")
            if op:
                opath = Path(op)
                ofree = get_free_space(opath)
                lines.append(f"Output drive:  {format_size(ofree)} free")
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
                summary.append(f"Temp folder:    {self.temp_path.get()}")
            if self.output_path.get():
                summary.append(f"Output folder:  {self.output_path.get()}")
            summary.append("")
            summary.append(f"Click Finish to launch {APP_NAME}.")
            for line in summary:
                ctk.CTkLabel(self.body, text=line, text_color=WHITE, anchor="w",
                              font=ctk.CTkFont(family=MONO_FONT, size=11)).pack(anchor="w", padx=14, pady=2)
        self._bottom_pad = ctk.CTkFrame(self.body, height=14, fg_color=PANEL)
        self._bottom_pad.pack(fill="x")                                            # bottom padding of the box

    def _probe_temp_drive_type(self, tpath: Path, label, tfree: int) -> None:
        """Detect SSD/HDD for the temp drive on a worker thread (get_drive_type may block
        on diskutil / PowerShell) and update the step-3 label — plus the HDD warning —
        once known. The result is dropped if the user has already left the step."""
        def _apply(dt: str) -> None:
            try:
                if not (self.winfo_exists() and label.winfo_exists()):
                    return
                label.configure(text=f"Temp drive:    {format_size(tfree)} free  ·  type: {dt}")
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

    def _next(self):
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


# ─── Detailed Error Dialog ─────────────────────────────────────────────────────

class ErrorDialog(MessageWindow):
    """One error window: the heading names the job kind, the body leads with
    the short message the backend already wrote, an inline explanation says what that
    means in plain words, and the log is one paragraph — not fifty repeated lines. The
    generic 'possible causes' list is gone: whenever we can recognise the failure (a
    disconnected drive, permission denied, low space, an unreadable archive password) the
    explanation is specific; otherwise the log stays the primary source of truth."""

    _TITLES = {
        "pack":         "Pack failed",
        "copy":         "Copy failed",
        "unpack":       "Extraction failed",
        "fpkg-extract": "fPKG extraction failed",
        "fpkg-build":   "fPKG build failed",
        "fake-sign":    "Fake-signing failed",
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

    # ── diagnosis ────────────────────────────────────────────────────────────
    @staticmethod
    def _diagnose(msg: str, log: str) -> str | None:
        """A short, specific sentence for known failure shapes, or None. Reads the
        backend's own line rather than guessing from a menu of causes."""
        t = (msg + "\n" + log).lower()
        # A drop-out on a mounted external drive: the OS returns EACCES on the mount
        # point itself when the volume vanished mid-run. The message names /Volumes/X.
        import re as _re
        m = _re.search(r"permission denied[^\n]*['\"](/volumes/[^'\"]+)", msg, _re.I)
        if m:
            vol = m.group(1)
            return (f"macOS refuses to read or write “{vol}”. The drive most likely disconnected "
                    f"or went to sleep during the job. Check the cable, wake the drive and rerun "
                    f"the job.")
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
            return "The archive's password is wrong or missing. Set it in Settings → Archive passwords."
        if "extraction failed" in t and ("is damaged" in t or "of the set is missing" in t
                                         or "of the set are missing" in t or "cannot be opened" in t):
            return ("The archive itself is broken: a part is damaged or missing. A password does not "
                    "help here. Download the part the message names (or the whole set) again and retry.")
        if "operation cancelled" in t or "cancelled by user" in t:
            return "You cancelled this job. Nothing was written to the output."
        if "sigkill" in t or "killed" in t or "-9" in t:
            return ("The backend was killed by the system. On macOS that is usually memory pressure "
                    "or a Gatekeeper block — try a lower CPU count in Settings, or run the app once "
                    "from a Finder open.")
        if "no eboot" in t or "not a ps5 game" in t:
            return "The source has no eboot.bin — it is not a PS5 game folder."
        if "function(s) that" in t and "lacks" in t:
            return ("The game calls functions that this firmware does not have, so lowering the SDK alone "
                    "would not start it. Edit job lets you pick another backport target (Check tells you "
                    "which one works), or put patched libraries for this target into the folder set in "
                    "Settings › Backport and Retry.")
        return None

    # ── layout ───────────────────────────────────────────────────────────────
    def _build(self):
        # No big red repeat of the window title. The heading names what was doing what,
        # one line, so the message underneath can read.
        ctk.CTkLabel(self, text=self._heading, font=ctk.CTkFont(size=15, weight="bold"),
                      text_color=RED).pack(anchor="w", padx=20, pady=(16, 2))
        # The buttons go in first (at the bottom) so a short window never hides them.
        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(side="bottom", fill="x", padx=20, pady=(0, 14))
        ctk.CTkButton(btns, text="Close", width=100, fg_color=ACCENT, text_color=ON_ACCENT,
                       hover_color=ACCENT_HOVER, command=self.destroy).pack(side="right")
        ctk.CTkButton(btns, text="Open log folder", width=132, fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER, command=self._open_folder, border_width=1, border_color=BTN_BORDER).pack(side="right", padx=(0, 8))
        ctk.CTkButton(btns, text="Copy details", width=110, fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER, command=self._copy, border_width=1, border_color=BTN_BORDER).pack(side="right", padx=(0, 8))
        # The job stays in the queue: change it, or run it again, straight from here.
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
                              font=ctk.CTkFont(family="Menlo", size=11), wrap="word")
        log.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        log.insert("end", self._tail(self._log) or "(no log available)")
        log.configure(state="disabled")


    @staticmethod
    def _tail(text: str, n: int = 20) -> str:
        """The last *n* non-empty lines, so the log box does not repeat itself. The header
        row and the [LAST 10 MB] banner are dropped — they added noise, not context."""
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


# ─── Summary Dialog ────────────────────────────────────────────────────────────

class SummaryDialog(MessageWindow):
    """Compression result summary with copy-to-clipboard button."""

    def __init__(self, parent, report: str):
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

        # The window takes the report's height (4 to 24 lines), so a short result gets
        # a short window; the text box scrolls beyond that.
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
                       text_color=ON_ACCENT).grid(row=0, column=1, sticky="ew")
        for seq in ("<Return>", "<KP_Enter>"):
            self.bind(seq, lambda e: self.destroy())


# ─── Space Diagnostics Dialog ──────────────────────────────────────────────────

class SpaceDiagnosticsDialog(MessageWindow):
    """Pre-flight space check shown before compression starts.
    Opens instantly — drive-type detection runs in a background thread."""

    def __init__(self, parent, item, temp_dir: Path, out_dir: Path):
        super().__init__(parent)
        self.title("Drive space check")
        self.geometry("520x500")
        self.resizable(False, False)
        self.configure(fg_color=BLACK)
        self.proceed = False
        self._auto_timer = None
        self._countdown  = 0
        self._proceed_btn = None   # set in _build
        self._build(item, temp_dir, out_dir)
        self.geometry(f"520x{300 + 28 * getattr(self, '_row_count', 8)}")
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        for seq in ("<Return>", "<KP_Enter>"):
            self.bind(seq, lambda e: self._ok())
        self.after(50, self.grab_set)
        # Auto-proceed after 4 s only when BOTH temp and output drives have room
        if _space_preflight_ok(item, temp_dir, out_dir):
            self._countdown = 4
            self.after(1000, self._tick_countdown)

    def _build(self, item, temp_dir: Path, out_dir: Path):
        ctk.CTkLabel(self, text="Drive space check",
                      font=ctk.CTkFont(size=17, weight="bold"),
                      text_color=WHITE).pack(anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(self, text="Pre-flight check before compression starts.",
                      text_color=MUTED).pack(anchor="w", padx=20, pady=(0, 10))

        panel = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        panel.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        # ── Needs per drive: the same numbers the pre-flight gate decides on ────
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

        # ── Drive type row — populated by background thread ───────────────────
        dt_row = ctk.CTkFrame(panel, fg_color=PANEL)
        dt_row.pack(fill="x", padx=14, pady=2)
        dt_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dt_row, text="Temp drive type:", text_color=MUTED,
                      anchor="w", width=200).grid(row=0, column=0, sticky="w")
        self._dt_label = ctk.CTkLabel(dt_row, text="Detecting…", text_color=MUTED,
                                       anchor="e")
        self._dt_label.grid(row=0, column=1, sticky="e")

        # ── Space result banner ────────────────────────────────────────────────
        result_color = SUCCESS if space_ok else YELLOW
        ctk.CTkLabel(panel, text=result_text, text_color=result_color,
                      font=ctk.CTkFont(size=13, weight="bold")
                     ).pack(anchor="w", padx=14, pady=(8, 2))

        # Filesystem warnings (fast — already have temp_fs / out_fs)
        if temp_fs in ("exFAT", "FAT32", "FAT"):
            ctk.CTkLabel(panel,
                          text=f"Temp drive is {temp_fs} — no hardlink support. Slower copy mode will be used.",
                          text_color=YELLOW, justify="left", wraplength=460
                         ).pack(anchor="w", padx=14, pady=(0, 2))
        if out_fs in ("exFAT", "FAT32", "FAT"):
            ctk.CTkLabel(panel,
                          text=f"Output drive is {out_fs}. NTFS recommended.",
                          text_color=YELLOW, justify="left", wraplength=460
                         ).pack(anchor="w", padx=14, pady=(0, 2))

        # HDD warning label — shown/hidden by background thread result
        self._hdd_warn = ctk.CTkLabel(panel,
                                       text="Temp folder is on a mechanical HDD — will be significantly slower.",
                                       text_color=YELLOW, justify="left", wraplength=460)
        # packed conditionally in background callback

        # ── Buttons ───────────────────────────────────────────────────────────
        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(fill="x", padx=20, pady=(0, 16))

        self._proceed_btn = ctk.CTkButton(btns, text="Start now",
                       fg_color=ACCENT, hover_color=ACCENT_HOVER,
                       text_color=ON_ACCENT,
                       font=ctk.CTkFont(size=13),
                       height=38,
                       command=self._ok
                      )
        self._proceed_btn.pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Cancel",
                       fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER,
                       command=self._cancel, border_width=1, border_color=BTN_BORDER
                      ).pack(side="right")

        # ── Background thread: drive type detection ───────────────────────────
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
            if self._proceed_btn:
                self._proceed_btn.configure(
                    text=f"Start now  (auto in {self._countdown} s)")
            self._countdown -= 1
            self._auto_timer = self.after(1000, self._tick_countdown)
        else:
            self._ok()

    def _ok(self):
        if self._auto_timer:
            try:
                self.after_cancel(self._auto_timer)
            except Exception:
                pass
        self.proceed = True
        self.destroy()

    def _cancel(self):
        if self._auto_timer:
            try:
                self.after_cancel(self._auto_timer)
            except Exception:
                pass
        self.proceed = False
        self.destroy()


# ─── Export Diagnostic Package ─────────────────────────────────────────────────

def _redact_settings_for_export(data):
    """Copy of the settings with the secrets removed before they leave the machine:
    the saved archive-password list is dropped and every other key that mentions
    'password' (the one-off field, a per-queue-item override, …) is blanked — a
    non-empty value becomes '<redacted>' so the reader can still tell one was set."""
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
                    return "—"
                try:
                    pp = Path(p)
                    # Cached probe only — this runs on the UI thread and get_drive_type()
                    # may shell out for seconds. 'Unknown' = not probed yet.
                    return (f"{get_filesystem_type(pp)} | "
                            f"{drive_type_cached(pp)} | "
                            f"Free: {format_size(get_free_space(pp))}")
                except Exception:
                    return "—"

            temp_p  = ""
            out_p   = ""
            try:
                s = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
                temp_p = s.get("temp_folder", "")
                out_p  = s.get("output_folder", "")
            except Exception:
                pass

            session_info = "\n".join([
                f"{APP_NAME} {APP_VERSION}",
                f"Generated:      {now_datetime()}",
                f"Python:         {sys.version}",
                f"OS:             {sys.platform} {os.name}",
                "",
                f"Last Command:   {last_cmd}",
                "",
                f"Temp Folder:    {temp_p or '—'}",
                f"Temp Drive:     {_drive_info(temp_p)}",
                f"Output Folder:  {out_p or '—'}",
                f"Output Drive:   {_drive_info(out_p)}",
                "",
                extra_info,
            ])
            zf.writestr("session_info.txt", session_info)
        return zip_path
    except Exception:
        return None


# ─── Settings Window ──────────────────────────────────────────────────────────

class SettingsView:
    """Settings as a view of the main window: a page list, then the chosen page. A page
    is built the first time it is shown. Everything applies immediately."""

    PAGES = [
        ("general", "General", "settings",
         "Where new jobs write, and how the app tells you it is done. Changes apply immediately."),
        ("compression", "Compression", "package", "What a new job starts with, and the CPU cores."),
        ("archives", "Archives", "key", "Passwords for protected archives."),
        ("drives", "Drives & space", "drive",
         "Where archives are unpacked, how much free space a job needs, and how drives are kept awake."),
        ("backport", "Backport & libraries", "layers",
         "Libraries for backports and PlayGo titles, and the optional Windows fPKG backend. You supply "
         "all of them; none ship with the app."),
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
        # FOLDERS
        self._section_label(scroll, "Folders")
        fold = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        fold.pack(fill="x", pady=(4, 12))
        fold.grid_columnconfigure(1, weight=1)
        for row_i, (lbl, var, key, title) in enumerate([
            ("Default output folder", self.app.output_var, "output_folder", "Choose the default output folder"),
            ("Default temp folder",   self.app.temp_var,   "temp_folder",   "Choose the default temp folder"),
        ]):
            ctk.CTkLabel(fold, text=lbl + ":", text_color=MUTED, anchor="w", width=170).grid(
                row=row_i, column=0, padx=14, pady=8, sticky="w")
            ctk.CTkEntry(fold, textvariable=var, fg_color=CARD, border_color=BORDER2,
                          text_color=WHITE).grid(row=row_i, column=1, sticky="ew", padx=(0, 8), pady=8)
            ctk.CTkButton(fold, text="Browse", width=80, fg_color=BTN, text_color=WHITE,
                           hover_color=BTN_HOVER,
                           command=lambda v=var, k=key, t=title: self._browse_folder(v, k, t), border_width=1, border_color=BTN_BORDER).grid(
                row=row_i, column=2, padx=(0, 14), pady=8)
        _ct = ctk.CTkFrame(fold, fg_color="transparent")
        _ct.grid(row=2, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 10))
        ctk.CTkLabel(_ct, text="Leftovers from cancelled or failed jobs stay in the temp folder until cleaned.",
                     text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left")
        ctk.CTkButton(_ct, text="Clean temp now", width=120, fg_color=BTN, text_color=WHITE, hover_color=BTN_HOVER,
                      border_width=1, border_color=BTN_BORDER, command=self.app.clear_temp_files).pack(side="right")

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

        # WHEN A JOB IS DONE
        self._section_label(scroll, "When a job is done")
        jd = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        jd.pack(fill="x", pady=(4, 12))
        self._after_seg = seg_row(jd, "Source", self.app.after_source_var, aj.ACTIONS, aj.LABELS,
                                  command=self._on_after_source, pady=(10, 2))
        self._after_extra = ctk.CTkFrame(jd, fg_color=PANEL)
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
        self._after_note = ctk.CTkLabel(self._after_extra, text="Deleted for good; it does not go to the Trash.",
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
        ctk.CTkFrame(jd, fg_color=PANEL, height=6).pack()

        # WHEN THE QUEUE IS DONE
        self._section_label(scroll, "When the queue is done")
        qd = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        qd.pack(fill="x", pady=(4, 12))
        seg_row(qd, "Then", self.app.after_queue_var, aj.QUEUE_ACTIONS, aj.QUEUE_LABELS, pady=(10, 2))
        ctk.CTkLabel(qd, text="Only after the last job of a run, not after Stop. Sleep and Quit wait 30 seconds "
                              "in a small window you can cancel.",
                     text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=600, justify="left").pack(
            anchor="w", padx=14 + 82, pady=(2, 10))

        # USER INTERFACE
        self._section_label(scroll, "Interface")
        ui = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        ui.pack(fill="x", pady=(4, 12))
        ctk.CTkCheckBox(ui, text="Auto-integrate patch from release folder", variable=self.app.auto_integrate_patch_var,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18,
                        checkbox_height=18).pack(anchor="w", padx=14, pady=(10, 6))
        oe_row = ctk.CTkFrame(ui, fg_color=PANEL)
        oe_row.pack(fill="x", padx=14, pady=(4, 4))
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


    def _page_compression(self, scroll):
        comp = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        comp.pack(fill="x", pady=(4, 12))
        for text, var, key in [
            ("Keep intermediate PFS image",           self.app.keep_pfs_var,        None),
            ("Verify output (slower, uses more RAM)", self.app.verify_output_var,    None),
            ("Auto-clear temp folder after success",  self.app.auto_clear_temp_var,  "auto_clear_temp"),
            ("Verbose mkpfs output (debug)",          self.app.verbose_var,           None),
        ]:
            cb = ctk.CTkCheckBox(comp, text=text, variable=var, fg_color=ACCENT,
                                  hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18, checkbox_height=18)
            if key:
                cb.configure(command=lambda k=key, v=var: save_settings({k: v.get()}))
            cb.pack(anchor="w", padx=14, pady=6)

        # Defaults for new jobs. Each job keeps its own compression (Add job › Output), the
        # editor starts from these and remembers the last one used; cores are the machine's.
        _st = ctk.CTkFrame(comp, fg_color="transparent")
        _st.pack(fill="x", padx=14, pady=(4, 8))
        _st.columnconfigure(1, weight=1)

        ctk.CTkLabel(_st, text=".ffpfsc level for new jobs:", text_color=WHITE,
                      font=ctk.CTkFont(size=12)).grid(row=0, column=0, sticky="w", pady=4)
        ctk.CTkSlider(_st, from_=1, to=9, number_of_steps=8,
                       variable=self.app.compression_level_var,
                       fg_color=BORDER2, progress_color=ACCENT, button_color=ACCENT,
                       button_hover_color=ACCENT_HOVER).grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        _cl_lbl = ctk.CTkLabel(_st, text=str(self.app.compression_level_var.get()),
                                 text_color=ACCENT, font=ctk.CTkFont(size=12, weight="bold"), width=24)
        _cl_lbl.grid(row=0, column=2)
        def _cl_cb(*_):
            # The app-level var already persists on write (trace added at creation);
            # this callback only refreshes the page-local label. Guard against firing
            # after the page was rebuilt (the trace can outlive the label).
            if _cl_lbl.winfo_exists():
                _cl_lbl.configure(text=str(self.app.compression_level_var.get()))
        self.app.compression_level_var.trace_add("write", _cl_cb)

        ctk.CTkLabel(_st, text=".pkg Kraken level for new jobs:", text_color=WHITE,
                      font=ctk.CTkFont(size=12)).grid(row=1, column=0, sticky="w", pady=4)
        _pkg_level = tk.IntVar(value=self.app._pkg_level_default())
        ctk.CTkSlider(_st, from_=-4, to=9, number_of_steps=13, variable=_pkg_level,
                       fg_color=BORDER2, progress_color=ACCENT, button_color=ACCENT,
                       button_hover_color=ACCENT_HOVER).grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        _pkg_lbl = ctk.CTkLabel(_st, text=str(_pkg_level.get()),
                                text_color=ACCENT, font=ctk.CTkFont(size=12, weight="bold"), width=24)
        _pkg_lbl.grid(row=1, column=2)

        def _pkg_cb(*_):
            try:
                v = max(-4, min(9, int(_pkg_level.get())))
            except (TypeError, ValueError):
                return
            self.app.fpkg_defaults["level"] = v
            save_settings({"fpkg_defaults": self.app.fpkg_defaults})
            if _pkg_lbl.winfo_exists():
                _pkg_lbl.configure(text=str(v))
        _pkg_level.trace_add("write", _pkg_cb)

        ctk.CTkLabel(_st, text="CPU cores (0=auto):", text_color=WHITE,
                      font=ctk.CTkFont(size=12)).grid(row=2, column=0, sticky="w", pady=4)
        ctk.CTkSlider(_st, from_=0, to=16, number_of_steps=16,
                       variable=self.app.cpu_count_var,
                       fg_color=BORDER2, progress_color=ACCENT, button_color=ACCENT,
                       button_hover_color=ACCENT_HOVER).grid(row=2, column=1, sticky="ew", padx=8, pady=4)
        _cpu_lbl = ctk.CTkLabel(_st, text="auto" if self.app.cpu_count_var.get() == 0 else str(self.app.cpu_count_var.get()),
                                  text_color=ACCENT, font=ctk.CTkFont(size=12, weight="bold"), width=24)
        _cpu_lbl.grid(row=2, column=2)
        def _cpu_cb(*_):
            if _cpu_lbl.winfo_exists():
                v = self.app.cpu_count_var.get()
                _cpu_lbl.configure(text="auto" if v == 0 else str(v))
        self.app.cpu_count_var.trace_add("write", _cpu_cb)

        ctk.CTkLabel(_st, text="Each job keeps its own compression, set in Add job under Output; a new job starts "
                               "from these values, and the editor remembers the last one you used. Higher .ffpfsc "
                               "levels give smaller files and take longer; 7 is the default. Blocks are always "
                               "64 KiB, the size the PS5 needs. With cores on auto the app decides by game size "
                               "and uses fewer when the output drive is a slow HDD.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                      wraplength=640).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ── Folder bundles ───────────────────────────────────────────────────
        _cs_cb = ctk.CTkCheckBox(
            comp,
            text="Copy extra files (DLCs etc.) next to the .ffpfsc when packing a folder",
            variable=self.app.copy_siblings_var, fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE,
            command=lambda: save_settings({"copy_bundle_siblings": self.app.copy_siblings_var.get()}), checkbox_width=18, checkbox_height=18
        )
        _cs_cb.pack(anchor="w", padx=14, pady=(2, 4))
        ctk.CTkLabel(comp, text="When a folder holds one game plus extras, the source folder is recreated "
                                "at the destination with the .ffpfsc and the extras inside.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left").pack(
            anchor="w", padx=14, pady=(0, 10))


    def _page_archives(self, scroll):
        comp = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        comp.pack(fill="x", pady=(4, 12))
        ctk.CTkLabel(comp, text="Default archive password (optional):", text_color=MUTED, anchor="w").pack(
            anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(comp, text="Tried FIRST. A one-off password for the next extraction.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w").pack(
            anchor="w", padx=14)
        ctk.CTkEntry(comp, textvariable=self.app.password_var, show="*",
                      fg_color=CARD, border_color=BORDER2, text_color=WHITE).pack(
            fill="x", padx=14, pady=(4, 10))

        # ── Global auto-tried password list ──────────────────────────────────
        ctk.CTkLabel(comp, text="Saved archive passwords (tried in order, one per line):",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(comp, text="Every password here is tried automatically, in order — handy for a "
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

        # Drive usage — where archives get extracted (shared with the main panel).
        dm_labels = {"auto": "Auto (smart)", "temp": "Temp drive only", "spread": "Spread across drives"}
        dm_rev = {v: k for k, v in dm_labels.items()}
        dm_menu = ctk.CTkOptionMenu(
            _ds_row("Drive usage:"), values=list(dm_labels.values()), width=200,
            command=lambda disp: self.app.drive_mode_var.set(dm_rev.get(disp, "auto")))
        dm_menu.set(dm_labels.get(self.app.drive_mode_var.get(), "Auto (smart)"))
        dm_menu.pack(side="left")

        # Temp space safety factor — scales only the temp headroom requirement.
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

        # What to do when temp/output is too small for the game.
        lp_labels = {"ask": "Ask me", "auto": "Proceed anyway", "skip": "Skip the game"}
        lp_rev = {v: k for k, v in lp_labels.items()}
        lp_menu = ctk.CTkOptionMenu(
            _ds_row("When space is low:"), values=list(lp_labels.values()), width=200,
            command=lambda disp: save_settings({"low_space_policy": lp_rev.get(disp, "ask")}))
        lp_menu.set(lp_labels.get(s.get("low_space_policy", "ask"), "Ask me"))
        lp_menu.pack(side="left")

        # Same-drive read+write: costless on an SSD, slow (seek thrashing) on an HDD. When
        # allowed, the build keeps the source on the temp drive instead of routing it to a
        # slower output drive. Auto = allow only when the temp drive probes as an SSD.
        sd_labels = {"auto": "Auto (allow on SSD)", "always": "Always allow", "never": "Never (always split)"}
        sd_rev = {v: k for k, v in sd_labels.items()}
        sd_menu = ctk.CTkOptionMenu(
            _ds_row("Same-drive read+write:"), values=list(sd_labels.values()), width=200,
            command=lambda disp: save_settings({"same_drive_rw": sd_rev.get(disp, "auto")}))
        sd_menu.set(sd_labels.get(s.get("same_drive_rw", "auto"), sd_labels["auto"]))
        sd_menu.pack(side="left")

        ctk.CTkCheckBox(ds, text="Show the drive space check before each pack",
                         variable=self.app.show_space_dialog_var, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18, checkbox_height=18).pack(anchor="w", padx=14, pady=(6, 4))
        ctk.CTkCheckBox(ds, text="Build via exFAT intermediate — PSBrew's most-stable path (cross-platform)",
                         variable=self.app.build_via_exfat_var, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18, checkbox_height=18).pack(anchor="w", padx=14, pady=(0, 4))
        def _confirm_fake_sign():
            # Warn once when ENABLING: every pack will then mutate the source folder
            # in place (the toolbar button asks per-run; the setting is persistent).
            if not self.app.fake_sign_before_pack_var.get():
                return
            if not messagebox.askyesno(
                    "Fake-sign before packing?",
                    "With this on, every pack of a game FOLDER will fake-sign its "
                    "executables IN PLACE before packing — modifying your source files "
                    "(already-signed files are skipped). Disk-image sources are unaffected.\n\n"
                    "Enable this?"):
                self.app.fake_sign_before_pack_var.set(False)
        ctk.CTkCheckBox(ds, text="Fake-sign executables before packing (folder sources; in place)",
                         variable=self.app.fake_sign_before_pack_var, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, text_color=WHITE,
                         command=_confirm_fake_sign, checkbox_width=18, checkbox_height=18).pack(anchor="w", padx=14, pady=(0, 4))
        ctk.CTkCheckBox(ds, text="Keep external drives spun-up during a run (bridges gaps between games)",
                         variable=self.app.keep_drives_awake_var, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, text_color=WHITE, checkbox_width=18, checkbox_height=18).pack(anchor="w", padx=14, pady=(0, 4))
        ka_labels = {5: "every 5 s", 8: "every 8 s (recommended)", 10: "every 10 s", 15: "every 15 s"}
        ka_rev = {v: k for k, v in ka_labels.items()}
        ka_menu = ctk.CTkOptionMenu(
            _ds_row("Keep-awake interval:"), values=list(ka_labels.values()), width=200,
            command=lambda disp: save_settings({"keep_awake_interval": ka_rev.get(disp, 8)}))
        try:
            _cur_ka = int(s.get("keep_awake_interval", 8))
        except (TypeError, ValueError):
            _cur_ka = 8
        ka_menu.set(ka_labels.get(_cur_ka, "every 8 s (recommended)"))
        ka_menu.pack(side="left")

        # ── Extra temp drives (pool) ─────────────────────────────────────────
        ctk.CTkLabel(ds, text="Extra temp drives (pool, one path per line):",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(ds, text="Add more fast scratch drives here (e.g. external SSDs). For a big archive "
                              "game that won't fit one drive, the source is extracted to one and the inner "
                              "image built on another — so pass 1 stays SSD↔SSD instead of reading off the "
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
            self.app._warm_drive_types()   # probe SSD/HDD for the new drives

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
                              "fit the finished .ffpfsc, and 'Skip' applies per game in a batch. The exFAT mode "
                              "builds through a real exFAT volume instead of the folder builder: slower, and "
                              "worth a try when a folder-built .ffpfsc crashes the console.\n\n"
                              "Keep-awake runs only while a job packs. A tiny write every few seconds keeps "
                              "bus-powered HDDs spinning between games, on the drive the job is not using. Keep "
                              "the interval under about 8 s, the drive's park timer; when idle the drive sleeps "
                              "normally.",
                      text_color=MUTED, justify="left", wraplength=640).pack(anchor="w", padx=14, pady=(0, 10))


    def _page_backport(self, scroll):
        ds = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        ds.pack(fill="x", pady=(4, 12))
        # ── AMPR / APR emu folder (PlayGo titles) ────────────────────────────
        ctk.CTkLabel(ds, text="AMPR / APR (PlayGo) — emu files folder:",
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
        ctk.CTkButton(_ampr_row, text="Browse", width=80, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=_browse_ampr).pack(side="left")

        # ── Backport folders ─────────────────────────────────────────────────
        # The firmware folder comes first: Prepare reads from it, Check compares with it,
        # and every subfolder in it is a backport target.
        ctk.CTkLabel(ds, text="Backport — firmware libraries:",
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
        _fw_status = tk.StringVar(value="")

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
        ctk.CTkButton(_fw_row, text="Browse", width=80, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=_browse_fw).pack(side="left")
        ctk.CTkLabel(ds, textvariable=_fw_status, text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w",
                      justify="left", wraplength=640).pack(anchor="w", padx=14, pady=(2, 4))
        _show_fw_status()

        ctk.CTkLabel(ds, text="Backport — patched libraries (default for new jobs):",
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
        ctk.CTkButton(_bl_row, text="Browse", width=80, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=_browse_bl).pack(side="left")

        # One-click prepare: BestPig BackPork patches (small, ~1 KB total per target) →
        # applied to the user's 10.01 libraries → written into the patched-libraries
        # folder above, in a target subfolder. Runs in a thread; log lines go to the app
        # log so the user sees per-library progress.
        _prep_row = ctk.CTkFrame(ds, fg_color="transparent")
        _prep_row.pack(fill="x", padx=14, pady=(6, 0))
        ctk.CTkLabel(_prep_row, text="Prepare from BestPig BackPork patches:",
                      text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left")
        for _t in ("7.61", "6.02"):
            ctk.CTkButton(_prep_row, text=f"Prepare {_t}", width=100, height=26,
                           fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                           command=lambda t=_t: self.app.prepare_backport_libs(t), border_width=1, border_color=BTN_BORDER).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(ds, text="Downloads BackPork's current .bps patches for the target from its public GitHub "
                              "repo (small, cached), applies them to the libraries in your 10.01 subfolder and "
                              "writes the result into the patched libraries folder, in a subfolder named after "
                              "the target. The libraries never leave your machine.",
                      text_color=MUTED, font=ctk.CTkFont(size=12), anchor="w", justify="left",
                      wraplength=640).pack(anchor="w", padx=14, pady=(4, 4))

        # ── fPKG: optional Sony Publishing Tools DLL ─────────────────────────
        ctk.CTkLabel(ds, text="fPKG — Publishing Tools DLL (optional, Windows only):",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(ds, text="Path to your own libScePubTools.dll (Sony SDK, not bundled). Only used on Windows "
                              "when an fPKG job selects the 'publishingtools' Kraken backend — LibProsperoPkg "
                              "refuses that backend on macOS (the build would fail), so here the built-in "
                              "managed Kraken encoder is always used.",
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
        ctk.CTkButton(_pt_row, text="Browse", width=80, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=_browse_pt).pack(side="left")


    def _page_about(self, scroll):
        about = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        about.pack(fill="x", pady=(4, 12))
        for line in [
            f"Version   {APP_VERSION}",
            f"MkPFS     {MKPFS_NAME} {MKPFS_VERSION}",
            f"Config    {SETTINGS_FILE}",
            f"History   {HISTORY_FILE}",
            f"Log       {RAW_LOG_FILE}",
        ]:
            ctk.CTkLabel(about, text=line, text_color=MUTED, anchor="w",
                         font=ctk.CTkFont(family=MONO_FONT, size=11)).pack(anchor="w", padx=14, pady=3)
        ctk.CTkLabel(about, text="By Knutwurst. The backend grew out of Bizkut's ps5-ffpfs-cli; images are built "
                                 "with PSBrew's MkPFS and packages with LibProsperoPkg. See NOTICES.md.",
                     text_color=MUTED, font=ctk.CTkFont(size=12), wraplength=520, justify="left").pack(
            anchor="w", padx=14, pady=(8, 12))
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
            if not messagebox.askyesno(
                    "Delete sources?",
                    "From now on, a new job deletes its source for good once it is Done: the game folder, "
                    "the archive with all its parts, or the container. It does not go to the Trash.\n\n"
                    "Jobs already in the queue keep their own choice. Use Delete for new jobs?"):
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


# ─── CLI Worker ────────────────────────────────────────────────────────────────

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

    # A PATCH/auto-patch job runs the backend's PATCH MODE: it EXTRACTS the source
    # first (one or two unpack passes) and THEN repacks (pass-1 build → pass-2
    # compress). The normal WEIGHTS put "Extracting" at 18-93%, which makes the bar
    # shoot to 93% during the extract and then jump BACKWARD when repacking starts.
    # These weights order the bands the way a patch actually runs, so progress only
    # ever moves forward. Selected when "--patch" is in the command.
    PATCH_WEIGHTS = {
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   40),   # outer→inner→files unpack(s)
        "Reading Game":        (40,  42),   # overlay / prep
        "Creating Temp PFS":   (42,  64),   # pass-1: build patched PFS image
        "Compressing":         (64,  92),   # pass-2: compress to .ffpfsc
        "Writing Final Image": (92,  98),
        "Verifying Output":    (98,  99),
        "Cleaning Up":         (99, 100),
        "Complete":            (100, 100),
    }

    # fPKG BUILD (LibProsperoPkg): source scan → [optional image unwrap] → inner
    # pfs_image.dat with Kraken (the long part) → NAPS + outer-PFS AES-XTS → CNT/FIH
    # finalize → auto-validate. Bands keep the fixed breadcrumb order forward-only;
    # the backend emits "[PHASE] <Stage>" markers + progress bars for each band.
    # Bands from a real 160 GB run (2.2.0, fast preset): unpack 16 min, Kraken 75 min on one
    # worker, outer PFS 22 min on one worker; both passes now use every core, so Kraken and
    # the outer pass shrink toward the unpack. The split below is the middle of that range,
    # an estimate; the bars inside each band are metered in bytes by the backend.
    FPKG_BUILD_WEIGHTS = {
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   34),   # only when the source is a .ffpfsc/.exfat image
        "Reading Game":        (34,  38),   # the staging copy of a folder source, when one is needed
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

    # A chain job (--to) that starts from a container unpacks it first, applies the
    # changes ("Reading Game": patch / backport / sign), then builds the output. The
    # bands follow that order so the bar only moves forward. The shares are an estimate:
    # the unpack runs at temp-drive speed, the compress onto the output drive is slower.
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
    CHAIN_FFPFS_WEIGHTS = {       # uncompressed output: pass 1 writes the output itself
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   40),
        "Reading Game":        (40,  45),
        "Creating Temp PFS":   (45,  96),
        "Verifying Output":    (96,  99),
        "Cleaning Up":         (99, 100),
        "Complete":            (100, 100),
    }
    CHAIN_FOLDER_WEIGHTS = {      # unpack (and change) into a folder
        "Scanning Files":      (0,    2),
        "Extracting":          (2,   90),
        "Reading Game":        (90,  98),
        "Cleaning Up":         (98, 100),
        "Complete":            (100, 100),
    }
    # A plain copy or move (the copy job, also what a chain job with nothing to change
    # becomes): the write is the whole job. Switched to on the backend's "[JOB] copy" line.
    COPY_WEIGHTS = {
        "Writing Final Image": (0,   97),
        "Cleaning Up":         (97, 100),
        "Complete":            (100, 100),
    }
    _COPY_ORDER = ["Writing Final Image", "Cleaning Up", "Complete"]
    # Stage order for the jobs that unpack first (patch, chain, fPKG). The plain pack
    # order below ranks "Extracting" late because an unpack job does nothing else.
    _EXTRACT_FIRST_ORDER = [
        "Scanning Files", "Extracting", "Reading Game", "Creating Temp PFS",
        "Compressing", "Writing Final Image", "Verifying Output", "Cleaning Up", "Complete",
    ]
    # Container suffixes a chain target reads without unpacking (cli.py `native`).
    _CHAIN_NATIVE = {"ffpfs": {".exfat", ".ffpkg", ".ffpfs", ".zip", ".rar"},
                     "ffpfsc": {".exfat", ".ffpkg", ".ffpfs", ".zip", ".rar"},
                     "pkg": {".ffpfs", ".ffpfsc", ".exfat", ".ffpkg"}}

    @staticmethod
    def _without_extract(weights: dict) -> dict:
        """*weights* for a run that skips "Extracting": the later bands stretch over the
        freed share, so a folder source does not jump straight past it."""
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
        """The progress bands and stage order for this job's command."""
        cmd = cmd or []
        if self._is_fpkg_build:
            src = self._cmd_value(cmd, "--fpkg-build")
            unpacks = bool(src) and Path(src).is_file()
            w = self.FPKG_BUILD_WEIGHTS
            return (w if unpacks else self._without_extract(w)), self._EXTRACT_FIRST_ORDER
        if self._is_fpkg_extract:
            return self.FPKG_EXTRACT_WEIGHTS, self._EXTRACT_FIRST_ORDER
        to = self._cmd_value(cmd, "--to")
        if to:
            src = Path(str(getattr(self.item, "path", "") or ""))
            changes = any(f in cmd for f in ("--patch", "--backport-target", "--sign"))
            unpacks = src.is_file() and (changes or src.suffix.lower() not in self._CHAIN_NATIVE.get(to, set()))
            if to == "pkg":
                # the fPKG builder unwraps an image source itself, behind the same marker
                unpacks = src.is_file()
                w = self.FPKG_BUILD_WEIGHTS
            elif to == "folder":
                w = self.CHAIN_FOLDER_WEIGHTS
            elif to == "ffpfs":
                w = self.CHAIN_FFPFS_WEIGHTS
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
        # PATCH MODE (manual Integrate Patch OR auto-integrate) extracts-then-repacks,
        # so it needs the forward-only patch weights and honours the backend's explicit
        # [PHASE] markers to advance past "Extracting". Detected by the --patch flag.
        self._is_patch = "--patch" in (cmd or []) and "--to" not in (cmd or [])
        self._is_fpkg_build   = "--fpkg-build"   in (cmd or [])
        self._is_fpkg_extract = "--fpkg-extract" in (cmd or [])
        # A chain to .pkg ends in the fPKG builder: its bar labels are free text, so it
        # needs the same stage lock, and no ShadowMount check on the .pkg it makes.
        _chain_to = self._cmd_value(cmd or [], "--to")
        self._is_fpkg = self._is_fpkg_build or self._is_fpkg_extract or _chain_to == "pkg"
        self._weights, self._stage_order = self._pick_weights(cmd)
        self._is_copy = False
        self.patch_backup = ""        # the originals a patch replaced, staged by the backend
        self.validate_failed = False   # the .pkg checklist reported failures: the source stays
        self.consumed = False          # the backend took the unpacked copy's files into the package as it went
        self._written: list[str] = []  # extra outputs of this job (bundle extras, PS4 title folders)
        # Highest whole-job progress sent so far: the queue bar never moves backward.
        self._overall_sent = 0.0
        # Snapshot the copy-extras toggle on the MAIN thread (CLIWorker is constructed
        # there); Tk variables are not safe to read from the worker thread.
        try:
            self.copy_siblings = bool(app.copy_siblings_var.get())
        except Exception:
            self.copy_siblings = True
        self.output_path = ""
        self.final_size = 0
        self.speed = "—"
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
        self._mem_error_shown = False   # reset per-job so next run can show it again

    def run(self):
        ensure_app_dir()
        self.start_time = time.time()
        self.last_heartbeat = self.start_time
        _ticker_stop = threading.Event()   # stops the liveness ticker (see below)

        # A bundle writes its .ffpfsc into a recreated sub-folder; make sure that
        # folder exists before the backend tries to write into it.
        if self.operation != "unpack":
            try:
                self.output_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

        # Raw log strategy: rolling tail buffer.
        # All backend lines are held in a deque; error lines are always kept in a
        # separate list so they are never dropped.  At job end the last 10 MB of
        # regular output + all errors are written to disk.
        RAW_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB tail window
        from collections import deque as _deque
        _raw_lines: _deque[str] = _deque()   # rolling ring buffer (all lines)
        _raw_errors: list[str]  = []          # every ERROR/FAILED line (always kept)
        _raw_buf_bytes          = 0           # current byte count in _raw_lines

        def _raw_append(text: str):
            nonlocal _raw_buf_bytes
            encoded = text.encode("utf-8", errors="replace")
            _raw_lines.append(text)
            _raw_buf_bytes += len(encoded)
            # Keep last 10 MB — drop oldest lines from the front
            while _raw_buf_bytes > RAW_LOG_MAX_BYTES and _raw_lines:
                dropped = _raw_lines.popleft()
                _raw_buf_bytes -= len(dropped.encode("utf-8", errors="replace"))

        self.app.log("INFO", f"{APP_NAME} {APP_VERSION} started")
        self.app.log("INFO", f"Backend: {BACKEND_NAME}")
        self.app.log("INFO", f"MkPFS: {MKPFS_NAME} v{MKPFS_VERSION}")
        _op_label = {"unpack": "Unpack", "patch": "Integrate patch",
                     "fake-sign": "Fake sign", "fpkg-extract": "Extract fPKG",
                     "fpkg-build": "Build fPKG"}.get(self.operation, "Pack")
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
                # Own session/process group on POSIX so cancel/close can kill the
                # whole tree (backend + mkpfs mp.Pool workers), not just the parent.
                start_new_session=(os.name != "nt"),
            )
            self.app.current_process = self.proc

            # ── Liveness ticker: heartbeat + temp-peak sampling on a wall-clock
            #    timer, independent of stdout. Without this the heartbeat lived
            #    inside the blocking stdout loop and never fired during silent
            #    phases (e.g. the final-image write), making the app look hung.
            def _ticker():
                _last_sample = 0.0
                while not _ticker_stop.wait(5):
                    t = time.time()
                    # Sizing the temp tree is a stat storm on the drive mkpfs is busy
                    # with; sample it every 30 s, not on every tick.
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
                            # Keep the status panel alive (elapsed ticking) during silent
                            # phases, but DON'T spam the log — a 30 s "Still working" line
                            # helps no one and buries the useful output.
                            # The detail the stage last showed, so the card text does not
                            # flip between two messages (and the layout with it).
                            self.app.status_update("Still Working",
                                                    getattr(self, "_detail", "") or "Backend is active. Do not close the app.",
                                                    self.phase, self.stage_progress.get(self.phase, 0),
                                                    self._job_overall(), elapsed, self.speed, "—",
                                                    job=self.item)
                        except Exception:
                            pass
            threading.Thread(target=_ticker, daemon=True).start()

            # ── Define flush helper (used periodically during run AND at end) ─
            _last_flush_t = time.time()
            FLUSH_INTERVAL = 30  # write raw log to disk every ~30 s during the run

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

            # ── Read backend output line by line ──────────────────────────────
            for line in self.proc.stdout or []:
                if self.app.cancel_requested:
                    self._terminate()
                    break

                clean = line.rstrip("\r\n")
                _raw_append(clean + "\n")
                # Always collect error lines separately so they survive the tail trim
                upper_c = clean.upper()
                if re.search(r'\bERROR\b|\bFAILED\b', upper_c):
                    _raw_errors.append(clean + "\n")
                self._handle_line(clean)

                # Heartbeat now runs on the independent ticker thread (above), so
                # it keeps firing during silent phases. Here we only flush the log.
                t = time.time()
                if t - _last_flush_t >= FLUSH_INTERVAL:
                    _flush_raw_log()
                    _last_flush_t = t

            # ── stdout fully consumed — now wait for process to exit ───────────
            code = self.proc.wait() if self.proc else 1

            # ── Final flush of rolling log buffer ─────────────────────────────
            _flush_raw_log()

            if self.app.cancel_requested:
                self.app.finish(False, "Cancelled by user.", self.last_cmd_str)
                return
            # A copy job's exit 2 / 3 are deliberate SKIPS, not failures: the source and
            # destination are the same file (2), or a different file already owns the
            # destination name (3). Nothing was written, nothing was lost, and the
            # pack-flavoured "Compression Failed" dialog (with MkPFS/temp-space causes)
            # would be actively misleading. Report them as a plain skip instead.
            if self.operation == "copy" and code in (2, 3):
                reason = ("source and destination are the same file"
                          if code == 2 else
                          "a different file already uses that name in the output folder")
                self._write_report(True)
                self.app.finish(True, f"Copy skipped — {reason}. Nothing was changed.",
                                self.last_cmd_str)
                return
            if code != 0:
                smart = smart_error_from_log()
                msg = smart if smart else f"Backend exited with code {code}."
                # Flag an out-of-memory kill so the main thread can auto-retry with fewer
                # cores: either a printed MemoryError, or a SIGKILL (-9 / 137) during a
                # pack — the OS memory-pressure kill leaves mkpfs no chance to print one.
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

            # Fake-sign produces NO new output file (it rewrites executables in place),
            # so the output-detection / bundle / ShadowMount checks below don't apply —
            # a clean exit 0 is success.
            if self.operation == "fake-sign":
                self._write_report(True)
                self.app.finish(True, "Fake-signing completed successfully.", self.last_cmd_str)
                return

            # A COPY job's output is a single file at a name the backend already picked
            # ("--copy-name" for auto-organize, else the source's basename). It landed
            # synchronously in this run — no post-hoc "did a new .ffpfsc appear" scan,
            # no bundle-siblings copy (this is a straight file transport), no ShadowMount
            # sniff (we didn't repack anything). Exit 0 with a printed [SUCCESS] line is
            # the success signal.
            if self.operation == "copy":
                self._strip_written_clutter()
                self._write_report(True)
                self.app.finish(True, "Copy completed successfully.", self.last_cmd_str)
                return

            if not self._find_output():
                self._write_report(False)
                expected = {"unpack": "extracted output folder",
                            "fpkg-extract": "extracted /app0 folder",
                            "fpkg-build": "new .pkg output"}.get(self.operation, "new .ffpfsc output")
                self.app.finish(False, f"Backend exited but no {expected} was created.", self.last_cmd_str)
                return

            # From here on the output exists and the job IS a success: bookkeeping
            # errors are logged, never turned into a failure (that would trigger the
            # failure cleanup and a full rebuild on the next START).
            # Bundle: copy the extra files (DLCs etc.) next to the new .ffpfsc / .pkg.
            try:
                if self.operation not in ("unpack", "fpkg-extract"):
                    self._copy_bundle_siblings()
            except Exception as e:
                self.app.log("WARN", f"Bundle copy failed: {e}")
            self._strip_written_clutter()
            # ShadowMount compatibility checks (a .pkg is installed, not mounted —
            # the fPKG path has its own validate checklist in the backend log).
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
            # NOTE: history is recorded on the MAIN thread in the done_q handler
            # (add_history mutates Tk widgets, which are not thread-safe).
            success_msg = {"unpack": "Extraction completed successfully.",
                           "fpkg-extract": "fPKG extracted successfully.",
                           "fpkg-build": "fPKG built and validated — see the checklist above."
                           }.get(self.operation, "Compression completed successfully.")
            self.app.finish(True, success_msg, self.last_cmd_str)
        except Exception as e:
            try:
                _raw_append(f"[GUI ERROR] {e}\n")
                _flush_raw_log()
            except Exception:
                pass
            # The reader died, not the backend: stop the backend tree before reporting,
            # or it keeps writing into scratch that the failure cleanup is about to remove.
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
        """Return the stage name inferred from a progress-bar label, or None if uncertain.

        Returning None means the caller should NOT update the stage — the line
        didn't contain enough signal to be confident about which stage we're in.
        This prevents unrecognised lines from locking the display on the current stage.
        """
        text = f"{label} {raw}".lower()  # full combined text for substring checks

        # The backend progress-bar label is everything after the "%" —
        #   "[###] 65% compress @ 290.95 MB/s ETA 14s"  → label = "compress @ 290.95 MB/s ETA 14s"
        #   "[###] 45% write @ 980.61 MB/s ETA 0s"      → label = "write @ 980.61 MB/s ETA 0s"
        #   "[###]  2% scan"                             → label = "scan"
        # Use the FIRST WORD of the label for reliable matching regardless of trailing speed/ETA.
        first_word = label.strip().lower().split()[0] if label.strip() else ""

        if self.operation == "unpack":
            if first_word in ("extract", "extracting", "unpack", "unpacking") or "extract" in text or "unpack" in text:
                return "Extracting"
            if first_word in ("read", "reading", "scan", "scanning") or "discover" in text:
                return "Reading Game"
            return "Extracting"

        # ── Final output ──────────────────────────────────────────────────────
        # Check before generic "write" so ".ffpfsc" always wins
        if ".ffpfsc" in text or "final image" in text or "final output" in text:
            return "Writing Final Image"

        # ── Backend label "write" ─────────────────────────────────────────────
        # Emitted during temp-PFS construction (before compress) AND final image write (after).
        # Distinguish by whether compression has started yet.
        if first_word in ("write", "writing"):
            if self.stage_progress.get("Compressing", 0) > 0:
                return "Writing Final Image"
            else:
                return "Creating Temp PFS"

        # ── Scan / discovery ──────────────────────────────────────────────────
        if first_word in ("scan", "scanning") or "discover" in text:
            return "Scanning Files"

        # ── Reading game files ────────────────────────────────────────────────
        if first_word in ("read", "reading"):
            return "Reading Game"

        # ── Compression ───────────────────────────────────────────────────────
        if first_word in ("compress", "compressing") or "compress" in text:
            return "Compressing"

        # ── Verify (only from a real progress bar, not plain-text messages) ───
        if first_word in ("verify", "verifying"):
            return "Verifying Output"

        if first_word in ("extract", "extracting", "unpack", "unpacking") or "extract" in text or "unpack" in text:
            return "Extracting"

        # ── Looser substring fallbacks for non-standard backend messages ──────
        if "scan" in text or "discover" in text:
            return "Scanning Files"
        if "read" in text and "write" not in text:
            return "Reading Game"
        if "clean" in text or "delete" in text or "removed" in text:
            return "Cleaning Up"

        # "Complete" is NEVER returned here — set only by run() after exit.
        # Returning None means: "uncertain — don't change the stage display."
        return None

    def _overall_for_stage(self, stage: str, pct: float) -> float:
        start, end = self._weights.get(stage, (0, 100))
        return max(0, min(100, start + (max(0, min(100, pct)) / 100) * (end - start)))

    def _overall(self):
        return self._overall_for_stage(self.phase, self.stage_progress.get(self.phase, 0))

    def _job_overall(self) -> float:
        """Whole-job progress for the queue: the current stage's band, squeezed behind
        the archive unpack the GUI already showed (ARCHIVE_EXTRACT_OVERALL_PCT) and never
        lower than a value sent before. Every status update uses this one value, so the
        heartbeat and the progress lines can no longer disagree."""
        overall = self._overall()
        if getattr(self.item, "_from_archive", False):
            share = self.app._archive_extract_pct(self.item)
            overall = share + overall * (100 - share) / 100.0
        self._overall_sent = max(self._overall_sent, overall)
        return self._overall_sent

    # Ordered list used to prevent backward stage transitions.
    # Must be a plain tuple/list literal here — _STAGE_DEFS is defined later in
    # the module (after CLIWorker), so we can't reference it at class-body time.
    _STAGE_ORDER = [
        "Scanning Files", "Reading Game", "Creating Temp PFS",
        "Compressing", "Extracting", "Writing Final Image", "Verifying Output",
        "Cleaning Up", "Complete",
    ]

    def _set_stage(self, stage, pct, label="", eta="—", force=False):
        # Never allow the stage to regress (e.g. backend prints "Writing PFS image"
        # after compression has already started — that would snap back to Temp PFS).
        order = getattr(self, "_stage_order", self._STAGE_ORDER)
        if not force and stage in order and self.phase in order:
            if order.index(stage) < order.index(self.phase):
                return
        self.phase = stage
        pct = max(0, min(100, pct))
        if stage == "Creating Temp PFS" and pct >= 100:
            pct = 99
        self.stage_progress[stage] = max(self.stage_progress.get(stage, 0), pct)

        # When a later stage begins, snap earlier stages to 100% so the
        # breadcrumbs never show a stale partial % (e.g. "Temp PFS 5%").
        # This handles backends that stop emitting progress before 100%.
        # Only stages this job has: a copy never builds a temp PFS or compresses.
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

        detail = label or f"{stage} is active."
        self._detail = None   # set below, once the stage's text is final
        if stage == "Creating Temp PFS" and not label:
            # the backend meters this step in bytes; the fixed text is only a fallback
            detail = ("Building temporary PFS image. "
                      "Large games may look frozen here — the backend is still working. "
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

        # Only log at 5 % bucket boundaries or when a stage hits 100 %.
        # Do NOT log at 0 % on every bar — that floods the log when the backend
        # emits dozens of 0 % lines before the first real progress tick.
        if self.last_stage_bucket.get(stage, -1) != bucket or int(self.stage_progress[stage]) == 100:
            self.last_stage_bucket[stage] = bucket
            self.app.log("PROGRESS", f"{stage}: {int(self.stage_progress[stage])}% {label}".strip())

    def _handle_line(self, line):
        if not line:
            return

        lower = line.lower()
        upper = line.upper()

        # ── Explicit backend phase marker ────────────────────────────────────
        # Multi-phase jobs (PATCH MODE: extract → repack) print "[PHASE] <Stage>"
        # at each step. Force the stage so the display advances even when the new
        # stage ranks "earlier" than the current one (the no-regress guard assumes
        # one forward pack sequence; a patch extracts first, then packs).
        if line.startswith("[PATCH-BACKUP] "):
            self.patch_backup = line[len("[PATCH-BACKUP] "):].strip()
            return
        if "[consume] freed" in line:
            self.consumed = True           # the extraction is no longer complete: not kept for a retry
        if line == "[JOB] copy":
            self._is_copy = True
            self._weights, self._stage_order = self.COPY_WEIGHTS, self._COPY_ORDER
            return
        if line.startswith("[PHASE] "):
            stage = line[8:].strip()
            if stage in self._weights:
                # Jobs with markers use the extract-first order, in which every marker
                # of a normal run moves forward. A marker that would move back (the fPKG
                # builder's own "Scanning Files" after a chain has unpacked) is dropped,
                # so the bar never jumps backward — and so are the bars that follow it
                # until the next accepted marker: a "100% source scan" credited to the
                # running stage pinned "Temp PFS" at 99 % for the whole Kraken pass.
                order = getattr(self, "_stage_order", self._STAGE_ORDER)
                forced = self._stage_order is self._STAGE_ORDER
                self._phase_dropped = (not forced and stage in order and self.phase in order
                                       and order.index(stage) < order.index(self.phase))
                self._set_stage(stage, 0, force=forced)
            return

        # ── Pass-1 complete (folder two-pass build) ──────────────────────────
        # The backend finished the inner uncompressed image and is about to start pass-2
        # compression. The extracted source is no longer needed (pass 2 reads only the inner
        # image) and an OOM/restart can now resume pass-2 from it — so record the image path
        # and free the extracted source NOW to halve peak temp usage.
        if line.startswith("[PASS1-DONE] "):
            inner = line[len("[PASS1-DONE] "):].strip()
            try:
                self.item._inner_image = inner
            except Exception:
                pass
            try:
                self.app.root.after(0, lambda it=self.item: self.app._free_source_after_pass1(it))
            except Exception:
                pass
            return

        # ── Intercept raw Python exception tracebacks from the backend ────────
        # Convert confusing Python tracebacks into readable, actionable messages
        # and always include UI settings suggestions the user can act on right now.

        # MemoryError — mkpfs multiprocessing pool ran out of RAM.
        # Each parallel worker loads a chunk of the source file; too many cores = OOM.
        # Only match raw Python exception lines (no leading '[' bracket like [INFO]/[OK]).
        # This avoids false-positives from mkpfs info messages that mention "MemoryError".
        # Only show once per job to avoid log spam.
        stripped = line.strip()
        if (stripped == "MemoryError"
                or ("memoryerror" in lower and not stripped.startswith("[") and "avoid" not in lower)):
            if not getattr(self, "_mem_error_shown", False):
                self._mem_error_shown = True
                self.app.log("ERROR",
                    "Out of RAM — mkpfs ran out of memory during parallel compression.\n"
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

        # Downstream noise caused by the MemoryError — suppress silently.
        if "concurrent send_bytes" in lower or "maybeencodingerror" in lower:
            return

        # OSError [Errno 22] Invalid argument on write.
        # Either the output drive is exFAT/FAT32 (4 GB file limit) or a corrupt
        # chunk was written after an OOM crash.
        if ("oserror" in lower or "ioerror" in lower) and (
            "errno 22" in lower or "invalid argument" in lower
        ):
            self.app.log("ERROR",
                "Write failed — OS error 22 (Invalid argument).\n"
                "\n"
                "  Most likely cause:  output drive is exFAT or FAT32\n"
                "    exFAT / FAT32 has a 4 GB per-file limit.\n"
                "    A large .ffpfsc will exceed this and fail mid-write.\n"
                "\n"
                "  ╔═ Settings to check / change: ═══════════════════════════════╗\n"
                "  ║  OUTPUT folder  →  move to an NTFS drive (e.g. C:\\  D:\\)   ║\n"
                "  ║  CPU cores      →  set to 1–2 if RAM could also be the cause ║\n"
                "  ╚══════════════════════════════════════════════════════════════╝"
            )
            return

        # No-space-left / disk full
        if ("errno 28" in lower or "no space left" in lower
                or "there is not enough space" in lower):
            self.app.log("ERROR",
                "Disk full — the output or temp drive ran out of space.\n"
                "\n"
                "  ╔═ Settings to check: ════════════════════════════════════════╗\n"
                "  ║  OUTPUT folder  →  point to a drive with more free space    ║\n"
                "  ║  TEMP folder    →  point to a drive with more free space    ║\n"
                "  ║                    (needs ~1.5× the game size during build) ║\n"
                "  ╚══════════════════════════════════════════════════════════════╝"
            )
            return

        if "calledprocesserror" in lower and "non-zero exit status" in lower:
            self.app.log("ERROR", "mkpfs exited with an error — see messages above.")
            if not getattr(self, "_mem_error_shown", False):
                # Generic hint only when a more specific error wasn't already shown.
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

        if "Compression complete:" in line:
            self.output_path = line.split("Compression complete:", 1)[-1].strip()
        if "fPKG complete:" in line:
            self.output_path = line.split("fPKG complete:", 1)[-1].strip()
        if line.startswith("[OK] Organized: "):
            self.output_path = line[len("[OK] Organized: "):].split(" (replaced)")[0].strip()
            self._written.append(self.output_path)
        if line.startswith("[OK] PS4 sorted: "):
            self.output_path = line[len("[OK] PS4 sorted: "):].strip()      # a title folder of the set
            self._written.append(self.output_path)
        _cm = re.match(r"\[SUCCESS\] (?:Copied|Moved) .+? → (.+)$", line)
        if _cm and self.operation == "copy":
            self.output_path = _cm.group(1).strip()                         # the copied file
        if "Validation reported failures" in line:
            self.validate_failed = True
        if "Extraction complete:" in line:
            # Only an UNPACK job ends at extraction. In PATCH MODE the backend
            # extracts then repacks, so latching "Extracting"=100 here would freeze
            # the bar for the whole repack — the [PHASE] markers drive it instead.
            if self.operation in ("unpack", "fpkg-extract"):
                self._set_stage("Extracting", 100, "Extraction complete.")
            maybe_path = line.split("Extraction complete:", 1)[-1].strip()
            if maybe_path:
                self.output_path = maybe_path
        if self.operation == "unpack" and re.search(r"\bOutput:\s+", line):
            self.output_path = re.split(r"\bOutput:\s+", line, maxsplit=1)[-1].strip()

        prog = PROGRESS_RE.search(line)
        if prog:
            pct = max(0, min(100, int(prog.group("pct"))))
            label = prog.group("label").strip()
            # fPKG and copy jobs: the backend drives the stage with explicit [PHASE] markers
            # and its bar labels are free text — lock the bar to the current phase instead of
            # guessing from keywords (a label like "extract inner PFS" is not mkpfs's extract,
            # and "copy" matches no keyword at all).
            if getattr(self, "_phase_dropped", False) and (self._is_fpkg or self._is_copy):
                return   # a bar of a phase the tracker refused (see the [PHASE] handling)
            stage = (self.phase if ((self._is_fpkg or self._is_copy) and self.phase in self._weights)
                     else self._stage_from_label(label, line))
            # Two bars for one step (MkPFS counts whole files, the backend's meter counts
            # bytes): a line that lags behind the leading one within a few seconds is the
            # coarser bar, and its label and speed would only make the display flicker.
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

            # Backend (mkpfs/pbar.py) emits ETA as integer seconds under 1h ('ETA 1695s')
            # but as DECIMAL MINUTES at/over 1h ('ETA 84.2m'). The capture must allow a
            # decimal AND the trailing unit, else '84.2m' grabs only '84' (no unit → read
            # as 84 SECONDS) and the leftover '.2m' is re-appended → 'ETA 1m 24s.2m'.
            # humanize_eta then renders it as 1h 05m / 28m 15s / 45s in the field + label.
            eta_match = re.search(
                r"ETA\s*([0-9]+(?:\.[0-9]+)?\s*(?:h|hr|hrs|hours?|m|min|mins|minutes?|s|sec|secs|seconds?)?)",
                label, re.I)
            if eta_match:
                eta = humanize_eta(eta_match.group(1).strip())
                label = label[:eta_match.start()] + f"ETA {eta}" + label[eta_match.end():]
            else:
                eta = "—"

            if stage is None:
                # Unrecognised progress line — update speed/eta but don't change stage
                return

            if stage == "Reading Game" and pct >= 100:
                self._set_stage("Reading Game", 100, label, eta)
                self._set_stage("Creating Temp PFS", 0, "Building temporary PFS image. Do NOT close the app.", "—")
                return

            if stage == "Compressing" and pct >= 100:
                self._set_stage("Compressing", 100, label, eta)
                # Auto-advance: compression finished — final image write is next.
                # If the backend emits its own "write" progress bars for the final
                # output, they will continue updating "Writing Final Image" from here.
                # If it writes silently, this at least moves the display off "Compressing".
                self._set_stage("Writing Final Image", 0, "Writing final .ffpfsc output file…", "—")
                return

            self._set_stage(stage, pct, label, eta)
            return

        # Hardlink / symlink failure — warn immediately, don't wait for exit code
        if "unable to stage source file" in lower or "hard link and symlink both failed" in lower:
            self.app.log("WARN",
                "Temp drive does not support hardlinks/symlinks. "
                "Fallback to copy mode — compression will be slower and needs extra space.")

        # Inner image auto-rename (MkPFS) — informational, not an error
        if "renaming inner image" in lower or "inner image renamed" in lower:
            self.app.log("INFO",
                "ℹ  mkpfs renamed the inner image to match the outer filename. "
                "This is normal — the .ffpfsc will mount correctly.")

        # Plain-text (non-progress-bar) stage hints.
        # IMPORTANT: only use very specific phrases here — broad keyword matches on
        # paths (e.g. _ffpfsc_temp, pfs_image.dat) fire too early because those
        # strings appear in the parameter dump before scanning even begins.
        if "writing pfs image to" in lower:
            # Only the exact "Writing PFS image to <path>" line marks temp-PFS start.
            # Use 0% so the subsequent [###] x% write progress bars can own the percentage
            # cleanly (the max() guard in _set_stage would pin it at 5 otherwise).
            self._set_stage("Creating Temp PFS", 0, "Building PFS image…")
        elif ".ffpfsc" in lower and self.stage_progress.get("Compressing", 0) > 0:
            # A line mentioning the final .ffpfsc output after compression has run
            # means the final image is being written (or has just been written).
            self._set_stage("Writing Final Image",
                            max(5, self.stage_progress.get("Writing Final Image", 0)),
                            "Writing final .ffpfsc output file…")
        elif "successfully wrote" in lower or "pfs creation complete" in lower:
            # PFS image fully written — advance to Compressing if not already there
            if self.stage_progress.get("Compressing", 0) == 0:
                self._set_stage("Creating Temp PFS", 100, line)
        elif self.operation == "unpack" and ("extract" in lower or "files written" in lower or "dirs created" in lower):
            self._set_stage("Extracting", 100 if "complete" in lower else max(5, self.stage_progress.get("Extracting", 0)), line)
        # NOTE: "Verifying Output" is NOT triggered from plain-text here because
        # lines like "MkPFS post-build verify is disabled..." contain "verify" and
        # would fire this stage at the very start of the run, blocking everything else.
        # Verification stage is advanced only by progress bars in _stage_from_label.
        # NOTE: "Complete" stage is intentionally NOT set here — only by run() after exit.

        tag = "INFO"
        # Use word-boundary regex so "MemoryError", "TypeError", etc. don't
        # falsely tag an INFO line as ERROR.
        if re.search(r'\bERROR\b|\bFAILED\b', upper):
            tag = "ERROR"
        elif re.search(r'\bWARN\b|\bWARNING\b', upper):
            tag = "WARN"
        elif "SUCCESS" in upper or "[OK]" in upper or "COMPLETE" in upper:
            tag = "OK"

        # Errors and warnings are ALWAYS shown — never throttled.
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
        """The output kind of a chain job ('folder' | 'ffpfs' | 'ffpfsc' | 'pkg'), None for
        every other job — so the result detection can treat a chain like the legacy job
        that produces the same thing."""
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
                if p.exists() and p.is_file() and p.stat().st_size > 0 and p.stat().st_mtime >= self.start_time - 2:
                    if self.operation == "fpkg-build" or self._chain_to() == "pkg":
                        # The backend's "[OK] fPKG complete: <path>" marker lands here — the
                        # usual path for a build, so the auto-organize rename must happen
                        # here (the glob fallback below is only reached without the marker).
                        p = self.app._finalize_pkg_name(self.item, p)
                        self.output_path = str(p)
                    self.final_size = p.stat().st_size
                    return True
            except OSError:
                pass

        if self.operation == "fpkg-extract" or self._chain_to() == "folder":
            # The backend writes straight into the job's output folder.
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
                best = self.app._finalize_pkg_name(self.item, best)   # auto-organize name
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

        # Prefer this job's expected name (<title_id>.ffpfsc) over "newest in the
        # folder" — the latter can wrongly attribute a stale/unrelated .ffpfsc to
        # a run that actually produced nothing, or pick the wrong one in a batch.
        if self.operation != "unpack":
            tid = (getattr(self.item, "title_id", "") or "").strip()
            if tid and tid not in ("📦", "Unknown"):
                try:
                    # Match both "<tid>.ffpfsc"/".ffpfs" and the descriptive
                    # "<name> [<tid>].ffpfsc"; pick the newest one this run created.
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

    def _strip_written_clutter(self) -> None:
        """Remove the '._' sidecars and other clutter from what this job wrote (see
        ultra_core.strip_written_clutter): its output, the bundle extras, PS4 title folders."""
        n = 0
        for p in [self.output_path, *self._written]:
            try:
                n += strip_written_clutter(str(p).strip('"')) if p else 0
            except Exception:
                pass
        if n:
            self.app.log("INFO", f"Removed {n} macOS clutter file(s) ('._' sidecars) from the output.")

    def _copy_bundle_siblings(self) -> None:
        """Copy the extra files AND folders (DLCs etc.) next to the packed .ffpfsc.
        Handles both loose files and whole subfolders (e.g. an '[ ALL DLC ]' wrapper),
        copied 1:1 into the output directory. No-op unless the item carries siblings and
        the user has 'copy extras' enabled. The source is never modified (copy, not move)."""
        siblings = getattr(self.item, "bundle_siblings", None)
        if not siblings:
            return
        if not getattr(self, "copy_siblings", True):
            self.app.log("INFO", "Copy extras is off — leaving DLC/extra files in the source folder.")
            return
        dest_dir = self.output_dir
        copied = 0
        for src in siblings:
            try:
                src = Path(src)
                if not src.exists():
                    continue
                target = dest_dir / src.name
                if src.is_dir():
                    # DLC / extra subfolder → copy the whole tree (merge). Skip when an
                    # identical-size copy is already there so a re-run doesn't re-copy GBs.
                    if target.resolve() == src.resolve():
                        continue   # source already sits in the destination — nothing to do
                    if target.exists() and get_folder_size(target) == get_folder_size(src):
                        copied += 1
                        continue
                    self.app.log("INFO", f"Copying extra folder next to output: {src.name} "
                                         f"({format_size(get_folder_size(src))})")
                    self._written.append(str(target))
                    shutil.copytree(src, target, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(*_COPYTREE_JUNK_GLOBS))
                    copied += 1
                elif src.is_file():
                    if target.exists() and target.stat().st_size == src.stat().st_size:
                        copied += 1
                        continue
                    self.app.log("INFO", f"Copying extra next to output: {src.name} ({format_size(src.stat().st_size)})")
                    shutil.copy2(src, target)
                    self._written.append(str(target))
                    copied += 1
            except Exception as e:
                self.app.log("WARN", f"Could not copy extra '{Path(src).name}': {e}")
        if copied:
            self.app.log("OK", f"Copied {copied} extra item(s) next to the .ffpfsc.")

    def _validate_shadowmount(self) -> list:
        """Post-compression ShadowMount compatibility checks.
        Returns a list of warning strings (empty = all OK)."""
        warns = []
        if not self.output_path:
            return ["No output path recorded — cannot validate output."]
        p = Path(self.output_path)
        if not p.exists():
            warns.append(f"Output file not found on disk: {p.name}")
            return warns
        name_lower = p.name.lower()
        if name_lower.endswith(".ffpfsc.ffpfsc"):
            warns.append(
                f"Double extension detected: {p.name}\n"
                "   Rename the file — remove one '.ffpfsc' suffix before mounting in ShadowMount."
            )
        elif not name_lower.endswith(".ffpfsc"):
            warns.append(
                f"Unexpected output extension '{p.suffix}' — expected .ffpfsc\n"
                "   ShadowMount may not recognise this file."
            )
        sz = p.stat().st_size
        if sz == 0:
            warns.append("Output file is 0 bytes — compression may have failed silently.")
        elif sz < 1 * 1024 * 1024:
            warns.append(
                f"Output file is very small ({format_size(sz)}) — "
                "the source dump may be incomplete or empty."
            )
        return warns

    def _write_report(self, success=True):
        if success:
            self._find_output()
        elapsed = time.time() - self.start_time
        if self.operation == "unpack":
            FINAL_REPORT_FILE.write_text(
                f"{APP_NAME} Report\n\n"
                f"Status: {'Success' if success else 'Failed'}\n"
                f"Operation: Extract PFS image\n"
                f"Source: {self.item.path}\n"
                f"Output: {self.output_path or 'Unknown'}\n"
                f"Source Size: {format_size(self.item.size)}\n"
                f"Extracted Size: {format_size(self.final_size)}\n"
                f"Elapsed: {format_duration(elapsed)}\n"
                f"Backend: {BACKEND_NAME}\n"
                f"MkPFS: {MKPFS_NAME} v{MKPFS_VERSION}\n",
                encoding="utf-8",
                errors="replace",
            )
            return
        saved = self.item.size - self.final_size if self.item.size and self.final_size else 0
        pct = (saved / self.item.size * 100) if self.item.size else 0
        rating, recommendation = compression_rating(pct)
        temp_removed = max(0, self.temp_peak_size - get_folder_size(self.temp_dir))
        FINAL_REPORT_FILE.write_text(
            f"{APP_NAME} Report\n\n"
            f"Status: {'Success' if success else 'Failed'}\n"
            f"Game: {self.item.name}\n"
            f"Title ID: {self.item.title_id}\n"
            f"Source: {self.item.path}\n"
            f"Output: {self.output_path or 'Unknown'}\n"
            f"Original Size: {format_size(self.item.size)}\n"
            f"Output Size: {format_size(self.final_size)}\n"
            f"Space Saved: {format_size(saved)} ({pct:.2f}%)\n"
            f"Compression Rating: {rating}\n"
            f"Recommendation: {recommendation}\n"
            f"Peak Temp Usage Seen: {format_size(self.temp_peak_size)}\n"
            f"Temporary Files Removed: {format_size(temp_removed)}\n"
            f"Elapsed: {format_duration(elapsed)}\n"
            f"Backend: {BACKEND_NAME}\n"
            f"MkPFS: {MKPFS_NAME} v{MKPFS_VERSION}\n",
            encoding="utf-8",
            errors="replace",
        )

    def _terminate(self):
        _kill_process_tree(self.proc)


# Stage definitions: (full backend name, short display label)
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
    """Terminate a backend subprocess AND its child process group (the mkpfs
    multiprocessing Pool workers). The backend is launched with start_new_session,
    so its pgid == its pid; killing the group stops the forked workers too — a bare
    proc.terminate() leaves them orphaned, burning CPU and writing temp with no UI."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                proc.kill()
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
    except Exception:
        pass


def _hdr_mode(v) -> str:
    """Normalise the fPKG HDR-flag setting to 'auto' | 'on' | 'off'. 1.1.12/1.1.13 stored a
    bool whose default (True) nobody chose consciously → 'auto'; an explicit False → 'off'."""
    if v is True:
        return "auto"
    if v is False:
        return "off"
    s = str(v or "auto").strip().lower()
    return s if s in ("auto", "on", "off") else "auto"




class JobDialog(EmbeddedDialog):
    """One job, three groups: 1. source → 2. change the content → 3. output.

    Every job the app can run is a source (folder, parent folder, archive, disk image,
    .ffpfs, .ffpfsc, .pkg), an optional set of content changes (patch → backport → sign)
    and an output format (folder / .ffpfs / .ffpfsc / .pkg). The sentence above the Add
    button says what the queue will do, and the queue row carries the same sentence. The
    backend's --to chain does the work (see cli.py CHAIN MODE); GameItem.from_chain is the
    item. Look inside (the PFS browser) is a peek, not a job — a link under the source.
    Edit mode (item given) refills the dialog and swaps the item in place."""
    LARGE = True

    _TARGET_LABEL = {"folder": "Folder", "ffpfs": ".ffpfs", "ffpfsc": ".ffpfsc", "pkg": ".pkg", "organize": "Organize"}
    _TARGETS = ("folder", "ffpfs", "ffpfsc", "pkg", "organize")
    _TARGET_KEY = {v: k for k, v in _TARGET_LABEL.items()}
    _SOURCE_HINT = ("Game folder, parent folder of games, archive (.zip/.rar/.7z), disk image "
                    "(.exfat/.ffpkg), .ffpfs, .ffpfsc or .pkg")
    _BACKPORT_HINTS = {
        "7.61":  "7.61 — public library patches exist for this target",
        "6.02":  "6.02 — experimental: a smaller public library set, only some titles run",
        "10.xx": "10.xx — SDK only, no library bundle; for a game newer than the console",
    }
    _HELP_IDLE = "Hover an option to see what it does and when you need it."
    _HELP = {
        "source": "Drop a path here, or pick a file or a folder. A parent folder of games makes one job "
                  "per game. Archives are extracted when the job runs; a container is unpacked only if "
                  "something has to change in it.",
        "look":   "Opens the browser on this image or package: the tree, and single files pulled out "
                  "without unpacking the rest. A peek, not a job.",
        "patch":  "Merges an update's files over the game's own before packing — the result is the "
                  "updated game. A folder, a .zip or a .rar. Applied first, so patched executables are "
                  "backported and signed too.",
        "backport": "Lowers the SDK version in eboot.bin and every prx/sprx so an older firmware loads "
                    "them. When the game uses functions the target lacks, it also needs patched libraries: "
                    "the job copies the set for its target from the patched libraries folder in Settings "
                    "into fakelib/. Without that folder the backport only lowers the SDK. Nothing is "
                    "bundled with this app.",
        "target": "7.61: public library patches exist, the documented path. 6.02: experimental, a "
                  "smaller public set, only some titles run. 10.xx: SDK only, for a game newer than "
                  "the console. Every other entry is a firmware from your firmware libraries folder "
                  "(Settings), with the SDK values its own libraries carry; Check shows whether it has "
                  "every function the game uses.",
        "check":  "Reads the functions the game imports and compares them with what the target "
                  "firmware's original libraries (Settings), your patched libraries and the game's own "
                  "modules export. Tells you whether lowering the SDK is enough or which libraries "
                  "are missing. Needs a game folder; a container is checked when the job runs.",
        "sign":   "Fake-signs eboot.bin and every prx/sprx so a jailbroken console loads them. Needed "
                  "when a dump's executables are still plain ELFs. Already-signed files are skipped, "
                  "so leaving it on costs nothing. A .pkg is always signed by its builder.",
        "output": "Folder: a plain /app0 folder (a folder source is changed in place). .ffpfs: "
                  "uncompressed image, fastest to build and mount, full size. .ffpfsc: compressed "
                  "image for ShadowMount, built with the 64 KiB block size verified on a console. "
                  ".pkg: installable package.",
        "saveto": "Where the result lands. With auto-organize it gets its own folder there, named "
                  "from the game.",
        "organize": "Names the folder and the file from the game's own metadata: "
                    "<Title> [TID] [vX.Y.Z]/<Title> [TID] [vX.Y] [fwN.NN], with the firmware the "
                    "game needs read from its eboot.bin (a backport lowers it to the target). Long names are shortened to "
                    "ShadowMount's byte limit.",
        "after":  "What happens to the source once this job is Done: the game folder, the archive with "
                  "all its parts, or the container. Keep leaves it where it is, also when the output is a "
                  "plain copy (on the same drive an APFS clone, instant and without extra space). Move to "
                  "Trash can be put back from the Trash; Delete cannot. Nothing happens when the job fails, "
                  "is skipped or stopped, or while another job in the queue still needs the same source. "
                  "Settings › General sets the default.",
        "retail": "Keep on. Drops placeholder license files and issues a valid debug license, sets the "
                  "retail DRM type and the retail flag in every executable, rebuilds a corrupt PlayGo "
                  "set and repairs presentation images — the configuration verified on a console. Off "
                  "only for a byte-exact re-pack or an A/B test.",
        "playgo": "Off (recommended): the game's own PlayGo layout is kept — which file sits in which "
                  "chunk, the chunk names and languages, the scenario names — and only the image "
                  "ranges are recomputed for the new package. On: the source's PlayGo files are "
                  "discarded and the builder writes a fresh one-chunk set. Same build time either "
                  "way. A corrupt or mismatching set is rebuilt regardless, so switch this on only "
                  "for a title that installs but will not start (CE-100022-5).",
        "hdr":    "auto keeps what the game's param.json declares — the publisher's intent; a console on "
                  "'HDR when supported' follows this flag. on forces it, off clears it.",
        "identity": "The package identity comes from the game's sce_sys/param.json when it is built. Fill these in "
                    "for a game folder without one (Content ID and Title ID are then required), or to supply what "
                    "param.json lacks.",
        "codec":  "The codec layer of the image inside the package. kraken is the verified default; zlib and none "
                  "are for experiments.",
        "compression": "How hard this job compresses. .ffpfsc: zlib level 1 to 9, higher gives a smaller "
                       "file and takes longer; 7 is the default. .pkg: Kraken level -4 to 9. Measured on a "
                       "retail sample: -4 to -1 are the fastest and about 2 % larger; 0 to 5 cost a tenth "
                       "more time and are 0.3 % larger than 7; 7 to 9 take 4 to 10 times as long for that "
                       "last 0.3 %. 0 is the default. Each job keeps its own; a new job starts from "
                       "Settings › Compression.",
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

    def __init__(self, app, item=None, init_src: str | None = None, init_to: str | None = None):
        super().__init__(app.root)
        self.app = app
        self.edit_item = item
        self.title("Edit job" if item else "Add job")
        self.geometry("780x600")
        self.minsize(660, 420)
        self.configure(fg_color=BLACK)
        self.transient(app.root)
        self.lift()
        self.after(50, self.grab_set)

        settings = load_settings()
        self._defaults = dict(settings.get("job_dialog_defaults", {}) or {})
        self._defaults_kind = None          # the source kind whose remembered choices are applied
        self._kind = "none"
        self._games: list[Path] = []
        self._check_thread = None

        src0 = init_src or ""
        if item is not None:
            src0 = str(getattr(item, "archive_path", None) or getattr(item, "path", "") or "")
        elif not src0:
            # Add job without a drop: pre-fill with the last source the user picked, so
            # the typical workflow (same library folder, add every new download) is one
            # press less. An invalid or missing path is dropped silently.
            _last = str(settings.get("last_source", "") or "").strip()
            if _last and Path(_last).exists():
                src0 = _last
        self.src_var = tk.StringVar(value=src0)
        # The folder the file / folder pickers open in: the parent of the last source.
        self._last_src_dir = str(settings.get("last_source_dir", "") or "").strip()
        self.detect_var = tk.StringVar(value="Choose or drop a source.")
        self.help_var = tk.StringVar(value=self._HELP_IDLE)
        self.sign_var = tk.BooleanVar(value=(bool(getattr(item, "chain_sign", False))
                                             or getattr(item, "operation", "") == "fake-sign") if item else False)
        self.patch_on_var = tk.BooleanVar(value=bool(getattr(item, "patch_source", None)) if item else False)
        self.patch_var = tk.StringVar(value=str(getattr(item, "patch_source", "") or "") if item else "")
        self.backport_on_var = tk.BooleanVar(value=bool(getattr(item, "backport_target", None)) if item else False)
        _bt0 = (getattr(item, "backport_target", None) if item else None) or settings.get("backport_target_default") or "7.61"
        self.backport_target_var = tk.StringVar(value=_bt0 if is_backport_target(_bt0) else "7.61")
        self._hint_cache: dict[str, str] = {}
        self.backport_hint_var = tk.StringVar(value="")
        self.check_var = tk.StringVar(value="")
        self.to_var = tk.StringVar(value=self._TARGET_LABEL.get(self._target_of(item) if item else "ffpfsc", ".ffpfsc"))
        out0 = str(getattr(item, "output_path", "") or "") if item else (app.output_var.get() or "").strip()
        self.out_var = tk.StringVar(value=out0)
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
        self._auto_ident: dict = {}      # the values filled in from param.json (cleared on a new source)
        self.summary_var = tk.StringVar(value="")
        self.summary_dest_var = tk.StringVar(value="")

        self._build()
        self.src_var.trace_add("write", lambda *_: self._on_source_changed())
        for v in (self.sign_var, self.patch_on_var, self.patch_var, self.backport_on_var,
                  self.backport_target_var, self.to_var, self.out_var,
                  self.organize_var, self.after_var):
            v.trace_add("write", lambda *_: self._refresh())
        self.bind("<Return>", lambda e: self._add())
        self.bind("<Escape>", lambda e: self.destroy())
        self._on_source_changed()
        if init_to in self._TARGET_LABEL:
            self.to_var.set(self._TARGET_LABEL[init_to])

    # ── layout ───────────────────────────────────────────────────────────────
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
        """Size the window to its content (options that open grow it), within the screen,
        until the user resizes it by hand; then leave the size alone."""
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
        # Packed at the end of _build: filling a scrollable frame that is already on screen
        # redraws its CTk scrollbar for every widget added (~0.4 s for this dialog).
        self.body = ScrollFrame(self, fg_color=BLACK)
        if not self.LARGE:   # a large panel fills the content area; only a window fits its content
            self.after(60, self._fit_to_content)
            self.body.bind("<Configure>", lambda e: self.after_idle(self._fit_to_content), add="+")

        # 1 · Source
        srow = self._group("1", "Source", "folder, parent folder, archive, disk image, .ffpfs, .ffpfsc or .pkg")
        sinner = ctk.CTkFrame(srow, fg_color=PANEL); sinner.pack(fill="x", padx=10, pady=(2, 2))
        ctk.CTkEntry(sinner, textvariable=self.src_var, fg_color=CARD2, text_color=WHITE).pack(side="left", fill="x", expand=True)
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

        # 2 · Change the content — each change is a box: its checkbox line, and its options
        #     frame INSIDE the same box, so the options always sit under their own checkbox.
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

        # Patch
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

        # Backport
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

        # Sign — for a .pkg the checkbox gives way to a fixed line (the builder always signs)
        so = option(self.sign_var, "Sign executables", "fake-sign eboot.bin and every prx/sprx", "sign")
        self._sign_cb, self._sign_note_lbl, self._sign_line = so.cb, so.note, so.line
        self._sign_fixed = ctk.CTkLabel(so.line, text="Signed by the package builder — every executable in a .pkg is fake-signed",
                                        text_color=MUTED, font=ctk.CTkFont(size=12))

        # 3 · Output
        orow = self._orow = self._group("3", "Output")
        oin = ctk.CTkFrame(orow, fg_color=PANEL); oin.pack(fill="x", padx=10, pady=(2, 2))
        self._to_seg = ctk.CTkSegmentedButton(oin, values=[self._TARGET_LABEL[k] for k in self._TARGETS],
                                              variable=self.to_var, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER)
        self._to_seg.pack(side="left")
        self._to_hint = tk.StringVar(value="")
        ctk.CTkLabel(oin, textvariable=self._to_hint, text_color=MUTED, font=ctk.CTkFont(size=12),
                      wraplength=420, justify="left").pack(side="left", padx=(10, 0))
        self._bind_help(self._HELP["output"], oin)
        self._oin = oin
        # Compression: one row for both compressed formats, each with its own control
        # (zlib level for .ffpfsc, the Kraken preset for .pkg); packed by _refresh.
        cr = self._comp_row = ctk.CTkFrame(orow, fg_color=PANEL)
        ctk.CTkLabel(cr, text="Compression:", text_color=MUTED, width=100, anchor="w",
                      font=ctk.CTkFont(size=12)).pack(side="left")
        self._comp_ffpfsc = ctk.CTkFrame(cr, fg_color=PANEL)
        ctk.CTkSlider(self._comp_ffpfsc, from_=1, to=9, number_of_steps=8, variable=self.level_var, width=180,
                      height=16, fg_color=BORDER2, progress_color=ACCENT, button_color=ACCENT,
                      button_hover_color=ACCENT_HOVER).pack(side="left")
        self._level_lbl = ctk.CTkLabel(self._comp_ffpfsc, text=f"level {self.level_var.get()}", text_color=WHITE,
                                       width=56, anchor="w", font=ctk.CTkFont(size=12))
        self._level_lbl.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(self._comp_ffpfsc, text="higher: smaller file, slower build", text_color=MUTED,
                      font=ctk.CTkFont(size=12)).pack(side="left", padx=(4, 0))
        self.level_var.trace_add("write", lambda *_: self._level_lbl.winfo_exists()
                                 and self._level_lbl.configure(text=f"level {self.level_var.get()}"))
        self._comp_pkg = ctk.CTkFrame(cr, fg_color=PANEL)
        ctk.CTkSlider(self._comp_pkg, from_=-4, to=9, number_of_steps=13, variable=self.pkg_level_var, width=180,
                      height=16, fg_color=BORDER2, progress_color=ACCENT, button_color=ACCENT,
                      button_hover_color=ACCENT_HOVER).pack(side="left")
        self._pkg_level_lbl = ctk.CTkLabel(self._comp_pkg, text=f"level {self.pkg_level_var.get()}", text_color=WHITE,
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
            self._pkg_level_lbl.configure(text=f"level {v}")
            self._pkg_level_hint.configure(text=self._pkg_level_text(v))
        self.pkg_level_var.trace_add("write", _pkg_level_cb)
        self._bind_help(self._HELP["compression"], cr)
        fin = ctk.CTkFrame(orow, fg_color=PANEL); fin.pack(fill="x", padx=10, pady=(4, 2))
        ctk.CTkLabel(fin, text="Save to:", text_color=MUTED, width=100, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
        self._out_entry = ctk.CTkEntry(fin, textvariable=self.out_var, fg_color=CARD2, text_color=WHITE)
        self._out_entry.pack(side="left", fill="x", expand=True)
        self._out_btn = ctk.CTkButton(fin, text="Folder…", width=76, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                                      command=self._pick_out, border_width=1, border_color=BTN_BORDER)
        self._out_btn.pack(side="left", padx=(6, 0))
        self._bind_help(self._HELP["saveto"], fin)
        oline = ctk.CTkFrame(orow, fg_color=PANEL); oline.pack(fill="x", padx=10, pady=(2, 6))
        self._organize_cb = ctk.CTkCheckBox(oline, text="Auto-organize — folder and file named from the game's own metadata",
                                            variable=self.organize_var, checkbox_width=18, checkbox_height=18,
                                            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=WHITE, font=ctk.CTkFont(size=12))
        self._organize_cb.pack(side="left")
        self._bind_help(self._HELP["organize"], self._organize_cb)
        self._oline = oline
        # After the job: what happens to the source once this job is Done (shown by _refresh)
        aj = _after_job_module()
        self._after_box = ab = ctk.CTkFrame(orow, fg_color=PANEL)
        ar = ctk.CTkFrame(ab, fg_color=PANEL); ar.pack(fill="x")
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
        self._after_note = ctk.CTkLabel(ab, text="Deleted for good once the job is Done; it does not go to the Trash.",
                                        text_color=RED, font=ctk.CTkFont(size=12), anchor="w")
        # .pkg options — two rows, shown only when .pkg is the output
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
        self._ident_head = tk.StringVar(value=self._IDENT_OPTIONAL)
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
        self._ident_note = tk.StringVar(value="")
        ctk.CTkLabel(ib, textvariable=self._ident_note, text_color=MUTED, font=ctk.CTkFont(size=11), anchor="w",
                      justify="left", wraplength=600).pack(fill="x", pady=(2, 6))
        cr = ctk.CTkFrame(ib, fg_color=PANEL); cr.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(cr, text="Codec layer:", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
        ctk.CTkSegmentedButton(cr, values=list(self._INNER), variable=self.inner_var, selected_color=ACCENT,
                                selected_hover_color=ACCENT_HOVER, height=24).pack(side="left")
        self._bind_help(self._HELP["codec"], cr)
        self._back_row = None
        if sys.platform == "win32":      # LibProsperoPkg refuses the Publishing Tools backend elsewhere
            br = self._back_row = ctk.CTkFrame(ib, fg_color=PANEL); br.pack(fill="x", pady=(0, 4))
            ctk.CTkLabel(br, text="Kraken backend:", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
            ctk.CTkSegmentedButton(br, values=list(self._BACKEND), variable=self.back_var, selected_color=ACCENT,
                                    selected_hover_color=ACCENT_HOVER, height=24).pack(side="left")
        self._bind_help(self._HELP["identity"], ig)

        # Help line + summary + buttons (outside the scroll area, always visible)
        foot = self._foot = ctk.CTkFrame(self, fg_color=BLACK); foot.pack(fill="x", padx=20, pady=(0, 14))
        ctk.CTkFrame(foot, height=1, corner_radius=0, fg_color=BORDER).pack(fill="x", pady=(0, 10))
        # The help line keeps one height while the pointer moves: the box is as tall as the
        # longest hover text needs at the current width, so the buttons below never jump.
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
                lines = max(_wrapped_line_count(help_font, text, width) for text in help_texts)
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
        # Inset by the panel's 1 px border: flush, the scroll area painted over the frame's edge.
        self.body.pack(fill="both", expand=True, padx=1, before=foot)

    def _bind_help(self, text: str, *widgets):
        """Show *text* in the help line while the pointer is over any of *widgets* or their
        children; clear it when the pointer leaves the first widget's box. customtkinter
        widgets are composites, so every descendant gets the binding."""
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
            except Exception:
                pass
            for c in w.winfo_children():
                _walk(c)
        for w in widgets:
            _walk(w)

    # ── pickers ──────────────────────────────────────────────────────────────
    def _initial_dir(self) -> str | None:
        """Where the source pickers open: the folder of what is in the source field, else
        the folder the last source came from."""
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
        """Save the chosen source and the folder it came from, so the next Add job starts
        there."""
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

    def _pick_patch_folder(self):
        p = filedialog.askdirectory(parent=self, title="Choose the patch folder")
        if p:
            self.patch_var.set(p)

    def _pick_out(self):
        p = filedialog.askdirectory(parent=self, title="Choose the output folder for this job")
        if p:
            self.out_var.set(p)

    def _look_inside(self):
        p = Path((self.src_var.get() or "").strip())
        if p.is_file():
            PfsBrowserDialog(self.app, image_path=str(p))

    # ── detection ────────────────────────────────────────────────────────────
    def _to_key(self) -> str:
        return self._TARGET_KEY.get(self.to_var.get(), "ffpfsc")

    @staticmethod
    def _sdk_text(folder: Path) -> str:
        """'SDK 8.00' read from eboot.bin's SCE param segment (raw or fake-signed),
        'encrypted eboot' when it cannot be read, '' when there is nothing to show."""
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
        """7.61, 6.02, 10.xx and every firmware folder under the firmware libraries folder."""
        try:
            names = _backport_module().available_targets(self._fw_root())
        except Exception:
            names = list(self._BACKPORT_HINTS)
        cur = self.backport_target_var.get()
        if is_backport_target(cur) and cur not in names:
            names.append(cur)            # a job made with a firmware folder that is gone
        return names

    def _backport_hint(self, target: str) -> str:
        """One line under the target: what the target is, or why it cannot be used."""
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
            self._hint_cache[target] = (f"{target}: cannot be used, {problem}" if problem else
                                        f"{target}: SDK values from your {target} libraries; Check shows "
                                        f"whether patched libraries are needed")
        return self._hint_cache[target]

    def _probe_ps4_archive(self, p: Path, raw: str):
        """Read, off the main thread, whether the archive holds PS4 packages; when it does
        (and the source is still the same), the editor switches to the PS4 view."""
        pw = self.app._candidate_passwords()
        box: dict = {}

        def work():
            try:
                box["info"] = ArchiveExtractor.ps4_archive_info(p, pw)
            except Exception:
                box["info"] = None

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
            bits = ["PS4 archive", f"{n} package{'s' if n != 1 else ''}"]
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
        bits = ["PS4 package" if p.is_file() else f"PS4 packages · {what}"]
        if first:
            bits += [first.title_id, f"v{first.version}" if first.version else ""]
            if p.is_file():
                bits.insert(1, {"game": "game", "update": "update", "dlc": "DLC"}.get(first.kind, first.kind))
        return " · ".join(b for b in bits if b)

    def _on_source_changed(self):
        raw = (self.src_var.get() or "").strip()
        p = Path(raw) if raw else None
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
        if _is_ps4_source(p):
            self._kind = "ps4"; self._games = [p]
            self.detect_var.set(self._ps4_detect_text(p))
            if p.is_file():
                self._look_btn.pack(side="left", padx=(10, 0))
            self._update_identity_for_source(None)
            self._refresh(); return
        if p.is_dir():
            if is_game_folder(p):
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
                sources = self._scan_sources(p)
                if sources:
                    self._kind = "parent"; self._games = sources
                    self.detect_var.set(f"{self._KIND_LABEL['parent']} · {self._describe_sources(sources)} — one job each")
                else:
                    self._kind = "folder-unknown"
                    self.detect_var.set("No game folder, archive, disk image, .ffpfs, .ffpfsc or .pkg in this folder")
        else:
            suf = p.suffix.lower()
            kind = {".exfat": "exfat", ".ffpkg": "ffpkg", ".ffpfs": "ffpfs", ".ffpfsc": "ffpfsc", ".pkg": "pkg"}.get(suf)
            if kind is None and (suf in (".zip", ".rar", ".7z", ".r00") or re.match(r"^\.r\d{2,}$", suf)
                                 or re.search(r"\.part\d+\.rar$", p.name, re.I)):
                kind = "archive"
            if kind is None:
                self._kind = "file-unknown"
                self.detect_var.set(f"Unsupported file type {suf or '(none)'}")
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
            self.detect_var.set(" · ".join(x for x in (self._KIND_LABEL[kind], tid, size) if x))
            if kind in ("ffpfs", "ffpfsc", "pkg"):
                self._look_btn.pack(side="left", padx=(10, 0))
            if kind == "archive":
                self._probe_ps4_archive(p, raw)
        self._update_identity_for_source(p)
        # Remembered choices for this kind of source (add mode only, once per kind).
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
        """The output format an existing job produces, whatever kind of job it is."""
        op = getattr(item, "operation", "pack")
        if op == "chain":
            return getattr(item, "chain_to", None) or "ffpfsc"
        if op == "fpkg-build":
            return "pkg"
        if op == "unpack":
            return "folder" if getattr(item, "unwrap", True) else "ffpfs"
        if op in ("fpkg-extract", "fake-sign"):
            return "folder"
        if op == "patch":
            return "ffpfsc"                  # a patch job always writes "<game> [patched].ffpfsc"
        if op == "copy" and getattr(item, "content_kind", "") == ORGANIZE_TARGET:
            return ORGANIZE_TARGET
        if op == "copy":
            return {".ffpfsc": "ffpfsc", ".ffpfs": "ffpfs", ".pkg": "pkg"}.get(
                Path(str(getattr(item, "path", "") or "")).suffix.lower(), "ffpfsc")
        # pack: output_compressed None means "not chosen yet", which packs a .ffpfsc
        return "ffpfs" if getattr(item, "output_compressed", None) is False else "ffpfsc"

    # ── package identity ─────────────────────────────────────────────────────
    def _update_identity_for_source(self, p: Path | None):
        """Fill the identity from a game folder's param.json (never over what was typed),
        and make it required for a game folder that has none."""
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
            self._ident_note.set(f"param.json could not be read ({e}); the identity is used as typed.")
            return
        d = d if isinstance(d, dict) else {}
        title = ""
        lp = d.get("localizedParameters") or {}
        if isinstance(lp, dict):
            lang = lp.get("defaultLanguage")
            block = lp.get(lang) if isinstance(lang, str) else None
            if isinstance(block, dict):
                title = str(block.get("titleName") or "")
            if not title:
                title = next((str(v["titleName"]) for v in lp.values() if isinstance(v, dict) and v.get("titleName")), "")
        title = title or str(d.get("titleName") or "")
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
        """The rows just opened sit at the bottom of the options: scroll them into view."""
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
        """Check the identity and codec rows and return them for the package parameters,
        or None after saying what is wrong (and showing the rows it is about)."""
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
            return _bad("Mismatch", f"The title id {tid} does not appear inside the content id {cid}.")
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

    # ── live state ───────────────────────────────────────────────────────────
    def _scan_sources(self, p: Path) -> list[Path]:
        """find_job_sources, remembered per folder and its modification time: the source
        field calls this on every keystroke, and a library on a USB drive is slow to walk."""
        try:
            key = (str(p), p.stat().st_mtime_ns)
        except OSError:
            return []
        cache = self.__dict__.setdefault("_source_scan_cache", {})
        if key not in cache:
            cache[key] = find_job_sources(p)
        return cache[key]

    @staticmethod
    def _describe_sources(sources: list[Path]) -> str:
        """'4 sources: 1 game folder, 2 archives, 1 .pkg'."""
        counts: dict[str, int] = {}
        for s in sources:
            if s.is_dir():
                label = "game folder"
            elif is_archive_file(s):
                label = "archive"
            elif s.suffix.lower() in (".exfat", ".ffpkg"):
                label = "disk image"
            else:
                label = s.suffix.lower()
            counts[label] = counts.get(label, 0) + 1
        order = ["game folder", "archive", "disk image", ".ffpfs", ".ffpfsc", ".pkg"]
        parts = []
        for label in sorted(counts, key=lambda k: order.index(k) if k in order else 99):
            n = counts[label]
            plural = "s" if n != 1 and not label.startswith(".") else ""
            parts.append(f"{n} {label}{plural}")
        return f"{len(sources)} source{'s' if len(sources) != 1 else ''}: " + ", ".join(parts)

    def _parent_todo(self) -> list[Path]:
        """The parent folder's sources this job setup would change: a game folder to a
        folder with nothing to change is left out (there is nothing to do for it)."""
        return [s for s in self._games if not chain_summary(self._stand_in(s)).startswith("Nothing to do")]

    def _in_place(self, to: str) -> bool:
        """Folder output changes a game folder where it is: true for a game folder source,
        and for a parent folder that holds only game folders."""
        if to != "folder":
            return False
        if self._kind == "folder":
            return True
        return self._kind == "parent" and bool(self._games) and all(s.is_dir() for s in self._games)

    def _stand_in(self, p: Path | None):
        """A lightweight object with the attributes chain_summary reads — no folder walk."""
        kind = self._kind
        is_archive = kind == "archive" or (kind == "parent" and p is not None and p.is_file() and is_archive_file(p))
        return types.SimpleNamespace(
            path=p, archive_path=(p if is_archive else None),
            chain_to=self._to_key(), chain_sign=bool(self.sign_var.get()) and self._to_key() != "pkg",
            patch_source=(self.patch_var.get().strip() if self.patch_on_var.get() else None),
            backport_target=(self.backport_target_var.get() if self.backport_on_var.get() else None))

    def _refresh(self):
        ps4 = self._kind == "ps4"
        seg = getattr(self, "_to_seg", None)
        if ps4:
            # a PS4 package is sorted into the library (.pkg) or unpacked (Folder, one package);
            # an archive or a folder of them only goes to the library
            src = Path((self.src_var.get() or "").strip())
            one_pkg = src.is_file() and src.suffix.lower() == ".pkg"
            allowed = ("folder", "pkg") if one_pkg else ("pkg",)
            if self._to_key() not in allowed:
                self.to_var.set(self._TARGET_LABEL["pkg"])
            if seg is not None:
                seg.configure(values=[self._TARGET_LABEL[k] for k in allowed])
                seg.set(self.to_var.get())
            for v in (self.patch_on_var, self.backport_on_var, self.sign_var):
                v.set(False)
        crow, orow = getattr(self, "_crow", None), getattr(self, "_orow", None)
        org = not ps4 and self._to_key() == ORGANIZE_TARGET     # the source goes over as it is
        if crow is not None and orow is not None:          # both exist once the dialog is built
            if ps4 or org:
                crow.pack_forget()
            elif not crow.winfo_manager():
                crow.pack(fill="x", padx=20, pady=5, before=orow)
        if not ps4 and seg is not None and len(seg.cget("values")) != len(self._TARGETS):
            seg.configure(values=[self._TARGET_LABEL[k] for k in self._TARGETS])
            seg.set(self.to_var.get())
        to = self._to_key()
        # progressive disclosure
        (self._patch_opts.pack(fill="x", pady=(2, 2)) if self.patch_on_var.get() else self._patch_opts.pack_forget())
        (self._backport_opts.pack(fill="x", pady=(2, 2)) if self.backport_on_var.get() else self._backport_opts.pack_forget())
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
        self._to_hint.set({"folder": "plain /app0 folder — for a folder source: changes in place",
                           "ffpfs": "uncompressed image — fastest to build and mount, full size",
                           "ffpfsc": "compressed image — mounts with ShadowMount",
                           "pkg": "installable package",
                           "organize": "into the library as it is: '<Title> [ID] [vX]/…', the same format, "
                                       "joining a title folder that is already there"}.get(to, "") if not ps4 else
                          {"pkg": "into the library: '<Title> [CUSA…] [vX]', UPDATE and DLC named, 'DLC Pack' from 4 DLCs",
                           "folder": "the package's files in a folder"}.get(to, ""))
        in_place = self._in_place(to)
        for w in (self._out_entry, self._out_btn):
            try:
                w.configure(state="disabled" if in_place else "normal")
            except Exception:
                pass
        # A change in place writes into the source itself: no after-job action there.
        try:
            aj = _after_job_module()
            if in_place or self._kind == "none":
                self._after_box.pack_forget()
            else:
                if not self._after_box.winfo_manager():
                    self._after_box.pack(fill="x", padx=10, pady=(0, 6), after=self._oline)
                act = self.after_var.get()
                self._after_seg.set(aj.LABELS.get(act, aj.LABELS[aj.KEEP]))
                self._after_dir_row.pack_forget(); self._after_note.pack_forget()
                if act == aj.MOVE:
                    self._after_dir_row.pack(fill="x", pady=(4, 0))
                elif act == aj.DELETE:
                    self._after_note.pack(anchor="w", padx=(100, 0), pady=(4, 0))
        except Exception:
            pass
        # summary
        raw = (self.src_var.get() or "").strip()
        p = Path(raw) if raw else None
        if self._kind in ("none", "folder-unknown", "file-unknown") or p is None:
            text = "Choose a source"
            ok = False
        elif ps4:
            text = "Sort into the PS4 library" if to == "pkg" else "Unpack to folder"
            ok = True
        elif self._kind == "parent":
            todo = self._parent_todo()
            if todo:
                text = chain_summary(self._stand_in(todo[0])) + f"  × {len(todo)} job{'s' if len(todo) != 1 else ''}"
            else:
                text = "Nothing to do"
            ok = bool(todo)
        else:
            text = chain_summary(self._stand_in(p))
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
        self.summary_var.set(text)
        self.summary_dest_var.set(dest)
        try:
            # Disabled, it greys out like the main window's Start instead of staying blue
            # with barely readable text.
            self._add_btn.configure(state="normal" if ok else "disabled", fg_color=ACCENT if ok else BTN)
        except Exception:
            pass

    # ── compatibility check (backport analyser, read-only) ───────────────────
    _CHECK_CONTAINERS = (".pkg", ".ffpfs", ".ffpfsc")

    def _check_folder(self) -> Path | None:
        """What Check reads: a game folder, a .pkg/.ffpfs/.ffpfsc (the backend reads only its
        executables out of it), or the first of those in a parent folder. None for an archive
        or a disk image: those are checked when the job runs, before the game is unpacked
        where the source allows it."""
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
                r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=600)
                out = (r.stdout or "").splitlines()
                verdict = next((l[len("[verdict] "):] for l in out if l.startswith("[verdict] ")), "")
                result["msg"] = verdict or (r.stderr or "").strip()[-300:] or "no result"
            except Exception as e:
                result["msg"] = f"check failed: {e}"

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

    # ── commit ───────────────────────────────────────────────────────────────
    def _add_ps4(self, p: Path, to: str, out: str):
        """A PS4 source: a sorted copy into the library, or (one package) an unpack job."""
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
        self.app.log("OK", f"Queued: {chain_summary(it)} — {it.display_name or it.name}.  Press ▶ START to run.")
        self.destroy()

    def _add_organize(self, p: Path, out: str):
        """The Organize output: each source goes into the library in its own format."""
        aj = _after_job_module()
        after = self.after_var.get() if self._after_box.winfo_manager() else aj.KEEP
        after_dir = self.after_dir_var.get().strip() if after == aj.MOVE else ""
        if after == aj.MOVE and not after_dir:
            messagebox.showerror("After the job", "Choose the folder the source moves to once the job is Done.",
                                 parent=self); return
        self._defaults[self._kind] = dict(self._defaults.get(self._kind) or {}, to=ORGANIZE_TARGET)
        try:
            save_settings({"job_dialog_defaults": self._defaults, "last_source": str(p),
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

        def make(s):
            it = app._organize_item_for(s, output_path=out)
            it.after_source, it.after_move_to = after, after_dir or None
            it.source_root = root
            return it

        if self.edit_item is None:
            app._add_jobs_async(list(sources), make)
            self.destroy(); return
        try:
            new = make(sources[0])
        except Exception as e:
            messagebox.showerror("Source", f"Could not read {sources[0]}:\n{e}", parent=self); return
        try:
            app.queue[app.queue.index(self.edit_item)] = new
        except ValueError:
            app.queue.append(new)
        app.update_queue_box(select_item=new)
        app.log("OK", f"Job updated: {chain_summary(new)} — {new.display_name or new.name}.")
        self.destroy()

    def _add(self):
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
            if verdict == "warn" and not messagebox.askyesno("Patch", why + "\n\nAdd the job anyway?", parent=self):
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
            return self._add_ps4(p, to, out)
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

        # Remember the choices for this kind of source, and the backport folder globally.
        self._defaults[self._kind] = {"to": to, "sign": bool(self.sign_var.get()),
                                      "backport": bool(self.backport_on_var.get()),
                                      "backport_target": self.backport_target_var.get()}
        upd = {"job_dialog_defaults": self._defaults,
               "last_source": str(p),
               "last_source_dir": str(p if p.is_dir() else p.parent),
               # the recipe Rescan reuses on new sources from the same folder:
               "rescan_template": {"to": to, "output": out, "sign": bool(self.sign_var.get()),
                                   "backport_target": target, "patch_source": patch or None,
                                   "organize": organize, "ff_level": ff_level,
                                   "after_source": after, "after_move_to": after_dir or None,
                                   "pkg_params": pkg_params if to == "pkg" else None}}
        if target:
            upd["backport_target_default"] = target
        try:
            save_settings(upd)
        except Exception:
            pass
        if to == "pkg":
            self.app.fpkg_defaults = {k: pkg_params[k] for k in ("inner", "backend", "level", "retail_normalize",
                                                                "hdr_flag", "regen_playgo", "fake_sign")}
            self.app.fpkg_defaults["v1112"] = True
            try:
                save_settings({"fpkg_defaults": self.app.fpkg_defaults})   # remembered across restarts
            except Exception:
                pass
        elif to == "ffpfsc":
            self.app.compression_level_var.set(ff_level)   # the next job starts from it (saved, shown in Settings)
        if out:
            self.app.output_var.set(out)
        self.app.auto_organize_var.set(organize)

        sources = (self._parent_todo() if self._kind == "parent"
                   else self._games if self._kind == "folder" else [p])
        # the folder picked here stays when sources are moved or cleaned up after their jobs
        root = str(p if (p.is_dir() and not ultra_core.is_game_folder(p)) else p.parent)
        sign = bool(self.sign_var.get()) and to != "pkg"     # a .pkg is always signed by its builder
        app = self.app

        def make(s):
            # Tk-free: runs on the add thread for a new job (archive headers are read here)
            it = GameItem.from_chain(s, to=to, output_path=out or None, sign=sign,
                                     patch_source=patch or None, backport_target=target,
                                     backport_libs_root=libs or None)
            if to == "pkg":
                app._apply_fpkg_params(it, pkg_params)
            it.compression_level = ff_level if to == "ffpfsc" else None
            it.auto_organize = organize
            it.after_source, it.after_move_to = after, after_dir or None
            it.source_root = root
            return it

        if self.edit_item is None:
            # New jobs: built one by one on a worker, each joins the queue as it is ready,
            # with a progress line above the list. The editor closes right away.
            app._add_jobs_async(list(sources), make)
            self.destroy(); return
        made = []
        for s in sources:
            try:
                made.append(make(s))
            except Exception as e:
                messagebox.showerror("Source", f"Could not read {s}:\n{e}", parent=self); return
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
                # Same source: keep where it came from, so a retry runs from the copy the job
                # kept (and a removal deletes it) instead of a stranger's path in the scratch.
                for attr in ("origin_archive", "origin_extracted_size", "kept_extract", "bundle_siblings",
                             "pkg_content_size", "extracted_size", "header_locked"):
                    if hasattr(old, attr):
                        setattr(new, attr, getattr(old, attr))
            new.status = "Pending Extract" if getattr(new, "archive_path", None) else "Queued"
            # the new settings must be checked anew against the output folder
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
            self.app.log("OK", f"Job updated: {chain_summary(new)} — {new.display_name or new.name}.{_run}")
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
        self.app.log("OK", f"Queued: {what} — {made[0].display_name or made[0].name}"
                           + (f" (+{len(made) - 1} more)" if len(made) > 1 else "") + ".  Press ▶ START to run.")
        self.destroy()




class ArchivePasswordPrompt(MessageWindow):
    """Modal: ask the user for ONE archive's password when no saved candidate unlocks
    its header (so the routing pre-check has no honest extracted size to work with).
    Result is exposed via .password ('' = Skip)."""

    def __init__(self, app, archive_name: str):
        super().__init__(app.root)
        self.app = app
        self.password = ""
        self.title("Archive password needed")
        self.configure(fg_color=BLACK); self.resizable(False, False)   # sized to its content
        self.protocol("WM_DELETE_WINDOW", self._skip)
        self.after(50, self.grab_set)

        ctk.CTkLabel(self, text="Archive password needed",
                      font=ctk.CTkFont(size=17, weight="bold"), text_color=WHITE
                      ).pack(anchor="w", padx=20, pady=(16, 2))
        ctk.CTkLabel(self,
                      text=f"None of your saved passwords opens '{archive_name}'. Enter its "
                           f"password so the real extracted size is known for the drive routing; "
                           f"the password is then saved for later archives. Skip adds the job "
                           f"anyway, and the extraction tries every saved password again.",
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
    """Before a start: jobs whose output file is already in the output folder. One answer
    for all of them: Skip (the safe default), Overwrite, Keep both, or Cancel the start.
    .choice is 'skip' | 'overwrite' | 'keep' | 'cancel'. Shown only when Settings says Ask."""

    MAX_ROWS = 8

    def __init__(self, parent, conflicts):
        super().__init__(parent)
        self.choice = "cancel"
        n = len(conflicts)
        head = "Already in the output folder"
        self.title(head)
        self.configure(fg_color=BLACK); self.resizable(False, False)   # sized to its content
        self.protocol("WM_DELETE_WINDOW", lambda: self._done("cancel"))
        self.after(50, self.grab_set)
        ctk.CTkLabel(self, text=head, font=ctk.CTkFont(size=17, weight="bold"), text_color=WHITE
                     ).pack(anchor="w", padx=20, pady=(16, 2))
        ctk.CTkLabel(self, text=(f"{n} job{'s' if n != 1 else ''} would write a file that is already there. "
                                 f"Skip leaves {'them' if n != 1 else 'it'} out of this run; Overwrite replaces "
                                 f"the file{'s' if n != 1 else ''} once the new build is finished; Keep both "
                                 f"gives the new build a numbered name. Settings › Interface can answer this "
                                 f"for every start."),
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
    """After the last job of a run: the computer sleeps or the app quits once SECONDS have
    passed. Cancel stops it; the other button does it now."""

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
        self._msg = tk.StringVar()
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
        return (f"{who} goes to sleep in {self._left} s." if self._action == "sleep"
                else f"PS5 UltraPack quits in {self._left} s.")

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
    """Organize as a view of the main window: pick a folder, see where everything in it
    belongs (old -> new, a checkbox each; what stays and why), Apply. Apply only renames on
    the folder's drive; Undo last organize moves it back. The backend does the work
    (--organize-scan / --organize-apply / --organize-undo); nothing here touches the files."""
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
        self.journal = Path(APP_DIR) / "organize_journal.json"
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
        ctk.CTkEntry(row, textvariable=self.root_var, fg_color=CARD2, text_color=WHITE).pack(
            side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Choose…", width=90, fg_color=BTN, hover_color=BTN_HOVER, text_color=WHITE,
                      border_width=1, border_color=BTN_BORDER, command=self._pick).pack(side="left", padx=(6, 0))
        self.scan_btn = ctk.CTkButton(row, text="Scan", width=90, fg_color=BTN, hover_color=BTN_HOVER,
                                      text_color=WHITE, border_width=1, border_color=BTN_BORDER, command=self.scan)
        self.scan_btn.pack(side="left", padx=(6, 0))
        self.status_var = tk.StringVar(value="Pick a folder and press Scan. Nothing moves before Apply.")
        ctk.CTkLabel(v, textvariable=self.status_var, text_color=MUTED, anchor="w", justify="left").grid(
            row=3, column=0, sticky="ew", padx=18, pady=(2, 6))
        style = ttk.Style(v)
        try:
            style.theme_use("default")          # the Aqua theme ignores the colours below
        except Exception:
            pass
        _pal = PALETTE["light" if ctk.get_appearance_mode().lower() == "light" else "dark"]
        style.configure("Org.Treeview", background=_pal["surface2"], fieldbackground=_pal["surface2"],
                        foreground=_pal["text"], rowheight=24, borderwidth=0)
        style.configure("Org.Treeview.Heading", background=_pal["surface"], foreground=_pal["muted"], borderwidth=0)
        style.map("Org.Treeview", background=[("selected", _pal["select"])], foreground=[("selected", _pal["text"])])
        tframe = ctk.CTkFrame(v, fg_color=PANEL, corner_radius=8)
        tframe.grid(row=4, column=0, sticky="nsew", padx=18, pady=(0, 6))
        self.tree = ttk.Treeview(tframe, columns=("game", "src", "dst"), style="Org.Treeview", selectmode="browse")
        for col, text, w in (("#0", "", 34), ("game", "Game", 170), ("src", "Now", 330), ("dst", "Becomes", 330)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=w, minwidth=30 if col == "#0" else 80, stretch=col != "#0", anchor="w")
        self.tree.tag_configure("stay", foreground=_pal["muted"])
        vsb = ctk.CTkScrollbar(tframe, command=self.tree.yview)
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

    # ── helpers ──
    def on_show(self):
        if not self.root_var.get().strip():
            self.root_var.set(str(self.app.output_var.get() or ""))
        self._refresh_buttons()

    def _pick(self):
        d = filedialog.askdirectory(title="Organize — the folder to sort in place", parent=self.app.root,
                                    initialdir=self.root_var.get() or str(self.app.output_var.get() or Path.home()))
        if d:
            self.root_var.set(d)
            self.scan()

    def _refresh_buttons(self):
        n = len(self._checked)
        busy = self._busy or bool(getattr(self.app, "_batch_running", False))
        self.apply_btn.configure(text=f"Apply ({n})" if n else "Apply",
                                 state="normal" if n and not busy else "disabled")
        self.scan_btn.configure(state="disabled" if self._busy else "normal")
        self.undo_btn.configure(state="normal" if self.journal.is_file() and not busy else "disabled")

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

    # ── backend runs ──
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
            self.status_var.set(f"Scan failed: {err}")
            self.app.log("ERROR", f"Organize: scan failed: {err}")
            return
        self._checked = set(range(len(self._plan["moves"])))
        moves, stays, ok = len(self._plan["moves"]), len(self._plan["stays"]), self._plan.get("in_place", 0)
        summary = (f"{moves} to move" if moves else "Nothing to move") + f", {ok} already in place" \
            + (f", {stays} staying (see why below)" if stays else "") + "."
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
        self._run("apply", ("--organize-apply", "--organize-plan", str(self.plan_file),
                            "--organize-journal", str(self.journal), "--organize-only", only))

    def _done_apply(self, rc, lines):
        for ln in lines:
            if ln.startswith(("[ORGANIZE]", "[WARN]", "[ERROR]")):
                self.app.log("WARN" if ln.startswith("[WARN]") else "ERROR" if ln.startswith("[ERROR]") else "INFO",
                             "Organize: " + ln.split("] ", 1)[-1])
        last = next((ln for ln in reversed(lines) if ln.startswith(("[OK]", "[ERROR]"))), "")
        self.app.log("OK" if rc == 0 else "WARN", "Organize: " + last.split("] ", 1)[-1])
        self.scan(then_status=last.split("] ", 1)[-1] + ".")

    def undo(self):
        if self._busy or not self.journal.is_file():
            return
        if not messagebox.askyesno("Undo last organize", "Move everything the last Apply moved back where it was?",
                                   parent=self.app.root):
            return
        self.status_var.set("Moving back…")
        self._run("undo", ("--organize-undo", "--organize-journal", str(self.journal)))

    def _done_undo(self, rc, lines):
        last = next((ln for ln in reversed(lines) if ln.startswith(("[OK]", "[ERROR]"))), "")
        self.app.log("OK" if rc == 0 else "WARN", "Organize: " + last.split("] ", 1)[-1])
        for ln in lines:
            if ln.startswith("[WARN]"):
                self.app.log("WARN", "Organize: " + ln[7:])
        if self.root_var.get().strip() and Path(self.root_var.get().strip()).is_dir():
            self.scan(then_status=last.split("] ", 1)[-1] + ".")
        else:
            self.status_var.set(last)


class PfsBrowserDialog(EmbeddedDialog):
    """Browse a packed (.ffpfs) or compressed (.ffpfsc) image: list its contents and
    pull out individual files or whole folders, WITHOUT unpacking the whole image. The
    backend reads only the blocks it needs (--list-image / --extract-from). Read-only."""
    LARGE = True

    def __init__(self, app, image_path=None, standalone=False):
        super().__init__(app.root)
        self.app = app
        self.standalone = standalone   # the only window (launched by double-clicking a .ffpfsc)
        self.image_path = None
        self._entries = []          # full [{path, type, size}]
        self._iid_path = {}         # tree item id -> rel path
        self._proc = None           # running extract subprocess (for cancel)
        self._q = queue.Queue()
        self.title("Look inside")
        self.geometry("780x560")
        self.configure(fg_color=BLACK)
        self.resizable(True, True)
        # When this IS the only window (browser-only launch), behave as a normal top-level
        # rather than a modal transient of the hidden main window.
        if standalone:
            self.lift(); self.focus_force()
        else:
            self.transient(app.root); self.lift(); self.focus_force()
            self.after(50, self.grab_set)

        ctk.CTkLabel(self, text="Look inside",
                      font=ctk.CTkFont(size=17, weight="bold"), text_color=WHITE
                      ).pack(anchor="w", padx=18, pady=(14, 2))
        ctk.CTkLabel(self, text="Open a .ffpfs, .ffpfsc or .pkg (fPKG), see what's inside, and extract "
                                "individual files or folders. The image is never fully unpacked — a .pkg "
                                "is read block by block through its encryption.",
                      text_color=MUTED, wraplength=720, justify="left").pack(anchor="w", padx=18, pady=(0, 8))

        srow = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=8); srow.pack(fill="x", padx=18, pady=4)
        sin = ctk.CTkFrame(srow, fg_color=PANEL); sin.pack(fill="x", padx=10, pady=8)
        self.src_var = tk.StringVar(value=str(image_path or ""))
        ctk.CTkEntry(sin, textvariable=self.src_var, fg_color=CARD2, text_color=WHITE).pack(side="left", fill="x", expand=True)
        # Secondary: Extract selected is the one primary action of this panel.
        ctk.CTkButton(sin, text="Open image…", width=110, fg_color=BTN, hover_color=BTN_HOVER,
                       text_color=WHITE, border_width=1, border_color=BTN_BORDER, font=ctk.CTkFont(size=13),
                       command=self._pick).pack(side="left", padx=(6, 0))

        frow = ctk.CTkFrame(self, fg_color="transparent"); frow.pack(fill="x", padx=18, pady=(2, 0))
        ctk.CTkLabel(frow, text="Filter:", text_color=MUTED).pack(side="left")
        self.filter_var = tk.StringVar()
        ctk.CTkEntry(frow, textvariable=self.filter_var, fg_color=CARD2, text_color=WHITE, width=240,
                      placeholder_text="name contains…").pack(side="left", padx=(6, 0))
        self.filter_var.trace_add("write", lambda *_: self._render())
        self.count_var = tk.StringVar(value="")
        ctk.CTkLabel(frow, textvariable=self.count_var, text_color=MUTED).pack(side="right")

        style = ttk.Style(self)
        try:
            style.theme_use("default")
        except Exception:
            pass
        _pal = PALETTE["light" if ctk.get_appearance_mode().lower() == "light" else "dark"]
        style.configure("PFS.Treeview", background=_pal["surface2"], fieldbackground=_pal["surface2"],
                         foreground=_pal["text"], rowheight=24, borderwidth=0)
        style.configure("PFS.Treeview.Heading", background=_pal["surface"], foreground=_pal["muted"], borderwidth=0)
        style.map("PFS.Treeview", background=[("selected", _pal["select"])], foreground=[("selected", _pal["text"])])
        tframe = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=8); tframe.pack(fill="both", expand=True, padx=18, pady=6)
        self.tree = ttk.Treeview(tframe, columns=("size", "type"), style="PFS.Treeview", selectmode="extended")
        self.tree.heading("#0", text="Name", anchor="w"); self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("type", text="Type", anchor="center")
        self.tree.column("#0", width=470, anchor="w")
        self.tree.column("size", width=110, anchor="e")
        self.tree.column("type", width=80, anchor="center")
        vsb = ctk.CTkScrollbar(tframe, command=self.tree.yview)   # the dark one, not the Aqua bar
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6)

        bottom = ctk.CTkFrame(self, fg_color=BLACK); bottom.pack(fill="x", padx=18, pady=(2, 4))
        self.status_var = tk.StringVar(value="No image loaded.")
        ctk.CTkLabel(bottom, textvariable=self.status_var, text_color=MUTED).pack(side="left")
        self.progress = ctk.CTkProgressBar(bottom, width=180); self.progress.set(0)

        btns = ctk.CTkFrame(self, fg_color=BLACK); btns.pack(fill="x", padx=18, pady=(0, 14))
        self.extract_sel_btn = ctk.CTkButton(btns, text="Extract selected…", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                       text_color=ON_ACCENT, font=ctk.CTkFont(size=13),
                       command=lambda: self._extract(False), state="disabled")
        self.extract_sel_btn.pack(side="right", padx=(8, 0))
        self.extract_all_btn = ctk.CTkButton(btns, text="Extract all…", fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER, command=lambda: self._extract(True), state="disabled", border_width=1, border_color=BTN_BORDER)
        self.extract_all_btn.pack(side="right")
        self.close_btn = ctk.CTkButton(btns, text="Close", fg_color=BTN, text_color=WHITE,
                       hover_color=BTN_HOVER, command=self._cancel_or_close, border_width=1, border_color=BTN_BORDER)
        self.close_btn.pack(side="left")

        self.after(120, self._poll)
        if image_path:
            self.after(150, self._load)

    def _pick(self):
        p = filedialog.askopenfilename(parent=self, title="Select a .ffpfs / .ffpfsc image or a .pkg (fPKG)",
                                       filetypes=[("PFS images / fPKG", "*.ffpfsc *.ffpfs *.pkg"),
                                                  ("PFS images", "*.ffpfsc *.ffpfs"),
                                                  ("PS5 packages", "*.pkg"), ("All files", "*.*")])
        if p:
            self.src_var.set(p); self._load()

    def _load(self):
        raw = (self.src_var.get() or "").strip()
        if not raw or not Path(raw).is_file():
            messagebox.showerror("Not found", "Pick a .ffpfs / .ffpfsc / .pkg file first.", parent=self); return
        self.image_path = Path(raw)
        kind = "  (fPKG)" if self.image_path.suffix.lower() == ".pkg" else ""
        self.title(f"Look inside  ·  {self.image_path.name}{kind}")
        self.status_var.set("Reading image…")
        self.extract_sel_btn.configure(state="disabled"); self.extract_all_btn.configure(state="disabled")
        cmd = self.app._backend_cmd("--list-image", str(self.image_path))
        def _worker():
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
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
                        self.status_var.set(f"Failed: {payload}")
                    elif kind == "ext_line":
                        self._on_ext_line(payload)
                    elif kind == "ext_done":
                        self._on_ext_done(payload)
                except Exception as e:
                    # A handler failure must not kill the poll loop — the dialog would
                    # sit frozen mid-extraction. Show it in the status line and go on.
                    try:
                        self.status_var.set(f"Error: {e}")
                        self.app.log("ERROR", f"PFS browser: '{kind}' handler failed: {e}")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        finally:
            # Re-arm no matter what happened above (the dialog may already be destroyed).
            try:
                if self.winfo_exists():
                    self.after(120, self._poll)
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
            self.status_var.set(f"Bad listing: {e}"); return
        self._entries = data.get("entries", [])
        self._render()
        nf, nd = data.get("file_count", 0), data.get("dir_count", 0)
        self.status_var.set(f"{nf} file{'' if nf == 1 else 's'}, {nd} folder{'' if nd == 1 else 's'}")
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
                                   values=("", "dir"), open=bool(flt))
            node[path] = iid
            self._iid_path[iid] = path
        for e in sorted((e for e in entries if e["type"] == "file"), key=lambda e: e["path"]):
            path = e["path"]
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            iid = self.tree.insert(node.get(parent, ""), "end", text="" + path.rsplit("/", 1)[-1],
                                   values=(format_size(e.get("size", 0)), "file"))
            self._iid_path[iid] = path
        self.count_var.set(f"{len(self._iid_path)} shown")

    def _extract(self, extract_all):
        if not self.image_path:
            return
        if extract_all:
            members = [e["path"] for e in self._entries if "/" not in e["path"]]
        else:
            members = [self._iid_path[i] for i in self.tree.selection() if i in self._iid_path]
            if not members:
                messagebox.showinfo("Nothing selected", "Select files or folders in the tree first.", parent=self)
                return
        dest = filedialog.askdirectory(parent=self, title="Extract to folder")
        if not dest:
            return
        try:
            fd, mfpath = tempfile.mkstemp(prefix="ffpfsc_members_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(members))
        except Exception as e:
            messagebox.showerror("Error", f"Could not stage the selection: {e}", parent=self); return
        cmd = self.app._backend_cmd("--extract-from", str(self.image_path), "--dest", dest, "--members-file", mfpath)
        self.status_var.set("Extracting…"); self.progress.set(0)
        self.progress.pack(side="right", padx=(8, 0))
        self.extract_sel_btn.configure(state="disabled"); self.extract_all_btn.configure(state="disabled")

        def _worker():
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               text=True, bufsize=1)
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

    def _on_ext_line(self, line):
        m = re.search(r"\[#{2,}\]\s*(\d{1,3})%", line)
        if m:
            self.progress.set(min(1.0, int(m.group(1)) / 100.0))
            self.status_var.set(f"Extracting… {m.group(1)}%")
        elif line.startswith("[ERROR]"):
            self.status_var.set(line)

    def _on_ext_done(self, rc):
        try:
            self.progress.pack_forget()
        except Exception:
            pass
        self.extract_sel_btn.configure(state="normal"); self.extract_all_btn.configure(state="normal")
        if rc == 0:
            self.status_var.set("Extracted")
        else:
            self.status_var.set(f"Extraction failed ({rc}).")

    def _cancel_or_close(self):
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self.status_var.set("Cancelled.")
            return
        self.destroy()


# ─── Main Application ──────────────────────────────────────────────────────────

class App:
    def __init__(self, root):
        self.root = root
        # Capture first-run BEFORE _setup() seeds settings.json — is_first_run() is a
        # file-existence check, so once _setup writes the file it would always be False
        # and the wizard would never appear for a genuinely new user.
        self._is_first_run = is_first_run()
        self.queue = []
        # Gate queue persistence until the saved queue is restored, so the initial
        # empty render doesn't overwrite the saved queue before we load it.
        self._queue_restored = False
        self.current_process = None
        self.cancel_requested = False
        self.extract_cancel_event = threading.Event()
        self.pending_start = False
        self.worker = None
        self._last_cmd_str = ""
        self._theme = "dark"
        # Batch auto-advance tracking (Feature 4)
        self._batch_total   = 0
        self._batch_done    = 0
        self._batch_failed  = 0
        self._batch_running = False
        self._details_item  = None   # GameItem currently shown in the details panel
        self._settings_win  = None
        self._active_item   = None   # item handed to the current worker (cleanup fallback)
        # Reclaim accounting: the batch must not re-check free space until all in-flight
        # cleanup rmtrees finish, or it reads stale (still-full) space and false-skips.
        self._cleanup_inflight = 0
        self._cleanup_lock     = threading.Lock()
        self._cleanup_wait_ticks = 0
        # Async source-scan tracking: a folder add launches a background scan that
        # leaves the queue briefly empty. Without this, pressing START in that window
        # makes start() re-add the SAME source (queue is still empty) → a duplicate.
        self._scan_in_flight = 0
        self._scan_lock      = threading.Lock()

        self.log_q      = queue.Queue()
        self.progress_q = queue.Queue()
        self.status_q   = queue.Queue()
        self.done_q     = queue.Queue()
        self.scan_q     = queue.Queue()
        self._extract_q = queue.Queue()   # archive extraction completion
        self.visible_log_lines = 0
        self.auto_scroll_logs = True

        self._setup()
        self._build()
        self._restore_queue()   # rebuild the saved queue now that the listbox exists
        self._poll()
        self._start_keep_awake()   # keep external HDDs from sleeping (if enabled)

        if self._is_first_run:
            self.root.after(200, self._show_first_run_wizard)

        # After the UI is fully loaded, remind user to report any untested games
        # Restore the user's saved section widths once the layout has settled.
        self.root.after(600, self._restore_sashes)
        # Offer to reclaim leftover scratch from a crashed/cancelled previous run.
        self.root.after(1500, self._offer_startup_sweep)

    def _restore_window_geometry(self):
        """Reapply the last saved window position/size, if valid. Vertically clamp so
        the title bar can never restore above the screen (e.g. hidden under the macOS
        menu bar) or absurdly off-screen. Horizontal position is left intact so an
        external-monitor placement is respected."""
        geo = (getattr(self, "_saved_window_geometry", "") or "").strip()
        m = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geo)
        if m:
            try:
                w, h, x, y = (int(m.group(i)) for i in range(1, 5))
                if abs(x) > 20000 or abs(y) > 20000:
                    return  # corrupt coordinates — let the default geometry stand
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
        """Debounced: persist the window geometry shortly after a move/resize so
        the position survives any quit path (close button, Cmd-Q, …)."""
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
        """Persist the job card's width (job_card_width). The card keeps that width when
        the window is resized, so the width, not the divider position, is what to remember.
        Called on every divider release."""
        card = getattr(self, "_card_pane", None)
        if card is not None:
            try:
                w = int(card.winfo_width())
                if w >= 300:
                    save_settings({"job_card_width": w})
            except Exception:
                pass

    def _restore_sashes(self):
        """Give the job card its remembered width (first run: 560 px) once the window has
        its final size. The window opens small and grows to the saved geometry a moment
        later; placing the divider before that would leave the card squeezed, because the
        list, not the card, takes the extra width."""
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
        # If a job is running, confirm and stop it (kill the whole backend tree)
        # before quitting — otherwise the backend + mkpfs Pool keep running headless.
        try:
            proc_alive = bool(self.current_process and self.current_process.poll() is None)
            if proc_alive or self._job_active():
                if not messagebox.askyesno(
                    "Quit?", "A job is still running.\n\nQuit and stop it?"
                ):
                    return
                self.cancel_requested = True
                self.extract_cancel_event.set()
                if proc_alive:
                    _kill_process_tree(self.current_process)
        except Exception:
            pass
        self._save_window_geometry()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _persisted_bool(self, settings, key, default):
        """A BooleanVar that loads from settings.json and auto-saves on every
        change, so the option sticks regardless of which checkbox toggles it."""
        v = tk.BooleanVar(value=bool(settings.get(key, default)))
        v.trace_add("write", lambda *_: save_settings({key: v.get()}))
        return v

    def _setup(self):
        settings = load_settings()
        self._theme = settings.get("appearance_mode", "dark")
        ctk.set_appearance_mode(self._theme)
        ctk.set_default_color_theme("blue")
        apply_ctk_theme(ctk)
        set_window_appearance(self.root, self._theme)
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("1280x820")
        self.root.minsize(900, 600)

        self._saved_output = settings.get("output_folder", "")
        self._saved_temp = settings.get("temp_folder", "")
        self._saved_source = settings.get("source_path", "")
        self._saved_auto_clear_temp = settings.get("auto_clear_temp", False)
        def _safe_int(v, default):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default
        # Coerce defensively: a hand-edited / corrupt non-numeric value here would make
        # tk.IntVar(value=...) raise TclError and hard-crash the GUI at startup.
        self._saved_compression_level = max(1, min(9, _safe_int(settings.get("compression_level", 7), 7)))
        self._saved_cpu_count = _safe_int(settings.get("cpu_count", 0), 0)
        self._saved_block_size = settings.get("block_size", "auto")
        # Migrate console-incompatible block sizes: "auto-fit" (picks 4 KiB for many-file
        # games) and explicit sub-64K values build images the PS5 misreads → crash. The
        # native size is 64 KiB; normalise to "auto" (= 65536). The backend enforces this
        # too, but fixing the saved value keeps the UI honest.
        if self._saved_block_size in ("auto-fit", "4096", "8192", "16384", "32768"):
            self._saved_block_size = "auto"
            save_settings({"block_size": "auto"})
        # Global, auto-tried archive password list — empty until the user adds entries
        # (Settings → Archives). Saved lists from earlier versions are kept as they are.
        self._saved_passwords = settings.get("archive_passwords")
        if self._saved_passwords is None:
            self._saved_passwords = []
            save_settings({"archive_passwords": self._saved_passwords})
        # Folder bundles: copy DLC/extra files next to the .ffpfsc (default on).
        self._saved_copy_siblings = settings.get("copy_bundle_siblings", True)
        # Remember the window position/size across launches.
        self._saved_window_geometry = settings.get("window_geometry", "")
        self._geo_save_after = None
        self._restore_window_geometry()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._on_window_configure, add="+")

    def _show_first_run_wizard(self):
        if getattr(self, "_browser_only", False):
            return   # launched only to browse a .ffpfsc (double-click) — skip the wizard
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
        """Keep long labels from forcing a pane wider than the user wants."""
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
            # A normal button looks like the main window's: a quiet fill, a hairline only in light mode
            color, hover, txt = BTN, BTN_HOVER, WHITE
        return ctk.CTkButton(parent, text=text, command=command, fg_color=color, hover_color=hover,
                              text_color=txt, border_width=0 if (green or red or yellow) else 1,
                              border_color=BTN_BORDER, **kw)

    def _build(self):
        """The main window: sidebar (views + tools) | the active view | status bar.

        Built from plain Tk widgets drawn by ui_kit, so it lays out and redraws fast. The
        attribute names the rest of the app talks to (queue_listbox, start_btn, cancel_btn,
        log_box, history_box, stats_box, command_label, overall_bar, stage_bar and every
        *_var) keep their meaning."""
        kit = self.kit = Kit(self.root, self._theme)
        self.root.configure(fg_color=ctk_pair("bg"))
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        # Kept for live stage writes (not shown; the stage sits in the job card).
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
        # Dialogs open as panels over this content area (see PanelHost).
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

        # ── Drag & drop registration ─────────────────────────────────────────
        if _HAS_DND:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)

    def _init_display_vars(self):
        """Every StringVar the worker and poll code write, plus the job card's own."""
        S = tk.StringVar
        self.overall_title_var = S(value="QUEUE")
        self.overall_pct_var = S(value="0%")
        self.cur_game_var = S(value="CURRENT STEP")
        self.stage_title_var = S(value="Ready")
        self.stage_detail_var = S(value="Add a job and start the queue.")
        self.stage_pct_var = S(value="")
        self.game_name_var = S(value="Name: No game selected")
        self.title_var = S(value="Title ID: —")
        self.source_detail_var = S(value="Source: —")
        self.orig_var = S(value="Original Size: —")
        self.files_var = S(value="Files: —")
        self.big_status_var = S(value="Ready")
        self.big_detail_var = S(value="Waiting for a game.")
        self.speed_var = S(value="Speed: —")
        self.elapsed_var = S(value="Elapsed: 00:00")
        self.eta_var = S(value="ETA: —")
        self.saved_var = S(value="Saved: —")
        self.ratio_var = S(value="Compression: —")
        self.rating_var = S(value="Rating: —")
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
        # The metric tiles show the values without the "Speed: " style prefix.
        self.m_speed_var, self.m_elapsed_var, self.m_eta_var = S(value="—"), S(value="—"), S(value="—")

        def _unprefix(src, dst):
            def f(*_):
                v = src.get()
                v = v.split(":", 1)[1].strip() if ":" in v else v.strip()
                dst.set(v if v and v not in ("—", "-") else "—")
            src.trace_add("write", f)
            f()
        _unprefix(self.speed_var, self.m_speed_var)
        _unprefix(self.elapsed_var, self.m_elapsed_var)
        _unprefix(self.eta_var, self.m_eta_var)

        def _last(*_):
            parts = [v.get() for v in (self.saved_var, self.ratio_var, self.rating_var)]
            if all(p.rstrip().endswith("—") for p in parts):
                self.last_result_var.set("Totals and the last jobs, newest first.")
            else:
                self.last_result_var.set("Last result:  " + "   ·   ".join(parts))
        for v in (self.saved_var, self.ratio_var, self.rating_var):
            v.trace_add("write", _last)
        _last()

        def _prog(*_):
            g = self.cur_game_var.get()
            name = g.split("·", 1)[1].strip() if "·" in g else ""
            self.progress_title_var.set(f"Now running  ·  {name}" if name else "Progress")
        self.cur_game_var.trace_add("write", _prog)

        def _foot(*_):
            self._footer_plain_var.set(self.footer_var.get().lstrip("●").strip() or "Ready")
        self.footer_var.trace_add("write", _foot)

    # ── Sidebar ──────────────────────────────────────────────────────────────
    def _build_sidebar(self, shell):
        kit = self.kit
        self._nav_all = []
        side = kit.frame(shell, bg="sidebar", width=188)
        side.grid(row=0, column=0, sticky="nsw")
        side.pack_propagate(False)
        brand = kit.frame(side, bg="sidebar")
        brand.pack(fill="x", padx=18, pady=(18, 14))
        kit.label(brand, bg="sidebar", text=APP_NAME, font=kit.fonts.app).pack(anchor="w")
        kit.label(brand, bg="sidebar", fg="faint", font=kit.fonts.caption, text=APP_VERSION).pack(anchor="w", pady=(1, 0))

        def nav(parent, text, icon, cmd, tooltip=None):
            b = IconButton(parent, kit, text=text, icon=icon, command=cmd, variant="nav", height=28,
                           bg="sidebar", icon_size=15, padx=10, tooltip=tooltip)
            b.pack(fill="x", padx=8, pady=1)
            self._nav_all.append(b)
            return b
        self._nav = {
            "queue": nav(side, "Queue", "queue", lambda: self._show_view("queue"), "⌘1"),
            "history": nav(side, "History", "history", lambda: self._show_view("history"), "⌘2"),
            "log": nav(side, "Log", "terminal", lambda: self._show_view("log"), "⌘3"),
        }
        kit.rule(side, bg_token="border").pack(fill="x", padx=16, pady=(12, 10))
        kit.label(side, bg="sidebar", fg="faint", font=kit.fonts.caption, text="Tools").pack(anchor="w", padx=18, pady=(0, 3))
        nav(side, "Look inside", "search", self.open_pfs_browser,
            "Browse a .ffpfsc, .ffpfs or .pkg and pull single files out")
        self._nav["organize"] = nav(side, "Organize", "folders", lambda: self._show_view("organize"),
                                    "Name and sort everything in a folder in place")
        nav(side, "Clean temp", "broom", self.clear_temp_files, "Delete leftovers in the temp folder")
        bottom = kit.frame(side, bg="sidebar")
        bottom.pack(side="bottom", fill="x", pady=(0, 10))
        self._nav["settings"] = nav(bottom, "Settings", "settings", self.open_settings, "⌘,")

    def _show_view(self, name):
        if name == "settings" and "settings" not in self._views:
            self._settings_view = SettingsView(self._views_parent, self)
            self._views["settings"] = self._settings_view.frame
        if name == "organize" and "organize" not in self._views:
            self._organize_view = OrganizeView(self._views_parent, self)
            self._views["organize"] = self._organize_view.frame
        if name == "organize":
            self._organize_view.on_show()
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
        """The toplevel's <Configure> bindings (CustomTkinter's and _on_window_configure)
        sit on the toplevel's bind tag, so Tk runs them for EVERY child widget's
        Configure: ~180 Python round trips per resize step, ~20 ms. Both only care about
        the window itself, so skip the rest in Tcl before Python is entered."""
        try:
            w = self.root._w
            script = self.root.bind("<Configure>")
            guard = 'if {"%W" ne "' + w + '"} continue'
            if script and not script.startswith(guard):
                self.root.tk.call("bind", w, "<Configure>", guard + "\n" + script)
        except Exception:
            pass

    def _set_nav_enabled(self, enabled: bool):
        """The sidebar is off while a panel is open: finish or close the panel first."""
        for b in getattr(self, "_nav_all", []):
            try:
                b.configure(state="normal" if enabled else "disabled")
            except Exception:
                pass

    def _bind_shortcuts(self):
        # On macOS the menus' key equivalents answer these keys first; the bindings serve
        # the other platforms. Neither acts behind an open panel.
        keys = {f"<{MOD}-n>": self.open_job_dialog, f"<{MOD}-o>": self.open_pfs_browser,
                f"<{MOD}-comma>": self.open_settings, f"<{MOD}-r>": self.start,
                f"<{MOD}-period>": self._stop_if_running,
                f"<{MOD}-Key-1>": lambda: self._show_view("queue"),
                f"<{MOD}-Key-2>": lambda: self._show_view("history"),
                f"<{MOD}-Key-3>": lambda: self._show_view("log"),
                DETAILS_SEQ: self.toggle_inspector}
        for seq, fn in keys.items():
            try:
                self.root.bind(seq, lambda e, f=fn: (None if self._panels.stack else f(), "break")[1])
            except tk.TclError:
                pass

    # ── Shared pieces ────────────────────────────────────────────────────────
    def _column_header(self, parent, title, subtitle=None, subtitle_var=None, bg="surface"):
        """A column's title (+ a line under it) on the left, a button row on the right."""
        kit = self.kit
        head = kit.frame(parent, bg=bg)
        head.grid_columnconfigure(0, weight=1)
        tb = kit.frame(head, bg=bg)
        tb.grid(row=0, column=0, sticky="w")
        kit.label(tb, bg=bg, text=title, font=kit.fonts.view).pack(anchor="w")
        if subtitle or subtitle_var:
            kw = {"textvariable": subtitle_var} if subtitle_var else {"text": subtitle}
            kit.label(tb, bg=bg, fg="muted", font=kit.fonts.small, **kw).pack(anchor="w", pady=(2, 0))
        btns = kit.frame(head, bg=bg)
        btns.grid(row=0, column=1, sticky="e")
        return head, btns

    def _small(self, parent, text, icon, cmd, variant="ghost", tooltip=None, bg="surface"):
        return IconButton(parent, self.kit, text=text, icon=icon, command=cmd, variant=variant, height=26,
                          padx=9, icon_size=13, font=self.kit.fonts.small, bg=bg, tooltip=tooltip)

    def _text_view(self, parent, fg="text", mirror=None):
        """A text area with a scrollbar filling *parent* (row 1 of its grid)."""
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

    # ── Queue view ───────────────────────────────────────────────────────────
    def _build_queue_view(self, parent):
        """Two flush columns, list | job card, split by a draggable hairline."""
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
        self._q_sub_var = tk.StringVar(value=self.queue_total_var.get())   # shortened when narrow
        head, btns = self._column_header(left, "Queue", subtitle_var=self._q_sub_var)
        head.grid(row=0, column=0, sticky="ew", padx=(18, 14), pady=(18, 12))
        self.queue_total_var.trace_add("write", lambda *_: self._fit_queue_header())
        self._clear_btn = self._small(btns, "Clear completed", "check-circle", lambda: self.clear_jobs("done"),
                                      tooltip="Remove the finished jobs. Right-click: clear failed, clear all")
        self._clear_btn.grid(row=0, column=0, padx=(0, 6))
        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self._clear_btn.bind(seq, self._clear_menu, add="+")
        self._rescan_btn = self._small(btns, "Rescan", "refresh", self.rescan_last_source,
                                       tooltip="Scan the last source folder for new downloads and add the ones that are not already here.")
        self._rescan_btn.grid(row=0, column=1, padx=(0, 6))
        self._add_btn = self._small(btns, "Add job", "plus", self.open_job_dialog, variant="secondary",
                                    tooltip=f"Pick a source, what to change in it and what comes out  {SHORTCUT['add']}")
        self._add_btn.grid(row=0, column=2, padx=(0, 6))
        # Start, Pause and Stop share one place of fixed size: Start while idle, Pause and Stop
        # side by side while the queue runs. Nothing beside it moves when a run starts.
        self.transport = TransportControl(
            btns, kit, on_start=self.start, on_pause=self.toggle_pause, on_stop=self.cancel,
            start_tip=f"Run the jobs from the top  {SHORTCUT['start']}", pause_tip=self._PAUSE_TIPS[False],
            stop_tip=f"Cancel the running job and stop the queue  {SHORTCUT['stop']}")
        self.transport.grid(row=0, column=3)
        self.start_btn, self.pause_btn, self.stop_btn = self.transport.start, self.transport.pause, self.transport.stop
        self._details_btn = self._small(btns, "", "sidebar-right", self.toggle_inspector,
                                        tooltip=f"Show or hide the details  {SHORTCUT['details']}")
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
        # While Add job builds its jobs (archive headers are read, one by one): a line and
        # a bar between the header and the list, so the window says what it is doing.
        self._add_box = kit.frame(left)
        self._add_box.grid(row=1, column=0, sticky="ew", padx=(18, 14), pady=(0, 10))
        self._add_box.grid_columnconfigure(0, weight=1)
        self._add_label_var = tk.StringVar(value="")
        kit.label(self._add_box, fg="muted", font=kit.fonts.small, textvariable=self._add_label_var,
                  anchor="w").grid(row=0, column=0, sticky="ew")
        self._add_bar = ProgressBar(self._add_box, kit, height=4)
        self._add_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._add_box.grid_remove()
        # While the queue runs: the whole run at the foot of the list, as a summary of it —
        # its share left, the time left right, a thin neutral bar (blue stays the running
        # job's colour), set off from the list by a hairline.
        self._all_box = kit.frame(left)
        self._all_box.grid(row=3, column=0, sticky="ew")
        self._all_box.grid_columnconfigure(0, weight=1)
        kit.rule(self._all_box).grid(row=0, column=0, columnspan=2, sticky="ew")
        self._all_left_var, self._all_right_var = tk.StringVar(value=""), tk.StringVar(value="")
        kit.label(self._all_box, fg="text", font=kit.fonts.small, textvariable=self._all_left_var,
                  anchor="w").grid(row=1, column=0, sticky="w", padx=(18, 8), pady=(10, 0))
        kit.label(self._all_box, fg="muted", font=kit.fonts.small, textvariable=self._all_right_var,
                  anchor="e").grid(row=1, column=1, sticky="e", padx=(8, 14), pady=(10, 0))
        self._all_bar = ProgressBar(self._all_box, kit, height=3, fill="muted")
        self._all_bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(18, 14), pady=(6, 12))
        self._all_box.grid_remove()

        # The job card keeps its width when the window is resized (only the list grows),
        # like an inspector. Re-laying out the whole card on every step of a window drag
        # cost ~60 ms per step; the divider still sets its width, and that is remembered.
        # The details pane is closed at first: a click on a job opens it, the header button,
        # the View menu and its shortcut toggle it, and the list takes the width it frees.
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
            m.add_command(label=f"Remove {n} job{'s' if n != 1 else ''}", command=self.queue_remove_selected,
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
        """A narrow queue column (details open, small window): Clear completed, then Add job
        drop their words and keep their icon and tooltip, so the title and Start stay whole."""
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

    # ── clearing finished jobs ────────────────────────────────────────────────
    _CLEAR_KINDS = {"done": ("Done",), "failed": ("Failed", "Skipped", "Cancelled")}

    def _clearable(self, which: str) -> list:
        """The jobs *which* (done | failed | all) removes; never the running one."""
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
        """Remove the finished (done), the failed / skipped / cancelled (failed) or all
        jobs from the queue. The running job stays. A failed job's kept extraction goes
        with it, so clearing those asks first."""
        gone = self._clearable(which)
        if not gone:
            return
        kept = [it for it in gone if getattr(it, "kept_extract", False)]
        if which == "all" and self._batch_running:
            if not messagebox.askyesno("Clear all", "A job is running. Remove every other job from the queue?\n"
                                                    "(The running job finishes normally.)"):
                return
        elif kept:
            n = len(kept)
            if not messagebox.askyesno(
                    "Clear jobs", f"{n} of these job{'s' if n != 1 else ''} keep{'' if n != 1 else 's'} what "
                                  f"{'they' if n != 1 else 'it'} extracted from {'their' if n != 1 else 'its'} "
                                  f"archive{'s' if n != 1 else ''}, for a retry. Clearing deletes "
                                  f"{'those copies' if n != 1 else 'that copy'}. Clear anyway?"):
                return
        for it in gone:
            self._drop_kept_extract(it)
            self.queue.remove(it)
        if which == "all":
            self._queue_missing_saved = []   # a deliberate clear also drops parked entries
        word = {"done": "completed", "failed": "failed or cancelled", "all": ""}[which]
        self.log("INFO", f"Cleared {len(gone)} {word + ' ' if word else ''}job{'s' if len(gone) != 1 else ''}.")
        self.update_queue_box()

    def _build_job_card(self, card):
        """The inspector next to the list: the selected job, and the running job's progress."""
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
                    tooltip=f"Hide the details  {SHORTCUT['details']}").grid(row=0, column=2, sticky="ne", padx=(8, 0))

        self._card_chips = Chips(body, kit, bg=B)
        self._card_chips.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        # Only shown when the selected job does not fit on its drives.
        self._space_warn = kit.label(body, bg=B, fg="warning", font=kit.fonts.small, textvariable=self.temp_space_var)
        self._space_warn.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._space_warn.grid_remove()
        self.temp_space_var.trace_add("write", lambda *_: (
            self._space_warn.grid() if "LOW" in self.temp_space_var.get() else self._space_warn.grid_remove()))

        # Progress of the running job; hidden while nothing runs.
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

        # The job itself, whatever it is doing: status, source, changes, compression, space.
        info = kit.frame(body, bg=B)
        info.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        info.grid_columnconfigure(1, weight=1)
        self._card_info_rows = {}
        for r, key in enumerate(self._CARD_INFO_KEYS):
            var = tk.StringVar(value="")
            k = kit.label(info, bg=B, fg="faint", font=kit.fonts.small, text=key, anchor="nw")
            v = kit.label(info, bg=B, fg="muted", font=kit.fonts.small, textvariable=var, anchor="nw",
                          justify="left")
            k.grid(row=r, column=0, sticky="nw", padx=(0, 16), pady=(0, 5))
            v.grid(row=r, column=1, sticky="ew", pady=(0, 5))
            self._card_info_rows[key] = (var, k, v)
        self._bind_dynamic_wrap(info, [v for _var, _k, v in self._card_info_rows.values()],
                                padding=110, min_width=160)

        # The log, as much of it as fits (the Log view has all of it): it takes the height
        # the pane has left, so a tall window shows more lines; the actions sit below it.
        line = kit.fonts.mono_small.metrics("linespace")
        tail_box = RoundBox(body, kit, bg=B, pad=10, height=4 * line + 2 * 10 + 2)
        tail_box.grid(row=5, column=0, sticky="nsew", pady=(14, 0))
        body.grid_rowconfigure(5, weight=1)
        self._log_tail = kit.text(tail_box.inner, bg="surface2", fg="log", font=kit.fonts.mono_small, height=4,
                                  state="disabled", padx=0, pady=0, wrap="none")
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
                ("Full log", "terminal", lambda: self._show_view("log"), "The whole log  ⌘3"),
                ("Command", "code", self._toggle_command, "Show the backend command for this job"),
                ("Edit", "edit", lambda: self._on_queue_double_click(None), "Change this job (or double-click it)"),
                ("Retry", "retry", self._retry_selected, "Run this job again"),
                ("Remove", "trash", self.queue_remove_selected,
                 f"Take the selected jobs out of the queue ({'⌘' if IS_MAC else 'Ctrl'}-click or Shift-click "
                 f"selects several)"))):
            b = self._small(acts, text, icon, cmd, tooltip=tip, bg=B)
            b.grid(row=0, column=col, padx=(0, 2))
            if text == "Retry":
                self.retry_btn = b
                b.autohide = True          # only for a job that failed, was cancelled or skipped
                b.configure(state="disabled")
        self.cancel_btn = self._small(acts, "Cancel", "x", self.cancel, variant="danger",
                                      tooltip="Stop the running job", bg=B)
        self.cancel_btn.grid(row=0, column=6, sticky="e")
        self.cancel_btn.autohide = True
        self.cancel_btn.configure(state="disabled")

        multi = self._card_multi = kit.frame(card, bg=B)
        multi.grid_columnconfigure(0, weight=1)
        self._multi_title_var, self._multi_meta_var, self._multi_hint_var = (
            tk.StringVar(value=""), tk.StringVar(value=""), tk.StringVar(value=""))
        mh = kit.frame(multi, bg=B)
        mh.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 0))
        mh.grid_columnconfigure(0, weight=1)
        kit.label(mh, bg=B, font=kit.fonts.title, textvariable=self._multi_title_var).grid(row=0, column=0, sticky="ew")
        self._small(mh, "", "x", lambda: self._set_inspector(False), bg=B,
                    tooltip=f"Hide the details  {SHORTCUT['details']}").grid(row=0, column=1, sticky="ne", padx=(8, 0))
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
        """The run controls follow the batch; the progress block shows only while the job
        in the details pane is the one running (checked on every poll tick)."""
        running = bool(getattr(self, "_batch_running", False))
        if running != getattr(self, "_run_ui_shown", None):
            self._run_ui_shown = running
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

    def _follow_next_job(self, before, item):
        """A job starts after *before*: whoever watched the running job keeps watching, so
        the list and the details move on to *item*. A job you picked yourself stays picked."""
        shown = getattr(self, "_details_item", None)
        if shown is None or shown is before or shown is item or shown not in self.queue:
            self.update_queue_box(select_item=item)
            if self._details_item is not item:
                self.update_game_details(item)

    def _sync_progress_box(self):
        """The details pane belongs to the selected job: its progress block shows that
        job's progress, so it is there only while the selected job runs (or while nothing
        is selected). A waiting job's own line says it waits; the running job's bar stays
        in its queue row."""
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

    # ── pause after the running job ───────────────────────────────────────────
    _PAUSE_TIPS = {False: "Pause the queue once the running job is done; Start runs the rest",
                   True: "Keep running the queue after this job"}

    def toggle_pause(self):
        """Pause after the running job, or take that back. The running job always finishes
        (unpacking, building, what happens after it); the queue then stops before the next."""
        if not self._batch_running:
            return
        self._pause_requested = not getattr(self, "_pause_requested", False)
        self._show_pause_state()
        self._update_batch_counter()
        self.log("INFO", "The queue pauses once the running job is done." if self._pause_requested
                 else "The queue keeps running after this job.")

    def _show_pause_state(self):
        p = bool(getattr(self, "_pause_requested", False))
        tr = getattr(self, "transport", None)
        if tr is not None:
            tr.set_armed(p)                      # the Pause half stays lit orange while armed
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
        """The running job is done and a pause was asked for: the queue stops here. It is not
        the queue's end, so no sleep, no quit and no summary; the jobs left stay queued."""
        left = sum(1 for it in self.queue if getattr(it, "status", "") not in self._TERMINAL_STATUSES)
        jobs = f"{left} job{'' if left == 1 else 's'}"
        self._pause_requested = False
        self._batch_running = False
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._update_batch_counter()
        self.update_queue_box()
        self.status_update("Paused", f"{jobs} left. Start runs them.", "Ready", 0, 0, "00:00", "—", "—")
        self.log("INFO", f"Queue paused after the running job: {jobs} left. Start runs them.")
        if self.notify_var.get() in ("job", "queue"):
            _after_job_module().notify("PS5 UltraPack", f"Queue paused: {jobs} left")

    def _sync_primary_action(self):
        """One primary button: Add job while the queue is empty, Start once it has jobs.
        While a batch runs, the run code owns the Start button's state."""
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
        """The details pane for several selected jobs: how many, their size, their states."""
        n = len(items)
        size = sum(int(display_size(it) or 0) for it in items)
        states = {}
        for it in items:
            st = str(getattr(it, "status", "") or "Queued")
            st = {"Pending Extract": "Queued", "Pending": "Queued"}.get(st, st)
            if it is self._running_item():
                st = "Running"
            states[st] = states.get(st, 0) + 1
        self._multi_title_var.set(f"{n} jobs selected")
        parts = [format_size(size)] + [f"{c} {s.lower()}" for s, c in sorted(states.items(), key=lambda kv: -kv[1])]
        self._multi_meta_var.set("  ·  ".join(parts))
        running_in = any(it is self._running_item() for it in items)
        self._multi_hint_var.set(
            ("Remove takes them out of the queue; the running job stays. " if running_in
             else "Remove takes them out of the queue. ")
            + f"{'⌘' if IS_MAC else 'Ctrl'}-click adds or drops a job, Shift-click a range, Escape keeps one.")

    # ── Details pane ─────────────────────────────────────────────────────────
    def _on_queue_clicked(self):
        """A click on a job shows it, and opens the details pane if it is closed."""
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
        """Give the opened pane its remembered width (first time: 560 px)."""
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
        """The job as chips: source kind → changes → output. With *detail* (the details
        pane) the output names its compression too: ".ffpfsc, level 7", ".pkg, fast"."""
        parts = self._job_recipe_parts(item)
        if detail and parts:
            if parts[-1] == ".ffpfsc":
                parts[-1] = f".ffpfsc, level {self._ffpfsc_level(item)}"
            elif parts[-1] == ".pkg":
                try:
                    _pl = int(getattr(item, "fpkg_level", None) if getattr(item, "fpkg_level", None) is not None
                              else self._pkg_level_default())
                except (TypeError, ValueError):
                    _pl = 0
                parts[-1] = f".pkg, level {_pl}"
        return parts

    def _job_recipe_parts(self, item) -> list[str]:
        op = getattr(item, "operation", "pack")
        src = source_label(item)
        if op == "copy" and getattr(item, "content_kind", "") == ORGANIZE_TARGET:
            if getattr(item, "archive_path", None) and not getattr(item, "path", None):
                src = "Archive"
            return [src, "Organize"]
        if op == "copy" and getattr(item, "content_kind", "") == "ps4":
            n = int(getattr(item, "ps4_count", 0) or 0)
            if getattr(item, "archive_path", None) and not getattr(item, "path", None):
                src = "Archive"
            return [src, "PS4 library" + (f" · {n} package{'s' if n != 1 else ''}" if n else "")]
        if op == "chain":
            ch = []
            for c in chain_changes(item):
                ch.append("Patch" if c == "patch" else "Sign" if c == "sign"
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

    # ── History view ─────────────────────────────────────────────────────────
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

    # ── Log view ─────────────────────────────────────────────────────────────
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

    # ── Status bar ───────────────────────────────────────────────────────────
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
        # The queue's overall progress is not shown (the job row carries the job's own
        # percentage); the bar object stays because the poll loop feeds it.
        self._overall_box = kit.frame(inner, bg="sidebar")
        self.overall_bar = ProgressBar(self._overall_box, kit, height=4, bg="sidebar")
        self._ram_label = kit.label(inner, bg="sidebar", fg="faint", font=f, textvariable=self.ram_var)
        self._ram_label.pack(side="right")
        kit.label(inner, bg="sidebar", fg="faint", font=f, textvariable=self.status_temp_var).pack(side="right", padx=(0, 14))
        kit.label(inner, bg="sidebar", fg="faint", font=f, textvariable=self.status_out_var).pack(side="right", padx=(0, 14))

        def _run_state(*_):
            running = bool(getattr(self, "_batch_running", False))
            txt = self._footer_plain_var.get().lower()
            color = ("danger" if any(w in txt for w in ("fail", "error", "cancel")) else
                     "success" if (not running and any(w in txt for w in ("ready", "done", "complete"))) else "accent")
            self._status_dot.set(color=color)
        self.batch_counter_var.trace_add("write", _run_state)
        self._footer_plain_var.trace_add("write", _run_state)
        return bar

    def _tick_space_status(self):
        """Free space on the temp and output drives for the status bar. The disk_usage
        calls run on a worker thread (a sleeping HDD can take seconds to answer); only the
        main thread touches Tk, so the paths are read here and the result is picked up on a
        later poll tick."""
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
                return f"{kind}: not reachable"
            parts = Path(p).parts
            drive = parts[2] if len(parts) > 2 and parts[1] == "Volumes" else "Mac"
            return f"{kind}: {drive}  ·  {format_size(free)} free"

        def _work():
            try:
                self._space_result = (_label("Temp", paths[0]), _label("Output", paths[1]))
            finally:
                self._space_busy = False
        self._space_busy = True
        threading.Thread(target=_work, daemon=True).start()

    # ── Menus ────────────────────────────────────────────────────────────────
    def _build_menubar(self):
        """File, Edit, View, Window and Help, with the same shortcuts as the buttons. On
        macOS the application menu carries About, Settings… and Quit (Tk routes the last
        two here); elsewhere Settings and Exit sit in File and About in Help. Entries that
        would act behind an open panel do nothing until it is closed."""
        root = self.root
        guard = lambda fn: (lambda: None if self._panels.stack else fn())
        mod_key = (lambda k: f"Command-{k}") if IS_MAC else (lambda k: f"Ctrl+{k}")
        mb = tk.Menu(root)
        if IS_MAC:
            appm = tk.Menu(mb, name="apple", tearoff=0)
            appm.add_command(label=f"About {APP_NAME}", command=guard(lambda: self._open_settings_page("about")))
            appm.add_separator()
            mb.add_cascade(menu=appm)
            root.createcommand("tk::mac::ShowPreferences", guard(self.open_settings))
            root.createcommand("tk::mac::Quit", self._on_close)
        fm = tk.Menu(mb, tearoff=0)
        fm.add_command(label="Add Job…", accelerator=ACCEL["add"], command=guard(self.open_job_dialog))
        fm.add_command(label="Look Inside…", accelerator=ACCEL["open"], command=guard(self.open_pfs_browser))
        fm.add_command(label="Organize…", command=guard(lambda: self._show_view("organize")))
        fm.add_separator()
        fm.add_command(label="Start Queue", accelerator=ACCEL["start"], command=guard(self.start))
        fm.add_command(label="Pause After This Job", command=self.toggle_pause)
        self._queue_menu, self._pause_menu_index = fm, fm.index("end")
        fm.add_command(label="Stop Queue", accelerator=ACCEL["stop"], command=self._stop_if_running)
        fm.add_command(label="Clear Completed Jobs", command=guard(lambda: self.clear_jobs("done")))
        fm.add_separator()
        fm.add_command(label="Clean Temp Folder", command=guard(self.clear_temp_files))
        if not IS_MAC:
            fm.add_separator()
            fm.add_command(label="Settings…", accelerator=ACCEL["settings"], command=guard(self.open_settings))
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
            vm.add_command(label=label, accelerator=ACCEL[key], command=guard(lambda k=key: self._show_view(k)))
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
        hm.add_command(label=f"{APP_NAME} on GitHub", command=lambda: webbrowser.open(REPO_URL))
        hm.add_separator()
        hm.add_command(label="Open Log Folder", command=self._open_log_folder)
        hm.add_command(label="Raw Backend Log", command=self.open_raw_log)
        hm.add_command(label="Export Diagnostics…", command=self.export_diagnostics)
        if not IS_MAC:
            hm.add_separator()
            hm.add_command(label=f"About {APP_NAME}", command=guard(lambda: self._open_settings_page("about")))
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
        """Cut, Copy, Paste and Select All act on the text field that has the focus."""
        w = self.root.focus_get()
        if w is not None:
            try:
                w.event_generate(event_name)
            except tk.TclError:
                pass

    def _stop_if_running(self):
        if self._batch_running:
            self.cancel()

    def _open_log_folder(self):
        ensure_app_dir()
        open_path(APP_DIR)

    def _init_state_vars(self):
        """The Tk variables behind the settings and the job defaults (paths, formats,
        tuning, drive behaviour), each persisted on change."""
        # Remember the last input path across restarts (a browsed folder stays in the
        # field). Only restore it if it still exists, so a moved/deleted path doesn't
        # linger. Picking anything new overwrites it via the trace below.
        _src0 = self._saved_source if (self._saved_source and Path(self._saved_source).exists()) else ""
        self.source_var = tk.StringVar(value=_src0)
        self.source_var.trace_add("write", lambda *_: save_settings({"source_path": self.source_var.get()}))
        self.output_var = tk.StringVar(value=self._saved_output)
        self.temp_var = tk.StringVar(value=self._saved_temp)
        # Persist typed/browsed paths so they survive a restart (previously only
        # browsed paths that happened to be saved elsewhere stuck).
        self.output_var.trace_add("write", lambda *_: save_settings({"output_folder": self.output_var.get()}))
        self.temp_var.trace_add("write", lambda *_: save_settings({"temp_folder": self.temp_var.get()}))
        # Probe (in the background) whether the temp/output drives are SSD or HDD so the
        # runtime placement labels can be honest WITHOUT blocking the UI thread on
        # diskutil/PowerShell. Re-probes when the folders change; cached per device.
        self.temp_var.trace_add("write", lambda *_: self._warm_drive_types())
        self.output_var.trace_add("write", lambda *_: self._warm_drive_types())
        self.source_var.trace_add("write", lambda *_: self._warm_drive_types())
        self._warm_drive_types()
        self.password_var = tk.StringVar()
        # Live copy of the global auto-tried password list (backs the settings editor).
        self.archive_passwords: list[str] = list(getattr(self, "_saved_passwords", []))
        # Extra temp/scratch drives (a "pool" added to the primary temp field). When more
        # than one fast drive is available the router can keep pass 1 SSD↔SSD for big
        # archive games by extracting the source to one and building the image on another.
        self.temp_pool: list[str] = [str(p) for p in (load_settings().get("temp_pool", []) or []) if str(p).strip()]
        self.copy_siblings_var = tk.BooleanVar(value=getattr(self, "_saved_copy_siblings", True))
        settings = load_settings()   # _build() is a separate method from _setup()
        self.keep_pfs_var        = self._persisted_bool(settings, "keep_pfs", False)
        self.open_output_var     = self._persisted_bool(settings, "open_output", False)
        self.summary_popup_var   = self._persisted_bool(settings, "summary_popup", True)
        self.sound_complete_var  = self._persisted_bool(settings, "sound_complete", True)
        self.sound_error_var     = self._persisted_bool(settings, "sound_error", True)
        self.batch_var = tk.BooleanVar(value=False)        # session state — not persisted
        self.unpack_mode_var = tk.BooleanVar(value=False)  # session state — not persisted
        # Output format for the whole queue: compressed .ffpfsc (smaller) vs uncompressed
        # .ffpfs (faster to build AND to mount — ShadowMountPlus decompresses .ffpfsc at only
        # ~150-250 MB/s and streaming-heavy games can stutter). True = compressed (default).
        self.output_compressed_var = self._persisted_bool(settings, "output_compressed", True)
        # AMPR/APR emu folder (PlayGo titles): holds libSceAmpr.sprx + libScePlayGo.sprx.
        self.ampr_var = tk.StringVar(value=settings.get("ampr_folder", ""))
        # Backport folders (both user-supplied, never bundled): the patched libraries the
        # job dialog offers by default, and the original target-firmware libraries the
        # compatibility check reads.
        _raw_libs, _fw = (settings.get("backport_libs_root", "") or ""), (settings.get("fw_libs_root", "") or "")
        _libs = _sane_patched_libs(_raw_libs, _fw)
        self._cleared_libs_setting = _raw_libs if _libs != _raw_libs else ""
        if self._cleared_libs_setting:
            save_settings({"backport_libs_root": ""})
        self.backport_libs_var = tk.StringVar(value=_libs)
        self.fw_libs_var = tk.StringVar(value=_fw)
        # Optional Sony Publishing Tools DLL for the fPKG 'publishingtools' Kraken backend.
        # Never bundled — the user points at their own copy. Empty = built-in encoder only.
        self.pubtools_dll_var = tk.StringVar(value=settings.get("pubtools_dll", ""))
        # The Output FORMAT is chosen PER JOB in the Pack dialog — three values:
        # 'ffpfsc' (compressed), 'ffpfs' (uncompressed) and 'pkg' (an installable fPKG).
        # output_format_var remembers the last choice as the default the dialog pre-fills
        # AND the format a dropped / browsed source is queued with (the queue snapshot in
        # update_queue_box applies it to every fresh pack item). output_compressed_var is
        # the legacy bool the pack pipeline still reads; it is kept in sync from here.
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
        # Remembered fPKG compression parameters (inner codec / Kraken backend / level) —
        # what the Pack dialog pre-fills for .pkg and what a dropped source gets when the
        # remembered format is .pkg. Identity is never remembered: it is per game and read
        # from sce_sys/param.json at build time.
        _fd = settings.get("fpkg_defaults") or {}
        try:
            _fl = int(_fd.get("level", 0) if _fd.get("level") is not None else 0)
        except Exception:
            _fl = 0
        if not _fd.get("v212"):
            # 2.2.0: the two presets ("normal" = 7, "fast" = -4) became a level slider whose
            # default is 0 — measured on a retail sample, 0 to 5 are 0.3 % larger than 7 and
            # 4.5x faster, so the old presets are moved to the new default once.
            _fl = 0
        _inner = _fd.get("inner") if _fd.get("inner") in ("none", "zlib", "kraken") else "kraken"
        if _inner == "none" and not _fd.get("v1112"):
            # Pre-1.1.12 remembered default. "none" was never verified on a console;
            # "kraken" is the layer every launching build used. Migrated once — a
            # "none" chosen after this (the settings then carry v1112) is respected.
            _inner = "kraken"
        self.fpkg_defaults: dict = {
            "inner":   _inner,
            "backend": _fd.get("backend") if _fd.get("backend") in ("builtin", "publishingtools") else "builtin",
            "level":   max(-4, min(9, _fl)),      # Kraken -4..9; see _pkg_level_text for what the steps buy
            "retail_normalize": bool(_fd.get("retail_normalize", True)),
            "hdr_flag":         _hdr_mode(_fd.get("hdr_flag", "auto")),   # 1.1.12/13 bool → auto/off
            "regen_playgo":     bool(_fd.get("regen_playgo", False)),
            "fake_sign":        bool(_fd.get("fake_sign", True)),
            "v1112":            True,
            "v212":             True,
        }
        self._pending_fpkg_identity = None   # (source path, identity dict) handed from the dialog to the scan result
        self.verify_output_var   = self._persisted_bool(settings, "verify_output", False)
        self.auto_clear_temp_var = self._persisted_bool(settings, "auto_clear_temp", False)
        # A finished job stays in the queue, marked Done, until it is cleared; with this on
        # it leaves the queue as soon as it succeeds. Failed jobs always stay.
        self.auto_remove_done_var = self._persisted_bool(settings, "auto_remove_done", False)
        # What happens once a job is done (the default Add job starts from) and once the
        # queue is done. Strings persisted like output_exists.
        _aj = _after_job_module()
        def _choice(key, allowed, default):
            v = settings.get(key, default)
            var = tk.StringVar(value=v if v in allowed else default)
            var.trace_add("write", lambda *_: save_settings({key: var.get()}))
            return var
        self.after_source_var = _choice("after_source", _aj.ACTIONS, _aj.KEEP)
        self.after_move_dir_var = tk.StringVar(value=str(settings.get("after_move_dir", "") or ""))
        self.after_move_dir_var.trace_add("write", lambda *_: save_settings(
            {"after_move_dir": self.after_move_dir_var.get().strip()}))
        self.notify_var = _choice("notify", _aj.NOTIFY, "off")
        self.after_queue_var = _choice("after_queue", _aj.QUEUE_ACTIONS, "nothing")
        # What a job does when its output is already in the output folder.
        _oe = settings.get("output_exists", "skip")
        self.output_exists_var = tk.StringVar(value=_oe if _oe in self.OUTPUT_EXISTS_CHOICES else "skip")
        self.output_exists_var.trace_add("write", lambda *_: save_settings({"output_exists": self.output_exists_var.get()}))
        # Auto-patch: when a release folder holds a base game plus a clearly-smaller
        # game-like sibling (a patch), overlay it onto the game before packing. Off
        # by default — opt in knowingly, since it changes what lands in the .ffpfsc.
        self.auto_integrate_patch_var = self._persisted_bool(settings, "auto_integrate_patch", False)
        # Drive-usage mode: auto | temp | spread (where archives get extracted).
        self.drive_mode_var = tk.StringVar(value=settings.get("drive_mode", "auto"))
        self.drive_mode_var.trace_add("write", lambda *_: save_settings({"drive_mode": self.drive_mode_var.get()}))
        # Show the drive-space pre-flight dialog before each pack (default on). The
        # tunable safety factor and the low-space policy live in settings.json and are
        # edited in Settings (see SettingsView).
        self.show_space_dialog_var = self._persisted_bool(settings, "show_space_dialog", True)
        # Opt-in: build an exFAT intermediate and compress that (PSBrew's most-stable
        # exfat->ffpfsc path) instead of the folder PFS builder. macOS only. Default off.
        self.build_via_exfat_var = self._persisted_bool(settings, "build_via_exfat", False)
        # Opt-in: fake-sign a game folder's executables in place before packing it.
        self.fake_sign_before_pack_var = self._persisted_bool(settings, "fake_sign_before_pack", False)
        # Auto-organize (default on): every pack / fPKG job lands in '<Title> [TID] [vX.Y.Z]/'
        # as '<Title> [TID] [vX.Y].ffpfsc|.pkg', named from the game's own param.json —
        # whatever the source was called. Per job (snapshotted like the format); this is the
        # remembered default the Pack dialog pre-fills.
        self.auto_organize_var = self._persisted_bool(settings, "auto_organize", True)
        # Keep external drives awake DURING A RUN only: a fast tiny flushed write so
        # bus-powered 2.5" USB HDDs (WD Elements) stay spun-up with heads LOADED across the
        # short gaps between games in a batch — so each game doesn't pay a fresh spinup. The
        # interval ('keep_awake_interval', default 8 s) is deliberately under WD IntelliPark's
        # 8 s park timer: a SLOWER ping would just unpark→re-park every cycle and ADD load
        # cycles. When no job runs we ping nothing and let the drive fully sleep (its lowest-
        # wear state). Default off; toggle + interval live in Settings → Drive & Space.
        self.keep_drives_awake_var = self._persisted_bool(settings, "keep_drives_awake", False)
        # MkPFS tuning
        self.compression_level_var = tk.IntVar(value=self._saved_compression_level)
        self.cpu_count_var         = tk.IntVar(value=self._saved_cpu_count)
        self.verbose_var           = self._persisted_bool(settings, "verbose", False)
        self.block_size_var        = tk.StringVar(value=self._saved_block_size)
        # Persist the main-window tuning controls on change (previously only the
        # Settings window saved these, so edits made in the main window were lost).
        self.compression_level_var.trace_add("write", lambda *_: save_settings({"compression_level": self.compression_level_var.get()}))
        self.cpu_count_var.trace_add("write", lambda *_: save_settings({"cpu_count": self.cpu_count_var.get()}))
        self.block_size_var.trace_add("write", lambda *_: save_settings({"block_size": self.block_size_var.get()}))

    # ── Theme toggle ─────────────────────────────────────────────────────────
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
        # CTk widgets (the dialogs) follow their (light, dark) tuples on their own; the
        # main window's Tk widgets are recoloured through the kit.
        try:
            self.kit.set_mode(self._theme)
        except Exception:
            pass



    def open_settings(self):
        self._show_view("settings")



    def _backend_cmd(self, *args) -> list:
        """Build a backend invocation for arbitrary args (list/extract image, …),
        matching how the queue worker calls the backend: frozen re-invokes this exe
        with --backend-internal; dev runs backend/cli.py under the interpreter."""
        pycmd = get_backend_python_command() or ["python"]
        if getattr(sys, "frozen", False):
            return pycmd + list(args)
        return pycmd + ["-u", str(backend_base_dir() / "cli.py")] + list(args)

    def open_pfs_browser(self):
        PfsBrowserDialog(self)

    def open_job_dialog(self, init_src: str | None = None):
        """The one door for every job: source → change the content → output (JobDialog)."""
        JobDialog(self, init_src=init_src)

    def _pkg_level_default(self) -> int:
        """The Kraken level a new .pkg job starts from (Settings › Compression), -4..9."""
        try:
            return max(-4, min(9, int(self.fpkg_defaults.get("level", 0))))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _pkg_level_text(level: int) -> str:
        """What a Kraken level buys, from the 2.2.0 measurement on a retail sample."""
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
        """The zlib level of a .ffpfsc build: the job's own, else the default in Settings."""
        for value in (getattr(item, "compression_level", None), self.compression_level_var.get()):
            try:
                if value is not None:
                    return max(1, min(9, int(value)))
            except (TypeError, ValueError):
                continue
        return 7

    def _backport_args(self, item) -> list[str]:
        """--backport-target, --backport-libs and --fw-libs-root for a job that backports.
        The firmware libraries folder is global (Settings): with it the backend reads the
        SDK values of non-public targets and checks the game's functions before building."""
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
        """Prepare the patched-libraries folder for *target* from BestPig BackPork.
        Runs in a background thread; per-library progress goes to the app log. The user
        must have set the two Settings folders first — otherwise a clear line says which
        one is missing and where to set it."""
        fw = (self.fw_libs_var.get() or "").strip()
        out = (self.backport_libs_var.get() or "").strip()
        if not fw or not Path(fw).is_dir():
            self.log("ERROR", f"Prepare {target}: set the firmware libraries folder in Settings first "
                              f"(“Backport — firmware libraries”), with your 10.01 libraries in a 10.01 subfolder.")
            return
        if _backport_module().firmware_folder(Path(fw), "10.01") is None:
            self.log("ERROR", f"Prepare {target}: no 10.01 folder in {fw}. The BackPork patches apply to "
                              f"10.01 libraries only.")
            return
        if not out:
            self.log("ERROR", f"Prepare {target}: set the PATCHED libraries folder in Settings first "
                              f"(“Backport — patched libraries folder”).")
            return
        pycmd = get_backend_python_command()
        if not pycmd:
            self.log("ERROR", "Backend not found; cannot run Prepare.")
            return
        cli_py = backend_base_dir() / "cli.py"
        head = pycmd if getattr(sys, "frozen", False) else pycmd + ["-u", str(cli_py)]
        argv = head + ["--prepare-backport-libs", target,
                       "--fw-libs-root", fw, "--backport-libs", out]
        self.log("INFO", f"Prepare {target}: downloading BackPork patches and applying them to {fw} → {out}/{target}…")

        def work():
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, errors="replace")
                for line in proc.stdout or ():
                    self.root.after(0, self.log, "INFO", line.rstrip())
                proc.wait()
                if proc.returncode == 0:
                    self.root.after(0, self.log, "OK", f"Prepare {target} finished — the job dialog will pick up "
                                                       f"{out}/{target}/ as Patched libraries.")
                else:
                    self.root.after(0, self.log, "ERROR", f"Prepare {target} finished with exit code {proc.returncode}.")
            except Exception as e:
                self.root.after(0, self.log, "ERROR", f"Prepare {target} failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def fake_sign_folder(self):
        """Fake Sign job: pick a decrypted PS5 dump folder and ADD it to the queue as a
        fake-sign job (eboot.bin / .elf / .prx / .sprx are signed in place when it runs).
        Press START to run — like every other job type."""
        path = filedialog.askdirectory(
            title="Select a PS5 game folder (or a parent folder of dumps) to fake-sign")
        if not path:
            return
        folder = Path(path)
        if not folder.is_dir():
            messagebox.showerror("Not a folder", f"This is not a folder:\n{folder}")
            return
        if not messagebox.askyesno(
                "Fake Sign — in place",
                f"Queue a fake-sign job for:\n\n{folder}\n\n"
                "When it runs it modifies the executables IN PLACE (already-signed files are "
                "skipped, so it is safe to repeat). Add to queue?"):
            return
        item = GameItem.from_fake_sign(folder)
        self.queue.append(item)
        self.update_queue_box(select_item=item)
        self.log("OK", f"Fake-sign queued: {folder.name}.  Press ▶ START to run.")
        self.status_update("Ready", f"Fake-sign queued: {folder.name} — press START.",
                            "Ready", 0, 0, "00:00", "—", "—", side=True)

    def fpkg_extract_dialog(self):
        """fPKG EXTRACT: pick a .pkg file and queue a job that pulls the /app0 tree out
        of it. Ships the bundled ffpfsc-pkg-tool binary — no external DLL required."""
        path = filedialog.askopenfilename(
            title="Select a PS5 fake package (.pkg) to extract",
            filetypes=[("PS5 package", "*.pkg"), ("All files", "*.*")])
        if not path:
            return
        pkg = Path(path)
        if not pkg.is_file():
            messagebox.showerror("Not a file", f"This is not a file:\n{pkg}"); return
        out = (self.output_var.get() or "").strip()
        if not out:
            out = str(pkg.parent / (pkg.stem + " [extracted]"))
        item = GameItem.from_fpkg_extract(pkg, output_path=out)
        self.queue.append(item)
        self.update_queue_box(select_item=item)
        self.log("OK", f"fPKG extract queued: {pkg.name} -> {out}.  Press ▶ START to run.")


    # ── Drag & drop ───────────────────────────────────────────────────────────
    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        # One dropped path opens the job dialog with it as the source — the user then says
        # what to change and what comes out. Several paths at once keep the batch path
        # below (remembered format), so a whole library can still be dropped in one go.
        if len(paths) == 1:
            p = Path(str(paths[0]).strip("{}").strip())
            if not p.exists():
                self.log("WARN", f"Dropped path not found: {p}"); return
            self.open_job_dialog(init_src=str(p)); return
        for raw in paths:
            p = Path(raw.strip("{}").strip())
            if not p.exists():
                self.log("WARN", f"Dropped path not found: {p}"); continue
            # A dropped .pkg becomes an fPKG-extract job (into a "<name> [extracted]" folder
            # next to it, unless a global Output is set).
            if p.is_file() and p.suffix.lower() == ".pkg" and _is_ps4_source(p):
                # a PS4 package goes into the library; without an Output the editor asks where
                out = (self.output_var.get() or "").strip()
                if not out:
                    self.open_job_dialog(init_src=str(p)); continue
                item = self._ps4_item_for(p, output_path=out)
                self.queue.append(item)
                self.update_queue_box(select_item=item)
                self.log("OK", f"PS4 package queued from drop: {p.name} -> {out}.")
                continue
            if p.is_file() and p.suffix.lower() == ".pkg":
                out = (self.output_var.get() or "").strip()
                if not out:
                    out = str(p.parent / (p.stem + " [extracted]"))
                item = GameItem.from_fpkg_extract(p, output_path=out)
                self.queue.append(item)
                self.update_queue_box(select_item=item)
                self.log("OK", f"fPKG extract queued from drop: {p.name} -> {out}.")
                continue
            self.source_var.set(str(p))
            self.add_source_to_queue()   # handles folders AND archives uniformly

    # ── Folder browse ─────────────────────────────────────────────────────────
    def browse_source_folder(self):
        """Folder picker — single game folder or parent folder containing multiple dumps."""
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
        """File picker — select an archive, disk image, or PFS image."""
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
        # A picked .pkg becomes an fPKG-extract queue item straight away.
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
        if p.suffix.lower() == ".ffpfsc":   # .ffpfs is a PACK source now (re-pack), not unpack
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
        # Cache: skip disk I/O if the file hasn't changed
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
                img.thumbnail((self.ART_PX, self.ART_PX), Image.LANCZOS)
                try:
                    from PIL import ImageChops, ImageDraw
                    mask = Image.new("L", img.size, 0)
                    ImageDraw.Draw(mask).rounded_rectangle(
                        [0, 0, img.size[0] - 1, img.size[1] - 1], radius=max(10, self.ART_PX // 7), fill=255)
                    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
                except Exception:
                    pass
                photo = ImageTk.PhotoImage(img)
            except Exception:
                photo = None
        self.art_img = photo   # hold a reference; Tk drops unreferenced images
        self.art_label.set_photo(photo)

    # ── Queue management ──────────────────────────────────────────────────────
    @staticmethod
    def _clean_path_str(raw: str) -> str:
        """Strip whitespace and surrounding quotes Windows sometimes adds."""
        s = raw.strip()
        if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
            s = s[1:-1].strip()
        return s

    def _classify_extracted_payload(self, extracted_root: Path, archive_name: str) -> tuple[str, list[Path]]:
        """Return the supported payload type and paths found inside an extracted archive."""
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

        # Nested archive (archive inside the archive) — give a clear, actionable error
        # instead of the misleading generic "no payload" message.
        inner_archives = find_files_by_suffix(extracted_root, {".zip", ".rar", ".7z"})
        if inner_archives:
            names = ", ".join(sorted({a.name for a in inner_archives})[:5])
            raise RuntimeError(
                f"{archive_name} contains another archive ({names}), not a game.\n\n"
                "Nested archives are not unpacked automatically — extract the inner "
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

    def _sibling_extras(self, parent: Path, game_paths) -> list:
        """Extra items (DLC subfolders, loose non-junk files) sitting BESIDE the game in an
        extracted archive tree, to copy next to the .ffpfsc. Excludes the detected game
        folder(s) and any wrapper that contains one, OS/Finder junk, and side-car note files
        (.txt/.nfo/images …). The source is never modified — these are copied, not moved."""
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
        # An fPKG job stays an fPKG job, and a job-editor chain stays a chain (its output
        # format, sign, patch and backport), whatever the archive turned out to hold: a
        # game folder, a disk image, a .ffpfsc; the backend unwraps images itself. Those
        # fields live on the target and are untouched here.
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
        # After extraction the source is a real folder on the build drive: it becomes
        # 'inplace' (packed in place), so the post-extraction re-gate sizes only the
        # remaining image+spool (~2.3x) rather than re-counting the already-written tree.
        target.source_kind    = getattr(source, "source_kind", "inplace")
        target.extracted_size = getattr(source, "extracted_size", source.size)
        # Now that the archive is a real folder, detect whether it's a PlayGo/APR title.
        # (Only acted on for pack jobs — the fPKG path never injects the emu.)
        try:
            target.ampr_emu = is_apr_game(target.path)
        except Exception:
            target.ampr_emu = False

    def _launch_scan(self, fn):
        """Run a source-scan worker on a daemon thread while tracking that a scan is in
        flight (so a START pressed during the scan doesn't re-add the same source). The
        counter is decremented in the worker's finally — exactly once per launch —
        regardless of how the scan ends (ok/error/exception)."""
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
                                  f"This path does not exist:\n{src}")
            return
        # One-shot "scan this folder for images to unpack" flag: whoever set it (the
        # Converter batch action, an image browse) meant THIS add. Consume it here so it
        # can never leak into the next folder the user drops.
        _unpack_folder = bool(self.unpack_mode_var.get())
        self.unpack_mode_var.set(False)

        # ── PS4 packages (one, or a folder of them): sorted into the library ─────────
        if not _unpack_folder and _is_ps4_source(src):
            out = (self.output_var.get() or "").strip()
            if not out:
                self.pending_start = False
                self.open_job_dialog(init_src=str(src)); return
            item = self._ps4_item_for(src, output_path=out)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f"PS4 packages queued: {src.name} -> {out}.  Press ▶ START to run.")
            return

        # ── Existing uncompressed .ffpfs — a PACK source (re-pack to .ffpfsc, or copy
        #    when the format toggle is set to uncompressed). Read in place, no extraction. ─
        if src.is_file() and src.suffix.lower() == ".ffpfs":
            item = GameItem.from_exfat(src)   # single-file image item, operation = "pack"
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f".ffpfs image queued for (re)packing: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                f".ffpfs image queued for packing: {src.name}",
                                "Ready", 0, 0, "00:00", "—", "—", side=True)
            return

        # ── Existing .ffpfsc image — unpack/convert via MkPFS, or, with '.pkg' as the
        #    remembered format, straight into an fPKG build (the backend unwraps it). ──
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
                self.status_update("Ready", f".ffpfsc queued for fPKG build: {src.name}",
                                    "Ready", 0, 0, "00:00", "—", "—", side=True)
                return
            item = GameItem.from_pfs_image(src)   # carries operation="unpack" itself
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f".ffpfsc image queued for extraction: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                f".ffpfsc image queued for extraction: {src.name}",
                                "Ready", 0, 0, "00:00", "—", "—", side=True)
            return

        # ── Direct .exfat / .ffpkg disk image — no extraction, passed straight to backend ─
        if src.is_file() and src.suffix.lower() in DISK_IMAGE_SUFFIXES:
            item = GameItem.from_exfat(src)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            label = "exFAT image" if src.suffix.lower() == ".exfat" else "ffpkg image"
            self.log("OK", f"{label} queued: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                f"{label} queued: {src.name}",
                                "Ready", 0, 0, "00:00", "—", "—", side=True)
            return

        # ── Single archive file — queue as placeholder, extract on its turn ─────
        if src.is_file() and src.suffix.lower() in (".zip", ".rar", ".7z"):
            # Reading the headers of a many-volume set takes seconds on a slow drive: do it
            # on the scan thread; the main loop queues the item ("archive" message below).
            self.status_update("Scanning", f"Reading archive headers: {src.name}…",
                                "Scanning Files", 0, 0, "00:00", "—", "—", side=True)

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
                    self.scan_q.put(("error", f"Could not read {a.name}: {e}"))
            self._launch_scan(_read_archive)
            return

        # ── Folder of existing PFS images to unpack ──────────────────────────
        if src.is_dir() and _unpack_folder:
            self.status_update("Scanning", f"Scanning {src.name} for PFS images…",
                                "Scanning Files", 0, 0, "00:00", "—", "—", side=True)
            self.log("INFO", f"Scanning folder for .ffpfs/.ffpfsc images: {src}")

            def _scan_pfs_folder(p=src):
                try:
                    images = find_files_by_suffix(p, PFS_IMAGE_SUFFIXES)
                    if not images:
                        self.scan_q.put(("error", f"No .ffpfs or .ffpfsc files found in:\n{p}"))
                    elif len(images) == 1:
                        self.scan_q.put(("ok", GameItem.from_pfs_image(images[0])))
                    else:
                        self.scan_q.put(("pfs_found", images))
                except Exception as e:
                    self.scan_q.put(("error", f"PFS scan error: {e}"))
            self._launch_scan(_scan_pfs_folder)
            return

        # ── Direct PS5 game folder ────────────────────────────────────────────
        if is_game_folder(src):
            warns = validate_game_structure(src)
            if warns:
                msg = "\n".join(f"• {w}" for w in warns)
                if not messagebox.askyesno(
                    "Game Structure Warning",
                    f"Potential issues detected:\n\n{msg}\n\n"
                    "Expected: sce_sys/param.json and eboot.bin\n\nAdd anyway?"
                ):
                    self.pending_start = False
                    self._pending_fpkg_identity = None
                    return
            self.status_update("Scanning", f"Reading {src.name}…",
                                "Scanning Files", 0, 0, "00:00", "—", "—", side=True)
            def _scan_single(p=src):
                try:
                    self.scan_q.put(("ok", GameItem(p)))
                except Exception as e:
                    self.scan_q.put(("error", str(e)))
            self._launch_scan(_scan_single)
            return

        # ── Parent / unknown folder ───────────────────────────────────────────
        # Scan for extracted game folders AND loose archive files
        self.status_update("Scanning", f"Scanning {src.name}…",
                            "Scanning Files", 0, 0, "00:00", "—", "—", side=True)
        self.log("INFO", f"Scanning folder: {src}")
        cand_pw = self._candidate_passwords()   # built on the Tk thread, for archive peeking

        def _scan_folder(p=src, cand_pw=cand_pw, detect_patch=self.auto_integrate_patch_var.get()):
            try:
                # 0. Bundle: one game (archive / game folder / disk image) plus
                #    extra files (DLCs etc.). Pack the game into a recreated copy
                #    of this folder and copy the extras next to it. Only triggers
                #    when there ARE extras — a plain game still packs normally.
                #    With auto-patch on, a clearly-smaller game-like sibling is
                #    detected as a patch and overlaid, so a bundle can be just
                #    base + patch with no other extras.
                game, siblings, all_games, patch = detect_game_bundle(p, cand_pw, self.log, detect_patch)
                # Bundle triggers when:
                #   - there's a game + extras (siblings/patch), OR
                #   - the folder holds exactly one game which is a single FILE (archive
                #     or disk image) and nothing else — so the folder name itself acts
                #     as the bundle identity at the destination (user request: "pick a
                #     folder with one RAR in it → mirror the folder name to the output").
                # A folder whose game is a SUBFOLDER (already a game folder) doesn't
                # trigger this — it stays a plain game pack so the output isn't pointlessly
                # nested in a duplicate folder layer.
                naked_file_game = (game is not None
                                   and game.is_file()
                                   and not siblings and not patch)
                if game is not None and (siblings or patch or naked_file_game):
                    if naked_file_game:
                        extra = f"single file game '{game.name}' — folder name mirrored to output"
                    else:
                        extra = f"1 game + {len(siblings)} extra file(s)"
                        if patch:
                            extra += f" + patch '{patch.name}' (integrated)"
                    self.log("OK", f"Folder bundle: {extra} — "
                                   "the folder will be recreated at the destination.")
                    self.scan_q.put(("ok", GameItem.from_bundle(p, game, siblings, patch)))
                    return
                # 0b. Library: each immediate subfolder holds its own game →
                #     one bundle per subfolder (each recreated at the destination
                #     with only the .ffpfsc plus extras inside). Covers "select
                #     /…/PS5 Games and convert every game".
                lib = scan_parent_for_bundles(p, cand_pw, self.log, detect_patch)
                if lib:
                    self.log("OK", f"Library scan: {len(lib)} game folder(s) found — each will be "
                                   "mirrored at the destination.")
                    self.scan_q.put(("bundles", lib))
                    return

                # 1. Look for extracted game folders first
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

                # 2. No extracted games — look for .exfat/.ffpkg images and archive files (one level deep)
                self.log("INFO", "No game folders found — scanning for disk images and archives…")
                image_files = []   # .exfat and .ffpkg
                archives_raw = []  # every .zip/.rar/.7z on disk, including every .partN.rar volume
                try:
                    for f in p.iterdir():
                        if not f.is_file():
                            continue
                        # Skip macOS/Windows filesystem junk: a '._Game.rar' AppleDouble
                        # sidecar (created when a .rar is copied to exFAT/FAT) carries the
                        # real file's suffix, so without this it would queue as a bogus
                        # 'archive'. Same for ._*.exfat, .DS_Store, Thumbs.db, …
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
                # Dedup multi-part RAR sets: a set like name.part1.rar, name.part2.rar, …
                # is ONE game, not N. Normalise every part to its first volume and unique
                # the result so the queue gets ONE item per archive set (the from_archive
                # builder already keys off the first volume for size + extraction). Log
                # one line per SET, not per file on disk.
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
                    self.log("INFO", f"Found {len(image_files)} disk image(s) — queuing directly (no extraction needed)")
                    if len(image_files) == 1:
                        self.scan_q.put(("ok", GameItem.from_exfat(image_files[0])))
                    else:
                        self.scan_q.put(("exfat_found", image_files))
                    return

                if archives:
                    self.log("INFO", f"Found {len(archives)} archive(s) — queuing for extraction")
                    self.scan_q.put(("archives_found", archives))
                    return

                # 3a. Split .7z/.zip volumes (.7z.001 / .zip.001 / .z01) — not
                #     recombined automatically. Recognise them so the user gets clear
                #     guidance instead of a confusing "nothing found".
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
                        f"Found split-archive parts in:\n{p}\n\n  {sample}\n\n"
                        "Split .7z / .zip volumes are not recombined automatically. "
                        "Recombine them into a single .7z, .zip or .rar first "
                        "(e.g. with 7-Zip or Keka), then add that file.\n"
                        "(Multi-part RAR — .partN.rar or .rNN — is supported directly.)"
                    ))
                    return

                # 3b. Nothing useful found
                self.log("WARN", f"No games, disk images, or archives found in {p}")
                self.scan_q.put((
                    "error",
                    f"Nothing found in:\n{p}\n\n"
                    "Expected either:\n"
                    "  • Game folders containing sce_sys/ and eboot.bin\n"
                    "  • Disk images (.exfat or .ffpkg)\n"
                    "  • Archive files (.zip / .rar / .7z)"
                ))
            except Exception as e:
                self.log("ERROR", f"Folder scan crashed: {e}")
                self.scan_q.put(("error", f"Scan error: {e}"))

        self._launch_scan(_scan_folder)

    def _archive_set_size(self, archive: Path) -> int:
        """Total on-disk size of the archive's volume set (third-party archives are usually
        stored, so this ~= the extracted payload size). Used for the auto drive decision."""
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
        """macOS: drop a `.metadata_never_index` marker in each scratch dir so Spotlight
        skips it. Without this, mdworker indexes every freshly-extracted game file (tens
        of thousands of them) on the same drive we're reading from, stealing HDD I/O and
        throttling the pack. Only our scratch dirs are touched — the user's finished
        outputs stay indexable. Best-effort, idempotent, no-op off macOS."""
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
        """Kick off background SSD/HDD probes for the source + temp + output drives so
        temp_drive_label()/drive_type_cached() have honest data at pack time without ever
        blocking the UI thread on diskutil/PowerShell. Cached per device, so this is a
        no-op after the first probe of a given drive. (Source is included so the keep-awake
        pinger correctly SKIPS a confirmed-SSD source instead of treating it as Unknown.)"""
        probes = [self.output_var.get().strip(), self.source_var.get().strip()]
        probes += [str(d) for d in self._temp_pool_dirs()]   # primary temp + extra pool drives
        for p in probes:
            if not p:
                continue
            threading.Thread(target=lambda pp=Path(p): get_drive_type(pp), daemon=True).start()

    # ── Keep-awake pinger ──────────────────────────────────────────────────────
    def _start_keep_awake(self) -> None:
        """One long-lived daemon that pings the configured drives so bus-powered
        external HDDs don't sleep. It checks the toggle each cycle (so flipping the
        Settings checkbox takes effect without restarting the thread)."""
        self._keepawake_stop = threading.Event()
        self._keepawake_announced = False
        self._keepawake_thread = threading.Thread(target=self._keep_awake_loop, daemon=True)
        self._keepawake_thread.start()

    @staticmethod
    def _dev_of(path):
        """st_dev for the volume holding *path* (its parent if it's a file), or None."""
        if not path:
            return None
        try:
            p = Path(path)
            d = p if p.is_dir() else p.parent
            return os.stat(str(d)).st_dev
        except OSError:
            return None

    def _temp_pool_dirs(self):
        """Ordered, device-deduped list of fast-temp candidate directories: the primary
        temp field first, then the extra pool dirs from Settings. The router spreads the
        inner image / extracted source across these (and the keep-awake pinger covers
        them). The primary is always included even if it doesn't exist yet (it's created on
        demand); extra pool entries must exist."""
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
        """(dir, st_dev) per physical drive the pinger may touch — source, output, and
        every temp-pool drive. Skips drives we have CONFIRMED to be SSDs (no point), and
        keeps HDD plus still-unknown drives (the WD Elements externals report 'Unknown')."""
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
        """Best-effort set of device IDs the current job is actively reading/writing, by
        worker phase, so the pinger can SKIP them — a flushed write there forces a pointless
        seek away from the streaming I/O. Empty set ⇒ ping every configured drive (the safe
        default when we can't tell, e.g. during pre-worker archive extraction or 'Starting')."""
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
            # pass 1: read source, write inner image. An archive reads from its extracted
            # dir (_build_root); a plain folder reads from its original path (_build_root is
            # an unused extract dir for folders, so don't treat it as busy).
            add((br or src) if arch else src, bt)
        elif phase in ("Compressing", "Writing Final Image"):
            add(bt, out)                 # pass 2: read inner image, write .ffpfsc
        elif phase == "Extracting":
            add(arch or src, br)         # archive extraction: read archive, write extract dir
        elif phase == "Verifying Output":
            add(out)                     # verify: read .ffpfsc
        return busy

    def _job_active(self) -> bool:
        """True while the app is actively working the drives (a batch, a live pack worker,
        a running backend process, or a pending extraction). Used to scope the keep-awake
        pinger to runs only — outside a run we let external HDDs fully sleep."""
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
                # Fast ping (default 8 s, under WD IntelliPark's 8 s park timer) keeps heads
                # LOADED across inter-game gaps instead of unpark→re-park churn.
                try:
                    interval = max(3, min(15, int(load_settings().get("keep_awake_interval", 8))))
                except Exception:
                    interval = 8
                # Ping only the drives the job ISN'T currently hammering: a flushed write on
                # an actively-streaming drive just forces a wasteful seek, and a busy drive
                # can't sleep anyway. The idle-but-needed-soon drive is the one worth holding.
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
            # Ping on the fast interval during a run; otherwise just poll the gate every 5 s.
            self._keepawake_stop.wait(interval if active else 5)

    def _same_drive_rw_allowed(self, path) -> bool:
        """Whether reading the source and writing the image/output on the SAME drive is OK
        for *path* — costless on an SSD (no seek), painful on an HDD. Controlled by the
        'same_drive_rw' setting: 'always' / 'never' / 'auto' (default → allow only when the
        drive probes as an SSD). When allowed, the router keeps everything on this drive
        instead of routing the source onto a slower output drive."""
        mode = (load_settings().get("same_drive_rw", "auto") or "auto").lower()
        if mode == "always":
            return True
        if mode == "never":
            return False
        try:
            # Use the warmed cache first; only block on a probe if it's not known yet
            # (now self-healing — Unknown is never cached, so a transient miss re-probes).
            dt = drive_type_cached(Path(path))
            if dt == "Unknown":
                dt = get_drive_type(Path(path))
            # Log the detection ONCE per drive so it's visible why same-drive is on/off.
            try:
                k = _drive_cache_key(Path(path))
                if not hasattr(self, "_drive_type_logged"):
                    self._drive_type_logged = set()
                if k not in self._drive_type_logged:
                    self._drive_type_logged.add(k)
                    self.log("INFO", f"Temp drive detected as {dt} — same-drive read+write "
                                     f"{'allowed (kept on this drive)' if dt == 'SSD' else 'avoided (split to output)'}.")
            except Exception:
                pass
            return dt == "SSD"
        except Exception:
            return False

    def _archive_extract_pct(self, item) -> float:
        """The share of the whole job an archive's unpack takes in the queue bar: a copy job
        whose unpack lands on its output drive only renames afterwards (nearly all of it),
        one that copies across drives moves the same bytes again (half); a build follows
        with its own long stages (ARCHIVE_EXTRACT_OVERALL_PCT)."""
        if getattr(item, "operation", "") != "copy":
            return ARCHIVE_EXTRACT_OVERALL_PCT
        root, out = getattr(item, "_build_root", None), self._job_output_dir(item)
        try:
            if root is not None and out is not None and same_drive(Path(str(root)).parent, out):
                return ARCHIVE_EXTRACT_RENAME_PCT
        except Exception:
            pass
        return ARCHIVE_EXTRACT_COPY_PCT

    def _resolve_extract_root(self, item) -> Path:
        """Place this run's artifacts across drives to maximise fast (SSD) temp use, and
        record the choice on the item: _build_root (where an archive extracts) and
        _build_temp (the backend --temp-dir = where the inner image goes). The pass-2 spool
        is then placed ADAPTIVELY by the backend (SSD if it still fits beside the image,
        else the output drive), so the SSD no longer has to hold image+spool together.

        AUTO weighs three independent placements, each preferring the temp/SSD drive and
        each falling back to the output drive — and never lets an HDD do a same-drive
        read+write for the heavy steps:
          1) Everything on the SSD (source + image + spool) when the full footprint fits.
          2) SPLIT: inner image on the SSD, source extracted to the output drive (the spool
             is auto-routed). Used when only the image — not image+spool — fits the SSD.
          3) Everything on the output drive when the SSD can't even hold the image.
          4) Nothing fits → leave temp; the space gate skips/aborts with real numbers.

        Modes 'temp'/'spread' force the SSD / the output drive respectively; the backend's
        adaptive spool still saves a too-tight 'temp' run from failing mid-pass-2."""
        archive = getattr(item, "archive_path", None)
        # Record the output format so the space gate sizes the OUTPUT-drive reservation
        # correctly (compressed .ffpfsc → realistic; uncompressed .ffpfs → full size).
        # Honour the per-job format when set, else the remembered default.
        try:
            _pj = getattr(item, "output_compressed", None)
            item._output_compressed = bool(self.output_compressed_var.get() if _pj is None else _pj)
            # (An fPKG is compressed too — every file is Kraken-packed whatever the codec
            # layer — so the compressed-output estimate applies to .pkg output as well.)
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

        # Placement is resolved several times per item (the space gate, the extraction
        # step, and the post-extraction re-gate), so log a given decision only ONCE per
        # item — re-log only if the decision actually changes — to keep the log clean.
        def _plog(level, msg):
            if getattr(item, "_last_placement_log", None) == msg:
                return
            item._last_placement_log = msg
            self.log(level, msg)

        mode = self.drive_mode_var.get() if getattr(self, "drive_mode_var", None) else "auto"
        _jo = self._job_output_dir(item)
        out_str = str(_jo) if _jo else ""
        size = _build_size_of(item)
        size_is_estimate = False
        # Encrypted-header archives (rar -hp / 7z with encrypted file names) report
        # extracted_size=0, so _build_size_of returns UNKNOWN and the SSD-aware routing
        # options (1/2/3 — all gated on `size > 0`) would fall through to the safe path
        # = the bigger output drive. But game .rar/.zip is already heavily compressed
        # game data, so the on-disk volume size is a tight lower bound for the extracted
        # payload (~1.0–1.05x). Estimate from on-disk size so the SSD routes can run;
        # the space gate is the backstop if the estimate ever undershoots reality.
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
        # Same-drive read+write: on an SSD (or when forced) it costs nothing, so DON'T
        # pre-reserve the extra final-image padding for a single-drive build — that padding
        # is what makes "everything on one drive" fail and pushes the source onto a slower
        # output drive. Keeping it off lets the source stay on the fast temp drive. The
        # backend's pre-pass-2 assert is the backstop if a title is unusually incompressible.
        same_rw_ok = self._same_drive_rw_allowed(temp_base_p)
        item._same_drive_ok = bool(same_rw_ok)
        same_pad = same_to and not same_rw_ok
        full_need  = estimate_peak_space_needed(size, factor, same_pad)  # src+image+spool on temp
        image_need = estimate_image_space_needed(size)                   # just the inner image on temp
        out_full   = estimate_peak_space_needed(size, factor, True)      # everything on the output drive
        # Output drive must hold, in the split: the source copy extracted there (archives
        # only) + the final container (the spool only spills here if the SSD can't hold it).
        comp_out = bool(getattr(item, "_output_compressed", True))
        known_out = int(getattr(item, "size", 0) or 0) if getattr(item, "source_kind", "") == "archive" else 0
        src_on_out = size if archive else 0
        out_split_need = int(src_on_out) + estimate_output_space_needed(size, comp_out, known_out)
        temp_free = get_free_space(temp_base_p)
        out_free  = get_free_space(probe_out)
        szs = (format_size(size) + " est.") if size_is_estimate else (format_size(size) if size else "?")
        contention = " (output is the source drive: read+write contention, slower)" if out_is_source else ""

        # ── Disk images (.exfat, .ffpkg): single-pass — mkpfs compresses the file
        # directly to .ffpfsc without building a temp inner image.  Temp = irrelevant;
        # only the output drive needs space for the final container. ──────────────────
        if _item_is_single_pass(item):
            output_need = estimate_output_space_needed(size, comp_out, 0)
            set_spread()   # _build_temp on the output drive (backend still needs a temp dir)
            if size == 0 or out_free >= output_need:
                if size > 0:
                    _plog("INFO", f"Auto: {item.name} (~{szs}): disk image → single-pass "
                                  f"to {out_dir} (~{format_size(output_need)} needed, "
                                  f"{format_size(out_free)} free){contention}.")
            else:
                _plog("WARN", f"Auto: {item.name} (~{szs}): disk image needs "
                              f"~{format_size(output_need)} on output drive, only "
                              f"{format_size(out_free)} free — the space gate will skip/abort it.")
            return spread_root

        if mode == "temp":
            if size and temp_free < image_need:
                _plog("WARN", f"Drive 'temp': {item.name} image needs ~{format_size(image_need)} on temp, "
                                  f"only {format_size(temp_free)} free — the space gate will handle it.")
            return temp_root
        if mode == "spread":
            set_spread()
            _plog("INFO", f"Drive 'spread': building on the output drive {out_dir}{contention}.")
            return spread_root
        # AUTO ---------------------------------------------------------------------
        # Fast-temp candidates (primary temp + any extra pool drives), each with free space.
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
            """App-owned scratch on pool drive *d*: the primary temp folder as configured;
            an EXTRA pool entry gets its own '_ffpfsc_temp' subfolder, so cleanup, the
            Spotlight marker and mkpfs scratch never land in the user's drive root."""
            d = Path(d)
            return d if d == temp_base_p else d / "_ffpfsc_temp"

        def _is_ssd(d):
            # Pure speed test for a pool drive (independent of the same-drive-rw policy):
            # used to keep the inner image / SSD↔SSD legs on actual flash, never a slow HDD
            # pool entry that merely happens to have the most free space.
            try:
                dt = drive_type_cached(Path(d))
                if dt == "Unknown":
                    dt = get_drive_type(Path(d))
                return dt == "SSD"
            except Exception:
                return False

        # 1) Everything on ONE fast drive (source + image + spool) — this is a same-drive
        #    read+write, so only consider drives where that's allowed (an SSD under 'auto');
        #    a big HDD pool entry must NOT win here just by having the most free space (that's
        #    the slow same-spindle case we avoid). Prefer an SSD, then most free.
        #    Skip this when the size is only an ESTIMATE (archive header unreadable): a
        #    well-compressing game can be far bigger than the on-disk guess, and putting the
        #    source on the SSD would then fill it so the inner image can't fit there and
        #    falls back to the slow output drive. With an estimate we reserve the SSD for
        #    the image and extract the source to the output drive (option 3) instead.
        fit_full = [(d, f) for d, f in pool if f >= full_need and self._same_drive_rw_allowed(d)]
        if size > 0 and fit_full and not size_is_estimate:
            ssd_full = [(d, f) for d, f in fit_full if _is_ssd(d)]
            best = max(ssd_full or fit_full, key=lambda t: t[1])[0]
            _set(_scratch(best), _scratch(best) / "_extracted", False, False)
            if _drive_cache_key(best) != _drive_cache_key(temp_base_p):
                _plog("INFO", f"Auto: {item.name} (~{szs}): whole scratch on pool drive {best}.")
            return item._build_root
        # 2) TWO fast drives (archives only): inner image on one, extracted source on
        #    ANOTHER — keeps pass 1 SSD↔SSD when no single fast drive holds image+source.
        #    Also skipped on an estimate (same reason as option 1 — don't commit the
        #    source to an SSD until the real size is known).
        if size > 0 and is_archive_pre and not size_is_estimate:
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
        # 3) SPLIT — inner image on a fast drive, source extracted to the OUTPUT drive; the
        #    backend routes the spool adaptively. Reads source off one drive while writing
        #    the image to the other, so neither HDD does a same-drive read+write.
        img_fit = [(d, f) for d, f in pool if f >= image_need]
        if size > 0 and img_fit and out_free >= out_split_need:
            ssd_img = [(d, f) for d, f in img_fit if _is_ssd(d)]
            img_dir = max(ssd_img or img_fit, key=lambda t: t[1])[0]  # prefer an SSD for the image
            _set(_scratch(img_dir), spread_root, True, False)   # source extracts to the big output drive
            _plog("INFO", f"Auto: {item.name} (~{szs}): 1) extract source → output drive "
                             f"({out_dir}); 2) build inner image → {temp_drive_label(img_dir)} "
                             f"({img_dir}); spool auto-routed. No same-drive read+write.")
            return spread_root
        # 4) Everything on the output drive (mechanical, slower, but it completes).
        if out_free >= out_full:
            set_spread()
            why = "too big for the temp drive(s)" if size > 0 else "unknown size — using the larger drive for safety"
            _plog("INFO", f"Auto: {item.name} (~{szs}) {why} → building on {out_dir}"
                             f"{contention or ' (mechanical drive, slower, but it completes)'}.")
            return spread_root
        # 5) Nothing fits — leave temp; the space gate aborts/skips with real numbers.
        _plog("WARN", f"Auto: {item.name} (~{szs}) fits neither the temp drive(s) (~{format_size(image_need)}) "
                          f"nor the output drive (~{format_size(out_full)}) — the space gate will skip/abort it.")
        return temp_root

    def _scratch_parent_roots(self, item=None) -> set:
        """Resolved folders under which THIS app creates its '_extracted' /
        '_ffpfsc_extract' scratch: the temp folder, every temp-pool entry (and its
        '_ffpfsc_temp' subfolder), the output folder(s) and the item's recorded build
        dirs. A folder the user happened to name '_extracted' anywhere else is not ours."""
        cands = []
        try:
            cands.append(self.temp_var.get().strip())
            cands.append(self.output_var.get().strip())
        except Exception:
            pass
        try:
            # Every configured pool entry, not only the device-deduplicated set the router
            # uses: a folder that was scratch under an earlier configuration stays ours.
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
        """The extract subdir THIS item was unpacked into (under a temp '_extracted' or
        an output-drive '_ffpfsc_extract' folder the app created), or None for a plain
        (non-extracted) source. Used so cleanup only ever removes this item's own data.
        The scratch folder's parent must be one of the app's own roots — a library the
        user named '_extracted' is never treated as throwaway."""
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

    def _reclaim_temp_base_inline(self, tp: str) -> None:
        """Remove our throwaway patch-extract dirs and, when the app-managed _ffpfsc_temp
        folder is empty, the folder itself — so no empty temp folder lingers after a job.
        Runs INLINE on the calling cleanup thread (after the heavy rmtree), so by the time
        it checks 'empty' this item's own scratch is already gone. *tp* is the temp path
        snapshotted on the MAIN thread (Tk vars aren't thread-safe). Only ever touches an
        _ffpfsc_temp dir we manage; a user's custom-named temp folder is left alone."""
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
        # Remove the app-managed temp base only when no more jobs are queued (the last
        # job is done) — mid-batch it's reused, so don't churn-delete it every item.
        try:
            if (not self.queue and base.name == "_ffpfsc_temp"
                    and base.is_dir() and not any(base.iterdir())):
                base.rmdir()
                self.log("INFO", "Removed the now-empty temp folder.")
        except Exception:
            pass

    def _cleanup_item_extract(self, item) -> None:
        """After an item finishes, remove ITS OWN extracted-source subdir (temp or output
        drive), then reclaim the temp base if it's now empty. Threaded (rmtree large)."""
        own = self._extract_dir_for_item(item)
        _tp = (self.temp_var.get() or "").strip()   # snapshot on the main thread
        def _work(d=own, tp=_tp):
            try:
                if d is not None and d.exists():
                    sz = get_folder_size(d)
                    shutil.rmtree(str(d), ignore_errors=True)
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
            # Always (even for folder/fake-sign jobs with no extracted dir): drop our
            # throwaway patch dirs and the now-empty temp base.
            self._reclaim_temp_base_inline(tp)
        self._run_cleanup(_work)

    def _free_source_after_pass1(self, item) -> None:
        """Mid-build hook (on the backend's [PASS1-DONE] marker): pass 1 has produced the
        inner image, so the extracted SOURCE is no longer needed (pass 2 reads only the inner
        image, and an OOM retry resumes from it). Free it NOW to halve peak temp. Only ever
        deletes a throwaway extract (under _extracted / _ffpfsc_extract); a user's own pack
        folder yields None from _extract_dir_for_item and is never touched."""
        if item is None or getattr(item, "_source_freed", False):
            return
        own = self._extract_dir_for_item(item)
        if own is None:
            return   # user's own folder — never delete
        item._source_freed = True
        def _work(d=own):
            try:
                if d.exists():
                    sz = get_folder_size(d)
                    shutil.rmtree(str(d), ignore_errors=True)
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
        """Remove the persisted pass-1 inner image (kept on the build drive so an OOM retry
        can resume pass-2 from it without rebuilding). Called on success and on terminal
        failure/cancel — NEVER between OOM retries (those resume from it). The backend also
        removes it on a clean pass-2 success; this is the GUI-side reaper for the failure /
        give-up / cancel paths where the backend left it behind."""
        if item is None:
            return
        inner = getattr(item, "_inner_image", None)
        if not inner:
            return
        try:
            item._inner_image = None
        except Exception:
            pass
        def _work(p=Path(str(inner))):
            try:
                if p.exists():
                    sz = p.stat().st_size if p.is_file() else get_folder_size(p)
                    try:
                        p.unlink()
                    except Exception:
                        shutil.rmtree(str(p), ignore_errors=True)
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
        """Ordered, de-duplicated password candidates for an extraction:
        1) the explicit single-field password (left panel / settings),
        2) a per-archive override on the queue item (if any),
        3) every entry in the global auto-tried list.
        Blank entries dropped; first occurrence wins."""
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

    # ── Extract archive when it reaches the front of the queue ───────────────
    def _extract_queued_item(self, item):
        """Extract item.archive_path in a background thread, then call start() again."""
        archive = item.archive_path
        extract_root = self._resolve_extract_root(item)
        # Keep Spotlight off the scratch: indexing the tens of thousands of files we're
        # about to extract competes for the SAME (often mechanical) drive we then read
        # them back from — a big throttle. Mark the extract + temp dirs no-index first.
        self._mark_no_spotlight(extract_root, getattr(item, "_build_temp", None))

        self._active_item = item   # so terminal handlers can clean THIS item if the queue changed
        item.status = "Extracting"
        self.cancel_requested = False
        self.extract_cancel_event.clear()
        self.update_queue_box()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        self.log("INFO", f"Extracting archive: {archive.name}")
        self._begin_job_progress(item)
        self.status_update("Extracting", f"Unpacking {archive.name}…  0%",
                            "Extracting", 0, 0, "00:00", "—", "—", job=item)

        _last_pct = [-1]
        _share = self._archive_extract_pct(item)
        _t0 = time.monotonic()
        # what the unpack writes: the size read from the archive's headers, else (an
        # encrypted header) the archive itself, which game data hardly shrinks
        _unpacked = int(getattr(item, "extracted_size", 0) or 0) or int(archive_set_ondisk_size(archive) or 0)

        def _progress(pct, filename):
            if pct - _last_pct[0] >= 1 or pct >= 100:
                _last_pct[0] = pct
                # Step bar shows the FULL extraction %; the queue bar (overall) counts the
                # unpack as its share of the whole job (see _archive_extract_pct).
                secs = time.monotonic() - _t0
                speed, eta = unpack_rate(pct, secs, _unpacked)
                self.status_update("Extracting",
                                    f"Unpacking {archive.name}…  {pct}%",
                                    "Extracting", pct, pct * _share / 100.0,
                                    format_duration(secs), speed, eta, job=item)

        candidate_passwords = self._candidate_passwords(item)  # includes per-archive override

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
                if getattr(item, "content_kind", "") == ORGANIZE_TARGET:
                    # The whole extraction goes into the library, each item in its own format.
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
                    # A package is unpacked through the chain; an older pack job keeps its
                    # format, an fPKG job builds a .pkg again.
                    item.chain_to = ("pkg" if getattr(item, "operation", "") == "fpkg-build" else
                                     "ffpfs" if getattr(item, "output_compressed", True) is False else "ffpfsc")
                    item.operation = "chain"
                payload_items = [self._item_from_payload_path(kind, path) for path in paths]
                primary = payload_items[0]
                item.origin_archive = str(archive)
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
                # (Extra payload items of a multi-game archive queued as an fPKG job are
                # converted in the _extract_q "ok" handler — on the main thread, since the
                # conversion reads Tk variables.)
                # Mark this as an extracted-archive item so the pack worker compresses its
                # overall progress into the tail after the extraction slice (monotonic
                # whole-game %). Underscore attr → not persisted in the saved queue.
                item._from_archive = True
                # Carry extra subfolders/files that sit BESIDE the game in the archive
                # (e.g. an '[ ALL DLC ]' wrapper) so they're copied next to the .ffpfsc.
                # Skipped when the archive root IS the game (no wrapper → no siblings).
                if kind == "game" and not (len(paths) == 1
                        and Path(paths[0]).resolve() == Path(extracted_root).resolve()):
                    try:
                        item.bundle_siblings = self._sibling_extras(Path(primary.path).parent, paths)
                    except Exception:
                        item.bundle_siblings = []
                self._extract_q.put(("ok", (item, payload_items[1:])))
            except ArchiveExtractionCancelled as exc:
                self.log("WARN", str(exc))
                self._extract_q.put(("cancelled", str(exc)))
            except Exception as exc:
                self.log("ERROR", f"Extraction failed: {exc}")
                self._extract_q.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # ── Patch jobs: resolve archive game / .7z patch before the job runs ───────
    def _patch_needs_prepare(self, item) -> bool:
        """True when a queued patch job still has an ARCHIVE game (.zip/.rar/.7z) or a
        .7z patch that must be extracted to a folder before packing. After prepare the
        game path is a folder, so this naturally returns False on re-entry. (.zip/.rar
        patches are NOT pre-extracted — the backend --patch handles those itself.)"""
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
        self.status_update("Failed", f"Patch prepare failed: {msg}", "Failed", 0, 0, "00:00", "—", "—")
        # Keep the failed item in the queue (marked Failed, moved to the end) — only
        # successful items disappear.
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
        """Extract a patch job's archive game and/or .7z patch to temp folders, update the
        item to point at the extracted folders, then re-enter the launcher. Runs on a
        background thread (extraction is slow); Tk touches are marshalled to the main loop."""
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
                    self.status_update("Extracting", f"Unpacking game: {game.name}…",
                                       "Extracting", 0, 0, "—", "—", "—")
                    gp = ArchiveExtractor.extract_with_passwords(
                        game, Path(temp_base) / "_patch_game", pw,
                        log_fn=self.log, cancel_event=self.extract_cancel_event)
                    inplace = True   # extracted to a throwaway temp → overlay in place
                pp = patch
                if patch is not None and patch.suffix.lower() == ".7z":
                    self.log("INFO", f"Extracting .7z patch: {patch.name}")
                    self.status_update("Extracting", f"Unpacking patch: {patch.name}…",
                                       "Extracting", 0, 0, "—", "—", "—")
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

    # ── Listbox keyboard reorder ──────────────────────────────────────────────
    # ── Queue selection helper ─────────────────────────────────────────────────
    def _queue_sel_idx(self) -> int | None:
        """Return the currently selected listbox index, or None."""
        sel = self.queue_listbox.curselection()
        return int(sel[0]) if sel else None

    def _on_queue_select(self, _event=None):
        """When a row is clicked, update the game details panel."""
        idx = self._queue_sel_idx()
        if idx is not None and idx < len(self.queue):
            self.update_game_details(self.queue[idx])

    def _resolve_archive_password(self, item) -> None:
        """Prompt for an archive item's password when no saved candidate unlocks its
        header (extracted_size is still 0 after from_archive's auto-probe). On success:
        store the password ON the item (per-job override), persist it to settings (so
        later archives unlock without asking again), AND fill in extracted_size — that
        last bit is what the auto routing needs to place the build on the SSD instead
        of falling back to the on-disk-size estimate. Skip leaves the item unchanged."""
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
            # A saved password (or none) opened the header; only the size looked odd, so the
            # drive routing estimates it. Asking for a password would be wrong here.
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
                return   # user skipped
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
                self.log("OK", f"Unlocked '{arc.name}' — " + (
                    f"size {format_size(item.extracted_size)} read for routing." if item.extracted_size
                    else "its size could not be trusted; the drive routing estimates it."))
                return
            self.log("WARN", f"Password did not unlock '{arc.name}'.")
            if not messagebox.askyesno(
                "Wrong password",
                f"That password did not unlock '{arc.name}'.\n\nTry again?",
                parent=self.root):
                return
        self.log("WARN", f"Gave up on the password for '{arc.name}' — routing will use the size estimate.")

    def _log_archive_problem(self, item) -> None:
        arc = getattr(item, "archive_path", None)
        self.log("WARN", f"'{Path(str(arc)).name if arc else item.name}' cannot be read: {item.archive_problem}. "
                         f"No password helps here; get the damaged or missing part again, or remove the job.")

    def _on_queue_double_click(self, event=None):
        """Double-click a queue row → open the matching submenu dialog pre-filled with
        this item's settings, so the user can change source / output / format etc. The
        item that is currently RUNNING and any TERMINAL item
        (Done/Failed/Skipped/Cancelled) are not edited — those just refresh details."""
        # Resolve which row was clicked. event.y gives the precise row (nearest()) even
        # if the listbox selection has not yet caught up to the click.
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
        """Open the editor on *item*. Not the running job (its paths are in use) and not a
        finished one; a failed or cancelled job points at a source that exists first."""
        if item not in self.queue:
            return
        idx = self.queue.index(item)
        # Block edit on the running job — its paths are in use.
        if item is self._running_item():
            self.log("WARN", "Cannot edit the currently running job.")
            return
        status = (getattr(item, "status", "") or "").lower()
        if status in ("failed", "skipped", "cancelled", "done"):
            # the extracted copy may be gone: the editor then shows the archive it came from
            self._rearm_from_archive(item)

        # One editor for every kind of job: it shows the job as source → changes → output,
        # and saving replaces the job at its place in the queue.
        try:
            JobDialog(self, item=item)
        except Exception as e:
            self.log("ERROR", f"Could not open the editor: {e}")


    # ── fPKG jobs — the Pack dialog's '.pkg' format ─────────────────────────────
    # A queued fPKG build is a GameItem with operation "fpkg-build" plus fpkg_* fields.
    # Any classified pack item (folder, archive placeholder, disk image, .ffpfs/.ffpfsc)
    # can be turned into one; the backend resolves the source (extract / unwrap) and reads
    # the identity from sce_sys/param.json, so the fields here are fallbacks only.
    FPKG_FILE_SOURCES = (".zip", ".rar", ".7z", ".exfat", ".ffpkg", ".ffpfs", ".ffpfsc")
    _TITLE_ID_RE = re.compile(r"^[A-Z]{4}[0-9]{5}$")

    def _fpkg_params_of(self, item) -> dict:
        """The fPKG parameters carried by *item* (an fpkg-build job), as one dict."""
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

    def _as_chain_job(self, extra, tpl) -> None:
        """Give another game from a multi-game archive the job of the first one: output
        format, folder, sign and backport. A patch belongs to one game and is not copied;
        for a .pkg only the compression settings are, the identity comes from each game."""
        extra.operation = "chain"
        extra.chain_to = getattr(tpl, "chain_to", None) or "ffpfsc"
        extra.chain_sign = bool(getattr(tpl, "chain_sign", False))
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
        """Only the build-option part of an fPKG job's parameters (inner / backend / level /
        dll / retail switches) — what sibling jobs made from the same source share. Identity
        is never shared: it is per game and read from each game's param.json at build time."""
        p = self._fpkg_params_of(item)
        return {k: p[k] for k in ("inner", "backend", "level", "dll",
                                  "retail_normalize", "hdr_flag", "regen_playgo", "fake_sign")}

    def _apply_fpkg_params(self, item, params: dict) -> None:
        """Write a params dict (see _fpkg_params_of) onto *item*."""
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
            # Cannot work here (LibProsperoPkg: "requires 64-bit Windows", hard failure) —
            # a restored/old job asking for it is built with the encoder that does work.
            try:
                self.log("WARN", f"{getattr(item, 'display_name', None) or item.name}: the Publishing Tools backend "
                                 f"is Windows-only — building with the built-in Kraken encoder instead.")
            except Exception:
                pass
            item.fpkg_kraken_backend = "builtin"
        if item.fpkg_kraken_backend == "publishingtools" and not dll:
            dll = (self.pubtools_dll_var.get() or "").strip()
        item.fpkg_pubtools_dll = dll if item.fpkg_kraken_backend == "publishingtools" else ""
        # Show a real title id in the queue row when the source name didn't reveal one.
        if item.fpkg_title_id and not self._TITLE_ID_RE.match(str(getattr(item, "title_id", "") or "")):
            item.title_id = item.fpkg_title_id

    def _as_fpkg_job(self, item, params: dict, identity: dict | None = None):
        """Turn a freshly classified PACK item into an fPKG build job IN PLACE, keeping its
        size / files / artwork / bundle tags. *identity* (content_id/title_id/title/version)
        overrides the corresponding keys of *params* when given."""
        p = dict(params)
        if identity:
            p.update({k: v for k, v in identity.items() if v})
        item.operation = "fpkg-build"
        item.output_compressed = True          # not meaningful for .pkg; marks the snapshot as taken
        self._apply_fpkg_params(item, p)
        # The fPKG builder has no patch overlay: a patch the folder scan detected next to
        # the game would be silently left out — say so, and drop it so the space gate
        # doesn't size for a patch job either.
        ps = getattr(item, "patch_source", None)
        if ps:
            try:
                self.log("WARN", f"{getattr(item, 'display_name', None) or item.name}: the detected patch "
                                 f"'{Path(str(ps)).name}' is NOT integrated into an fPKG build — the .pkg is built "
                                 f"from the base game only. Patch it separately.")
            except Exception:
                pass
            item.patch_source = None
        # Space gate: a compressed .ffpfsc is unwrapped to its full tree before the build.
        # Measured .ffpfsc/unpacked ratio averages ~0.59 (see COMPRESSED_OUTPUT_RATIO), so
        # the tree is ~1.7x the image; reserve 2x to keep headroom.
        try:
            _p = Path(str(getattr(item, "path", "") or ""))
            if _p.is_file() and _p.suffix.lower() == ".ffpfsc":
                item.extracted_size = int((getattr(item, "size", 0) or 0) * 2)
        except Exception:
            pass
        return item

    def _take_pending_fpkg_identity(self, item) -> dict | None:
        """The identity the Pack dialog pre-filled for ONE game folder, handed to exactly
        the queue item made from that folder (matched by resolved path), then dropped."""
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
        """A fresh fPKG build job for a single source path: a packed image (.ffpfsc / .ffpfs /
        .exfat / .ffpkg — unwrapped by the backend), an archive (extracted on its turn) or a
        folder. Returns None (after an error dialog) for anything else."""
        src = Path(src)
        suf = src.suffix.lower() if src.is_file() else ""
        try:
            if src.is_file() and suf in (".zip", ".rar", ".7z"):
                base = GameItem.from_archive(src)
            elif src.is_file() and suf in (".ffpfsc", ".ffpfs", ".exfat", ".ffpkg"):
                base = GameItem.from_exfat(src)      # single-file image item; the backend unwraps it
            elif src.is_dir():
                base = GameItem(src)
            else:
                base = None
        except Exception as e:
            messagebox.showerror("Build failed", f"Could not classify the source:\n{e}",
                                 parent=parent or self.root)
            return None
        if base is None:
            messagebox.showerror("Unsupported source",
                                 f"Not a valid fPKG source:\n{src}\n\nUse a game folder, an archive, "
                                 "or a .ffpfsc / .ffpfs / .exfat / .ffpkg image.",
                                 parent=parent or self.root)
            return None
        self._as_fpkg_job(base, params)
        base.output_path = Path(output_path) if output_path else None
        return base

    def _copy_item_for(self, src, *, output_path=None, mode="organize",
                       auto_organize=None, parent=None):
        """A fresh COPY job for a source whose format already matches the target format
        (`.ffpfsc` / `.ffpfs` / `.pkg`). *mode* is copy_job's: "organize" (the Organize
        default) renames on the same drive and copies across drives, keeping the source
        there; "keep" always copies; "move" also deletes after a cross-drive copy.
        Returns None (after an error dialog) for anything else."""
        src = Path(src)
        if not (src.is_file() and src.suffix.lower() in (".ffpfsc", ".ffpfs", ".pkg")):
            messagebox.showerror("Wrong type",
                                 f"Copy needs a .ffpfsc, .ffpfs or .pkg file:\n{src}",
                                 parent=parent or self.root)
            return None
        try:
            base = GameItem.from_exfat(src)   # single-file image; we override operation below
        except Exception as e:
            messagebox.showerror("Copy failed", f"Could not classify the source:\n{e}",
                                 parent=parent or self.root)
            return None
        base.operation = "copy"
        base.copy_mode = mode
        base.output_compressed = (src.suffix.lower() == ".ffpfsc")   # marks the snapshot as taken
        base.output_path = Path(output_path) if output_path else None
        if auto_organize is not None:
            base.auto_organize = bool(auto_organize)
        # Show the file's stem in the queue row (from_exfat already does that, but keep
        # explicit so a restored queue can't accidentally regress).
        base.name = src.stem
        return base

    @staticmethod
    def _ps4_copy_mode(item) -> str:
        """How a PS4 job transports its packages: the app's own extraction of an archive is
        moved (its archive gets the After-job rule); your own packages are copied and stay
        where they are unless the job's After-job rule is Delete (then moved); Trash and
        Move to folder act on them once the job is Done."""
        if getattr(item, "origin_archive", None) or getattr(item, "_from_archive", False):
            return "move"
        return "move" if (getattr(item, "after_source", None) or "keep") == "delete" else "keep"

    @staticmethod
    def _is_archive_path(p: Path) -> bool:
        n = p.name.lower()
        return p.is_file() and (p.suffix.lower() in (".zip", ".rar", ".7z") or bool(re.search(r"\.r\d{2,}$", n)))

    def _organize_item_for(self, src, *, output_path=None):
        """A copy job that writes *src* (a game folder, an image, a package, an archive or a
        folder of them) into the library in its own format: '<Title> [ID] [vX]/…' under the
        output, see backend/organize_lib.organize_into. Tk-free."""
        item = GameItem.from_chain(Path(src), to="ffpfsc", output_path=output_path)
        item.operation = "copy"
        item.content_kind = ORGANIZE_TARGET
        item.chain_to = ORGANIZE_TARGET
        item.copy_mode = "move" if getattr(item, "archive_path", None) else "keep"
        return item

    def _ps4_archive_item(self, archive, info: dict, *, output_path=None):
        """A sorting job for an archive of PS4 packages (see _ps4_item_for), named from the
        first package's param.sfo when the archive allows reading it. Tk-free."""
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

    def _ps4_item_for(self, src, *, output_path=None, info=None):
        """A copy job that sorts PS4 packages into the library: one .pkg, or a folder of
        them (an archive's extraction, a download folder), or an archive of them. The
        backend names and places each package ('<Title> [CUSA…] [vX]/…'); see
        backend/ps4_sort.py."""
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
        return item

    # ── Auto-organize: where a job lands and what it is called ───────────────
    def _auto_organize_on(self, item) -> bool:
        v = getattr(item, "auto_organize", None)
        return bool(self.auto_organize_var.get()) if v is None else bool(v)

    def _game_identity(self, item) -> dict | None:
        """Title / title id / version of the GAME behind *item*, read from the source itself:
        a folder's sce_sys/param.json, or the param.json inside a .ffpfs/.ffpfsc/.exfat/
        .ffpkg image (MkPFS's game_metadata reads just that file — no unpacking). Cached on
        the item (transient) so an OOM resume, whose source is gone by then, still names the
        output identically. None when nothing usable is readable — an archive before its
        extraction, or a source without param.json."""
        cached = getattr(item, "_identity", None)
        if cached:
            return cached
        if getattr(item, "archive_path", None) and not getattr(item, "path", None):
            return self._archive_identity(item)
        p = getattr(item, "path", None)
        if not p:
            return None
        p = Path(str(p))
        # A source without readable metadata stays that way until it changes: remember the
        # miss (keyed by path + mtime) instead of re-reading — for an image that means one
        # backend process per preview refresh on the UI thread.
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
            # Fill gaps from the source name (a '[PPSA…]' / version in a release name).
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
        """The game behind an archive job before it is extracted, from the param.json read
        out of the archive alone (ZIP, RAR and 7z that are not solid). Kept on the job
        (archive_title / _id / _version, saved with the queue); not the full identity: the
        firmware tag needs the executable, so the extracted game is read again later and
        this is never cached as _identity. *passwords* for a call off the main thread."""
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
        """Name every job after its game as soon as the game can be read: a folder's or an
        image's param.json, a .pkg's, or the one inside a ZIP / RAR / 7z that is not solid.
        A release name such as '[site]-PPSA12345.part01' says little. Runs off the main
        thread (an image or a .pkg is read by a helper process); the passwords are
        collected here first, because they come from a Tk field."""
        todo = [it for it in self.queue if not getattr(it, "_name_probed", False)]
        if not todo:
            return
        for it in todo:
            it._name_probed = True
        pw = {id(it): self._candidate_passwords(it) for it in todo
              if getattr(it, "archive_path", None) and not getattr(it, "path", None)}

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
                                     else ident_from_folder_name(it.archive_path))
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

    def _job_art(self, item):
        """The cover to show for *item*: its cached copy, else the icon in its folder (cached
        on the way, so it stays when the folder goes)."""
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
        """Read *item*'s icon0.png from its source and cache it: a folder, an archive read
        alone (ZIP, RAR and 7z that are not solid), a .ffpfs/.ffpfsc or a .pkg (only that
        one entry is decoded). Runs off the main thread. True when a cover was added."""
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
        """sce_sys/icon0.png out of a .ffpfs/.ffpfsc or a PS5/PS4 .pkg, or None."""
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
                               capture_output=True, text=True, timeout=300)
            f = dest / "sce_sys" / "icon0.png"
            return f.read_bytes() if f.is_file() else None
        except Exception:
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _ps4_icon_from_archive(archive: Path, member: str, passwords):
        """A PS4 package's icon0.png read from the start of the package inside *archive*."""
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
        """The job's label becomes the game's title (without ™ ®) and its title id:
        'Example Quest [PPSA01234]'. True when it changed."""
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
        """{'title','title_id','version'} from a sce_sys/param.json (Sony layout)."""
        try:
            return ident_from_param_bytes(Path(pj).read_bytes())
        except OSError:
            return None
        try:
            d = json.loads(Path(pj).read_text(encoding="utf-8-sig", errors="replace"))
        except Exception:
            return None
        if not isinstance(d, dict):
            return None
        title = ""
        lp = d.get("localizedParameters") or {}
        if isinstance(lp, dict):
            lang = lp.get("defaultLanguage")
            block = lp.get(lang) if isinstance(lang, str) else None
            if isinstance(block, dict):
                title = str(block.get("titleName") or "")
            if not title:
                for v in lp.values():
                    if isinstance(v, dict) and v.get("titleName"):
                        title = str(v["titleName"]); break
        if not title:
            title = str(d.get("titleName") or "")
        tid = str(d.get("titleId") or "").strip().upper()
        ver = str(d.get("contentVersion") or d.get("masterVersion") or "").strip()
        if not (title or tid):
            return None
        return {"title": title.strip(), "title_id": tid, "version": ver}

    def _read_image_metadata(self, p: Path) -> dict | None:
        """param.json-derived identity of a packed image, without unpacking it.

        .ffpfs / .ffpfsc: our images nest an inner PFS inside an outer one, which MkPFS's
        generic metadata reader does not follow (it reports 'missing exFAT signature') —
        so ask the backend's browse path for the single member sce_sys/param.json, exactly
        like the PFS browser does (only the touched blocks are decompressed).
        .pkg: read sce_sys/param.json out of the CNT via ffpfsc-pkg-tool's selective
        extract (touches only the CNT entry — no full decompression).
        .exfat / .ffpkg: the vendored MkPFS exFAT reader."""
        suf = p.suffix.lower()
        if suf in (".ffpfs", ".ffpfsc"):
            tmp = Path(tempfile.mkdtemp(prefix="ffpfsc_ident_"))
            try:
                mfile = tmp / "members.txt"
                mfile.write_text("sce_sys/param.json\n", encoding="utf-8")
                dest = tmp / "out"
                cmd = self._backend_cmd("--extract-from", str(p), "--dest", str(dest), "--members-file", str(mfile))
                subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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
                # Route through the backend's fpkg module so a frozen build finds the tool.
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
        title = (getattr(m, "game_title", "") or "").strip()
        tid = (getattr(m, "title_id", "") or "").strip()
        ver = (getattr(m, "version", "") or "").strip()
        if tid in ("-", "—", "Unknown"):
            tid = ""
        if not (title or tid):
            return None
        return {"title": title, "title_id": tid.upper(), "version": ver}

    def _ps4_identity(self, path):
        """{title, title_id, version} of a PS4 package, or None."""
        try:
            i = _ps4pkg_module().read_identity(path)
            return {"title": i.title, "title_id": i.title_id, "version": i.version}
        except Exception:
            return None

    def _source_fw(self, p: Path) -> str:
        """The firmware the game in *p* needs, from its eboot.bin's SDK version: read in
        place for a folder, through the backend for a .ffpfs/.ffpfsc/.pkg (headers only).
        '' when unreadable (an encrypted eboot, a disk image)."""
        try:
            if _is_ps4_source(p):
                return ""                       # PS5 firmware tags do not apply to PS4 content
            if p.is_dir():
                bp = _backport_module()
                words = bp.sdk_words_of_file(p / "eboot.bin")
                return bp.sdk_firmware(words[0]) if words else ""
            if p.suffix.lower() in (".ffpfs", ".ffpfsc", ".pkg"):
                r = subprocess.run(self._backend_cmd("--sdk-of", str(p)), capture_output=True, text=True,
                                   errors="replace", timeout=120)
                for line in (r.stdout or "").splitlines():
                    if line.startswith("SDK_JSON: "):
                        return str(json.loads(line[len("SDK_JSON: "):]).get("fw") or "")
        except Exception:
            pass
        return ""

    @staticmethod
    def _param_bytes_of(path: Path, passwords) -> bytes | None:
        """The game's param.json from a folder (the shallowest one) or a ZIP / RAR read alone."""
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
        """("refuse" | "warn" | "ok", why) for integrating *patch* into the game at *source*:
        another title id is refused; a backport (a lower SDK than the game's) made for
        another version is a warning. "ok" when either param.json cannot be read cheaply;
        the backend checks again when the job runs."""
        pw = [p.strip() for p in (load_settings().get("archive_passwords") or []) if str(p).strip()]
        try:
            g = json.loads(self._param_bytes_of(source, pw) or b"{}")
            pt = json.loads(self._param_bytes_of(patch, pw) or b"{}")
        except Exception:
            return "ok", ""
        gtid, ptid = str(g.get("titleId") or "").upper(), str(pt.get("titleId") or "").upper()
        if gtid and ptid and gtid != ptid:
            return "refuse", f"This patch is for {ptid}, but the game is {gtid}."

        def num(v):
            try:
                t = str(v).strip()
                return int(t, 16) if t.lower().startswith("0x") else int(t)
            except Exception:
                return None
        gsdk, psdk = num(g.get("sdkVersion")), num(pt.get("sdkVersion"))
        gver, pver = str(g.get("contentVersion") or ""), str(pt.get("contentVersion") or "")
        if gsdk and psdk and psdk < gsdk and gver and pver and gver != pver:
            return "warn", (f"This patch is a backport made for version {pver}, but the game is version {gver}. "
                            f"A backport usually works only with the version it was made for.")
        return "ok", ""

    def _patch_fw(self, patch: Path) -> str | None:
        """The firmware a patch's own eboot.bin needs. None when the patch carries no
        eboot.bin (the game keeps its own), '' when it has one this cannot read."""
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
        """The firmware the job's OUTPUT needs: the source's, unless an integrated patch
        brings its own eboot.bin, lowered to the target by a backport. '' (no tag) when
        it cannot be known — never a guess."""
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
        """(base_dir, file_name) for the auto-organize layout under *out*, or (None, None)
        when no identity is readable — the caller then keeps today's naming (logged once)."""
        ident = self._game_identity(item)
        if not ident:
            if not getattr(item, "_organize_warned", False):
                try:
                    item._organize_warned = True
                except Exception:
                    pass
                self.log("WARN", f"Auto-organize: no param.json readable for {getattr(item, 'display_name', None) or item.name} "
                                 f"— naming from the source name instead.")
            return None, None
        ident = dict(ident, fw=self._job_fw(item, ident.get("fw", "")))
        folder, fname = organized_names(ident, ext, item)
        return out / folder, fname

    _FW_TAG = re.compile(r"\s*\[fw[\d.x]+\]$", re.I)
    # Settings › Interface: skip (default, no question) | ask | overwrite | keep (both)
    OUTPUT_EXISTS_CHOICES = ("skip", "ask", "overwrite", "keep")
    OUTPUT_EXISTS_LABELS = {"skip": "Skip", "ask": "Ask", "overwrite": "Overwrite", "keep": "Keep both"}

    def _predicted_output(self, item) -> Path | None:
        """The file this job will write, named the way build_command names it, or None when
        that cannot be known before the job runs: an archive whose game cannot be read yet
        (its name then comes from the extracted game), a .pkg without auto-organize (the
        package tool names it), a folder output, a PS4 set (the backend names every package)."""
        if getattr(item, "content_kind", "") in ("ps4", ORGANIZE_TARGET):
            return None
        op = getattr(item, "operation", "pack")
        if op == "chain":
            to = getattr(item, "chain_to", None) or "ffpfsc"
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
        if to not in ("ffpfs", "ffpfsc", "pkg"):
            return None
        out = self._job_output_dir(item)
        if out is None:
            return None
        if out.suffix.lower() in (".ffpfs", ".ffpfsc", ".pkg"):
            return out                                   # an explicit file
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
        """The firmware a backport job lowers to ("7.61", …), else None."""
        bt = getattr(item, "backport_target", None) if item is not None else None
        if not is_backport_target(bt):
            return None
        try:
            bp = _backport_module()
            return bp.sdk_firmware(bp.SDK_TARGETS["10.xx"][0]) if bt == "10.xx" else str(bt)
        except Exception:
            return None

    def _existing_output(self, target: Path, item=None) -> Path | None:
        """The file in the output folder that is this job's output already: same title,
        version and format. When the job knows the firmware its output needs, the name says
        so ([fwN.NN]) and only that file, or one from before firmware tags existed, counts.
        When it does not know it yet (an archive before it is unpacked), the tag does not
        count, except that a backport job wants a file at or below its target."""
        try:
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
        """Settings › Interface › When the output is already there: skip | ask | overwrite | keep."""
        try:
            v = self.output_exists_var.get()
        except Exception:
            v = "skip"
        return v if v in self.OUTPUT_EXISTS_CHOICES else "skip"

    def _check_existing_outputs(self) -> bool:
        """Before a fresh start. Each job checks its own output when it is due (before its
        archive is unpacked when the game is readable, else right after). With Ask, one
        window here lists the outputs already known to be there, and its answer is the rule
        for this run. False on Cancel."""
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
        """overwrite | skip | cancel, asked in a window of its own."""
        dlg = OutputExistsDialog(self.root, [(getattr(it, "display_name", None) or it.name, hit)
                                             for it, hit in conflicts])
        try:
            self.root.wait_window(dlg)
        except Exception:
            return "cancel"
        return dlg.choice or "cancel"

    def _apply_output_choice(self, item, hit: Path, choice: str) -> None:
        name = getattr(item, "display_name", None) or item.name
        if choice == "overwrite":
            item._replace_output = True
            self.log("INFO", f"{name}: replaces {hit}.")
        elif choice == "keep":
            item._keep_both = True
            self.log("INFO", f"{name}: {hit.name} is already there; the new build gets a numbered name beside it.")


    def _late_output_check(self, item) -> str:
        """Right before a job's backend starts (an archive has been unpacked by now): its
        output, if the start could not know it. proceed | skip | cancel; the choice made at
        the start applies, else the user is asked for this job."""
        if (getattr(item, "_output_checked", False) or getattr(item, "_replace_output", False)
                or getattr(item, "_keep_both", False)):
            return "proceed"
        try:
            target = self._predicted_output(item)
        except Exception:
            target = None
        if target is None:
            return "proceed"                     # known once the archive is unpacked: asked again then
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
        """*path* with ' (2)', ' (3)', … after the title, the first one that is free and
        still within the ShadowMount name limit (the title is shortened when needed):
        'Title (2) [PPSA01234] [v01.000] [fw10.00].ffpfsc'."""
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
        """Keep both: the new build of a job set to Keep both goes beside the old file."""
        if getattr(item, "_keep_both", False) and out.suffix.lower() in (".ffpfs", ".ffpfsc") and out.exists():
            new = self._free_output_name(out)
            self.log("INFO", f"Keep both: {out.name} is already there, the new build is {new.name}.")
            return new
        return out

    def _skip_late(self, item, hit_note: str = "") -> None:
        """A running batch skips *item* after its archive was unpacked: it leaves this run
        (not counted as a failure), the scratch it wrote is reclaimed, the batch goes on."""
        self._cleanup_after_failure(item)
        hit = None
        try:
            predicted = self._predicted_output(item)
            hit = self._existing_output(predicted, item) if predicted is not None else None
        except Exception:
            pass

        def finish(done: bool):
            if self._batch_running:
                if done:
                    self._batch_done += 1
                else:
                    self._batch_total = max(0, self._batch_total - 1)
            self._skip_late_advance()
        self._settle_existing(item, hit, hit_note, then=finish)

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
        """A bundle mirrors its source folder name at the destination — unless that would be
        the source folder ITSELF (output folder == the source's parent), which would drop
        the result next to the archive it came from. Then the output folder is used as is."""
        target = out / sanitize_filename(sub)
        try:
            src_dir = getattr(item, "bundle_dir", None)
            if src_dir and target.resolve() == Path(str(src_dir)).resolve():
                if not getattr(item, "_mirror_warned", False):
                    item._mirror_warned = True
                    self.log("INFO", f"Output folder is the source's parent — not mirroring '{sub}' into the "
                                     f"source folder itself; writing to {out}.")
                return out
        except Exception:
            pass
        return target

    def _finalize_pkg_name(self, item, pkg_path: Path) -> Path:
        """Auto-organize for fPKG: the tool names its output '<content-id>-A…-V….pkg'; give it
        the library name decided in build_command. Returns the final path."""
        want = getattr(item, "_organized_pkg_name", None)
        pkg_path = Path(pkg_path)
        if not want or pkg_path.name == want:
            return pkg_path
        target = pkg_path.with_name(want)
        try:
            if target.exists() and getattr(item, "_replace_output", False):
                # the user chose Overwrite at the start: the new build takes its place
                os.replace(pkg_path, target)
                self.log("INFO", f"Auto-organize: {want} replaced with the new build.")
                return target
            if target.exists():
                # Never replace a file that is already there (a second build of the same
                # title with other settings is the common case) — keep both.
                stem, ext = os.path.splitext(want)
                n = 2
                while target.exists():
                    target = pkg_path.with_name(f"{stem} ({n}){ext}")
                    n += 1
                self.log("WARN", f"Auto-organize: {want} already exists — keeping both, "
                                 f"the new build is named {target.name}")
            pkg_path.rename(target)
            self.log("INFO", f"Auto-organize: renamed {pkg_path.name} → {target.name}")
            return target
        except Exception as e:
            self.log("WARN", f"Auto-organize: could not rename {pkg_path.name} → {want}: {e}")
            return pkg_path

    # ── Queue management ──────────────────────────────────────────────────────
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
        """Move the job at *src* so it stands before the job now at *dst* (len = to the end):
        drag and drop, Move up / down, Run next. The list order is the order the jobs that
        have not run yet are worked through; the running job may move too."""
        if not (0 <= src < len(self.queue)):
            return
        dst = max(0, min(len(self.queue), dst))
        if dst in (src, src + 1):
            return
        item = self.queue.pop(src)
        self.queue.insert(dst - 1 if dst > src else dst, item)
        self.update_queue_box(select_item=item)

    def run_job_next(self, item) -> None:
        """Run next: the job moves above the first job that is still waiting."""
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
        """The selected jobs, top to bottom (the one in focus alone when nothing else is)."""
        try:
            rows = self.queue_listbox.marked_rows()
        except Exception:
            return []
        return [self.queue[i] for i in rows if 0 <= i < len(self.queue)]

    def queue_remove_selected(self):
        """Remove every selected job. The running job stays (cancel it first); removing a job
        deletes the copy it kept from its archive, so several of those are asked about."""
        rows = [i for i in self.queue_listbox.marked_rows() if 0 <= i < len(self.queue)]
        if not rows or not self.queue:
            return  # nothing selectable (e.g. the "Queue is empty" placeholder row)
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
        if len(items) > 1 and kept:
            n = len(kept)
            if not messagebox.askyesno(
                    "Remove jobs", f"{n} of these job{'s' if n != 1 else ''} keep{'' if n != 1 else 's'} what "
                                   f"{'they' if n != 1 else 'it'} extracted from {'their' if n != 1 else 'its'} "
                                   f"archive{'s' if n != 1 else ''}, for a retry. Removing deletes "
                                   f"{'those copies' if n != 1 else 'that copy'}. Remove anyway?"):
                return
        # What to show afterwards: the first job below the removed ones, else the one above.
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
        """Legacy helper — removes the first (non-running) queue entry."""
        if self.queue and not self._batch_running:
            self.queue.pop(0)
        self.update_queue_box()

    def clear_queue(self):
        """Every job but the running one (the same as Clear all, without the kept-copy question
        for callers that already asked)."""
        if self._batch_running:
            ok = messagebox.askyesno("Clear Queue",
                                      "A compression is running. Clear the waiting games?\n"
                                      "(The current game will finish normally.)")
            if not ok:
                return
            run = self._running_item()
            for it in self.queue:
                if it is not run:
                    self._drop_kept_extract(it)
            self.queue[:] = [run] if run is not None else []   # keep the running job, clear the rest
        else:
            for it in self.queue:
                self._drop_kept_extract(it)
            self.queue.clear()
        self._queue_missing_saved = []   # a deliberate clear also drops parked entries
        self.update_queue_box()

    # GameItem fields that hold a Path (everything else is str/int/None and JSON-safe).
    _QUEUE_PATH_FIELDS = ("path", "archive_path", "bundle_dir", "patch_source", "output_path")

    def _save_queue(self):
        """Persist the current queue (paths + metadata, minus the PIL artwork and the
        transient _-prefixed placement state) so it survives a restart/crash. Gated until
        _restore_queue runs so the initial empty render can't clobber the saved queue.
        Best-effort — never breaks the UI rebuild."""
        if not getattr(self, "_queue_restored", False):
            return
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
                    # anything else (unexpected) is dropped rather than risk a crash
                items.append(d)
            items += list(getattr(self, "_queue_missing_saved", None) or [])
            save_settings({"queue": items})
        except Exception:
            pass

    def _restore_queue(self):
        """Rebuild the saved queue on startup. Items whose source path no longer exists
        are skipped; a mid-run status is reset to pending. Best-effort. Enables saving
        afterwards (sets _queue_restored)."""
        saved = []
        missing = []   # saved entries whose source is not reachable right now
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
                # An archive job whose extracted copy was cleaned up restarts at its archive.
                _src = getattr(obj, "path", None)
                _org = getattr(obj, "origin_archive", None)
                if (_org and not getattr(obj, "archive_path", None) and _src
                        and not Path(str(_src)).exists() and Path(_org).exists()):
                    obj.archive_path, obj.path = Path(_org), None
                    obj.source_kind, obj.files, obj.bundle_siblings = "archive", 0, []
                    obj.extracted_size = int(getattr(obj, "origin_extracted_size", 0) or 0)
                    try:
                        obj.size = archive_set_ondisk_size(Path(_org))
                    except Exception:
                        pass
                    if getattr(obj, "status", "") in ("Failed", "Cancelled", "Skipped"):
                        obj.status = "Pending Extract"
                # The source must still be on disk to be packable/unpackable.
                probe = getattr(obj, "archive_path", None) or getattr(obj, "path", None)
                # A finished job is a record: it stays listed when its source is gone.
                if getattr(obj, "status", "") != "Done" and (not probe or not Path(probe).exists()):
                    # Unreachable right now (drive unplugged, share down): keep the saved
                    # entry so it is not lost when the queue is next persisted.
                    skipped += 1
                    missing.append(d)
                    continue
                # Emu files injected into the user's own folder by a run that never
                # finished (crash, force-quit): restore the folder now.
                if getattr(obj, "ampr_injected", None) or getattr(obj, "ampr_index_path", None):
                    try:
                        self._ampr_cleanup(obj)
                    except Exception:
                        pass
                obj.artwork = None   # the cover comes back from the art cache (art_key)
                # A queue saved before per-job formats existed has no format snapshot; it
                # was a compressed .ffpfsc job then. Without this, the snapshot step below
                # would turn it into whatever format is remembered today (e.g. .pkg).
                if getattr(obj, "operation", "pack") == "pack" and getattr(obj, "output_compressed", None) is None:
                    obj.output_compressed = True
                if getattr(obj, "status", "") in ("Running", "Extracting", "Patching"):
                    obj.status = "Pending Extract" if getattr(obj, "archive_path", None) else "Queued"
                self.queue.append(obj)
                restored += 1
            except Exception:
                skipped += 1
        self._queue_missing_saved = missing   # re-persisted by _save_queue, untouched
        self._queue_restored = True   # from here on, queue mutations persist
        # Covers no job uses any more go after a while (the parked jobs keep theirs).
        _keep = {getattr(o, "art_key", "") for o in self.queue} | {str(d.get("art_key") or "") for d in missing}
        threading.Thread(target=prune_art_cache, args=(_keep,), daemon=True).start()
        if self.queue:
            self.update_queue_box()
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
        if ultra_core._SETTINGS_CORRUPT_COPY:
            self.log("WARN", f"settings.json could not be read and was kept as "
                             f"{ultra_core._SETTINGS_CORRUPT_COPY.name}; the profile was re-created with defaults.")
        if skipped:
            self.log("WARN", f"Saved queue: {skipped} item(s) skipped (source path missing/invalid).")

    def update_queue_box(self, select_item=None):
        """Rebuild the listbox.

        select_item: if given, that GameItem will be highlighted after the
        rebuild (used by move-up/down so the correct item is tracked even
        though the listbox selection is stale).  When omitted the previously
        selected item is looked up by object identity; falls back to row 0.
        """
        # Per-job output: snapshot the current global Output onto any pack/convert item
        # that doesn't already carry one, so each queued job keeps the output it was added
        # with (patch/sign set their own; fake-sign has none). Runs right after an item is
        # appended (update_queue_box is called then), capturing the output at add time.
        _gout = (self.output_var.get() or "").strip()
        try:
            _fmt = self.output_format_var.get()
        except Exception:
            _fmt = "ffpfsc"
        for _it in self.queue:
            _op = getattr(_it, "operation", "pack")
            if _gout and getattr(_it, "output_path", None) is None and _op in ("pack", "unpack", "chain"):
                _it.output_path = Path(_gout)
            # Format is per-job: snapshot the remembered default onto a fresh pack item once.
            # With '.pkg' remembered the fresh pack item becomes an fPKG build job — this is
            # how every source the classifier knows (game folder, folder scan with N games,
            # library bundle, archive, disk image, .ffpfs) turns into a .pkg without a second
            # code path. Identity is read from each game's param.json at build time; the one
            # the dialog may have pre-filled for a single game folder rides along.
            if _op == "pack" and getattr(_it, "output_compressed", None) is None:
                if _fmt == "pkg":
                    self._as_fpkg_job(_it, dict(self.fpkg_defaults),
                                      identity=self._take_pending_fpkg_identity(_it))
                _it.output_compressed = (_fmt != "ffpfs")
            # Auto-organize is per job too: snapshot the remembered default once.
            if _op in ("pack", "fpkg-build", "chain") and getattr(_it, "auto_organize", None) is None:
                _it.auto_organize = bool(self.auto_organize_var.get())
        self._save_queue()   # persist the (just-mutated) queue across restarts
        # Decide which item to keep selected. A plain refresh keeps the job in focus (found by
        # its key, so a reorder or a removal above it does not move the focus to another job)
        # and every other marked job; an explicit select_item replaces the selection.
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
            # Capture a STABLE display name the first time we render this item — at add
            # time item.name is the friendly name (a bundle's folder, the archive's name).
            # After extraction _copy_item_payload rewrites item.name to the extracted
            # stem (e.g. "PPSA00001"), which still drives the output filename; this keeps
            # the queue row showing what the user added.
            if not getattr(item, "display_name", None):
                item.display_name = (getattr(item, "bundle_subfolder", None)
                                     or getattr(item, "name", "") or item.title_id)
            prefix = "▶ " if item is _run_item else f"{i + 1}. "
            opn = getattr(item, "operation", "pack")
            badge = {"unpack": "CONVERT", "patch": "PATCH ",
                     "fake-sign": "SIGN   ",
                     "fpkg-extract": "fPKG-EX",
                     "fpkg-build":   "fPKG-BD",
                     "copy":         "COPY   ",
                     "chain":        ("→ " + CHAIN_TARGET_LABEL.get(getattr(item, "chain_to", ""), "?")).ljust(7)
                     }.get(opn, "PACK   ")
            # Per-job detail: jobs that don't have a meaningful source size show their
            # target/mode instead of "0 B".
            if opn == "chain":
                _ch = ", ".join(chain_changes(item)) or "no changes"
                _sz = (f"~{format_size(display_size(item))}" if shows_extracted_size(item)
                       else format_size(getattr(item, "size", 0) or 0))
                detail = f"{_ch}  ·  {_sz}"
            elif opn == "fake-sign":
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
                # Archives store the COMPRESSED set size in .size; show the EXTRACTED size
                # (what space/placement actually use), tagged with ~ as a header estimate.
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
            sub = "  →  ".join(self._job_recipe(item))
            if size_txt:
                sub += f"   ·   {size_txt}"
            tid = shown_title_id(item)
            if tid and tid not in str(disp):
                sub = f"{tid}   ·   {sub}"
            chip = (f"{int(self._cur_job_pct)}%" if running else
                    {"Pending": "Queued", "Pending Extract": "Queued"}.get(st, st or "Queued"))
            rows.append({"key": id(item), "text": line, "title": str(disp), "subtitle": sub, "state": state, "chip": chip,
                         "progress": (max(0.0, min(1.0, self._cur_job_pct / 100.0)) if running else None)})

        n = len(self.queue)
        self.queue_total_var.set(f"{n} job{'' if n == 1 else 's'}  ·  {format_size(total)}")
        try:
            self._nav["queue"].configure(badge=n)
        except Exception:
            pass

        # Find the target item's new index; fall back to row 0
        try:
            sel = self.queue.index(select_item) if select_item in self.queue else 0
        except (ValueError, TypeError):
            sel = 0
        self.queue_listbox.set_rows(rows, selected=sel, keep_marked=keep_marked)
        self.queue_listbox.see(sel)

        # Only refresh the details panel when the selected item actually changed.
        # Using `is` (reference equality) is safe here: we hold _details_item as a
        # real reference so Python cannot reuse the address while it lives in the queue.
        sel_item = self.queue[sel]
        if sel_item is not self._details_item:
            self.update_game_details(sel_item)
        elif len(self.queue_listbox.marked_rows()) > 1:
            self._card_show(True)                # the summary of several selected jobs, refreshed

    def update_game_details(self, item):
        self._details_item = item   # record before any call that might raise
        self._sync_progress_box()
        self.game_name_var.set(f"Name: {getattr(item, 'display_name', None) or item.name}")
        mode = {"unpack": "Convert", "patch": "Integrate patch",
                "fake-sign": "Fake sign",
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
        """The job card's header lines and recipe chips for *item*."""
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
            size_txt = (f"~{format_size(item.extracted_size)} unpacked" if shows_extracted_size(item)
                        else format_size(getattr(item, "size", 0) or 0))
            files = getattr(item, "files", 0) or 0
            meta = [x for x in (getattr(item, "title_id", ""), f"v{ver}" if ver else "", size_txt,
                                f"{files:,} file{'' if files == 1 else 's'}" if files else "") if x]
            self.card_meta_var.set("   ·   ".join(meta))
            out = self._job_output_dir(item)
            self.card_target_var.set(f"→  {out}" if out else "→  no output folder set")
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
        """The details pane's facts about *item*, one short text per row."""
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
            status += f"; the archive cannot be read: {item.archive_problem}"
        info["Status"] = status
        src = getattr(item, "archive_path", None) or getattr(item, "path", None)
        if src:
            info["Source"] = str(src)
        changes = []
        patch = getattr(item, "patch_source", None)
        if patch:
            changes.append(f"Integrate the patch from {patch}")
        bt = getattr(item, "backport_target", None)
        if bt:
            libs = getattr(item, "backport_libs_root", None) or (self.backport_libs_var.get() or "").strip()
            fw = (self.fw_libs_var.get() or "").strip()
            line = f"Backport to {bt}"
            line += f"; patched libraries from {libs}" if libs else "; no patched libraries set"
            line += "; the functions are checked against your firmware folder" if fw else \
                    "; no function check (no firmware folder in Settings)"
            changes.append(line)
        out = self._job_recipe_parts(item)[-1] if self._job_recipe_parts(item) else ""
        if getattr(item, "chain_sign", False) or getattr(item, "operation", "") == "fake-sign":
            changes.append("Fake-sign the executables")
        elif out == ".pkg":
            changes.append("Executables signed by the package builder")
        info["Changes"] = "\n".join(changes) if changes else "None"
        if out == ".ffpfsc":
            try:
                cores = int(self.cpu_count_var.get())
            except (TypeError, ValueError):
                cores = 0
            info["Compression"] = (f".ffpfsc, zlib level {self._ffpfsc_level(item)}, 64 KiB blocks, "
                                   f"{'cores by game size' if cores == 0 else f'{cores} cores'}")
        elif out == ".pkg":
            p = self._fpkg_params_of(item)
            speed = f"level {int(p.get('level', 0))}"
            try:
                cores = int(self.cpu_count_var.get())
            except (TypeError, ValueError):
                cores = 0
            info["Compression"] = (f".pkg, Kraken {speed} on {'all cores' if cores <= 0 else f'{cores} cores'}, "
                                   f"codec layer {p.get('inner', 'kraken')}; "
                                   f"retail fixes {'on' if p.get('retail_normalize', True) else 'off'}, "
                                   f"HDR {p.get('hdr_flag', 'auto')}")
        elif out == ".ffpfs":
            info["Compression"] = ".ffpfs, uncompressed"
        work = getattr(item, "_build_temp", None) or (self.temp_var.get() or "").strip()
        out_dir = self._job_output_dir(item)
        if getattr(item, "operation", "") == "copy" and out_dir:
            # nothing is built: an archive is unpacked on the output drive, then moved in
            arc = getattr(item, "archive_path", None) and not getattr(item, "path", None)
            info["Drives"] = (f"unpacks and writes on {_drive_name(out_dir)} ({out_dir})" if arc
                              else f"writes to {_drive_name(out_dir)} ({out_dir})")
        elif work or out_dir:
            bits = []
            if work:
                bits.append(f"works on {_drive_name(Path(str(work)))} ({work})")
            if out_dir:
                bits.append(f"writes to {_drive_name(out_dir)} ({out_dir})")
            info["Drives"] = ", ".join(bits)
        space = (self.temp_space_var.get() or "").strip()
        if space and not space.endswith("—"):
            info["Space"] = space.replace("  |  ", " · ")
        aj = _after_job_module()
        act = getattr(item, "after_source", None) or aj.KEEP
        if (act in aj.ACTIONS and act != aj.KEEP
                and (getattr(item, "operation", "") not in ("fake-sign", "copy")
                     or getattr(item, "content_kind", "") in ("ps4", ORGANIZE_TARGET))):
            text = aj.DONE_TEXT[act].format(dest=getattr(item, "after_move_to", None) or "a folder")
            info["After"] = text[0].upper() + text[1:] + ", once the job is Done"
        return info

    def _probe_pkg_content(self, item) -> int:
        """Read the size of the game in a chain job's .pkg source from the package's own
        directory (the tool's list-inner reads it, nothing is decoded). The package data is
        compressed, so the game is larger than the file: the space check and the placement
        need this number. Sets item.pkg_content_size and item.extracted_size; returns the
        size, 0 when it cannot be read (the file size stays the floor)."""
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
            content = sum(int(e.get("size") or 0) for e in doc.get("entries", []) if e.get("type") == "file")
        except Exception as e:
            self.log("WARN", f"Could not read the size of the game in {Path(str(item.path)).name}: {e}")
            return 0
        if content > 0:
            item.pkg_content_size = content
            item.extracted_size = max(content, int(getattr(item, "extracted_size", 0) or 0))
        return content

    def _refresh_space_for_item(self, item=None):
        """Recalculate free-space vs what this game needs and update the stats label."""
        if item is None:
            item = self._shown_or_next()
        if item is None or getattr(item, "size", 0) == 0:
            self.temp_space_var.set("Temp Needed: —")
            return
        if getattr(item, "operation", "pack") == "copy":
            out_dir = self._job_output_dir(item)
            tp = self.temp_var.get().strip()
            try:
                needs = _space_requirements(item, Path(tp) if tp else out_dir, out_dir) if out_dir else []
                if not needs:
                    self.temp_space_var.set("Same drive: renamed, no extra space")
                else:
                    ok = all(get_free_space(d) >= n for _l, d, n in needs)
                    self.temp_space_var.set("  |  ".join(f"{label}: ~{format_size(n)}, {format_size(get_free_space(d))} free"
                                                         for label, d, n in needs)
                                            + f"  |  {'fits' if ok else 'LOW, may not fit'}")
            except Exception:
                self.temp_space_var.set(f"Needs: ~{format_size(_build_size_of(item))}")
            return
        if getattr(item, "operation", "pack") == "unpack":
            try:
                op = self.output_var.get().strip()
                out_dir = Path(op) if op else None
                out_free = get_free_space(out_dir) if out_dir else 0
                self.temp_space_var.set(
                    f"Extract Needs: ~{format_size(item.size)}+  |  Out Free: {format_size(out_free)}"
                )
            except Exception:
                self.temp_space_var.set(f"Extract Needs: ~{format_size(item.size)}+")
            return
        try:
            tp = self.temp_var.get().strip()
            temp_dir = Path(tp) if tp else None
            out_dir  = self._job_output_dir(item)
            if temp_dir is None:
                self.temp_space_var.set(f"Peak Needed: ~{format_size(int(_build_size_of(item) * _peak_factor_for(item)))}")
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
            # It completes if the SSD can hold the image (split — the spool auto-routes to
            # the big output drive) OR the output drive can hold the whole build.
            ok = (size > 0 and free >= image_need) or (out_dir is not None and out_free >= out_full)
            flag = "fits" if ok else "LOW, may not fit"
            self.temp_space_var.set(
                f"{need_label}: ~{format_size(image_need)}  |  Temp Free: {format_size(free)}  "
                f"|  Out Free: {format_size(out_free)}  |  {flag}"
            )
        except Exception:
            self.temp_space_var.set(f"Peak Needed: ~{format_size(item.size * 2.2)}")

    def _update_format_label(self):
        comp = self.output_compressed_var.get()
        try:
            self.format_hint_var.set("Compressed (.ffpfsc, smaller)" if comp
                                     else "Uncompressed (.ffpfs, faster)")
        except Exception:
            pass

    def _on_format_toggle(self):
        """Output format changed — applies to the WHOLE queue. Refresh the hint and the
        command/queue preview so output names/extensions reflect the chosen format."""
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
        """The job the details pane shows, else the running or next job, else the first."""
        it = getattr(self, "_details_item", None)
        if it is not None and it in self.queue:
            return it
        return self._running_item() or self._next_pending() or (self.queue[0] if self.queue else None)

    def update_command_preview(self):
        item = self._shown_or_next()
        # Archive placeholders have no path yet — show a friendly message instead
        if item and getattr(item, "archive_path", None):
            self.command_label.configure(
                text=f"{item.name} — archive will be extracted before compression starts.")
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
        # Per-job output: each queued job can carry its own output target (snapshotted
        # when it was added); fall back to the current global Output folder otherwise.
        _job_out = getattr(item, "output_path", None)
        out = Path(str(_job_out)) if _job_out else Path(self.output_var.get().strip())
        # Give every pack job a findable, descriptive, collision-resistant output name
        # "<Game> [v<ver>] [<TITLEID>].ffpfsc" when the user picked an output FOLDER.
        # The backend honours an explicit .ffpfsc path (it only auto-names by title id
        # when handed a directory) — so naming by title id alone would let two queued
        # games with the same title id overwrite each other in a batch. An explicit
        # .ffpfsc the user typed is respected as-is.
        op = getattr(item, "operation", "pack")
        # Output format (whole-queue toggle): uncompressed .ffpfs ONLY for the PFS family —
        # a game folder or a .ffpfs source. .exfat/.ffpkg disk images are always compressed
        # to .ffpfsc (the backend ignores --no-compress for them). .ffpfs is faster to build
        # (no pass 2) and to mount (no decompression), at full size.
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
            # Where and as what the .ffpfsc lands:
            #  • auto-organize: '<out>/<Title> [TID] [vX.Y.Z]/<Title> [TID] [vX.Y].ffpfsc' from
            #    the game's param.json (falls back to the rules below when unreadable);
            #  • bundle: recreate the source folder under the output dir — never INTO the
            #    source folder itself (see _mirror_base);
            #  • else: straight into the output folder.
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
                out = base   # keep the subfolder; backend names <title_id><ext> inside
        temp = Path(self.temp_var.get().strip())
        backend = backend_base_dir()
        cli_py = backend / "cli.py"
        pycmd = get_backend_python_command()
        if not pycmd:
            raise RuntimeError("Python was not found. Install Python, or run the app from source.")

        # ── CHAIN job: source → [patch → backport → sign] → folder/.ffpfs/.ffpfsc/.pkg ──
        # The one shape the job dialog produces. The backend's --to does the work; this
        # only names the output the way the matching legacy job would (so a library
        # built by chains and by old jobs looks the same) and passes the tuning that
        # applies to the chosen output.
        if op == "chain":
            to = getattr(item, "chain_to", None) or "ffpfsc"
            src = Path(item.path)
            item._organized_pkg_name = None
            if to == "folder":
                if src.is_dir():
                    out = src                                   # changes in place; nothing is written elsewhere
                elif not out.name.endswith(" [extracted]"):
                    out = out / f"{sanitize_filename(src.stem)} [extracted]"
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
                ext = ".ffpfs" if to == "ffpfs" else ".ffpfsc"
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
                run_dir = out.parent if out.suffix.lower() in (".ffpfsc", ".ffpfs") else out
            self._cmd_uncompressed = (to == "ffpfs")
            try:
                (out if to in ("folder", "pkg") or not out.suffix else out.parent).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            head = (pycmd + [str(src), str(out)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), str(src), str(out)])
            cmd = head + ["--to", to]
            # Should the job end as a plain copy (same format, nothing to change), the source
            # stays unless the job deletes it afterwards anyway: then a move is the fast way.
            cmd += ["--copy-mode", "move" if getattr(item, "after_source", None) == "delete" else "keep"]
            if to == "pkg" and self._extract_dir_for_item(item) is not None:
                cmd.append("--stage-in-place")      # our own unpacked copy: built in place, taken in as it goes
            _ps = getattr(item, "patch_source", None)
            if _ps:
                cmd += ["--patch", str(_ps)]
            cmd += self._backport_args(item)
            if getattr(item, "chain_sign", False):
                cmd.append("--sign")
            if getattr(item, "_build_temp", None) is None:
                try:
                    self._resolve_extract_root(item)
                except Exception:
                    pass
            bt = getattr(item, "_build_temp", None)
            tstr = str(Path(bt)) if bt else str(temp)
            if tstr:
                cmd += ["--temp-dir", tstr]
            if to in ("ffpfs", "ffpfsc", "pkg"):
                # the CPU cores setting: mkpfs workers for an image, Kraken workers for a .pkg
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
                for flag, attr in (("--content-id", "fpkg_content_id"), ("--title-id", "fpkg_title_id"),
                                   ("--fpkg-title", "fpkg_title"), ("--fpkg-version", "fpkg_version")):
                    _v = str(getattr(item, attr, "") or "").strip()
                    if _v and not (attr == "fpkg_version" and _v == "01.000.000"):
                        cmd += [flag, _v]
                _lvl = getattr(item, "fpkg_level", None)
                cmd += ["--compression-level", str(int(self._pkg_level_default() if _lvl is None else _lvl))]
            if self.verbose_var.get():
                cmd.append("--verbose")
            cmd.append("--overwrite")
            return cmd, backend, run_dir, temp

        # ── FAKE-SIGN job ────────────────────────────────────────────────────
        if op == "fake-sign":
            head = pycmd if getattr(sys, "frozen", False) else pycmd + ["-u", str(cli_py)]
            cmd = head + ["--fake-sign", str(item.path)]
            return cmd, backend, Path(item.path), temp

        # ── COPY job (same format in/out — no re-encode) ─────────────────────
        # The source is a packed image (.ffpfsc / .ffpfs / .pkg). The output
        # folder mirrors the auto-organize layout used for a fresh build: a
        # per-title folder + library filename derived from param.json. When the
        # source and target land on the same drive the backend performs an
        # atomic os.rename; otherwise it does a chunked copy and (unless the
        # user unchecked the option) deletes the source afterwards.
        if op == "copy" and getattr(item, "content_kind", "") == ORGANIZE_TARGET:
            # the backend writes the source into '<Title> [ID] [vX]/…' under the output, in its
            # own format, joining a title folder that is already there
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
            # the backend sorts the package(s) into '<Title> [CUSA…] [vX]/…' under the output
            try:
                out.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            head = (pycmd + ["placeholder", str(out)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), "placeholder", str(out)])
            cmd = head + ["--ps4-sort", str(item.path),
                          "--copy-mode", self._ps4_copy_mode(item),
                          "--if-exists", self.output_exists_var.get() or "skip"]
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
                # Explicit output path — split the filename off so --copy-name carries it.
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

        # ── fPKG EXTRACT job (built package -> /app0 folder) ─────────────────
        if op == "fpkg-extract":
            # Never dump /app0 straight into a library folder: the extract gets its own
            # "<package> [extracted]" subfolder (the dialog pre-names it that way when
            # the global Output is empty).
            if not out.name.endswith(" [extracted]"):
                out = out / f"{sanitize_filename(Path(item.path).stem)} [extracted]"
            head = (pycmd + ["placeholder", str(out)] if getattr(sys, "frozen", False)
                    else pycmd + ["-u", str(cli_py), "placeholder", str(out)])
            cmd = head + ["--fpkg-extract", str(item.path)]
            try:
                out.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return cmd, backend, out, temp

        # ── fPKG BUILD job (/app0 folder or packed image -> built .pkg) ──────
        if op == "fpkg-build":
            # Same placement rules as a pack: auto-organize into the game's own folder (the
            # tool names the .pkg by content id; the worker renames it afterwards — see
            # _finalize_pkg_name), else a bundle mirrors its folder, never into the source.
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
            if self._extract_dir_for_item(item) is not None:
                cmd.append("--stage-in-place")      # our own unpacked copy: built in place, taken in as it goes
            # Retail switches (CHANGELOG 1.1.12). Only the non-default positions are passed;
            # the backend's defaults are the console-verified configuration.
            if not getattr(item, "fpkg_retail_normalize", True):
                cmd += ["--fpkg-no-retail-normalize"]
            _hdr = _hdr_mode(getattr(item, "fpkg_hdr_flag", "auto"))
            if _hdr != "auto":
                cmd += ["--fpkg-hdr-flag", _hdr]
            if getattr(item, "fpkg_regen_playgo", False):
                cmd += ["--fpkg-regen-playgo"]
            if not getattr(item, "fpkg_fake_sign", True):
                cmd += ["--fpkg-no-fake-sign"]
            # Backport: --backport-target lowers eboot/prx/sprx SDK words to the
            # target's public values BEFORE the C# tool spiegels+signs+packs. Runs
            # in-place on a folder source, same as --fake-sign-first.
            cmd += self._backport_args(item)
            # Identity fields are FALLBACKS: the backend reads sce_sys/param.json of the
            # resolved source first (folder, unwrapped image, extracted archive alike) and
            # only uses these for what it lacks. Empty fields are simply not passed.
            for flag, attr in (("--content-id", "fpkg_content_id"), ("--title-id", "fpkg_title_id"),
                               ("--fpkg-title", "fpkg_title"), ("--fpkg-version", "fpkg_version")):
                _v = str(getattr(item, attr, "") or "").strip()
                if _v and not (attr == "fpkg_version" and _v == "01.000.000"):
                    cmd += [flag, _v]
            _dll = getattr(item, "fpkg_pubtools_dll", "") or ""
            if _dll:
                cmd += ["--fpkg-pubtools-dll", str(_dll)]
            # Staging drive for the inner image / CNT / outer image (and the unwrap of an
            # image source): the placement-chosen build temp, else the app temp.
            bt = getattr(item, "_build_temp", None)
            tstr = str(Path(bt)) if bt else str(temp)
            if tstr:
                cmd += ["--temp-dir", tstr]
            # Per-job Kraken speed: 7 = normal, -4 = fast preset (the tool's 0..9 are
            # identical). Never the tuning-bar level — it means nothing to this encoder.
            _lvl = getattr(item, "fpkg_level", None)
            if _lvl is None:
                _lvl = self._pkg_level_default()
            cmd += ["--compression-level", str(int(_lvl))]
            try:
                out.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return cmd, backend, out, temp

        # ── PATCH job (manual Integrate Patch as a queue item) ───────────────
        if op == "patch":
            game = Path(item.path)
            patch_src = getattr(item, "patch_source", None)
            if not patch_src:
                raise RuntimeError("Patch job has no patch source.")
            if getattr(item, "patch_overwrite", False) and game.suffix.lower() == ".ffpfsc":
                pout = game                                  # patch the .ffpfsc in place
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
            # Same spill target as a pack job: a big patched image can park its pass-2
            # spool on the output drive instead of failing on a tight temp drive.
            cmd += ["--spool-fallback-dir", str(pout.parent)]
            if self.verbose_var.get():
                cmd.append("--verbose")
            return cmd, backend, pout.parent, temp

        # OOM-resume: a retry compresses the already-built inner image (pass 1 is skipped;
        # the extracted source was freed after pass 1). Source the command from that image
        # so it takes the backend's single-pass .ffpfs -> .ffpfsc route. The output NAME
        # still comes from the item's metadata (descriptive_ffpfsc_name), so it matches the
        # original run exactly even though the source folder is gone.
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
            # Converter "decompress one level" (.ffpfsc → inner .ffpfs): stop unwrapping at
            # the first nested image instead of recursing all the way to a folder.
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
        if comp_level != 7:  # only pass if non-default
            cmd += ["--compression-level", str(comp_level)]
        # An OOM auto-retry pins an explicit (lower) core count on the item — honour it
        # over the global setting so the retry actually uses fewer mkpfs workers.
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
        # Route the backend scratch (inner image + pass-2 spool) to the SAME drive this
        # run was placed on (item._build_temp), so ALL artifacts follow one drive. Folder
        # and folder+patch jobs are never extracted, so resolve their placement here (the
        # PATCH factor applies for auto-patch jobs); archives carry _build_temp from
        # extraction / the post-extraction re-gate. Falls back to the user temp folder.
        patch_src = getattr(item, "patch_source", None)
        is_patch_job = (bool(patch_src) and self.auto_integrate_patch_var.get()
                        and bool(getattr(item, "path", None)) and item.path.is_dir()
                        and not _resume_ok)   # a resume only re-compresses the inner image
        if getattr(item, "_build_temp", None) is None:
            try:
                self._resolve_extract_root(item)   # sets item._build_temp via the right factor
            except Exception:
                pass
        build_temp = getattr(item, "_build_temp", None)
        if build_temp is not None:
            temp = Path(build_temp)
        temp_str = str(temp) if str(temp) else ""
        if temp_str:
            cmd += ["--temp-dir", temp_str]
        # Spill target for the pass-2 spool when --temp-dir can't hold image+spool: the
        # OUTPUT ROOT (a big drive). The backend writes the spool under <root>/_ffpfsc_temp,
        # which the startup sweep and failure cleanup already reclaim. Lets a big game keep
        # its inner image on the fast SSD instead of falling entirely onto the HDD.
        out_root = str(_job_out) if _job_out else self.output_var.get().strip()
        if out_root:
            cmd += ["--spool-fallback-dir", out_root]
        # Auto-patch: overlay a detected patch sibling onto the game before packing,
        # via the backend's PATCH MODE (now routed to the chosen --temp-dir above).
        if is_patch_job:
            cmd += ["--patch", str(patch_src)]
        # Opt-in exFAT workflow: only for a plain folder pack (not patch jobs, not a
        # disk-image source). The backend builds an exFAT image of the folder and
        # compresses that instead of running the folder PFS builder.
        if (self.build_via_exfat_var.get() and not is_patch_job and not _resume_ok
                and getattr(item, "operation", "pack") == "pack"
                and getattr(item, "path", None) and Path(item.path).is_dir()):
            cmd.append("--via-exfat")
        # Opt-in: fake-sign the source folder's executables in place before packing.
        # The backend applies this per folder-item (and warns-and-skips non-folder
        # sources), so it is safe to pass for any pack job that isn't a patch job.
        # AMPR items are EXCLUDED here: their fake-signing is done GUI-side inside
        # _prepare_ampr BEFORE the ampr_emu.index is built, so the index records the
        # final (signed) file sizes/mtimes. Letting the backend re-sign afterwards
        # would invalidate that index.
        if (self.fake_sign_before_pack_var.get() and not is_patch_job and not _resume_ok
                and getattr(item, "operation", "pack") == "pack"
                and not getattr(item, "ampr_emu", False)):
            cmd.append("--fake-sign-first")
        # Backport for a pack (mkpfs) job: same semantics as for fpkg — lowers
        # SDK words in place on the folder source before the pack, and pipes
        # the user's fakelib folder into sce_sys/../fakelib/.
        if (not is_patch_job and not _resume_ok
                and getattr(item, "operation", "pack") == "pack"
                and getattr(item, "path", None) and Path(item.path).is_dir()):
            cmd += self._backport_args(item)
        cmd.append("--overwrite")
        return cmd, backend, out if out.suffix.lower() not in (".ffpfsc", ".ffpfs") else out.parent, temp

    def _run_cleanup(self, work) -> None:
        """Run a cleanup function in a daemon thread, counted so the batch can wait for
        all in-flight reclaims to finish before re-reading free space (a boolean cannot
        represent N concurrent rmtrees)."""
        with self._cleanup_lock:
            self._cleanup_inflight += 1
        def _wrapped():
            try:
                work()
            finally:
                with self._cleanup_lock:
                    self._cleanup_inflight = max(0, self._cleanup_inflight - 1)
        threading.Thread(target=_wrapped, daemon=True).start()

    # ── AMPR / APR (PlayGo) support ───────────────────────────────────────────
    def _ampr_folder(self):
        """The configured folder holding the two emu .sprx files, or None."""
        try:
            p = self.ampr_var.get().strip()
        except Exception:
            p = ""
        return Path(p) if p and Path(p).is_dir() else None

    def _ensure_ampr_folder(self) -> bool:
        """Ensure the AMPR emu folder is set; prompt once if not. True when ready."""
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
                          "ampr_emu.index is built. (Stored in Settings — asked only once.)",
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
        """Copy the two emu .sprx into <game>/fakelib/. Tracks injected paths on the item
        so a DIRECT (non-archive) source folder can be cleaned up after packing."""
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
                self.log("INFO", f"AMPR: {fname} already present — skipping injection")
                continue
            try:
                shutil.copy2(src, dst)
                item._ampr_injected.append(dst)
                item.ampr_injected.append(str(dst))
                self.log("INFO", f"AMPR: injected {fname}")
            except Exception as exc:
                self.log("WARN", f"AMPR: failed to inject {fname}: {exc}")

    def _build_ampr_index(self, item) -> None:
        """Build ampr_emu.index (AMPRIDX3) in the game folder. Ported byte-exact from the
        reference tool: header <8sIIQQQII>, records <IIQq>, FNV-1a-64 open-addressed slots
        <QII>, /app0/-prefixed lowercased POSIX paths, atomic temp-rename write."""
        if not getattr(item, "ampr_emu", False) or not item.path or not Path(item.path).is_dir():
            return
        import struct as _struct
        root       = Path(item.path).resolve()
        output     = root / "ampr_emu.index"
        output_tmp = output.with_suffix(output.suffix + ".tmp")

        def _key(p):
            # The emulator runtime's key (drakmor's reference builder): UTF-8 bytes with
            # "\\" -> "/" and only ASCII A-Z folded. Unicode .lower() would change the hash
            # and the sort order of non-ASCII paths.
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
        # OS junk (.DS_Store, ._*, __MACOSX, Thumbs.db, …) is stripped by the backend
        # before packing, so it must not be indexed either — same rule as MkPFS.
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
        """Run the recursive fake-signer over *folder* in this (main) thread, used
        by the AMPR path so signing completes BEFORE the ampr_emu.index is built.
        The backend's fake_sign module is pure-Python; import it directly rather
        than spawning a subprocess so the index build can follow synchronously."""
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
        """Right before packing an APR/PlayGo folder: ensure the emu folder, inject the
        .sprx into fakelib/, and build the index. No-op for non-APR games / disk images."""
        if not getattr(item, "ampr_emu", False) or not getattr(item, "path", None):
            return
        game = Path(item.path)
        self.log("INFO", f"AMPR: {item.name} is a PlayGo/APR title — preparing emu files.")
        # Releases often SHIP the emu already — fakelib/libSceAmpr.sprx + libScePlayGo.sprx
        # and usually an ampr_emu.index. Then there is nothing to ask for or to inject.
        shipped = [f for f in AMPR_SPRX_FILES if (game / "fakelib" / f).is_file()]
        shipped_index = (game / "ampr_emu.index").is_file()
        if len(shipped) == len(AMPR_SPRX_FILES):
            self.log("INFO", "AMPR: the game ships its own fakelib/ emu files"
                             + (" and ampr_emu.index" if shipped_index else "") + " — no emu folder needed.")
            want_sign = bool(self.fake_sign_before_pack_var.get()) and game.is_dir()
            if want_sign and shipped_index and not getattr(item, "_from_archive", False):
                # Signing changes file sizes, so the shipped index would go stale — and this
                # is the user's own library folder, whose files we never rewrite.
                self.log("WARN", "AMPR: fake-sign-before-pack skipped for this game — it ships an "
                                 "ampr_emu.index that must match its files, and the source is your own folder.")
                want_sign = False
            if want_sign:
                self._fake_sign_folder_inproc(game)
            if want_sign or not shipped_index:
                self.log("INFO", "AMPR: rebuilding ampr_emu.index after fake-signing (AMPRIDX3)." if shipped_index
                                 else "AMPR: no ampr_emu.index shipped — building one (AMPRIDX3).")
                self._build_ampr_index(item)
            else:
                self.log("INFO", "AMPR: keeping the shipped ampr_emu.index as-is.")
            return
        if shipped:
            missing = [f for f in AMPR_SPRX_FILES if f not in shipped]
            self.log("INFO", f"AMPR: the game ships {', '.join(shipped)} but not {', '.join(missing)} — "
                             f"the emu folder supplies the rest.")
        if not self._ensure_ampr_folder():
            self.log("WARN", "AMPR: no emu folder set — packing WITHOUT AMPR support; this "
                             "APR title may not boot until you set the folder in Settings.")
            return
        # If fake-signing is enabled, sign the game's executables FIRST so the index
        # below records their final (signed) sizes/mtimes. The injected .sprx shims
        # are added after this and are themselves already signed, so they're skipped.
        if self.fake_sign_before_pack_var.get() and Path(item.path).is_dir():
            self._fake_sign_folder_inproc(Path(item.path))
        self._inject_ampr_files(item)
        self._build_ampr_index(item)

    def _ampr_cleanup(self, item) -> None:
        """Remove emu files we injected into a DIRECT (non-archive) source folder — that's
        the user's own library, so restore it after packing. Archive-extracted sources sit
        in temp and are removed wholesale by the normal teardown, so they're left alone."""
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
        """Requeue *item* with one fewer mkpfs worker after an out-of-memory kill.
        Returns True if a retry was scheduled (caller should NOT count this as a failure).
        Step-down: an explicit N → N-1 → … → 1; AUTO (0) drops straight to 1 worker (the
        backend already auto-capped it, so 1 is the only guaranteed reduction). Capped at
        two retries; gives up at one worker."""
        MAX_RETRIES = 2
        tries = getattr(item, "_oom_retries", 0)
        prev = getattr(item, "_cpu_retry_override", None)
        base = self.cpu_count_var.get()
        # The core count the failed run actually used: an earlier retry's override, else
        # the configured count (AUTO = 0 is unknown here; the backend capped it, so 1 is
        # the only guaranteed reduction).
        current = prev if prev is not None else (base if (base and base > 0) else None)
        new_cpu = max(1, (current - 1) if current is not None else 1)
        if tries >= MAX_RETRIES or (current is not None and new_cpu >= current):
            self.log("ERROR", f"Still out of memory at {new_cpu} core(s) — giving up on "
                              f"{item.name}. Try a lower compression level or smaller block size.")
            return False
        item._cpu_retry_override = new_cpu
        item._oom_retries = tries + 1
        # If pass 1 already built the inner image (the usual case — OOM strikes in pass-2
        # compression), resume from it: the retry compresses that image with fewer cores,
        # skipping pass 1 entirely. (No inner image yet = a rare pass-1 OOM → rebuild from
        # the source, which keep_source below preserves.)
        inner = getattr(item, "_inner_image", None)
        if inner and Path(str(inner)).is_file():
            item._resume_inner = str(inner)
            self.log("INFO", "Resuming from the already-built inner image — pass 1 is skipped "
                             "on this retry (compression only).")
        item.status = "Pending"
        # Reclaim the failed run's partial scratch (pass-2 spool, mkpfs tmp dirs) but KEEP the
        # extracted source: a pass-1 retry rebuilds from it, a pass-2 resume already freed it
        # (the keep is then a no-op). The inner image (_ffpfsc_inner) is preserved for resume.
        self._cleanup_after_failure(item, keep_source=True)
        if item not in self.queue:
            self.queue.append(item)
        self._run_next = item
        self._active_item = item
        self.update_queue_box()
        self.log("WARN", f"Out of memory — retrying {item.name} with {new_cpu} CPU core(s) "
                         f"(attempt {tries + 1}/{MAX_RETRIES}).")
        self.status_update("Retrying", f"Out of memory — retrying with {new_cpu} core(s)…",
                           "Retrying", 0, 0, "—", "—", "—")
        self._batch_running = True   # keep the loop alive even for a single-game run
        self.root.after(800, self._batch_auto_start)
        return True

    def _cleanup_after_failure(self, item, keep_source: bool = False) -> None:
        """Reclaim a failed/skipped/cancelled run's scratch: the mkpfs tmp* working dirs
        (orphaned inner image + pass-2 spool) AND this item's own extracted-source subdir
        — scoped to the drive the run actually built on (item._build_temp), the user temp
        folder, AND the output drive's _ffpfsc_temp (where a SPLIT run may have spilled its
        pass-2 spool, ffpfsc_spool_*). Counted so the next batch gate waits for the reclaim.
        Threaded (rmtree of a ~150 GB tree must not freeze the UI).

        *keep_source* (set by an OOM retry): DON'T delete the extracted-source subdir — a
        pass-1 retry rebuilds the inner image from it, and a pass-2 resume already freed it.
        Only terminal failures/cancels delete the source. The persisted inner image
        (_ffpfsc_inner) is never touched here; its lifecycle is _cleanup_inner_image."""
        self._ampr_cleanup(item)   # restore a direct source folder we injected emu files into
        roots, seen = [], set()
        _jo = self._job_output_dir(item)
        out_str = str(_jo) if _jo else ""
        out_spool = (str(Path(out_str) / "_ffpfsc_temp") if out_str else None)
        # A build cancelled or killed mid-pass-2 leaves the backend's swap file
        # "<name>.ffpfsc.partial" (and its .tmp) in the output folder: reclaim those too.
        _w = getattr(self, "worker", None)
        _partial_dir = Path(str(getattr(_w, "output_dir", "") or "")) if _w is not None else None
        _partial_since = float(getattr(_w, "start_time", 0) or 0) - 2.0
        _stage_parent = None
        try:
            _src = Path(str(getattr(item, "path", "") or ""))
            if getattr(item, "operation", "") == "fpkg-build" and _src.is_dir():
                _stage_parent = _src.parent
        except Exception:
            _stage_parent = None
        for cand in (getattr(item, "_build_temp", None), self.temp_var.get().strip(), out_spool):
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
        def _work(tp=_tp):
            freed = 0
            for root in roots:
                try:
                    for p in root.iterdir():
                        if p.is_dir() and (_is_app_tmp_dir(p.name) or p.name.startswith("ffpfsc_spool_")
                                           or (not keep_source and p.name == "_ffpfsc_inner")):
                            sz = get_folder_size(p)
                            shutil.rmtree(str(p), ignore_errors=True)
                            freed += sz
                except Exception:
                    pass
            # The fPKG tool stages a hard-link mirror of a folder source next to it
            # ("<parent>/.ffpfsc-stage-<id>"); a killed build can leave it behind. Removing
            # it only unlinks the mirror's links — the source files keep their own.
            if _stage_parent is not None and _stage_parent.is_dir():
                try:
                    for p in _stage_parent.iterdir():
                        if p.is_dir() and p.name.startswith(".ffpfsc-stage-") \
                                and p.stat().st_mtime >= _partial_since:
                            shutil.rmtree(str(p), ignore_errors=True)
                except Exception:
                    pass
            if _partial_dir is not None and _partial_dir.is_dir():
                try:
                    for p in _partial_dir.iterdir():
                        n = p.name.lower()
                        if p.is_file() and (n.endswith(".partial") or n.endswith(".partial.tmp")) \
                                and p.stat().st_mtime >= _partial_since:
                            sz = p.stat().st_size
                            p.unlink()
                            freed += sz
                except Exception:
                    pass
            if not keep_source:
                try:
                    own = self._extract_dir_for_item(item)   # temp/_extracted OR output/_ffpfsc_extract
                    if own is not None and own.exists():
                        sz = get_folder_size(own)
                        shutil.rmtree(str(own), ignore_errors=True)
                        freed += sz
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
            # Drop throwaway patch dirs + the now-empty temp base, like the success path.
            self._reclaim_temp_base_inline(tp)

        self._run_cleanup(_work)

    # ── Feature 5: Auto-clear temp ────────────────────────────────────────────
    def _offer_startup_sweep(self):
        """On launch, look for orphaned scratch from a crashed/cancelled run — tmp* and
        _extracted on the temp drive, plus _ffpfsc_temp / _ffpfsc_extract on the output
        drive — and offer to reclaim it. Sizing walks large trees, so it runs in a
        background thread; the confirm prompt + delete are marshalled to the main thread.
        Only this app's own working dirs are ever touched.

        1.1.8: also scans every unique output folder recorded in the history so leftovers
        on drives the user built to in past sessions (a spread run, an aborted job on an
        external HDD) get surfaced instead of aging out silently. Threshold dropped from
        1 GiB → 64 MiB so small-but-real orphans stop accumulating."""
        if getattr(self, "_browser_only", False):
            return   # browser-only launch (double-clicked a .ffpfsc) — don't prompt on the hidden window
        NAMES = ("_extracted", "_ffpfsc_extract", "_ffpfsc_temp", "_ffpfsc_inner")
        kept: set[str] = set()                 # copies that queued jobs kept after a cancel
        for it in self.queue:
            own = self._extract_dir_for_item(it) if getattr(it, "kept_extract", False) else None
            if own is not None:
                try:
                    kept.add(str(own.resolve()))
                except OSError:
                    kept.add(str(own))
        # Tk variables are read on the main thread only; the scan thread gets a snapshot.
        bases0 = [self.temp_var.get().strip(), self.output_var.get().strip()]
        try:
            bases0 += [str(p) for p in self._temp_pool_dirs()]   # extra scratch SSDs too
        except Exception:
            pass

        def _scan():
            targets, seen = [], set()
            bases = list(bases0)
            # Drives from the history: each recorded output path is a folder we WROTE to,
            # so its parent may still hold our _ffpfsc_temp from a run that never cleaned up.
            try:
                hist = load_history() or []
                hist_bases: set[str] = set()
                for h in hist[-100:]:
                    out = str((h or {}).get("output") or "").strip()
                    if not out:
                        continue
                    op = Path(out)
                    # Try the output path itself and its parent — the temp dir might sit
                    # next to the .ffpfsc file OR one level up (per-title organize folder).
                    for cand in (op, op.parent):
                        try:
                            s = str(cand.resolve())
                        except Exception:
                            s = str(cand)
                        hist_bases.add(s)
                bases += sorted(hist_bases)
            except Exception:
                pass
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
                        if p.is_dir() and (_is_app_tmp_dir(p.name) or p.name in NAMES):
                            try:
                                key = str(p.resolve())
                            except Exception:
                                key = str(p)
                            if any(k == key or k.startswith(key + os.sep) for k in kept):
                                # holds a kept copy: offer only its other contents
                                try:
                                    for c in p.iterdir():
                                        ck = str(c.resolve())
                                        if ck not in kept and ck not in seen:
                                            seen.add(ck)
                                            targets.append(c)
                                except Exception:
                                    pass
                                continue
                            if key not in seen:
                                seen.add(key)
                                targets.append(p)
                except Exception:
                    continue
            total = 0
            for p in targets:
                try:
                    total += get_folder_size(p)
                except Exception:
                    pass
            if targets and total >= 64 * 1024**2 and not self._job_active():   # 64 MiB threshold
                self.root.after(0, lambda: self._prompt_startup_sweep(targets, total))

        threading.Thread(target=_scan, daemon=True).start()

    def _prompt_startup_sweep(self, targets, total):
        try:
            if self._job_active():
                self.log("INFO", "Startup sweep skipped — a job is already working in those folders.")
                return
            n = len(targets)
            listing = "\n".join(f"  • {p}" for p in targets[:8]) + ("\n  • …" if n > 8 else "")
            msg = (f"Found ~{format_size(total)} of leftover working data from a previous "
                   f"run in {n} folder(s):\n\n{listing}\n\nDelete it now to reclaim the space?")
            if not messagebox.askyesno("Reclaim leftover temp space?", msg):
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

    def _auto_clear_temp(self):
        """Silently clear temp folder contents after a successful compression."""
        tp = self.temp_var.get().strip()
        if not tp:
            return
        temp_dir = Path(tp)
        if not temp_dir.exists():
            return
        freed = 0
        errors = 0
        for p in list(temp_dir.iterdir()):
            # Only remove THIS app's working data (mkpfs tmp* dirs, the _extracted tree,
            # and the patch-prepare extract dirs); never wipe unrelated files a user may
            # keep in their temp folder.
            if not (_is_app_tmp_dir(p.name)
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
        msg = f"Auto-cleared temp: freed {format_size(freed)}"
        if errors:
            msg += f" ({errors} item(s) could not be removed)"
        self.log("OK", msg)

    # ── Feature 4: Batch auto-advance ─────────────────────────────────────────
    def _update_batch_counter(self):
        if not self._batch_running:
            self.batch_counter_var.set("")
            return
        current = self._batch_done + self._batch_failed + 1
        extra = "".join(f"  ·  {n} {w}" for n, w in ((self._batch_done, "done"), (self._batch_failed, "failed")) if n)
        frac, eta = getattr(self, "_all_frac", None), getattr(self, "_all_eta", None)
        if frac is not None:
            extra += f"  ·  {int(frac * 100)} % of all" + (f", {self._fmt_left(eta)}" if eta is not None else "")
        line = f"Job {current} of {self._batch_total}{extra}"
        if getattr(self, "_pause_requested", False):
            line += "  ·  pauses after this job"
        if self.batch_counter_var.get() != line:
            self.batch_counter_var.set(line)

    def _job_output_dir(self, item) -> Path | None:
        """The folder this job writes to: its own output_path when set (the per-job
        snapshot taken when it was queued), else the global Output field. The space
        gate and the drive placement must look at THIS drive, not at whatever the
        Output field says today."""
        op = getattr(item, "output_path", None)
        if op:
            try:
                return Path(str(op))
            except Exception:
                pass
        g = (self.output_var.get() or "").strip()
        return Path(g) if g else None

    def _space_gate(self, item, out_dir):
        """Place the run on a drive sized for its REAL footprint (sets item._build_root /
        _build_temp via _resolve_extract_root), then decide go/skip/cancel against THAT
        drive. Returns 'proceed', 'skip', or 'cancel' per free space, the low-space policy
        (ask / auto / skip) and the diagnostics-dialog toggle. Honest extracted size +
        per-kind factor + safety factor feed the check, so a game that fits a drive (the
        big HDD) is routed there and proceeds; only a game that fits NO drive is skipped."""
        op = getattr(item, "operation", "pack")
        # Unpack and fPKG extract write to the output drive only; fake-sign rewrites in
        # place (no temp, no new output). None of them needs the pack space gate.
        if op in ("unpack", "fake-sign", "fpkg-extract"):
            return "proceed"
        # A chain that only changes a folder in place, or unpacks to a folder, writes no
        # inner image — same as the three above.
        if op == "chain" and getattr(item, "chain_to", None) == "folder":
            return "proceed"
        out_dir = Path(out_dir)
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
            return "proceed" if diag.proceed else "cancel"

        if ok:
            return _ask() if self.show_space_dialog_var.get() else "proceed"
        policy = (load_settings().get("low_space_policy", "ask") or "ask").lower()
        if policy == "auto":
            self.log("WARN", f"Low space — proceeding anyway (policy: auto): {item.name}")
            return "proceed"
        if policy == "skip":
            self.log("WARN", f"Low space — {item.name} fits no available drive; "
                             f"skipping (policy: skip).")
            return "skip"
        return _ask()   # 'ask' — show the dialog and let the user decide

    # Statuses that mean an item has finished its run and must NOT be processed again.
    _TERMINAL_STATUSES = ("Done", "Failed", "Skipped", "Cancelled")

    def _has_pending(self) -> bool:
        """True if any queued item still needs to run (not in a terminal status)."""
        return any(getattr(it, "status", "") not in self._TERMINAL_STATUSES for it in self.queue)

    def _running_item(self):
        """The job the batch works on right now (extracting or running). It keeps its place
        in the list: the queue is shown and kept in the order the jobs were added."""
        it = getattr(self, "_active_item", None)
        return it if (self._batch_running and it is not None and it in self.queue) else None

    def _next_pending(self):
        """The job to run next, without moving anything: the one the batch is already on (a
        re-entry after its archive was unpacked), else the one Retry asked for, else the
        first job from the top that has not run yet. Reorder the list to change it."""
        act = getattr(self, "_active_item", None)
        if (self._batch_running and act is not None and act in self.queue
                and getattr(act, "status", "") not in self._TERMINAL_STATUSES):
            return act
        pref = getattr(self, "_run_next", None)
        if pref is not None and pref in self.queue and getattr(pref, "status", "") not in self._TERMINAL_STATUSES:
            return pref
        return next((it for it in self.queue if getattr(it, "status", "") not in self._TERMINAL_STATUSES), None)

    def _pick_next(self):
        """_next_pending, made the active job."""
        item = self._next_pending()
        if item is not None:
            self._active_item = item
            if getattr(self, "_run_next", None) is item:
                self._run_next = None
            self.__dict__.setdefault("_job_t0", {}).setdefault(id(item), time.time())
        return item

    def _extract_is_complete(self, item) -> bool:
        """True when *item* runs from a copy it extracted from an archive, and that copy is
        whole: the payload already replaced the archive fields, so extraction finished. A job
        cancelled while its archive is still extracting has only half a copy."""
        own = self._extract_dir_for_item(item)
        if own is None or not own.exists() or getattr(item, "archive_path", None):
            return False
        src = getattr(item, "path", None)
        return bool(src) and Path(str(src)).exists()

    _RETRYABLE = ("Failed", "Cancelled", "Skipped")

    def _retry_selected(self):
        item = getattr(self, "_details_item", None)
        if item is not None and item in self.queue:
            self._retry_job(item)

    def _retry_job(self, item) -> None:
        """Run one failed, cancelled or skipped job again: from the copy it kept, else from
        its archive. While the queue runs it joins the jobs still waiting; otherwise it
        starts now, and the other failed jobs stay as they are."""
        if item not in self.queue or getattr(item, "status", "") not in self._RETRYABLE:
            return
        self._rearm_from_archive(item)
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
        """One key for 'is this the same source': a RAR volume's first part, else the
        resolved path. Case-insensitive so a case-only rename counts as the same file."""
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
        """The PPSA/CUSA id from a path's name, upper-case; '' when there is none. Reads
        the file name, every parent folder name, and (if present) a title id already
        stored on the GameItem; so an item built from an unpacked folder still carries it."""
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
        """What Rescan matches a new source against: (resolved source paths, title ids).
        Each entry comes from the queue (any status) or from the history, so a job that
        is already queued, is done, failed, was skipped or ran in an earlier session is
        found again — the archive at the same path, the same release renamed, and the
        folder output of an unpacked archive all match."""
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
            # the display name / stem may still carry it after extraction (archive_path and path
            # both go to None once the extracted copy is cleaned up on a done run)
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
        """True when a file or a folder for *tid* is already under *out_root* (the
        auto-organized "<Title> [TID] [v…]" folder, or any .ffpfsc/.ffpfs/.pkg carrying it
        in its name). The scan is cheap: one iterdir at the root and, when the entry is a
        plain folder with no suffix, its direct children only."""
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
                            if tag in q.name.lower() and q.suffix.lower() in (".ffpfs", ".ffpfsc", ".pkg"):
                                return True
                    except OSError:
                        continue
        except OSError:
            return False
        return False

    def rescan_last_source(self) -> None:
        """Scan the folder of the last Add job for new sources and queue the ones that are
        not already here or in the history. The settings of the last Add job are reused;
        no editor is opened. Nothing is added when nothing is new. Runs on a worker (the
        folder walk and the archive header reads can take a moment)."""
        settings = load_settings()
        last_dir = (settings.get("last_source_dir") or "").strip()
        last = (settings.get("last_source") or "").strip()
        if not (last_dir or last):
            messagebox.showinfo("Rescan", "No source to rescan yet. Add a job once, then Rescan "
                                           "finds new downloads in that folder.")
            return
        # last_source_dir is set by _add() to the parent of a file source or to the folder
        # itself; a folder as source means a single game, so the parent is what Rescan uses.
        folder = Path(last_dir) if last_dir else (Path(last).parent if Path(last).is_file() else Path(last).parent)
        if not folder.is_dir():
            messagebox.showerror("Rescan", f"The last source folder is gone:\n{folder}")
            return
        tpl = (settings.get("rescan_template") or {})
        if not tpl.get("to"):
            messagebox.showinfo("Rescan", "Add a job once with the output and the changes you want, "
                                           "then Rescan reuses those for new sources in the same folder.")
            return

        cleanup = tpl.get("after_source") in ("trash", "move", "delete")
        # With a clean-up chosen, a game already in the output folder or the history is
        # queued anyway: when its turn comes its source is moved (or trashed, deleted) and
        # the job is Done. Only what is in the queue already is left out.
        known_paths, known_tids = self._known_signatures(include_history=not cleanup)
        out_root = None if cleanup else ((tpl.get("output") or "").strip() or None)
        self.log("INFO", f"Rescan: looking for new sources in {folder}…")
        self.status_update("Scanning", f"Rescan: reading {folder}…", "Scanning Files",
                           0, 0, "00:00", "—", "—", side=True)

        def work():
            try:
                found = find_job_sources(folder)
            except Exception as e:
                self.scan_q.put(("rescan-error", str(e)))
                return
            new, skipped = [], 0
            for p in found:
                if self._source_key(p) in known_paths:
                    skipped += 1
                    continue
                tid = self._title_id_from_path(p)
                if tid and (tid in known_tids or self._output_already_has(out_root, tid)):
                    skipped += 1
                    continue
                new.append(p)
            self.scan_q.put(("rescan-found", {"folder": str(folder), "sources": new, "tpl": tpl,
                                              "scanned": len(found), "skipped": skipped}))
        self._launch_scan(work)

    def _rescan_make(self, tpl: dict):
        """A `make(source)` callable like JobDialog._add uses, built from *tpl* (the saved
        Add job settings): produces a chain job + fPKG params + compression + organize."""
        to = str(tpl.get("to") or "ffpfsc")
        out = str(tpl.get("output") or "")
        sign = bool(tpl.get("sign")) and to != "pkg"
        target = tpl.get("backport_target") or None
        patch = tpl.get("patch_source") or None
        pkg_params = tpl.get("pkg_params") or {}
        ff_level = int(tpl.get("ff_level") or 7)
        organize = bool(tpl.get("organize"))
        after = tpl.get("after_source") if tpl.get("after_source") in _after_job_module().ACTIONS else "keep"
        after_dir = tpl.get("after_move_to") or None
        root = str(tpl.get("_rescan_root") or "") or None

        def make(src):
            if to == ORGANIZE_TARGET:
                it = self._organize_item_for(src, output_path=out or None)
                it.after_source, it.after_move_to = after, after_dir
                it.source_root = root
                return it
            it = GameItem.from_chain(src, to=to, output_path=out or None, sign=sign,
                                     patch_source=patch, backport_target=target,
                                     backport_libs_root=None)
            if to == "pkg" and pkg_params:
                self._apply_fpkg_params(it, pkg_params)
            it.compression_level = ff_level if to == "ffpfsc" else None
            it.auto_organize = organize
            it.after_source, it.after_move_to = after, after_dir
            it.source_root = root
            return it
        return make

    def _add_jobs_async(self, sources, make) -> None:
        """Build the jobs for *sources* on a worker thread and hand them to the main loop one
        at a time (_drain_add_q): each appears in the queue as soon as it is ready, a line
        and a bar above the list count them, and Start waits until the last one is in. An
        archive's headers are read here, which for a many-part set on a slow drive takes
        seconds each; on the main thread the window froze for all of them."""
        if not sources:
            return
        st = self.__dict__.setdefault("_add_state", {"total": 0, "done": 0, "made": [], "errors": 0})
        st["total"] += len(sources)
        with self._scan_lock:
            self._scan_in_flight += 1
        q = self.__dict__.setdefault("_add_q", queue.Queue())

        def work():
            try:
                for s in sources:
                    try:
                        q.put(("item", make(s), s, ""))
                    except Exception as e:
                        q.put(("error", None, s, str(e)))
            finally:
                q.put(("done", None, None, ""))
        threading.Thread(target=work, daemon=True).start()
        self._show_add_progress()

    def _estimate_left(self, left_bytes: int, cur_bytes: int, cur_pct: float, cur_item) -> float | None:
        """Seconds the whole run still needs, roughly: the bytes still to do times the pace
        (seconds per byte) of the jobs this run finished, or, before the first one is done,
        of the running job so far. None while there is too little to go on (the first
        ninety seconds, or under two percent of the running job)."""
        now = time.time()
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

    @staticmethod
    def _fmt_left(secs: float) -> str:
        """'about 2 h 10 min left', rounded so the number does not twitch on every update."""
        if secs < 90:
            return "about a minute left"
        mins = int(round(secs / 60.0))
        if mins < 60:
            return f"about {mins} min left"
        mins = int(round(mins / 5.0)) * 5                  # five-minute steps beyond an hour
        h, m = divmod(mins, 60)
        return f"about {h} h {m:02d} min left" if m else f"about {h} h left"

    def _all_jobs_line(self) -> str:
        frac = getattr(self, "_all_frac", None)
        if frac is None:
            return ""
        eta = getattr(self, "_all_eta", None)
        left = self._fmt_left(eta) if eta is not None else "estimating the time left…"
        return f"All jobs  {int(frac * 100)} %   ·   {left}"

    def _show_all_progress(self) -> None:
        """The foot of the queue list while the queue runs: the share of the whole run that
        is done (left), the time it still needs (right), and a neutral bar."""
        try:
            frac = getattr(self, "_all_frac", None)
            if not self._batch_running or frac is None:
                if self._all_box.winfo_manager():
                    self._all_box.grid_remove()
                return
            eta = getattr(self, "_all_eta", None)
            left = f"All jobs  {int(frac * 100)} %"
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
        """The line under the queue header while Add job builds jobs; the run's summary at
        the foot of the list (_show_all_progress) is refreshed with it."""
        self._show_all_progress()
        st = getattr(self, "_add_state", None)
        try:
            if not st or st["total"] == 0:
                self._add_box.grid_remove()
                return
            n, total = st["done"], st["total"]
            last = st["made"][-1] if st["made"] else None
            name = (getattr(last, "display_name", None) or getattr(last, "name", "")) if last else ""
            self._add_label_var.set(f"Adding jobs…  {n} of {total}" + (f"   ·   {name}" if name else ""))
            self._add_bar.set(n / total if total else 0)
            self._add_box.grid()
        except Exception:
            pass

    def _drain_add_q(self) -> None:
        """Main loop: take the jobs the add worker has built so far into the queue."""
        q = getattr(self, "_add_q", None)
        st = getattr(self, "_add_state", None)
        if q is None or st is None:
            self._show_add_progress()          # the all-jobs line while the queue runs
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
                try:
                    if getattr(it, "archive_path", None):
                        self._resolve_archive_password(it)
                except Exception:
                    pass
            elif kind == "error":
                st["done"] += 1
                st["errors"] += 1
                self.log("WARN", f"Could not add {Path(str(src)).name}: {err}")
            else:                                   # one worker finished
                with self._scan_lock:
                    self._scan_in_flight = max(0, self._scan_in_flight - 1)
                if st["done"] >= st["total"]:
                    made = st["made"]
                    if made:
                        what = chain_summary(made[0]) + (f" × {len(made)}" if len(made) > 1 else "")
                        self.log("OK", f"Queued: {what} — {made[0].display_name or made[0].name}"
                                       + (f" (+{len(made) - 1} more)" if len(made) > 1 else "")
                                       + ".  Press ▶ START to run.")
                    self._add_state = {"total": 0, "done": 0, "made": [], "errors": 0}
                    if self.pending_start and self._scan_in_flight == 0:
                        self.pending_start = False
                        self.root.after(50, self.start)
        if added:
            self.update_queue_box(select_item=st["made"][-1] if st["made"] else None)
        self._show_add_progress()

    def _release_failed_copies(self, item) -> bool:
        """Before the next job of a batch: when it does not fit, delete the extracted copies
        that failed jobs kept for a retry, so a failure never costs the jobs after it their
        space. A copy kept after a cancel is the user's and stays. True when a reclaim
        started (the caller waits for it and checks the space again)."""
        kept = [it for it in self.queue
                if it is not item and getattr(it, "status", "") == "Failed" and getattr(it, "kept_extract", False)]
        if not kept or getattr(item, "operation", "pack") in ("unpack", "fake-sign", "fpkg-extract"):
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
        """A job leaves the queue: the extracted copy it kept after a cancel goes too."""
        if getattr(item, "kept_extract", False):
            item.kept_extract = False
            self._cleanup_item_extract(item)

    def _due_checks(self, item) -> bool:
        """Right before a job runs, on both start paths (start() for the first job of a run,
        _batch_auto_start() for the rest): read its archive set again, then settle an output
        that is already there, before an archive is unpacked when the game can be read
        (otherwise once it is unpacked). False when the job does not run now."""
        if not self._refresh_archive_set(item):
            return False
        due = self._late_output_check(item)
        if due == "cancel":
            self._batch_running = False
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Start cancelled.", "Ready", 0, 0, "00:00", "—", "—")
            return False
        if due == "skip":
            self._ensure_batch_started()        # the jobs after this one still run
            self._skip_late(item)
            return False
        return True

    def _refresh_archive_set(self, item) -> bool:
        """Right before an archive job runs: the parts of its set, their size on disk, and
        the unpacked size from the headers, read again when the set changed since the job
        was added or that size is still unknown. A set that cannot be read (a part missing,
        damaged) fails the job with that reason instead of unpacking half of it. False when
        the job was taken out of this run; the queue goes on with the next one."""
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
        self.status_update("Reading", f"Reading the headers of {first.name}…", "Scanning Files", 0, 0,
                           "00:00", "—", "—", side=True)
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
            note = f"The archive cannot be read: {problem}"
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
                self.status_update("Ready", f"Not started: {note}", "Ready", 0, 0, "00:00", "—", "—")
                self._queue_finished()
            return False
        return True                              # unknown: the extraction step reports what it finds

    def _rearm_from_archive(self, item) -> bool:
        """A job whose source was extracted from an archive, when that extracted copy is gone
        (a failed or cancelled run cleans it up): point it back at the archive, so the next
        run extracts it again instead of failing on a missing source. True when it did."""
        origin = getattr(item, "origin_archive", None)
        if not origin or getattr(item, "archive_path", None):
            return False
        src = getattr(item, "path", None)
        try:
            # A copy kept after a cancel is used again. Any other extracted copy in the app's
            # scratch is removed after a failed run (maybe right now), so never run from it; a
            # source outside the scratch that still exists is used as it is.
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
        item.bundle_siblings = []
        try:
            item.size = archive_set_ondisk_size(arc)
        except Exception:
            pass
        item.extracted_size = int(getattr(item, "origin_extracted_size", 0) or 0)
        item.pkg_content_size = 0           # read again from the package the archive yields
        self.log("INFO", f"{getattr(item, 'display_name', None) or item.name}: the extracted copy is gone; "
                         f"the job starts again from {arc.name}.")
        return True

    def _retire_failed(self, item, status: str = "Failed", reason: str = "") -> None:
        """Keep a failed/skipped item in the queue (marked, moved to the END) instead of
        discarding it — only SUCCESSFUL items leave the queue. Works whether the item is
        still in the queue (move it) or was already popped (re-add it). *reason* is shown
        in the details pane."""
        try:
            item.status = status
            item.status_note = " ".join(str(reason or "").split())[:300]
            if item not in self.queue:          # popped by an older path: back at the end
                self.queue.append(item)
        except Exception:
            pass

    def _batch_auto_start(self):
        """Start the next game in the queue — rechecks disk space before each game."""
        # Honor a cancel requested during the 600 ms advance gap (cancel_requested is
        # reset below, so a dropped cancel would otherwise silently keep the batch going).
        if self.cancel_requested or self.extract_cancel_event.is_set():
            self._batch_running = False
            self.cancel_requested = False
            self.extract_cancel_event.clear()
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Batch cancelled.", "Ready", 0, 0, "00:00", "—", "—")
            self.log("WARN", "Batch cancelled by user.")
            return
        # Wait for any in-flight scratch reclaim to finish before re-reading free space —
        # otherwise the gate sees a still-full drive and false-skips the next game. Cap the
        # wait generously (~10 min) for a slow exFAT rmtree of a 150-260 GB tree.
        if getattr(self, "_cleanup_inflight", 0) > 0:
            self._cleanup_wait_ticks += 1
            if self._cleanup_wait_ticks <= 1200:   # 1200 * 500 ms ≈ 10 min
                if self._cleanup_wait_ticks == 1:
                    self.status_update("Cleaning up", "Reclaiming temp space before the next game…",
                                        "Cleaning", 0, 0, "—", "—", "—")
                self.root.after(500, self._batch_auto_start)
                return
            self.log("WARN", "Cleanup still running past the wait cap — continuing; the space gate decides.")
        self._cleanup_wait_ticks = 0
        # Pause asked for: stop before the next job. The job in progress coming back here
        # (after its archive was unpacked, an out-of-memory retry) is not the next job.
        if (getattr(self, "_pause_requested", False) and self._next_pending() is not None
                and self._next_pending() is not getattr(self, "_active_item", None)):
            self._pause_queue()
            return
        # The next job that has not run, in list order (finished jobs keep their places).
        # When none remain, the batch is complete — failed/skipped items stay in the queue.
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

        # ── Space pre-flight gate — places the run on a drive sized for its real
        #    footprint, then go/skip/cancel. Skip cleans any partial scratch and keeps
        #    the batch moving; a big game that fits the HDD is routed there, not skipped.
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
                        kind = f"image on {temp_drive_label(Path(bt))} (spool auto-routed)"
                    else:
                        need = estimate_peak_space_needed(_sz, _peak_factor_for(item), same_drive(Path(bt), od))
                        kind = "full scratch"
                    self.log("INFO", f"Space check — {item.name}: {kind} on {bt} | "
                                     f"need ~{format_size(need)} | free {format_size(get_free_space(bt))} | {gate}")
                if gate == "cancel":
                    self._batch_running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self._update_batch_counter()
                    return
                if gate == "skip":
                    self._cleanup_after_failure(item)   # reclaim any partial scratch
                    self._retire_failed(item, "Skipped")   # keep it in the queue, marked
                    self._batch_failed += 1
                    self._update_batch_counter()
                    self.update_queue_box()
                    self.root.after(600, self._batch_auto_start)
                    return
        except Exception as e:
            self.log("WARN", f"Space pre-check skipped: {e}")

        # Patch job with an archive game / .7z patch — resolve to folders first, re-enter.
        if self._patch_needs_prepare(item):
            self._prepare_patch_item(item)
            return
        # Archive placeholder — extract first
        if getattr(item, "archive_path", None):
            self._extract_queued_item(item)
            return
        # The output of an unpacked archive is known now: is it there already?
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
        # Folder pack: keep Spotlight off the temp/image dir (the source folder is the
        # user's own — left indexable). Archives were marked in _extract_queued_item.
        self._mark_no_spotlight(getattr(item, "_build_temp", None))
        # AMPR/APR: inject the emu .sprx + build ampr_emu.index on the resolved game folder
        # (post-extraction for archives) before packing, so they ride into the .ffpfsc.
        # PACK ONLY (see start()).
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
        self.status_update(
            f"Game {current}/{self._batch_total}",
            f"Starting: {item.name}",
            "Scanning Files", 0, _floor, "00:00", "—", "—", job=item
        )
        self.log("INFO", f"── Batch auto-advance: game {current}/{self._batch_total} — {item.name}")
        self.cancel_requested = False
        self._active_item = item
        self.worker = CLIWorker(self, item, cmd, cwd, out_dir, temp_dir)
        self.worker.start()

    # ── After a job is done, after the queue is done ──────────────────────────
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
        """The game a job started from: its archive with every part, or its container file
        or game folder. Never the app's extracted copy."""
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
        """What a job's after-action works on: the game it started from plus a patch it integrated."""
        return self._uniq_paths(self._after_game_sources(item) + self._after_patch_sources(item))

    def _after_protected(self, item, dest=None) -> list:
        """Folders an after-action never moves or removes as a whole: where the job was added
        from, the output and destination folders, and the app's own."""
        out = [str(ultra_core.APP_DIR), getattr(item, "source_root", None), dest]
        try:
            out += [self.temp_var.get().strip(), self.output_var.get().strip(),
                    str(self._job_output_dir(item) or ""), (load_settings().get("last_source_dir") or "").strip()]
        except Exception:
            pass
        out += self._app_folders()
        return [x for x in out if x]

    def _app_folders(self) -> list:
        """Folders that are the app's own: its profile, and the scratch it makes under the
        temp folder, the temp pool and the output folder. A source never overlaps them."""
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
        """(action, sources, destination, reason to leave them) for *item*, which is Done."""
        aj = _after_job_module()
        act = getattr(item, "after_source", None) or aj.KEEP
        if act not in aj.ACTIONS or act == aj.KEEP:
            return aj.KEEP, [], None, None
        ps4 = getattr(item, "content_kind", "") in ("ps4", ORGANIZE_TARGET)   # library jobs (see _ps4_copy_mode)
        if getattr(item, "operation", "") in ("fake-sign", "copy") and not ps4:
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
            rel = aj.release_folder(game, list_sources=find_job_sources, title_id=self._item_title_id(item) or "",
                                    title=title, protected=keep_dirs, may_be=getattr(item, "source_root", None))
        whole = rel is not None and act in (aj.TRASH, aj.MOVE)
        targets = ([rel] if whole else game) + [p for p in patch
                                                if not (whole and str(os.path.realpath(p)).startswith(
                                                    str(os.path.realpath(rel)) + os.sep))]
        item._after_info = {"release": rel, "whole": whole, "keep_dirs": keep_dirs}
        return act, targets, dest, aj.refusal(targets, output=out, dest=dest, protected=self._app_folders(),
                                              others=others)

    def _run_after_job(self, item, worker, then) -> None:
        """Keep, trash, move or delete *item*'s source as the job says, then call *then*
        (which moves the queue on). A move across drives runs on a worker thread; the
        queue waits for it, so the copy and the next job do not share the drive."""
        aj = _after_job_module()
        try:
            act, srcs, dest, why = self._after_job_plan(item, worker)
        except Exception as e:
            act, srcs, dest, why = "error", [], None, str(e)
        item._after_result = ("keep", "")
        if act == aj.KEEP:
            then(); return
        name = getattr(item, "display_name", None) or item.name
        if why:
            item._after_result = ("refused", why)
            self.log("WARN", f"After the job: the source of {name} stays where it is: {why}.")
            then(); return
        size = aj.size_of(srcs)
        names = ", ".join(p.name for p in srcs)
        info = getattr(item, "_after_info", None) or {}
        what = "the source folder" if info.get("whole") else "the source"
        doing = {aj.TRASH: f"moving {what} to the Trash", aj.MOVE: f"moving {what} to {dest}",
                 aj.DELETE: "deleting the source"}[act]
        self.log("INFO", f"After the job: {doing}: {names} ({format_size(size)}).")
        results = queue.Queue()
        last = [-10]

        def progress(done, total):
            pct = int(done * 100 / total) if total else 100
            if pct >= last[0] + 10:
                last[0] = pct - pct % 10
                results.put(("PROGRESS", f"Moving the source: {last[0]}%"))

        def work():
            try:
                where = aj.apply(act, srcs, dest=dest, on_progress=progress)
                if act == aj.TRASH:
                    msg = (f"Moved {what} of {name} to the Trash ({format_size(size)}; it frees the space once "
                           f"the Trash is emptied): {names}.")
                    item._after_result = ("done", f"{what} was moved to the Trash.")
                elif act == aj.MOVE:
                    msg = f"Moved {what} of {name} to {dest}: " + ", ".join(Path(w).name for w in where) + "."
                    item._after_result = ("done", f"{what} was moved to {dest}.")
                else:
                    msg = f"Deleted the source of {name} ({format_size(size)} freed): {names}."
                    item._after_result = ("done", "the source was deleted.")
                results.put(("SUCCESS", msg))
                # Nothing empty left behind: a Delete takes the release folder too when only
                # notes, checksums or pictures are left in it; emptied folders above go.
                keep_dirs = info.get("keep_dirs") or []
                left = {Path(t).parent for t in srcs}
                rel = info.get("release")
                if act == aj.DELETE and rel is not None and aj.sweep_sidecars(rel, keep_dirs):
                    results.put(("INFO", f"Removed the release folder {rel.name}: only notes, checksums "
                                         f"or pictures were left in it."))
                    left.add(Path(rel).parent)
                gone = aj.prune_empty_dirs(left, keep_dirs)
                if gone:
                    results.put(("INFO", "Removed empty folder(s): " + ", ".join(str(g) for g in gone) + "."))
            except Exception as e:
                item._after_result = ("failed", str(e))
                results.put(("WARN", f"After the job: {name}: {e}. Whatever was not handled stays where it is."))
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

    def _settle_existing(self, item, hit, hit_note: str = "", then=None) -> None:
        """*item*'s output is already there (*hit*). With Move/Trash/Delete chosen, the source
        gets that now and the job is Done, as if it had run; with Keep, or when the action
        cannot run, it is Skipped. then(done) moves the queue on."""
        then = then or (lambda done: None)
        name = getattr(item, "display_name", None) or item.name
        item._output_there = True
        note = f"Its output is already there: {hit}" if hit else (hit_note or "Its output is already there.")
        self._retire_failed(item, "Skipped", note)
        act = getattr(item, "after_source", None) or "keep"
        if hit is None or act == "keep":
            self.log("INFO", f"{name}: skipped, {note[0].lower() + note[1:]}")
            then(False); return

        def after():
            res = getattr(item, "_after_result", None) or ("failed", "")
            if res[0] == "done":
                self._retire_failed(item, "Done", f"Its output was already there ({Path(hit).name}); {res[1]}")
                self.log("SUCCESS", f"{name}: its output was already there; {res[1]} Marked Done.")
                if self.auto_remove_done_var.get() and item in self.queue:
                    self.queue.remove(item)
                self.update_queue_box()
                then(True)
            else:
                self._retire_failed(item, "Skipped", f"{note}; the source stays: {res[1]}")
                self.update_queue_box()
                then(False)
        import types as _t
        self._run_after_job(item, _t.SimpleNamespace(output_path=str(hit), _is_copy=False, validate_failed=False), after)

    def _publish_patch_backup(self, item, worker, then) -> None:
        """A patched job is done: move the folder of original files the patch replaced (with
        its README) from the temp drive to beside the output, then call *then*. Runs on a
        worker thread; the queue waits for it like for the after-job action."""
        staged = self._staged_patch_backup(worker)
        out = (getattr(worker, "output_path", "") or "").strip('"') if worker is not None else ""
        if staged is None or not out:
            then(); return
        out_p = Path(out)
        dest_dir = out_p.parent                  # beside the file (or the output folder)
        dest = dest_dir / staged.name
        n = 2
        while dest.exists():
            dest = dest_dir / f"{staged.name} ({n})"
            n += 1
        results = queue.Queue()

        def work():
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged), str(dest))
                if staged.parent.name.startswith("patch-backup-"):
                    shutil.rmtree(staged.parent, ignore_errors=True)
                results.put(("INFO", f"Kept the original files the patch replaced, with a README, in {dest}."))
            except Exception as e:
                results.put(("WARN", f"The original files the patch replaced could not be moved beside the "
                                     f"output ({e}); they are still in {staged}."))
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
        """The folder the backend staged a patch's originals in, only when it is exactly
        that: an absolute path to an existing folder inside a 'patch-backup-…' folder under
        the app's _ffpfsc_temp scratch. Anything else (an empty line read as '.', a
        stray path) is never moved or removed."""
        raw = str(getattr(worker, "patch_backup", "") or "").strip() if worker is not None else ""
        if not raw:
            return None
        p = Path(raw)
        if (not p.is_absolute() or not p.parent.name.startswith("patch-backup-")
                or "_ffpfsc_temp" not in p.parts or not p.is_dir()):
            return None
        return p

    def _drop_patch_backup(self, worker) -> None:
        """A failed or cancelled job: its staged originals are not needed."""
        p = self._staged_patch_backup(worker)
        if p is not None:
            shutil.rmtree(p.parent, ignore_errors=True)
        if worker is not None and getattr(worker, "patch_backup", ""):
            worker.patch_backup = ""

    def _notify_job(self, item, ok: bool, detail: str = "") -> None:
        if self.notify_var.get() != "job" or item is None:
            return
        name = getattr(item, "display_name", None) or item.name
        _after_job_module().notify(name, "Done" if ok else f"Failed: {detail}"[:180])

    def _queue_finished(self) -> None:
        """The run ended on its own, not by Stop: the queue banner, then Sleep or Quit."""
        aj = _after_job_module()
        total, done, fail = self._batch_total, self._batch_done, self._batch_failed
        if self.notify_var.get() == "queue" or (self.notify_var.get() == "job" and total > 1):
            aj.notify("PS5 UltraPack", f"Queue finished: {done} of {total} done" + (f", {fail} failed" if fail else ""))
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
            f"Queue finished  ·  {done} of {total} done" + (f"  ·  {fail} failed" if fail else "")
        )
        msg = (
            f"Batch finished.\n\n"
            f"Total items:  {total}\n"
            f"Successful:   {done}\n"
            f"Failed:       {fail}\n"
        )
        if fail == 0:
            self.log("SUCCESS", f"Batch complete — all {total} item(s) processed successfully.")
        else:
            self.log("WARN", f"Batch complete — {done}/{total} succeeded, {fail} failed.")
        if self.after_queue_var.get() not in ("sleep", "quit"):   # a modal box would hold them up
            messagebox.showinfo("Batch Complete", msg)

    def _ensure_batch_started(self):
        """Establish batch state for a FRESH queue run (idempotent via the _batch_running
        guard). MUST run before the first item is processed — including before an archive
        extraction or patch-prepare, which return early from start() — so that a FIRST-item
        failure advances the queue (the done/extract error handlers only continue while
        _batch_running is True) instead of aborting the whole queue. Re-entry after a
        successful archive extraction (the _extract_q 'ok' branch calls start() again) is a
        no-op thanks to the guard, preserving running progress."""
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
            # Size-weighted total progress: a 187 GB game advances the queue bar far more
            # than a 35 GB one (games finish in queue order; done-bytes = sum of first _done).
            try:
                self._batch_sizes = [max(0, int(display_size(it) or 0)) for it in todo]
            except Exception:
                self._batch_sizes = []
            self._batch_items = list(todo)   # by job, not by place: the list can be reordered
            # For the estimate over all jobs: when the run began, and what the jobs that
            # finished took (seconds and bytes) — their pace is the best guess for the rest.
            self._batch_t0 = time.time()
            self._batch_fin_secs, self._batch_fin_bytes = 0.0, 0
            self._job_t0 = {}
            self._all_frac, self._all_eta = None, None
        self._update_batch_counter()

    # ── Start / Cancel ────────────────────────────────────────────────────────
    def start(self, rearm_failed: bool = True):
        """Run the queue. A fresh Start also re-runs failed, skipped and cancelled jobs;
        Retry passes rearm_failed=False to run only the job it re-armed."""
        if not self._batch_running and (getattr(self, "_add_state", None) or {}).get("total"):
            # Add job is still building jobs: start once the last one is in the queue.
            self.pending_start = True
            self.status_update("Adding", "Adding the jobs, then starting…", "Scanning Files",
                               0, 0, "00:00", "—", "—", side=True)
            self.log("INFO", "Start: the queue starts as soon as every new job has been added.")
            return
        if not self.output_var.get().strip():
            messagebox.showerror("Missing output", "Select an output folder.")
            return
        if not self.temp_var.get().strip():
            self.temp_var.set(str(Path(self.output_var.get()) / "_ffpfsc_temp"))
        if not self.queue:
            # A source-scan from a just-pressed Add may still be running (queue is briefly
            # empty during an async folder scan). Re-adding here would scan the SAME source
            # again → a duplicate queue item. Just arm pending_start; the in-flight scan's
            # result handler will honor it once the item lands.
            if getattr(self, "_scan_in_flight", 0) > 0:
                self.pending_start = True
                self.status_update("Scanning", "Finishing the source scan, then starting…",
                                    "Scanning Files", 0, 0, "00:00", "—", "—")
                return
            # Nothing queued: do not fall back to the last-used source path (it is not
            # shown anywhere) — just say so.
            self.log("INFO", "Nothing to start — add a game folder, archive or image first.")
            self.status_update("Idle", "Queue is empty.", "", 0, 0, "00:00", "—", "—")
            return

        # A FRESH Start re-arms previously failed/skipped/cancelled jobs so they're retried
        # (within a run they stay skipped to avoid an endless loop; pressing Start again
        # retries them). The user deletes any they don't want. NOT on the internal re-entry
        # after an archive extraction — that's mid-batch (_batch_running is True then).
        if not self._batch_running and rearm_failed is not False:
            requeued = 0
            for it in self.queue:
                if getattr(it, "status", "") in ("Failed", "Skipped", "Cancelled"):
                    self._rearm_from_archive(it)
                    it.status = "Pending Extract" if getattr(it, "archive_path", None) else "Queued"
                    requeued += 1
            if requeued:
                self.log("INFO", f"Retrying {requeued} previously failed/skipped job(s).")
                self.update_queue_box()

        # Outputs that are already there: one question for all, before anything is unpacked.
        if not self._batch_running and not self._check_existing_outputs():
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            return

        # Run the first NOT-yet-run item from the top; finished, failed and skipped jobs keep
        # their places. If everything left is terminal, there's nothing to start.
        if self._next_pending() is None:
            self.log("INFO", "Nothing to start — every job in the queue has run. Clear completed removes "
                             "the finished ones; Retry runs a failed one again." if self.queue
                     else "Nothing to start — the queue is empty.")
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            return
        item = self._pick_next()

        # An archive's set may have changed since the job was added (a download that was
        # still running): read it again, so placement and the space gate use real numbers.
        if not self._due_checks(item):
            return

        # ── Pre-flight space gate FIRST — place the run on a drive sized for its real
        #    footprint and decide go/skip/cancel BEFORE any extraction or packing, so a
        #    too-big archive is judged on its true extracted size (from headers) rather
        #    than extracted onto a full SSD and only then failing. ─────────────────────
        gate = self._space_gate(item, self._job_output_dir(item) or Path(self.output_var.get().strip()))
        if gate == "cancel":
            if self._batch_running:
                self._batch_running = False
            # Always return to idle — start() may run after extraction (buttons left
            # in the running state), so a single-game cancel here must reset them too.
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Drive check cancelled.", "Ready", 0, 0, "00:00", "—", "—")
            return
        if gate == "skip":
            # Reclaim any scratch this item already wrote (e.g. an archive extracted
            # before the post-extraction re-gate), then skip.
            self._cleanup_after_failure(item)
            if self._batch_running:
                self._retire_failed(item, "Skipped")   # keep it in the queue, marked
                self._batch_failed += 1
                self._update_batch_counter()
                self.update_queue_box()
                self.root.after(600, self._batch_auto_start)
            else:
                # Single run: reset to idle (Start was disabled / Cancel enabled by the
                # extraction step) and mark the item, so the UI isn't left frozen.
                item.status = "Skipped"
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self.update_queue_box()
                self.status_update("Ready", f"Skipped — low space: {item.name}", "Ready", 0, 0, "00:00", "—", "—")
            return

        # ── Patch job with an archive game / .7z patch — resolve to folders first ────
        if self._patch_needs_prepare(item):
            self._ensure_batch_started()   # mark the batch running BEFORE the early return
            self._prepare_patch_item(item)
            return
        # ── Archive placeholder — extract first, then compress (re-gates after) ──────
        if getattr(item, "archive_path", None):
            self._ensure_batch_started()   # so a first-item extraction failure advances,
            self._extract_queued_item(item)  # not aborts, the rest of the queue
            return

        # The output of an unpacked archive is known now: is it there already?
        _late = self._late_output_check(item)
        if _late == "cancel":
            self._cleanup_after_failure(item)
            self._batch_running = False
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Start cancelled.", "Ready", 0, 0, "00:00", "—", "—")
            return
        if _late == "skip":
            self._skip_late(item)
            return
        # Folder pack: keep Spotlight off the temp/image dir (the source folder is the
        # user's own — left indexable). Archives were marked in _extract_queued_item.
        self._mark_no_spotlight(getattr(item, "_build_temp", None))
        # AMPR/APR: inject the emu .sprx + build ampr_emu.index on the resolved game folder
        # (post-extraction for archives) before packing, so they ride into the .ffpfsc.
        # PACK ONLY: an fPKG is installed natively, the ShadowMount PlayGo emu has no
        # business inside the package.
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
                # Count ALL leftover temp data — mkpfs tmp* working dirs AND old
                # _extracted game trees — but never the CURRENT item's own source,
                # which legitimately lives under <temp>/_extracted right now.
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
                    elif _is_app_tmp_dir(p.name):
                        stale_items.append(p)
                if stale_items:
                    stale_size = sum(folder_size(p) for p in stale_items)
                    if stale_size > 1024 * 1024 * 1024:
                        keep_running = messagebox.askyesno(
                            "Temporary data found",
                            f"Found {format_size(stale_size)} of old temporary data in:\n{temp_dir}\n\n"
                            "Continue anyway?\n\nChoose No to manually delete old temp folders first."
                        )
                        if not keep_running:
                            return
        except Exception:
            pass

        # Establish batch state for a fresh start (idempotent; archive/patch items already
        # did this before their early return, and a re-entry after extraction is a no-op).
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
        self.status_update(label, "Launching backend.", "Starting", 0, _floor, "00:00", "—", "—", job=item)
        self._active_item = item
        self.worker = CLIWorker(self, item, cmd, cwd, out_dir, temp_dir)
        self.worker.start()

    def cancel(self):
        self.cancel_requested = True
        self.extract_cancel_event.set()
        # Immediate, main-thread visual feedback — don't wait for the ~100 ms poll tick,
        # and repaint the big status + footer + button NOW so it isn't masked by the
        # worker's still-queued progress updates (the "Cancel does nothing" symptom).
        try:
            self.cancel_btn.configure(state="disabled")
            self.stop_btn.configure(state="disabled")
            self.big_status_var.set("Cancelling…")
            self.big_detail_var.set("Stopping the current job and reclaiming temp space…")
            self.footer_var.set("● Cancelling…")
            self.header_status_var.set(f"v{APP_VERSION}  |  Cancelling…")
            self.root.update_idletasks()
        except Exception:
            pass
        _kill_process_tree(self.current_process)
        self.status_update("Cancelling", "Cancel requested — stopping…", "Cancelling", 0, 0, "—", "—", "—")

    def status_update(self, title, detail, stage, stage_pct, overall_pct, elapsed, speed, eta,
                      job=None, side=False):
        """Queue a status for the panel. *job* is the queue item the progress belongs to:
        its queue bar then never moves backward. A *side* message (a source scan, "3 games
        added") shows only in the footer while a job runs, so it cannot reset that job's
        bars to 0 %."""
        if stage == "Creating Temp PFS" and stage_pct >= 100:
            stage_pct = 99
        self.status_q.put((title, detail, stage, stage_pct, overall_pct, elapsed, speed, eta, job, side))

    def _begin_job_progress(self, item, floor: float = 0.0):
        """A job (re)starts: its queue bar starts again from *floor*."""
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

    # ── Tools ─────────────────────────────────────────────────────────────────
    def clear_temp_files(self):
        tp = self.temp_var.get().strip()
        if not tp:
            messagebox.showerror("No temp folder", "No temp folder is set.")
            return
        temp_dir = Path(tp)
        if not temp_dir.exists():
            messagebox.showinfo("Clear Temp", "Temp folder does not exist. Nothing to clear.")
            return
        # Only this app's own scratch folders — never everything inside a folder the
        # user may have pointed at a drive root or a shared directory.
        def _ours(p: Path) -> bool:
            return p.is_dir() and (p.name in ("_extracted", "_ffpfsc_extract", "_ffpfsc_inner",
                                              "_patch_game", "_patch_files")
                                   or _is_app_tmp_dir(p.name) or p.name.startswith("ffpfsc_spool_"))
        try:
            targets = [p for p in temp_dir.iterdir() if _ours(p)]
        except Exception as e:
            messagebox.showerror("Error", f"Cannot read the temp folder:\n{e}")
            return
        size = sum(get_folder_size(p) for p in targets)
        if not targets or size == 0:
            messagebox.showinfo("Clear Temp", "No working data of this app in the temp folder.")
            return
        listing = "\n".join(f"  • {p.name}" for p in targets[:8]) + ("\n  • …" if len(targets) > 8 else "")
        ok = messagebox.askyesno(
            "Clear Temp Files",
            f"Delete this app's working data inside:\n{temp_dir}\n\n{listing}\n\n"
            f"Size to free: {format_size(size)}\n\n"
            "Other files in that folder are left alone. Continue?"
        )
        if not ok:
            return
        try:
            for p in targets:
                shutil.rmtree(str(p), ignore_errors=True)
            messagebox.showinfo("Clear Temp", f"Temp folder cleared. Freed {format_size(size)}.")
            self.log("OK", f"Temp folder cleared: {temp_dir} ({format_size(size)} freed)")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to clear temp folder:\n{e}")

    def export_diagnostics(self):
        zip_path = export_diagnostic_zip(last_cmd=self._last_cmd_str)
        if zip_path and zip_path.exists():
            ok = messagebox.askyesno(
                "Diagnostic Package",
                f"Diagnostic ZIP saved to:\n{zip_path}\n\nOpen folder?"
            )
            if ok:
                open_path(APP_DIR)
        else:
            messagebox.showerror("Error", "Failed to create diagnostic ZIP.")

    # ── History & Statistics ──────────────────────────────────────────────────
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

    # ── Sound + Summary ───────────────────────────────────────────────────────
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
            report = "Compression complete."
        if "Operation: Extract PFS image" not in report:
            report += (
                "\n\nNote: Compression does not improve FPS or graphics quality. "
                "If the rating is POOR, keep the original uncompressed folder instead."
            )
        self._last_result_text = report
        SummaryDialog(self.root, report)

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
        # Prefer the actual result location — a bundle lands in its own subfolder, not
        # directly in the chosen output dir — then fall back to the output folder.
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

    # ── Stage display ─────────────────────────────────────────────────────────
    def update_stages_display(self, current_stage, pct):
        full_names   = [s[0] for s in _STAGE_DEFS]
        current_idx  = full_names.index(current_stage) if current_stage in full_names else -1
        # Only tick a prior stage done if it was ACTUALLY entered: pack and unpack use
        # disjoint subsets of _STAGE_DEFS, so a raw index check would mark e.g.
        # "Compress" done during an unpack or "Extract" done during a pack.
        sp = getattr(self.worker, "stage_progress", {}) if getattr(self, "worker", None) else {}
        # Only the stations this job goes through: the ones it has entered, the current one,
        # and what is still ahead (Extract only happens first; Verify only when switched on).
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

    # ── Poll loop ─────────────────────────────────────────────────────────────
    def _tick_elapsed(self):
        """Called every poll cycle while a worker is live — keeps elapsed ticking
        regardless of whether the backend is printing anything."""
        w = getattr(self, "worker", None)
        if w and w.is_alive() and w.start_time:
            self.elapsed_var.set(f"Elapsed: {format_duration(time.time() - w.start_time)}")

    def _update_ram_meter(self):
        """Refresh the RAM readout in the status bar (plain <70 %, amber <85 %, red above)."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            avail_gb = mem.available / 1024**3
            total_gb = mem.total / 1024**3
            pct = mem.percent
            tok = "faint" if pct < 70 else "warning" if pct < 85 else "danger"
            self.ram_var.set(f"RAM: {avail_gb:.1f} of {total_gb:.0f} GB free")
            try:
                self._ram_label.configure(fg=self.kit.c(tok))
            except Exception:
                pass
        except Exception:
            # psutil missing or unavailable — hide the meter rather than error.
            try:
                self.ram_var.set("")
            except Exception:
                pass

    def _poll(self):
        try:
            self._poll_inner()
        except Exception as e:
            # Last-resort catch — log and keep the loop alive no matter what.
            try:
                self.log("ERROR", f"[_poll crash — loop kept alive] {e}")
            except Exception:
                pass
        self.root.after(200, self._poll)

    def _poll_inner(self):
        self._tick_elapsed()
        # Refresh the RAM meter ~every 2 s (poll runs every 200 ms → every 10th tick).
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
                self.update_queue_box(select_item=_sel)
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
                    tail = (f" ({skipped} already queued, done or in the output folder)" if skipped else "")
                    if not sources:
                        msg = (f"Nothing new in {folder}{tail}." if scanned
                               else f"Rescan found no sources in {folder}.")
                        self.log("INFO", msg)
                        self.status_update("Ready", msg, "Ready", 0, 0, "00:00", "—", "—", side=True)
                    else:
                        self.log("OK", f"Rescan: {len(sources)} new source(s) in {folder}{tail}.")
                        self._add_jobs_async(list(sources), self._rescan_make(dict(tpl, _rescan_root=folder)))
                    continue
                if status == "rescan-error":
                    self.log("ERROR", f"Rescan failed: {payload}")
                    self.status_update("Error", f"Rescan failed: {payload}", "Error", 0, 0, "00:00", "—", "—", side=True)
                    continue
                if status == "archive":
                    item = payload
                    self.queue.append(item)
                    self.update_queue_box(select_item=item)
                    self.log("OK", f"Archive queued: {item.archive_path.name}  [{format_size(item.size)}]")
                    self.status_update("Ready",
                                        f"Archive queued — will extract when compression starts: {item.archive_path.name}",
                                        "Ready", 0, 0, "00:00", "—", "—", side=True)
                    # If the header is encrypted and no saved password worked, ask now — so
                    # the auto routing knows the real extracted size and can use the SSD.
                    self._resolve_archive_password(item)
                    self.update_queue_box(select_item=item)
                    if self.pending_start:
                        self.pending_start = False
                        self.start()
                elif status == "ok":
                    item = payload
                    self.queue.append(item)
                    self.update_queue_box(select_item=item)   # applies the remembered format (may make it an fPKG job)
                    self.status_update("Ready", f"{item.title_id} added to queue.",
                                        "Ready", 0, 0, "00:00", "—", "—", side=True)
                    _as = "  → fPKG (.pkg)" if getattr(item, "operation", "pack") == "fpkg-build" else ""
                    self.log("OK", f"Added {item.title_id} | {item.name} | {format_size(item.size)}{_as}")
                    if self.pending_start:
                        self.pending_start = False
                        self.start()

                elif status == "bundles":
                    # Library scan: one bundle per game subfolder, each mirrored.
                    items: list = payload
                    count = len(items)
                    preview = "\n".join(f"  • {getattr(it, 'bundle_subfolder', it.name)}" for it in items[:12])
                    if count > 12:
                        preview += f"\n  … and {count - 12} more"
                    proceed = count == 1 or messagebox.askyesno(
                        f"Found {count} Games",
                        f"Found {count} game folder(s):\n\n{preview}\n\n"
                        f"Convert all {count}? Each keeps its own folder at the destination, "
                        "with any extras/DLCs next to the .ffpfsc."
                    )
                    if proceed:
                        for it in items:
                            self.queue.append(it)
                        self.update_queue_box(select_item=items[0] if items else None)
                        self.log("OK", f"Queued {count} game folder(s) — each mirrored at the destination.")
                        self.status_update("Ready", f"{count} game(s) added to queue.",
                                            "Ready", 0, 0, "00:00", "—", "—", side=True)
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "Batch add cancelled.", "Ready", 0, 0, "00:00", "—", "—", side=True)
                        self.pending_start = False

                elif status == "multi_found":
                    # Multiple extracted game folders discovered
                    games: list = payload
                    count = len(games)
                    preview = "\n".join(f"  • {g.name}" for g in games[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = messagebox.askyesno(
                        f"Found {count} Games",
                        f"Found {count} PS5 game folder(s):\n\n{preview}\n\n"
                        f"Add all {count} to the queue?"
                    )
                    if ok:
                        self.log("INFO", f"Queuing {count} games…")
                        self.status_update("Scanning", f"Adding {count} games to queue…",
                                            "Scanning Files", 0, 0, "00:00", "—", "—", side=True)
                        def _add_all(paths=games):
                            for gpath in paths:
                                try:
                                    self.scan_q.put(("ok", GameItem(gpath)))
                                except Exception as e:
                                    self.log("ERROR", f"Skipped {gpath.name}: {e}")
                        threading.Thread(target=_add_all, daemon=True).start()
                    else:
                        self.status_update("Ready", "Batch add cancelled.", "Ready", 0, 0, "00:00", "—", "—", side=True)
                        self.pending_start = False

                elif status == "exfat_found":
                    # Multiple .exfat / .ffpkg disk images found — no extraction needed, queue directly
                    image_list: list = payload
                    count = len(image_list)
                    preview = "\n".join(f"  • {f.name}" for f in image_list[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = messagebox.askyesno(
                        f"Found {count} Disk Image{'s' if count > 1 else ''}",
                        f"Found {count} disk image(s) (.exfat / .ffpkg):\n\n{preview}\n\n"
                        f"Add all {count} to the queue?\n"
                        "(Each image will be compressed directly — no extraction needed.)"
                    )
                    if ok:
                        for img in image_list:
                            item = GameItem.from_exfat(img)
                            self.queue.append(item)
                            lbl = "exFAT" if img.suffix.lower() == ".exfat" else "ffpkg"
                            self.log("OK", f"{lbl} image queued: {img.name}")
                        self.update_queue_box()
                        self.status_update("Ready", f"{count} disk image(s) added to queue.",
                                            "Ready", 0, 0, "00:00", "—", "—", side=True)
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "Image add cancelled.", "Ready", 0, 0, "00:00", "—", "—", side=True)
                        self.pending_start = False

                elif status == "archives_found":
                    # Archive files found inside a scanned folder — queue as placeholders
                    archives: list = payload
                    count = len(archives)
                    preview = "\n".join(f"  • {a.name}" for a in archives[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = messagebox.askyesno(
                        f"Found {count} Archive{'s' if count > 1 else ''}",
                        f"Found {count} archive file(s):\n\n{preview}\n\n"
                        f"Add all {count} to the queue?\n"
                        "(Each archive will be extracted when it is its turn.)"
                    )
                    if ok:
                        added = []
                        for arc in archives:
                            item = GameItem.from_archive(arc)
                            self.queue.append(item)
                            added.append(item)
                            self.log("OK", f"Archive queued: {arc.name}")
                        self.update_queue_box()
                        # For each just-added archive whose header could not be read,
                        # ask the user for its password — so the routing decision sees
                        # the real extracted size up front.
                        for it in added:
                            self._resolve_archive_password(it)
                        self.update_queue_box()
                        self.status_update("Ready", f"{count} archive(s) added to queue.",
                                            "Ready", 0, 0, "00:00", "—", "—", side=True)
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "Archive add cancelled.", "Ready", 0, 0, "00:00", "—", "—", side=True)
                        self.pending_start = False

                elif status == "pfs_found":
                    images: list = payload
                    count = len(images)
                    preview = "\n".join(f"  • {f.name}" for f in images[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = messagebox.askyesno(
                        f"Found {count} PFS Image{'s' if count > 1 else ''}",
                        f"Found {count} .ffpfs/.ffpfsc image(s):\n\n{preview}\n\n"
                        f"Add all {count} to the queue for extraction?"
                    )
                    if ok:
                        for image in images:
                            item = GameItem.from_pfs_image(image)
                            self.queue.append(item)
                            self.log("OK", f"PFS image queued for extraction: {image.name}")
                        self.update_queue_box()
                        self.status_update("Ready", f"{count} PFS image(s) added to queue.",
                                            "Ready", 0, 0, "00:00", "—", "—", side=True)
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "PFS image add cancelled.", "Ready", 0, 0, "00:00", "—", "—", side=True)
                        self.pending_start = False

                elif status == "cancelled":
                    self.pending_start = False
                    self._pending_fpkg_identity = None
                    self._batch_running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.status_update("Ready", str(payload), "Ready", 0, 0, "00:00", "—", "—")

                else:  # "error"
                    self.pending_start = False
                    self._pending_fpkg_identity = None   # the item it was meant for never landed
                    messagebox.showerror("Scan failed", str(payload))
        except queue.Empty:
            pass

        # Capture whether the log is scrolled to the bottom BEFORE inserting — yview()
        # read AFTER an insert always reports < 1.0 (the content grew but the view hasn't
        # moved yet), so checking it post-insert would never re-follow during active
        # logging. Empty/short logs read (0.0, 1.0) → treated as "at bottom" → follow.
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

            # Auto-scroll to the newest line — but only when the user was already at the
            # bottom (captured above), so scrolling up to read older output isn't yanked back.
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
                # Lower bar = CURRENT STEP: the progress of the operation running right now
                # (extract / read / temp-PFS / compress / write …) = stage_pct. The game name
                # sits above as context; whole-game progress feeds the QUEUE bar below.
                _ri = self._running_item()
                _gname = ((getattr(_ri, "display_name", None) or _ri.name) if _ri is not None else "") or ""
                self.cur_game_var.set(f"CURRENT STEP  ·  {_gname}" if _gname else "CURRENT STEP")
                self.stage_title_var.set(stage or title)
                self.stage_detail_var.set(detail)
                self.stage_pct_var.set(f"{int(stage_pct)}%")
                self.stage_bar.set(max(0, min(1, stage_pct / 100)))
                # Upper bar = QUEUE: TOTAL progress over the whole batch — finished games plus
                # the current game's fraction. SIZE-weighted when we have the size snapshot
                # (so a 187 GB game moves the bar far more than a 35 GB one and it does NOT
                # just mirror the current game); falls back to equal-weight game count.
                _total = max(1, getattr(self, "_batch_total", 1))
                _done  = getattr(self, "_batch_done", 0) + getattr(self, "_batch_failed", 0)
                # By job, not by place in the list (it can be reordered): the jobs of this
                # batch that have finished, plus the running one's share.
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
                        self._all_eta = self._estimate_left(_left, _cur, overall_pct, _ri)
                    except Exception:
                        self._all_eta = None
                    self._update_batch_counter()
                self.overall_pct_var.set(f"{int(_qfrac * 100)}%")
                self.overall_bar.set(_qfrac)
                self.overall_title_var.set(
                    f"QUEUE  ·  Game {min(_done + 1, _total)}/{_total}" if _total > 1 else "QUEUE")
                self.speed_var.set(f"Speed: {speed}")
                self.elapsed_var.set(f"Elapsed: {elapsed}")
                self.eta_var.set(f"ETA: {eta}")
                self.header_status_var.set(f"v{APP_VERSION}  |  Stage: {stage}")
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

        # ── Archive extraction completion ─────────────────────────────────────
        try:
            status, payload = self._extract_q.get_nowait()
            if status == "ok":
                if isinstance(payload, tuple):
                    item, extra_items = payload
                else:
                    item, extra_items = payload, []
                if extra_items and getattr(item, "operation", "pack") == "chain":
                    for extra in extra_items:
                        self._as_chain_job(extra, item)
                elif extra_items and getattr(item, "operation", "pack") == "fpkg-build":
                    # A multi-game archive queued as an fPKG job: every extra game becomes an
                    # fPKG job with the same COMPRESSION settings only — identity is per game
                    # and comes from each game's param.json at build time. Main thread here.
                    _tpl = self._fpkg_compression_of(item)
                    for extra in extra_items:
                        self._as_fpkg_job(extra, _tpl)
                        extra.output_path = getattr(item, "output_path", None)
                if extra_items:
                    try:
                        idx = self.queue.index(item)
                    except ValueError:
                        idx = 0
                    for offset, extra in enumerate(extra_items, start=1):
                        self.queue.insert(idx + offset, extra)
                    self.log("OK", f"Queued {len(extra_items)} additional payload item(s) from the archive")
                # Item was updated in-place — clear cache so details panel refreshes fully
                self._details_item   = None
                self._loaded_art_key = None
                self.update_queue_box(select_item=item)
                self.log("OK", f"Extraction complete: {item.name}  [{format_size(item.size)}]")
                # Continue into compression now that the item has a real path
                self.start()
            elif status == "cancelled":
                # Cancel = STOP the batch (don't advance). Clean the partial extract of
                # the item being unpacked (it stays in the queue, marked Cancelled).
                _ci = self._active_item
                if _ci is not None and _ci in self.queue:
                    self._cleanup_after_failure(_ci)
                    _ci.status = "Cancelled"
                self._batch_running = False
                self.pending_start = False
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self.update_queue_box()
                self.status_update("Ready", str(payload), "Ready", 0, 0, "00:00", "—", "—")
                self.log("WARN", str(payload))
            else:
                # Extraction failed — clean the partial tree, KEEP the item in the queue
                # (marked Failed, moved to the end) so only successful items disappear, then
                # continue or end the batch like a pack failure.
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

            # Mark current game done/failed and pop from queue. When the queue is already
            # empty (single-game / last-in-batch) fall back to the tracked active item so
            # a failure still cleans its scratch (otherwise a single-game failure strands).
            completed_item = self._active_item
            if completed_item is not None and completed_item in self.queue:
                completed_item.status = "Done" if success else "Failed"

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
                self.status_update("Complete", msg, "Complete", 100, 100, "—", "—", "—")
                self.log("SUCCESS", msg)
                self.play_complete_sound(True)

                _final_sz = getattr(self.worker, "final_size", 0) if self.worker else 0
                completed_operation = getattr(completed_item, "operation", "pack") if completed_item else "pack"
                # History applies only to jobs that PRODUCE a .ffpfsc (pack, patch).
                # Unpack and fake-sign create no packed game → skip them.
                if completed_operation not in ("unpack", "fake-sign", "fpkg-extract", "copy"):
                    # Record history HERE (main thread) — add_history mutates Tk widgets.
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

                # Feature 5: auto-clear temp after success
                if self.auto_clear_temp_var.get():
                    self._auto_clear_temp()
                # Always reclaim THIS item's extracted source — covers the spread-mode
                # extract on the OUTPUT drive, which _auto_clear_temp (temp only) misses.
                _done_worker = self.worker
                if completed_item is not None:
                    self._cleanup_item_extract(completed_item)
                    self._cleanup_inner_image(completed_item)   # drop the pass-1 inner cache
                    self._ampr_cleanup(completed_item)   # restore a direct source folder
                    self._notify_job(completed_item, True)
                    if self.auto_remove_done_var.get():
                        if completed_item in self.queue:
                            self.queue.remove(completed_item)
                    else:
                        self._retire_failed(completed_item, "Done")   # stays in its place, marked Done

                def _advance():
                    # Feature 4: batch auto-advance (the next PENDING item; kept failed/skipped
                    # items don't count as work left to do).
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

                # The originals a patch replaced go beside the output, then what the job says
                # happens to its source (keep, Trash, move, delete), then on.
                def _after_backup():
                    if completed_item is not None:
                        self._run_after_job(completed_item, _done_worker, _advance)
                    else:
                        _advance()
                self._publish_patch_backup(completed_item, _done_worker, _after_backup)

            elif self.cancel_requested or self.extract_cancel_event.is_set():
                # A user cancel surfaces here as a failed result — treat it as a cancel,
                # not a failure: stop the batch, KEEP the item in the queue (marked
                # Cancelled, moved to the end) so only successful items disappear, don't
                # inflate the failed count. _retire_failed re-adds the popped item.
                self._batch_running = False
                self.cancel_requested = False
                self.extract_cancel_event.clear()
                self._drop_patch_backup(self.worker)
                if completed_item is not None:
                    keep = self._extract_is_complete(completed_item) and not getattr(self.worker, "consumed", False)
                    self._cleanup_after_failure(completed_item, keep_source=keep)
                    self._cleanup_inner_image(completed_item)   # no resume after a cancel
                    self._retire_failed(completed_item, "Cancelled")
                    completed_item.kept_extract = keep
                    if keep:
                        own = self._extract_dir_for_item(completed_item)
                        self.log("INFO", f"Kept the extracted copy in {own} on {_drive_name(own)}: Start runs the job "
                                         f"from it again, and removing the job from the queue deletes it.")
                self.update_queue_box()
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self._update_batch_counter()
                self.status_update("Ready", "Cancelled by user.", "Ready", 0, 0, "00:00", "—", "—")
                self.log("WARN", "Cancelled by user.")
            else:
                # OOM auto-retry: if the backend was out-of-memory-killed and we can still
                # drop a core, requeue the SAME game with fewer workers instead of failing.
                if (self.worker is not None and getattr(self.worker, "oom_killed", False)
                        and completed_item is not None and self._oom_retry(completed_item)):
                    return
                self._batch_failed += 1
                self.status_update("Failed", msg, "Failed", 0, 0, "—", "—", "—")
                self.log("ERROR", msg)
                self.play_complete_sound(False)
                self._drop_patch_backup(self.worker)
                self._notify_job(completed_item, False, msg)
                if completed_item is not None:
                    # A finished extraction stays, as after a cancel: Edit and Retry run the
                    # job from it again instead of unpacking the archive a second time. A
                    # later job that needs the space frees it (_release_failed_copies).
                    keep = self._extract_is_complete(completed_item) and not getattr(self.worker, "consumed", False)
                    self._cleanup_after_failure(completed_item, keep_source=keep)
                    self._cleanup_inner_image(completed_item)   # terminal failure / gave up — no resume
                    # Keep the failed item in the queue (marked Failed, moved to the end) —
                    # only successful items disappear.
                    self._retire_failed(completed_item, "Failed", msg)
                    completed_item.kept_extract = keep
                    if keep:
                        own = self._extract_dir_for_item(completed_item)
                        self.log("INFO", f"Kept the extracted copy in {own} on {_drive_name(own)}: Edit or Retry "
                                         f"runs the job from it again, and removing the job deletes it.")
                self.update_queue_box()
                # Batch resilience: one failure must not abort the rest of the queue.
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
                        # Aggregate (done/failed) is reported here; no modal per failure.
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
    """macOS: a .ffpfsc / .ffpfs that was double-clicked (or 'Open With') opens in Look
    inside, as a panel of the main window. No-op off macOS (the OpenDocument AppleEvent is
    macOS-only); argv_emulation stays off in the spec so the event reaches Tk instead of
    being swallowed into argv."""
    if sys.platform != "darwin":
        return

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
                PfsBrowserDialog(app, f)
            except Exception as e:
                try:
                    app.log("ERROR", f"Could not open {f} in Look inside: {e}")
                except Exception:
                    pass

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
