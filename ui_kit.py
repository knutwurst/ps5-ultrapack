"""Lightweight Tk widgets for the PS5 UltraPack main window.

Everything here draws on plain tk.Canvas / tk.Frame / tk.Label. CustomTkinter widgets are
one canvas each and redraw in Python on every resize, which made the 1.1.x window lag
(280-420 ms per resize step with ~230 of them). Icons are vector strokes on a 24-unit grid,
drawn as canvas lines, so they stay sharp on Retina screens and take any colour.

Colour lives in PALETTE (one dict per appearance mode). Widgets never hold hex values:
they ask Kit for a token ("surface", "accent", ...), and Kit.set_mode() recolours every
registered widget in place.
"""
from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont

# ── Palette ──────────────────────────────────────────────────────────────────────────
# A PlayStation-like blue carries the one primary action and the selection. Green means
# success only, amber a warning, red an error.
PALETTE = {
    "dark": {
        "bg": "#0b0d12", "sidebar": "#0f1218", "surface": "#12161e", "surface2": "#1a1f2a",
        "inspector": "#161b24", "raised": "#20262f", "border": "#262d3a", "border_strong": "#3a4354",
        "text": "#e7ebf2", "muted": "#9aa3b2", "faint": "#6b7385",
        "accent": "#3d8bff", "accent_fill": "#1f6feb", "accent_hover": "#3b82f6",
        "accent_bg": "#16294a", "accent_text": "#7ab0ff", "on_accent": "#ffffff",
        "select": "#162644", "hover": "#171c26", "track": "#262d3a",
        "success": "#3fb950", "success_bg": "#10301b",
        "warning": "#d29922", "warning_bg": "#382a0c",
        "danger": "#f85149", "danger_bg": "#3a1417",
        "log": "#a9b3c2", "log_debug": "#5c6576",
        "control": "#20262f", "control_hover": "#2a3140", "danger_hover": "#5a1f22",
        "btn": "#262d3a", "btn_hover": "#313949", "btn_border": "#262d3a",
    },
    "light": {
        "bg": "#f4f6f9", "sidebar": "#eaedf2", "surface": "#ffffff", "surface2": "#f1f4f8",
        "inspector": "#f7f9fc", "raised": "#ffffff", "border": "#dde2ea", "border_strong": "#c3cbd8",
        "text": "#141821", "muted": "#566070", "faint": "#8a93a3",
        "accent": "#0064d2", "accent_fill": "#0064d2", "accent_hover": "#0057b8",
        "accent_bg": "#dcebff", "accent_text": "#0057b8", "on_accent": "#ffffff",
        "select": "#dcebff", "hover": "#eef1f6", "track": "#e2e7ee",
        "success": "#1a7f37", "success_bg": "#dcf3e3",
        "warning": "#9a6700", "warning_bg": "#fbefd0",
        "danger": "#cf222e", "danger_bg": "#fde4e4",
        "log": "#3b4454", "log_debug": "#9aa1ad",
        "control": "#ffffff", "control_hover": "#eef1f6", "danger_hover": "#b91c1c",
        "btn": "#ffffff", "btn_hover": "#f1f4f8", "btn_border": "#c9d0db",
    },
}


def ctk_pair(token: str) -> tuple[str, str]:
    """(light, dark) tuple for CustomTkinter widgets that should follow the palette."""
    return PALETTE["light"][token], PALETTE["dark"][token]


def apply_ctk_theme(ctk, body_px: int | None = None) -> None:
    """Point CustomTkinter's default widget colours (used by the dialogs) at the palette,
    so a CTk checkbox, switch, entry or segmented button looks like the main window.
    Call after set_default_color_theme() and before any CTk widget is created."""
    P = ctk_pair
    patch = {
        "CTk": {"fg_color": P("surface")},
        "CTkToplevel": {"fg_color": P("surface")},
        "CTkFrame": {"fg_color": P("surface2"), "top_fg_color": P("surface2"), "border_color": P("border")},
        "CTkButton": {"fg_color": P("accent_fill"), "hover_color": P("accent_hover"), "border_color": P("border_strong"),
                      "text_color": ("#ffffff", "#ffffff"), "text_color_disabled": P("faint")},
        "CTkLabel": {"text_color": P("text")},
        "CTkEntry": {"fg_color": P("control"), "border_color": P("border_strong"), "text_color": P("text"),
                     "placeholder_text_color": P("faint")},
        "CTkCheckBox": {"fg_color": P("accent_fill"), "border_color": P("border_strong"), "hover_color": P("accent_hover"),
                        "checkmark_color": ("#ffffff", "#ffffff"), "text_color": P("text"), "text_color_disabled": P("faint")},
        "CTkSwitch": {"fg_color": P("track"), "progress_color": P("accent_fill"), "button_color": ("#ffffff", "#e7ebf2"),
                      "button_hover_color": ("#ffffff", "#ffffff"), "text_color": P("text"), "text_color_disabled": P("faint")},
        "CTkRadioButton": {"fg_color": P("accent_fill"), "border_color": P("border_strong"), "hover_color": P("accent_hover"),
                           "text_color": P("text"), "text_color_disabled": P("faint")},
        "CTkProgressBar": {"fg_color": P("track"), "progress_color": P("accent_fill"), "border_color": P("border")},
        "CTkSlider": {"fg_color": P("track"), "progress_color": P("accent_fill"), "button_color": P("accent_fill"),
                      "button_hover_color": P("accent_hover")},
        # One field with a chevron, like a macOS pop-up button: no separate arrow block.
        "CTkOptionMenu": {"fg_color": P("control"), "button_color": P("control"), "button_hover_color": P("control"),
                          "text_color": P("text"), "text_color_disabled": P("faint")},
        "CTkComboBox": {"fg_color": P("control"), "border_color": P("border_strong"), "button_color": P("control_hover"),
                        "button_hover_color": P("border_strong"), "text_color": P("text")},
        "CTkScrollbar": {"button_color": P("border_strong"), "button_hover_color": P("faint")},
        "CTkSegmentedButton": {"fg_color": P("control"), "selected_color": P("accent_fill"),
                               "selected_hover_color": P("accent_hover"), "unselected_color": P("control"),
                               "unselected_hover_color": P("control_hover"), "text_color": P("text"),
                               "text_color_disabled": P("faint")},
        "CTkTextbox": {"fg_color": P("control"), "border_color": P("border"), "text_color": P("text"),
                       "scrollbar_button_color": P("border_strong"), "scrollbar_button_hover_color": P("faint")},
        "CTkScrollableFrame": {"label_fg_color": P("surface2")},
        "DropdownMenu": {"fg_color": P("raised"), "hover_color": P("select"), "text_color": P("text")},
    }
    theme = ctk.ThemeManager.theme
    if body_px and "CTkFont" in theme:
        theme["CTkFont"]["size"] = body_px       # the default text of every CTk widget
    for widget, colors in patch.items():
        if widget in theme:
            for key, value in colors.items():
                if key in theme[widget]:
                    theme[widget][key] = list(value)
    # An 18 px checkbox (set per widget) with a 2 px ring reads like a macOS one; CTk's
    # 3 px ring was drawn for its 24 px default.
    if "CTkCheckBox" in theme:
        theme["CTkCheckBox"]["border_width"] = 2
        theme["CTkCheckBox"]["corner_radius"] = 5


class Fonts:
    """The type scale, in logical pixels like every macOS app (Tk's POINTS are 1.33 px
    here, so positive Tk sizes would render a third larger than native text). The dialogs
    use the same numbers as CTkFont sizes, which are pixels too.

      view       17 bold  the title of a view, a Settings page list, a panel
      title      15 bold  the title of the selected thing (the job card)
      app        14 bold  the app name in the sidebar
      metric     15       a tile's number
      heading    13 bold  a section heading, a running stage
      body       13       labels, list rows, controls, navigation
      small      12       a row's second line, descriptions, hints
      caption    11       status bar, tile captions, stage names
      mono       11       the Log view
      mono_small 10       the log and command preview in the job card
    """

    def __init__(self, root):
        base = tkfont.nametofont("TkDefaultFont").actual().get("family", "Helvetica")
        # Menlo ships with every macOS, so it is looked up first: listing all families costs
        # ~350 ms, and so does asking for one that is missing (Tk searches for a fallback).
        mono = next((fam for fam in ("Menlo", "Monaco", "Courier New")
                     if tkfont.Font(root=root, family=fam, size=-11).actual("family") == fam), "Courier")
        # Negative Tk sizes are pixels.
        f = lambda px, weight="normal", fam=base: tkfont.Font(root=root, family=fam, size=-px, weight=weight)
        self.view = f(17, "bold")
        self.title = f(15, "bold")
        self.app = f(14, "bold")
        self.metric = f(15)
        self.heading = f(13, "bold")
        self.body = f(13)
        self.body_bold = f(13, "bold")
        self.small = f(12)
        self.small_bold = f(12, "bold")
        self.caption = f(11)
        self.mono = f(11, fam=mono)
        self.mono_small = f(10, fam=mono)

# ── Icons ────────────────────────────────────────────────────────────────────────────
# Ops on a 24x24 grid: l = polyline, p = closed outline, c = circle, cf = filled circle,
# ck = knob (circle filled with the background), r = rounded rect, pie = filled pie slice,
# pf = filled polygon, rf = filled rounded rect.
ICONS: dict[str, list[tuple]] = {
    "queue": [("l", 4, 6.5, 5.5, 8, 8, 5), ("l", 4, 12.5, 5.5, 14, 8, 11), ("l", 4, 18.5, 5.5, 20, 8, 17),
              ("l", 11, 6.5, 20, 6.5), ("l", 11, 12.5, 20, 12.5), ("l", 11, 18.5, 20, 18.5)],
    "history": [("c", 12, 12, 8.5), ("l", 12, 7.5, 12, 12, 15, 14)],
    "terminal": [("r", 3, 5, 21, 19, 2.5), ("l", 7, 10, 9.5, 12.5, 7, 15), ("l", 12, 15, 16, 15)],
    "search": [("c", 10.5, 10.5, 6), ("l", 15, 15, 20, 20)],
    "folders": [("p", 3, 6.5, 9.5, 6.5, 11.5, 8.5, 21, 8.5, 21, 18.5, 3, 18.5)],
    "settings": [("l", 4, 7, 20, 7), ("l", 4, 12, 20, 12), ("l", 4, 17, 20, 17),
                 ("ck", 9, 7, 2.2), ("ck", 15, 12, 2.2), ("ck", 7.5, 17, 2.2)],
    "plus": [("l", 12, 5, 12, 19), ("l", 5, 12, 19, 12)],
    "play": [("pf", 8.5, 6.5, 18, 12, 8.5, 17.5)],
    "stop": [("rf", 6.5, 6.5, 17.5, 17.5, 2.5)],
    "pause": [("rf", 7, 6, 10.5, 18, 1.5), ("rf", 13.5, 6, 17, 18, 1.5)],
    "sidebar-right": [("r", 3, 5, 21, 19, 2.5), ("l", 14.5, 5, 14.5, 19)],
    "x": [("l", 6.5, 6.5, 17.5, 17.5), ("l", 17.5, 6.5, 6.5, 17.5)],
    "check-circle": [("c", 12, 12, 9), ("l", 8, 12.5, 11, 15.3, 16.2, 9.3)],
    "alert": [("p", 12, 3.8, 21, 19.5, 3, 19.5), ("l", 12, 9.5, 12, 13.5), ("cf", 12, 16.6, 0.9)],
    "clock": [("c", 12, 12, 9), ("l", 12, 7, 12, 12, 15.5, 14)],
    "package": [("p", 12, 3, 20, 7.5, 20, 16.5, 12, 21, 4, 16.5, 4, 7.5),
                ("l", 4, 7.5, 12, 12, 20, 7.5), ("l", 12, 12, 12, 21)],
    "photo": [("r", 3, 5, 21, 19, 2.5), ("l", 3, 16, 8.5, 11, 13.5, 15.5, 16.5, 12.8, 21, 16.5),
              ("c", 15.5, 9, 1.5)],
    "arrow-right": [("l", 5, 12, 19, 12), ("l", 13, 6, 19, 12, 13, 18)],
    "chevron-right": [("l", 10, 6, 16, 12, 10, 18)],
    "contrast": [("c", 12, 12, 8.5), ("pie", 12, 12, 8.5, 90, 180)],
    "trash": [("l", 4, 7, 20, 7), ("l", 6.5, 7, 7.5, 20, 16.5, 20, 17.5, 7), ("l", 10, 4, 14, 4),
              ("l", 10, 11, 10, 16), ("l", 14, 11, 14, 16)],
    "edit": [("p", 4, 20, 4, 16, 15, 5, 19, 9, 8, 20), ("l", 13, 7, 17, 11)],
    "copy": [("r", 8, 8, 20, 20, 2), ("l", 16, 8, 16, 4, 4, 4, 4, 16, 8, 16)],
    "export": [("l", 12, 15, 12, 4), ("l", 7.5, 8.5, 12, 4, 16.5, 8.5), ("l", 5, 14, 5, 20, 19, 20, 19, 14)],
    "circle": [("c", 12, 12, 8)],
    "dot": [("c", 12, 12, 8), ("cf", 12, 12, 3.6)],
    "skip": [("c", 12, 12, 8.5), ("l", 8, 12, 16, 12)],
    "upload": [("l", 12, 16, 12, 5), ("l", 7.5, 9.5, 12, 5, 16.5, 9.5), ("l", 5, 19.5, 19, 19.5)],
    "code": [("l", 8, 7, 3, 12, 8, 17), ("l", 16, 7, 21, 12, 16, 17)],
    "broom": [("l", 19, 4, 11, 12), ("p", 11, 12, 13.5, 14.5, 9, 20.5, 3.5, 20.5, 3.5, 15)],
    "key": [("c", 8, 15.5, 4.2), ("l", 11, 12.5, 20, 3.5), ("l", 15.5, 8, 18.5, 11), ("l", 18, 5.5, 20.5, 8)],
    "drive": [("r", 3, 6, 21, 18, 2.5), ("l", 3, 13, 21, 13), ("cf", 17, 15.5, 1)],
    "layers": [("p", 12, 4, 21, 8.5, 12, 13, 3, 8.5), ("l", 3, 12.5, 12, 17, 21, 12.5), ("l", 3, 16.5, 12, 21, 21, 16.5)],
    "info": [("c", 12, 12, 9), ("l", 12, 11, 12, 16.5), ("cf", 12, 7.8, 1)],
}


def _retry_icon():
    """A circular arrow: an open arc, clockwise, with its head at the top right."""
    cx, cy, r = 12, 12, 7.5
    arc = []
    for k in range(15):
        a = math.radians(30 + k * 20)            # 30° … 310°, clockwise on screen
        arc += [round(cx + r * math.cos(a), 2), round(cy + r * math.sin(a), 2)]
    ex, ey = arc[-2], arc[-1]
    a = math.radians(310)
    tx, ty = -math.sin(a), math.cos(a)           # direction of travel at the end
    nx, ny = -ty, tx
    head = [round(ex - 4 * tx + 3 * nx, 2), round(ey - 4 * ty + 3 * ny, 2), ex, ey,
            round(ex - 4 * tx - 3 * nx, 2), round(ey - 4 * ty - 3 * ny, 2)]
    return [("l", *arc), ("l", *head)]


ICONS["retry"] = _retry_icon()


def _refresh_icon():
    """Two arcs chasing each other, each ending in an arrow head: the sync symbol."""
    cx, cy, r = 12, 12, 7
    def arc(start, extent, head_dir):
        pts = []
        for k in range(13):
            a = math.radians(start + extent * k / 12)
            pts += [round(cx + r * math.cos(a), 2), round(cy + r * math.sin(a), 2)]
        ex, ey = pts[-2], pts[-1]
        a = math.radians(start + extent)
        tx, ty = -math.sin(a), math.cos(a)
        if head_dir < 0:
            tx, ty = -tx, -ty
        nx, ny = -ty, tx
        head = [round(ex - 4 * tx + 3 * nx, 2), round(ey - 4 * ty + 3 * ny, 2), ex, ey,
                round(ex - 4 * tx - 3 * nx, 2), round(ey - 4 * ty - 3 * ny, 2)]
        return [("l", *pts), ("l", *head)]
    return arc(-70, 160, 1) + arc(110, 160, 1)


ICONS["refresh"] = _refresh_icon()



def unpack_touchpad_delta(d) -> tuple[int, int]:
    """(dx, dy) in pixels from a <TouchpadScroll> %D (Tk 9, TIP 684): dx in the high 16
    bits, dy in the low 16, both signed. The same arithmetic as tk::PreciseScrollDeltas."""
    d = int(d or 0)
    dx = d >> 16
    low = d & 0xFFFF
    dy = low if low < 0x8000 else low - 0x10000
    return dx, dy


def attach_wheel_scroll(widget, *, pixel_unit: int = 1, notch_px: int = 40) -> None:
    """Scroll *widget* (a Canvas, or anything with yview/xview) with the mouse wheel and
    with a trackpad swipe, on Tk 8.6 and Tk 9.

    Tk 9 on macOS reports a trackpad swipe as <TouchpadScroll> with pixel deltas (TIP
    684), never as <MouseWheel>; Tk binds that event for Text, Listbox and TCombobox
    itself, a Canvas gets nothing. A wheel notch arrives as <MouseWheel> ±120 on Tk 9
    (every platform) and on Windows, ±1 on Tk 8.6/macOS; Linux sends Button-4/5. Attach
    this only to widgets WITHOUT a class binding (a Text scrolls itself, and would move
    twice). *pixel_unit* is how many pixels one yview unit is (the canvas's
    yscrollincrement); *notch_px* what one wheel notch moves."""
    rem = {"x": 0.0, "y": 0.0}

    def _span_ok(axis: str) -> bool:
        try:
            lo, hi = (widget.yview if axis == "y" else widget.xview)()
            return (hi - lo) < 0.999
        except Exception:
            return False

    def scroll_px(axis: str, px: float) -> bool:
        """Move the view by *px* pixels (positive: the view goes down / right). True when
        it moved. The remainder below one unit is carried to the next call, so a slow
        swipe still adds up, and a tick never rounds away to nothing."""
        if not px or not _span_ok(axis):
            return False
        rem[axis] += px / float(pixel_unit)
        units = int(rem[axis])
        if units == 0:
            units = 1 if rem[axis] > 0 else -1
        rem[axis] -= units
        try:
            (widget.yview_scroll if axis == "y" else widget.xview_scroll)(units, "units")
            return True
        except tk.TclError:
            return False

    def on_wheel(e, axis: str = "y"):
        d = float(getattr(e, "delta", 0) or 0)
        if not d:
            return None
        px = -d / 120.0 * notch_px if abs(d) >= 120 else -d * notch_px
        return "break" if scroll_px(axis, px) else None

    def on_touchpad(e):
        dx, dy = unpack_touchpad_delta(getattr(e, "delta", 0))
        moved = False
        if dy:
            moved = scroll_px("y", -dy) or moved      # Tk's Text binding: yview scroll -dy pixels
        if dx:
            moved = scroll_px("x", -dx) or moved
        return "break" if moved else None

    widget._scroll_px = scroll_px
    widget._on_touchpad = on_touchpad
    widget.bind("<MouseWheel>", on_wheel, add="+")
    widget.bind("<Shift-MouseWheel>", lambda e: on_wheel(e, "x"), add="+")
    widget.bind("<Button-4>", lambda e: "break" if scroll_px("y", -notch_px) else None, add="+")
    widget.bind("<Button-5>", lambda e: "break" if scroll_px("y",  notch_px) else None, add="+")
    try:
        widget.bind("<TouchpadScroll>", on_touchpad, add="+")    # Tk 9; Tk 8.6 has no such event
    except tk.TclError:
        pass


def _hand_cursor(w) -> str:
    for name in ("pointinghand", "hand2"):
        try:
            w.configure(cursor=name)
            return name
        except tk.TclError:
            pass
    return ""


def _solid_round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, color, kw):
    """A filled rounded rectangle with antialiased corners. Tk on macOS fills polygons
    without antialiasing (the corners come out as pixel steps) but antialiases strokes, so
    the shape is the inner rectangle drawn with a stroke of twice the radius and round
    joins, the trick CustomTkinter uses. Tk rounds stroke widths to whole points."""
    d = min(round(2 * r), int(min(x2 - x1, y2 - y1)))
    if d < 2:
        return cv.create_rectangle(x1, y1, x2, y2, fill=color, outline="", **kw)
    r = d / 2
    return cv.create_polygon(x1 + r, y1 + r, x2 - r, y1 + r, x2 - r, y2 - r, x1 + r, y2 - r,
                             fill=color, outline=color, width=d, joinstyle="round", **kw)


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, fill="", outline="", width=None, **kw):
    """A rounded rectangle; returns the id of its top shape. A border is drawn as the
    same shape in the border colour with the fill inset on top, so both edges stay
    antialiased. An outline without fill (icons) is a stroked polygon with sampled corners."""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    bw = (1 if width is None else width) if outline else 0
    if fill:
        if bw:
            _solid_round_rect(cv, x1, y1, x2, y2, r, outline, kw)
            x1, y1, x2, y2, r = x1 + bw, y1 + bw, x2 - bw, y2 - bw, max(0.0, r - bw)
        return _solid_round_rect(cv, x1, y1, x2, y2, r, fill, kw)
    if not bw:
        return None
    if r < 0.5:
        return cv.create_rectangle(x1, y1, x2, y2, outline=outline, width=bw, **kw)
    n = 10 if r >= 6 else 6
    pts = []
    for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
        for k in range(n + 1):
            ang = math.radians(a0 + 90 * k / n)
            pts += [cx + r * math.cos(ang), cy + r * math.sin(ang)]
    return cv.create_polygon(pts, fill="", outline=outline, width=bw, joinstyle="round", **kw)


def draw_icon(cv: tk.Canvas, name: str, x: float, y: float, size: float, color: str,
              bg: str = "", width: float | None = None, tags=()):
    """Draw icon *name* with its top-left corner at (x, y), *size* points square."""
    s = size / 24.0
    w = width if width is not None else max(1.2, 1.9 * s)
    common = dict(tags=tags)
    for op in ICONS.get(name, ()):
        kind, args = op[0], op[1:]
        if kind in ("l", "p", "pf"):
            pts = [x + v * s if i % 2 == 0 else y + v * s for i, v in enumerate(args)]
            if kind == "l":
                cv.create_line(*pts, fill=color, width=w, capstyle="round", joinstyle="round", **common)
            elif kind == "p":
                cv.create_polygon(*pts, outline=color, fill="", width=w, joinstyle="round", **common)
            else:   # filled: the stroke gives the edge its antialiasing and round corners
                cv.create_polygon(*pts, outline=color, fill=color, width=2, joinstyle="round", **common)
        elif kind in ("c", "cf", "ck"):
            cx, cy, r = x + args[0] * s, y + args[1] * s, args[2] * s
            if kind == "c":
                cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=w, **common)
            elif kind == "cf":      # a hairline stroke antialiases the filled disc's edge
                r = max(0.5, r - 0.5)
                cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, fill=color, width=1, **common)
            else:
                cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, fill=bg or "", width=w, **common)
        elif kind in ("r", "rf"):
            box = (x + args[0] * s, y + args[1] * s, x + args[2] * s, y + args[3] * s, args[4] * s)
            if kind == "r":
                round_rect(cv, *box, outline=color, fill="", width=w, **common)
            else:
                round_rect(cv, *box, fill=color, **common)
        elif kind == "pie":
            cx, cy, r = x + args[0] * s, y + args[1] * s, args[2] * s
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=args[3], extent=args[4],
                          style="pieslice", outline=color, fill=color, width=1, **common)


def truncate(font: tkfont.Font, text: str, max_w: int) -> str:
    """*text* shortened with an ellipsis so it fits into *max_w* points."""
    if max_w <= 0:
        return ""
    if font.measure(text) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"



def on_resize(widget, fn, settle_ms=0):
    """Call fn() when *widget* changes SIZE. <Configure> also fires when a widget only
    moves (e.g. right-aligned buttons while the window is resized); redrawing then is
    wasted work that Tk has to paint again. With settle_ms, a burst of size changes (a
    window being dragged) runs fn() once, after the size has been still that long."""
    last = [None]
    pending = [None]

    def fire():
        pending[0] = None
        fn()

    def handler(e):
        size = (e.width, e.height)
        if size == last[0]:
            return
        first = last[0] is None
        last[0] = size
        if not settle_ms or first:
            fn()
            return
        if pending[0] is not None:
            widget.after_cancel(pending[0])
        pending[0] = widget.after(settle_ms, fire)
    widget.bind("<Configure>", handler, add="+")

# ── Theme registry ───────────────────────────────────────────────────────────────────
class Kit:
    """Palette + fonts + the registry of widgets to recolour on a mode switch."""

    def __init__(self, root, mode: str = "dark"):
        self.root = root
        self.mode = mode if mode in PALETTE else "dark"
        self.pal = PALETTE[self.mode]
        self.fonts = Fonts(root)
        self._styled: list[tuple[tk.Misc, dict]] = []
        self._custom: list = []

    def c(self, token: str) -> str:
        return self.pal[token]

    def style(self, widget, **opts):
        """Register *widget* and set each option to its palette token, e.g. bg="surface"."""
        self._styled.append((widget, opts))
        widget.configure(**{k: self.pal[v] for k, v in opts.items()})
        return widget

    def register(self, widget):
        self._custom.append(widget)
        return widget

    def frame(self, parent, bg="surface", **kw) -> tk.Frame:
        return self.style(tk.Frame(parent, bd=0, highlightthickness=0, **kw), bg=bg)

    def label(self, parent, bg="surface", fg="text", font=None, **kw) -> tk.Label:
        kw.setdefault("anchor", "w")
        kw.setdefault("justify", "left")
        lb = tk.Label(parent, bd=0, padx=0, pady=0, font=font or self.fonts.body, **kw)
        return self.style(lb, bg=bg, fg=fg)

    def rule(self, parent, bg_token="border", horizontal=True) -> tk.Frame:
        f = tk.Frame(parent, bd=0, highlightthickness=0, height=1 if horizontal else 0, width=0 if horizontal else 1)
        return self.style(f, bg=bg_token)

    def text(self, parent, bg="surface", fg="log", font=None, **kw) -> tk.Text:
        for k, v in (("wrap", "none"), ("padx", 10), ("pady", 8)):
            kw.setdefault(k, v)
        txt = tk.Text(parent, bd=0, highlightthickness=0, relief="flat", font=font or self.fonts.mono, **kw)
        return self.style(txt, bg=bg, fg=fg, insertbackground=fg, selectbackground="select",
                          selectforeground="text")

    def set_mode(self, mode: str):
        self.mode = mode if mode in PALETTE else "dark"
        self.pal = PALETTE[self.mode]
        alive = []
        for w, opts in self._styled:
            try:
                if not w.winfo_exists():
                    continue
            except tk.TclError:
                continue
            vals = {k: self.pal[v] for k, v in opts.items()}
            try:
                w.configure(**vals)
            except tk.TclError:
                # CustomTkinter replaces a tk master's configure() with one that forwards bg
                # to its CTk child first; once that child is destroyed the forward fails and
                # the master is never recoloured. Apply the colours to the widget itself.
                try:
                    type(w).configure(w, **vals)
                except tk.TclError:
                    continue
            alive.append((w, opts))
        self._styled = alive
        keep = []
        for w in self._custom:
            try:
                if w.winfo_exists():
                    w.apply_palette()
                    keep.append(w)
            except tk.TclError:
                pass
        self._custom = keep


# ── Widgets ──────────────────────────────────────────────────────────────────────────
class IconButton(tk.Canvas):
    """A button drawn on one canvas: optional icon + text, four variants.

    primary   blue fill, the one main action of a view
    secondary quiet fill with a hairline border
    ghost     no fill until hovered (toolbars)
    danger    secondary with red text
    nav       full-width sidebar row with a selected state and an optional badge

    configure()/cget() understand state, text, icon, command, selected and badge, so the
    app can keep calling start_btn.configure(state="disabled") as it did on CTkButton.
    """

    def __init__(self, parent, kit: Kit, text="", icon=None, command=None, variant="secondary",
                 height=30, width=None, bg="surface", font=None, padx=12, icon_size=16, badge=None,
                 tooltip=None, tooltip_side=None):
        super().__init__(parent, height=height, highlightthickness=0, bd=0, bg=kit.c(bg))
        self._hand = _hand_cursor(self)
        super().configure(cursor=self._hand)
        self.kit, self._text, self._icon, self._command = kit, text, icon, command
        self.variant, self._bg_token, self._font = variant, bg, font
        self._padx, self._icon_size, self._badge = padx, icon_size, badge
        self._fixed_w = width
        self._state, self._hover, self._pressed, self._selected = "normal", False, False, False
        self.autohide = False      # set True on a gridded button: hidden while disabled
        self._grid_ok = False
        kit.register(self)
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        on_resize(self, self._draw)
        self.tooltip = (Tooltip(self, kit, tooltip, side=tooltip_side or ("right" if variant == "nav" else "below"))
                        if tooltip else None)
        self._resize()
        self._draw()

    # — API compatible with the CTkButton calls the app makes —
    def configure(self, cnf=None, **kw):
        mine = {k: kw.pop(k) for k in ("state", "text", "icon", "command", "selected", "badge", "variant")
                if k in kw}
        if cnf or kw:
            super().configure(cnf, **kw)
        if not mine:
            return
        if "state" in mine:
            self._state = "disabled" if str(mine["state"]) == "disabled" else "normal"
            super().configure(cursor="arrow" if self._state == "disabled" else self._hand)
            if self.autohide and self.winfo_manager() in ("grid", ""):
                if self._state == "disabled":
                    self.grid_remove()
                elif self._grid_ok:
                    self.grid()
        if "text" in mine:
            self._text = mine["text"]
        if "icon" in mine:
            self._icon = mine["icon"]
        if "command" in mine:
            self._command = mine["command"]
        if "selected" in mine:
            self._selected = bool(mine["selected"])
        if "badge" in mine:
            self._badge = mine["badge"]
        if "variant" in mine:
            self.variant = mine["variant"]
        if "text" in mine or "icon" in mine:
            self._resize()
        self._draw()

    config = configure

    def grid(self, *a, **kw):
        self._grid_ok = True
        return super().grid(*a, **kw)

    grid_configure = grid

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().cget(key)

    def invoke(self):
        if self._state == "normal" and self._command:
            self._command()

    def apply_palette(self):
        super().configure(bg=self.kit.c(self._bg_token))
        self._draw()

    # — drawing —
    def _font_obj(self):
        # Regular weight like a macOS push button; the fill carries the emphasis, and the
        # 1 pt icon strokes match regular text better than bold.
        return self._font or self.kit.fonts.body

    def _resize(self):
        if self._fixed_w or self.variant == "nav":
            if self._fixed_w:
                super().configure(width=self._fixed_w)
            return
        w = 2 * self._padx
        if self._icon:
            w += self._icon_size + (6 if self._text else 0)
        if self._text:
            w += self._font_obj().measure(self._text)
        super().configure(width=max(w, int(self.winfo_fpixels(self.cget("height")))))

    def _set(self, **kw):
        changed = False
        for k, v in kw.items():
            if getattr(self, "_" + k) != v:
                setattr(self, "_" + k, v)
                changed = True
        if changed:
            self._draw()

    def _release(self, e):
        was = self._pressed
        self._set(pressed=False)
        if was and self._state == "normal" and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            if self._command:
                self._command()

    def _colors(self):
        c, v, dis = self.kit.c, self.variant, self._state == "disabled"
        if v == "primary":
            if dis:
                return c("track"), "", c("faint")
            return (c("accent_hover") if (self._hover or self._pressed) else c("accent_fill")), "", c("on_accent")
        if v == "ghost":
            fill = c("hover") if (self._hover and not dis) else ""
            return fill, "", (c("faint") if dis else (c("text") if self._hover else c("muted")))
        if v == "nav":
            if dis:
                return (c("select") if self._selected else ""), "", c("faint")
            if self._selected:
                return c("select"), "", c("text")
            return (c("hover") if self._hover else ""), "", (c("text") if self._hover else c("muted"))
        fill = c("btn_hover") if ((self._hover or self._pressed) and not dis) else c("btn")
        fg = c("faint") if dis else (c("danger") if v == "danger" else c("text"))
        border = c("btn_border")
        return fill, ("" if border == c("btn") else border), fg

    def _draw(self):
        self.delete("all")
        W = self.winfo_width() if self.winfo_width() > 1 else int(self.winfo_fpixels(self.cget("width")))
        H = int(self.winfo_fpixels(self.cget("height")))
        fill, border, fg = self._colors()
        if fill or border:
            round_rect(self, 0, 0, W, H, 7, fill=fill or self.kit.c(self._bg_token), outline=border)
        font = self._font_obj()
        tw = font.measure(self._text) if self._text else 0
        iw = self._icon_size if self._icon else 0
        gap = 8 if (self._icon and self._text and self.variant == "nav") else (6 if (self._icon and self._text) else 0)
        if self.variant == "nav":
            x = self._padx
        else:
            x = (W - (iw + gap + tw)) / 2
        if self._icon:
            draw_icon(self, self._icon, x, (H - iw) / 2, iw, fg, bg=fill or self.kit.c(self._bg_token))
            x += iw + gap
        if self._text:
            self.create_text(x, H / 2, text=self._text, anchor="w", fill=fg, font=font)
        if self._badge not in (None, "", 0):
            bf = self.kit.fonts.caption
            bt = str(self._badge)
            bw = max(18, bf.measure(bt) + 12)
            bx2 = W - 10
            round_rect(self, bx2 - bw, H / 2 - 9, bx2, H / 2 + 9, 9,
                       fill=self.kit.c("accent_bg"), outline="")
            self.create_text(bx2 - bw / 2, H / 2, text=bt, fill=self.kit.c("accent_text"), font=bf)


class ProgressBar(tk.Canvas):
    """A thin rounded progress bar; set(fraction) like CTkProgressBar."""

    def __init__(self, parent, kit: Kit, height=6, bg="surface", fill="accent_fill"):
        super().__init__(parent, height=height, highlightthickness=0, bd=0, bg=kit.c(bg))
        self.kit, self._bg, self._fill, self._v = kit, bg, fill, 0.0
        kit.register(self)
        on_resize(self, self._draw)

    def set(self, value):
        try:
            v = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            v = 0.0
        if abs(v - self._v) >= 0.001 or v in (0.0, 1.0):
            self._v = v
            self._draw()

    def get(self):
        return self._v

    def set_fill(self, token):
        self._fill = token
        self._draw()

    def apply_palette(self):
        self.configure(bg=self.kit.c(self._bg))
        self._draw()

    def _draw(self):
        self.delete("all")
        W, H = self.winfo_width(), int(self.winfo_fpixels(self.cget("height")))
        if W <= 2:
            return
        round_rect(self, 0, 0, W, H, H / 2, fill=self.kit.c("track"), outline="")
        if self._v > 0:
            round_rect(self, 0, 0, max(H, W * self._v), H, H / 2, fill=self.kit.c(self._fill), outline="")


class Tooltip:
    """A small delayed tooltip for a widget."""

    def __init__(self, widget, kit: Kit, text: str, delay=550, side="below"):
        self.w, self.kit, self.text, self.delay, self.side = widget, kit, text, delay, side
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._hide()
        self._after = self.w.after(self.delay, self._show)

    def _show(self):
        if self._tip or not self.text:
            return
        if self.side == "right":
            x = self.w.winfo_rootx() + self.w.winfo_width() + 8
            y = self.w.winfo_rooty() + 4
        else:
            x = self.w.winfo_rootx() + 8
            y = self.w.winfo_rooty() + self.w.winfo_height() + 6
        self._tip = tk.Toplevel(self.w)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self.text, bg=self.kit.c("raised"), fg=self.kit.c("text"),
                 font=self.kit.fonts.caption, padx=8, pady=4, justify="left", wraplength=320,
                 highlightthickness=1, highlightbackground=self.kit.c("border_strong")).pack()

    def _hide(self, _e=None):
        if self._after:
            try:
                self.w.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class FlowCanvas(tk.Canvas):
    """A canvas that lays out a row of items, wraps them, and sets its own height."""

    def __init__(self, parent, kit: Kit, bg="surface", min_height=22):
        super().__init__(parent, height=min_height, highlightthickness=0, bd=0, bg=kit.c(bg))
        self.kit, self._bg, self._min_h = kit, bg, min_height
        kit.register(self)
        on_resize(self, self._draw, settle_ms=70)

    def apply_palette(self):
        self.configure(bg=self.kit.c(self._bg))
        self._draw()

    def _fit(self, h):
        h = max(self._min_h, int(h))
        if int(self.winfo_fpixels(self.cget("height"))) != h:
            self.configure(height=h)

    def _draw(self):   # overridden
        pass


class Chips(FlowCanvas):
    """The job recipe as chips: Folder → Sign → .pkg."""

    def __init__(self, parent, kit, bg="surface"):
        self._parts: list[str] = []
        super().__init__(parent, kit, bg=bg, min_height=24)

    def set_parts(self, parts):
        parts = [str(p) for p in parts if p]
        if parts != self._parts:
            self._parts = parts
            self._draw()

    def _draw(self):
        self.delete("all")
        W = max(self.winfo_width(), 50)
        f, x, y, h = self.kit.fonts.caption, 0, 1, 18
        for i, part in enumerate(self._parts):
            w = f.measure(part) + 16
            if i:
                if x + 20 + w > W:
                    x, y = 0, y + h + 6
                else:
                    draw_icon(self, "arrow-right", x + 3, y + 3, 12, self.kit.c("faint"))
                    x += 18
            if x + w > W and x > 0:
                x, y = 0, y + h + 6
            round_rect(self, x, y, x + w, y + h, 6, fill=self.kit.c("surface2"), outline="")
            self.create_text(x + w / 2, y + h / 2, text=part, fill=self.kit.c("muted"), font=f)
            x += w
        self._fit(y + h + 2 if self._parts else self._min_h)


class StepStrip(FlowCanvas):
    """The job's stages: done ✓, the current one with its percentage, pending ones."""

    def __init__(self, parent, kit, bg="surface"):
        self._steps: list[tuple[str, str, int]] = []   # (label, state, pct)
        super().__init__(parent, kit, bg=bg, min_height=20)

    def set_steps(self, steps):
        steps = [(str(a), str(b), int(c or 0)) for a, b, c in steps]
        if steps != self._steps:
            self._steps = steps
            self._draw()

    def _draw(self):
        self.delete("all")
        W = max(self.winfo_width(), 50)
        f, fb = self.kit.fonts.caption, self.kit.fonts.caption
        x, y, h = 0, 1, 18
        for label, state, pct in self._steps:
            txt = f"{label} {pct}%" if state == "current" else label
            w = 14 + 4 + (fb if state == "current" else f).measure(txt) + 10
            if x + w > W and x > 0:
                x, y = 0, y + h + 4
            if state == "done":
                icon, col = "check-circle", self.kit.c("success")
            elif state == "current":
                icon, col = "dot", self.kit.c("accent")
            elif state == "failed":
                icon, col = "alert", self.kit.c("danger")
            else:
                icon, col = "circle", self.kit.c("faint")
            draw_icon(self, icon, x, y + 2, 13, col, bg=self.kit.c(self._bg))
            self.create_text(x + 17, y + h / 2 + 0.5, text=txt, anchor="w", font=f,
                             fill=col if state in ("current", "failed") else (
                                 self.kit.c("muted") if state == "done" else self.kit.c("faint")))
            x += w
        self._fit(y + h + 2 if self._steps else self._min_h)


class QueueList(tk.Frame):
    """The job list, drawn on one canvas.

    Rows are dicts: title, subtitle, state (running|queued|done|failed|skipped|waiting),
    chip, progress (0..1 or None), text (the one-line summary returned by get()) and an
    optional key (stable per job, so a selection follows its jobs across a refresh).
    The methods curselection/selection_set/selection_clear/see/nearest/size/get mirror
    tk.Listbox, so the app code that drove the old listbox keeps working; curselection
    is the one row in focus. Several rows can be marked besides it: Command-click (Ctrl
    on Windows and Linux) adds or drops one, Shift-click marks a range, Command-A all,
    Escape keeps only the one in focus; marked_rows() returns them.
    """

    ROW_H = 50

    DRAG_START = 6     # points the pointer moves before a press turns into a drag

    def __init__(self, parent, kit: Kit, on_select=None, on_activate=None, on_context=None,
                 on_key_up=None, on_key_down=None, on_delete=None, on_move=None, bg="surface",
                 empty_title="", empty_body=""):
        super().__init__(parent, bd=0, highlightthickness=0, bg=kit.c(bg))
        self.kit, self._bg = kit, bg
        self.on_select, self.on_activate, self.on_context = on_select, on_activate, on_context
        self.on_move = on_move               # on_move(src, dst): drop row src before row dst
        self._press = None                   # (row, y) of a left press that may become a drag
        self._drop = None                    # insertion index (0..len) while dragging
        self.on_key_up, self.on_key_down, self.on_delete = on_key_up, on_key_down, on_delete
        self.empty_title, self.empty_body = empty_title, empty_body
        self.rows: list[dict] = []
        self.sel: int | None = None          # the row in focus (the details pane shows it)
        self.marked: set[int] = set()        # every selected row, the one in focus included
        self._anchor: int | None = None      # where a Shift-click range starts
        self.hover: int | None = None
        self.cv = tk.Canvas(self, highlightthickness=0, bd=0, bg=kit.c(bg), yscrollincrement=1,
                            takefocus=1)
        self.sb = tk.Scrollbar(self, orient="vertical", command=self.cv.yview)
        self.cv.configure(yscrollcommand=self._on_yscroll)
        self.cv.grid(row=0, column=0, sticky="nsew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._sb_shown = False
        self._redraw_pending = False
        kit.register(self)
        cv = self.cv
        on_resize(cv, self._schedule, settle_ms=70)
        aqua = self.tk.call("tk", "windowingsystem") == "aqua"
        mod = "Command" if aqua else "Control"
        cv.bind("<Button-1>", self._click)
        cv.bind("<B1-Motion>", self._drag)
        cv.bind("<ButtonRelease-1>", self._release)
        cv.bind("<Double-Button-1>", self._double)
        cv.bind(f"<{mod}-Button-1>", self._toggle_click)
        cv.bind("<Shift-Button-1>", self._range_click)
        cv.bind("<Button-2>", self._context)          # right click on macOS
        if aqua:
            cv.bind("<Control-Button-1>", self._context)   # Ctrl-click is a right click there
        cv.bind("<Button-3>", self._context)
        cv.bind(f"<{mod}-a>", lambda e: self._select_all())
        cv.bind("<Escape>", lambda e: self._keep_focus_only())
        cv.bind("<Motion>", self._motion)
        cv.bind("<Leave>", lambda e: self._set_hover(None))
        attach_wheel_scroll(cv, pixel_unit=1, notch_px=40)   # wheel, Linux buttons, Tk 9 trackpad
        cv.bind("<Up>", lambda e: self._key(self.on_key_up))
        cv.bind("<Down>", lambda e: self._key(self.on_key_down))
        cv.bind("<BackSpace>", lambda e: self._key(self.on_delete))
        cv.bind("<Delete>", lambda e: self._key(self.on_delete))

    # — Listbox-compatible API —
    def curselection(self):
        return (self.sel,) if self.sel is not None and 0 <= self.sel < len(self.rows) else ()

    def selection_set(self, first, last=None):
        try:
            i = int(first)
        except (TypeError, ValueError):
            return
        self.sel = i if 0 <= i < len(self.rows) else None
        self.marked = {self.sel} if self.sel is not None else set()
        self._anchor = self.sel
        self._schedule()

    def selection_clear(self, first=0, last=None):
        self.sel = None
        self.marked = set()
        self._schedule()

    def marked_rows(self) -> list:
        """Every selected row, top to bottom (the one in focus alone when nothing else is)."""
        rows = {i for i in self.marked if 0 <= i < len(self.rows)}
        if self.sel is not None and 0 <= self.sel < len(self.rows):
            rows.add(self.sel)
        return sorted(rows)

    def focus_key(self):
        """The key of the row in focus, or None."""
        if self.sel is not None and 0 <= self.sel < len(self.rows):
            return self.rows[self.sel].get("key")
        return None

    def size(self):
        return len(self.rows)

    def get(self, i):
        return self.rows[int(i)].get("text", "")

    def nearest(self, y):
        if not self.rows:
            return 0
        cy = self.cv.canvasy(y)
        return max(0, min(len(self.rows) - 1, int((cy - 6) // self.ROW_H)))

    def see(self, i):
        if not self.rows or i is None:
            return
        if self._redraw_pending:
            # The scroll region is set by the pending redraw; scroll after it ran.
            self.after_idle(lambda: self._see_now(i))
            return
        self._see_now(i)

    def _see_now(self, i):
        if not self.rows or not (0 <= i < len(self.rows)):
            return
        H = max(1, self.cv.winfo_height())
        total = 12 + len(self.rows) * self.ROW_H
        top, bot = 6 + i * self.ROW_H, 6 + (i + 1) * self.ROW_H
        y0 = self.cv.canvasy(0)
        if top < y0:
            self.cv.yview_moveto(max(0.0, (top - 6) / total))
        elif bot > y0 + H:
            self.cv.yview_moveto(max(0.0, (bot + 6 - H) / total))

    # — data —
    def set_rows(self, rows, selected=None, keep_marked=False):
        """New rows. *selected* is the row in focus; with *keep_marked* the other marked rows
        stay marked when their key is still in the list, otherwise only *selected* is."""
        keys = {self.rows[i].get("key") for i in self.marked if 0 <= i < len(self.rows)} if keep_marked else set()
        keys.discard(None)
        self.rows = list(rows)
        self.sel = selected if (selected is not None and 0 <= selected < len(self.rows)) else None
        self.marked = {j for j, r in enumerate(self.rows) if r.get("key") in keys}
        if self.sel is not None:
            self.marked.add(self.sel)
        if self._anchor is None or not (0 <= self._anchor < len(self.rows)) or not keep_marked:
            self._anchor = self.sel
        self._schedule()

    def update_row(self, i, **fields):
        """Change one row's fields (e.g. progress/chip of the running job) and redraw."""
        if 0 <= i < len(self.rows):
            row = self.rows[i]
            if any(row.get(k) != v for k, v in fields.items()):
                row.update(fields)
                self._schedule()

    def apply_palette(self):
        self.configure(bg=self.kit.c(self._bg))
        self.cv.configure(bg=self.kit.c(self._bg))
        self._draw()

    # — events —
    def _index_at(self, y):
        if not self.rows:
            return None
        cy = self.cv.canvasy(y)
        i = int((cy - 6) // self.ROW_H)
        return i if 0 <= i < len(self.rows) and cy >= 6 else None

    def _click(self, e):
        self.cv.focus_set()
        i = self._index_at(e.y)
        self._press = (i, e.y) if i is not None else None
        self._drop = None
        if i is not None and (i != self.sel or self.marked != {i}):
            self.sel, self.marked, self._anchor = i, {i}, i
            self._draw()
        if i is not None and self.on_select:
            self.on_select(i)

    def _toggle_click(self, e):
        """Command-click (Ctrl on Windows and Linux): add the row to the selection or drop it."""
        self.cv.focus_set()
        i = self._index_at(e.y)
        self._press, self._drop = None, None
        if i is None:
            return "break"
        marked = set(self.marked_rows())
        if i in marked and len(marked) > 1:
            marked.discard(i)
            if self.sel == i:
                self.sel = min(marked, key=lambda j: abs(j - i))
        else:
            marked.add(i)
            self.sel = i
        self.marked, self._anchor = marked, i
        self._draw()
        if self.on_select:
            self.on_select(self.sel)
        return "break"

    def _range_click(self, e):
        """Shift-click: every row from the last plain or Command-click to this one."""
        self.cv.focus_set()
        i = self._index_at(e.y)
        self._press, self._drop = None, None
        if i is None:
            return "break"
        a = self._anchor if self._anchor is not None and 0 <= self._anchor < len(self.rows) else (
            self.sel if self.sel is not None else i)
        self.marked = set(range(min(a, i), max(a, i) + 1))
        self.sel = i
        self._draw()
        if self.on_select:
            self.on_select(i)
        return "break"

    def _select_all(self):
        if not self.rows:
            return "break"
        if self.sel is None:
            self.sel = 0
        self.marked = set(range(len(self.rows)))
        self._draw()
        if self.on_select:
            self.on_select(self.sel)
        return "break"

    def _keep_focus_only(self):
        if self.sel is not None and self.marked != {self.sel}:
            self.marked = {self.sel}
            self._draw()
            if self.on_select:
                self.on_select(self.sel)
        return "break"

    def _drag(self, e):
        """Drag a row to another place: a line shows where it lands. Near the top or the
        bottom edge the list scrolls along."""
        if self._press is None or self.on_move is None or len(self.rows) < 2:
            return
        if self._drop is None and abs(e.y - self._press[1]) < self.DRAG_START:
            return
        H = max(1, self.cv.winfo_height())
        if e.y < 12:
            self.cv.yview_scroll(-6, "units")      # units are pixels here
        elif e.y > H - 12:
            self.cv.yview_scroll(6, "units")
        cy = self.cv.canvasy(e.y)
        drop = int(round((cy - 6) / self.ROW_H))
        drop = max(0, min(len(self.rows), drop))
        if drop != self._drop:
            self._drop = drop
            self.cv.configure(cursor="fleur")
            self._draw()

    def _release(self, e):
        press, drop = self._press, self._drop
        self._press, self._drop = None, None
        if drop is None:
            return
        self.cv.configure(cursor="")
        self._draw()
        if press is not None and self.on_move is not None and drop not in (press[0], press[0] + 1):
            self.on_move(press[0], drop)

    def _double(self, e):
        i = self._index_at(e.y)
        if i is not None and self.on_activate:
            self.on_activate(e)
        return "break"

    def _context(self, e):
        i = self._index_at(e.y)
        if i is None:
            return
        if i in self.marked_rows() and len(self.marked_rows()) > 1:
            pass                                 # the menu acts on every selected row
        elif i != self.sel or self.marked != {i}:
            self.sel, self.marked, self._anchor = i, {i}, i
            self._draw()
            if self.on_select:
                self.on_select(i)
        if self.on_context:
            self.on_context(e, i)

    def _motion(self, e):
        self._set_hover(self._index_at(e.y))

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self._draw()

    def _key(self, fn):
        if fn:
            fn()
        return "break"

    def _on_yscroll(self, lo, hi):
        need = not (float(lo) <= 0.0 and float(hi) >= 1.0)
        if need != self._sb_shown:
            self._sb_shown = need
            if need:
                self.sb.grid(row=0, column=1, sticky="ns")
            else:
                self.sb.grid_remove()
        self.sb.set(lo, hi)

    # — drawing —
    def _schedule(self):
        if not self._redraw_pending:
            self._redraw_pending = True
            self.after_idle(self._draw)

    def _draw(self):
        self._redraw_pending = False
        cv, k = self.cv, self.kit
        cv.delete("all")
        W, H = max(cv.winfo_width(), 120), max(cv.winfo_height(), 60)
        bg = k.c(self._bg)
        if not self.rows:
            cv.configure(scrollregion=(0, 0, W, H))
            cy = H / 2 - 40
            draw_icon(cv, "upload", W / 2 - 16, cy - 16, 32, k.c("faint"))
            cv.create_text(W / 2, cy + 34, text=self.empty_title, fill=k.c("text"), font=k.fonts.body_bold)
            cv.create_text(W / 2, cy + 58, text=self.empty_body, fill=k.c("muted"), font=k.fonts.small,
                           width=min(W - 60, 360), justify="center", anchor="n")
            return
        f_t, f_s, f_c = k.fonts.body, k.fonts.small, k.fonts.caption
        chosen = set(self.marked_rows())
        for i, r in enumerate(self.rows):
            y0 = 6 + i * self.ROW_H
            x0, x1 = 8, W - 8
            if i in chosen:
                round_rect(cv, x0, y0 + 2, x1, y0 + self.ROW_H - 2, 8, fill=k.c("select"), outline="")
            elif i == self.hover:
                round_rect(cv, x0, y0 + 2, x1, y0 + self.ROW_H - 2, 8, fill=k.c("hover"), outline="")
            state = r.get("state", "queued")
            icon, icol = {
                "running": ("dot", k.c("accent")), "done": ("check-circle", k.c("success")),
                "failed": ("alert", k.c("danger")), "skipped": ("skip", k.c("faint")),
                "waiting": ("clock", k.c("warning")),
            }.get(state, ("clock", k.c("faint")))
            draw_icon(cv, icon, x0 + 10, y0 + 9, 16, icol, bg=bg)
            # chip on the right
            chip = r.get("chip") or ""
            cw = 0
            if chip:
                cw = f_c.measure(chip) + 14
                cfill, ctext = {
                    "running": ("accent_bg", "accent_text"), "done": ("success_bg", "success"),
                    "failed": ("danger_bg", "danger"), "waiting": ("warning_bg", "warning"),
                }.get(state, ("surface2", "muted"))
                round_rect(cv, x1 - 10 - cw, y0 + 8, x1 - 10, y0 + 24, 5, fill=k.c(cfill), outline="")
                cv.create_text(x1 - 10 - cw / 2, y0 + 16, text=chip, fill=k.c(ctext), font=f_c)
            tx = x0 + 34
            cv.create_text(tx, y0 + 16, text=truncate(f_t, r.get("title", ""), x1 - 20 - cw - tx),
                           anchor="w", fill=k.c("faint") if state == "skipped" else k.c("text"), font=f_t)
            cv.create_text(tx, y0 + 32, text=truncate(f_s, r.get("subtitle", ""), x1 - 14 - tx),
                           anchor="w", fill=k.c("muted"), font=f_s)
            p = r.get("progress")
            if p is not None and state == "running":
                bx0, bx1, by = tx, x1 - 12, y0 + self.ROW_H - 7
                round_rect(cv, bx0, by, bx1, by + 3, 1.5, fill=k.c("track"), outline="")
                if p > 0:
                    round_rect(cv, bx0, by, bx0 + max(3, (bx1 - bx0) * p), by + 3, 1.5,
                               fill=k.c("accent_fill"), outline="")
            if i not in chosen and i + 1 not in chosen and i < len(self.rows) - 1:
                cv.create_line(tx, y0 + self.ROW_H, x1 - 8, y0 + self.ROW_H, fill=k.c("border"))
        if self._drop is not None and self._press is not None:
            # where the dragged row lands: an accent line between two rows
            ly = 6 + self._drop * self.ROW_H
            cv.create_line(16, ly, W - 16, ly, fill=k.c("accent"), width=2, capstyle="round")
            cv.create_oval(10, ly - 4, 18, ly + 4, outline=k.c("accent"), width=2, fill=bg)
        total = 12 + len(self.rows) * self.ROW_H
        cv.configure(scrollregion=(0, 0, W, max(total, H)))


class LogText(tk.Text):
    """The log widget. `_textbox` returns the widget itself (the app code was written for
    CTkTextbox), and every line inserted at the end is mirrored into an optional tail
    widget (the last lines shown in the job card)."""

    def __init__(self, parent, kit: Kit, mirror: tk.Text | None = None, mirror_lines=8, **kw):
        super().__init__(parent, bd=0, highlightthickness=0, relief="flat", wrap="none",
                         padx=10, pady=8, font=kit.fonts.mono, **kw)
        self.kit, self.mirror, self.mirror_lines = kit, mirror, mirror_lines
        self.mirror_visible = mirror_lines      # lines that fit in the tail widget right now
        kit.style(self, bg="surface", fg="log", insertbackground="log", selectbackground="select",
                  selectforeground="text")
        kit.register(self)
        self.apply_palette()

    @property
    def _textbox(self):
        return self

    def apply_palette(self):
        c = self.kit.c
        for w in [self] + ([self.mirror] if self.mirror is not None else []):
            for tag, tok in (("SUCCESS", "success"), ("OK", "success"), ("ERROR", "danger"),
                             ("WARN", "warning"), ("INFO", "log"), ("PROGRESS", "accent_text"),
                             ("DEBUG", "log_debug")):
                try:
                    w.tag_configure(tag, foreground=c(tok))
                except tk.TclError:
                    pass

    def insert(self, index, chars, *args):
        super().insert(index, chars, *args)
        m = self.mirror
        if m is not None and str(index) == "end":
            try:
                m.configure(state="normal")
                m.insert("end", chars, *args)
                text_lines = self._mirror_line_count()
                if text_lines > self.mirror_lines:           # a bounded history
                    m.delete("1.0", f"{text_lines - self.mirror_lines + 1}.0")
                    text_lines = self.mirror_lines
                self._show_mirror_tail(text_lines)
                m.configure(state="disabled")
            except tk.TclError:
                pass

    def _mirror_line_count(self) -> int:
        m = self.mirror
        last = int(m.index("end-1c").split(".")[0])
        return last - 1 if m.get(f"{last}.0", "end-1c") == "" else last

    def _show_mirror_tail(self, text_lines: int | None = None):
        """Show the newest mirror_visible lines, the oldest of them at the top: a view
        scrolled to the end would show the empty line after the last newline and a
        sliver of the line above."""
        n = self._mirror_line_count() if text_lines is None else text_lines
        self.mirror.yview(f"{max(1, n - self.mirror_visible + 1)}.0")

    def set_mirror_visible(self, lines: int):
        """The tail widget changed its height: show as many lines as fit now."""
        self.mirror_visible = max(1, int(lines))
        if self.mirror is not None:
            try:
                self._show_mirror_tail()
            except tk.TclError:
                pass

    def delete(self, index1, index2=None):
        super().delete(index1, index2)
        if str(index1) == "1.0" and str(index2) == "end":
            self.clear_mirror()

    def clear_mirror(self):
        if self.mirror is not None:
            self.mirror.configure(state="normal")
            self.mirror.delete("1.0", "end")
            self.mirror.configure(state="disabled")


class IconView(tk.Canvas):
    """One icon on its own small canvas, in a palette colour."""

    def __init__(self, parent, kit: Kit, icon: str, size=16, color="muted", bg="surface"):
        super().__init__(parent, width=size, height=size, highlightthickness=0, bd=0, bg=kit.c(bg))
        self.kit, self.icon, self.size_, self.color, self._bg = kit, icon, size, color, bg
        kit.register(self)
        self.apply_palette()

    def set(self, icon=None, color=None):
        self.icon = icon or self.icon
        self.color = color or self.color
        self.apply_palette()

    def apply_palette(self):
        self.configure(bg=self.kit.c(self._bg))
        self.delete("all")
        draw_icon(self, self.icon, 0, 0, self.size_, self.kit.c(self.color), bg=self.kit.c(self._bg))


class ArtView(tk.Canvas):
    """The cover art square: a photo when there is one, else a placeholder icon."""

    def __init__(self, parent, kit: Kit, size=72, bg="surface"):
        super().__init__(parent, width=size, height=size, highlightthickness=0, bd=0, bg=kit.c(bg))
        self.kit, self.size_, self._bg, self._photo = kit, size, bg, None
        kit.register(self)
        self.apply_palette()

    def set_photo(self, photo):
        self._photo = photo           # keep a reference: Tk drops unreferenced images
        self.apply_palette()

    def apply_palette(self):
        k, s = self.kit, self.size_
        self.configure(bg=k.c(self._bg))
        self.delete("all")
        if self._photo is not None:
            self.create_image(s / 2, s / 2, image=self._photo)
            return
        round_rect(self, 0, 0, s, s, 10, fill=k.c("surface2"), outline=k.c("border"))
        draw_icon(self, "photo", s / 2 - 12, s / 2 - 12, 24, k.c("faint"))


class Tile(tk.Canvas):
    """A rounded metric tile: a caption over a value (both follow StringVars)."""

    def __init__(self, parent, kit: Kit, caption: str, var: tk.StringVar, bg="surface", fill="surface2", height=46):
        super().__init__(parent, height=height, highlightthickness=0, bd=0, bg=kit.c(bg))
        self.kit, self._bg, self._fill, self.caption, self.var = kit, bg, fill, caption, var
        kit.register(self)
        on_resize(self, self._draw)
        var.trace_add("write", lambda *_: self._draw())

    def apply_palette(self):
        self.configure(bg=self.kit.c(self._bg))
        self._draw()

    def _draw(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self.delete("all")
        W, H = self.winfo_width(), int(self.winfo_fpixels(self.cget("height")))
        if W <= 2:
            return
        k = self.kit
        round_rect(self, 0, 0, W, H, 9, fill=k.c(self._fill), outline="")
        self.create_text(12, 14, text=self.caption, anchor="w", fill=k.c("faint"), font=k.fonts.caption)
        self.create_text(12, 31, text=truncate(k.fonts.metric, self.var.get(), W - 20), anchor="w",
                         fill=k.c("text"), font=k.fonts.metric)


class RoundBox(tk.Frame):
    """A rounded background behind one child widget (e.g. a tk.Text), drawn on a canvas.
    Create the child with `box.inner` as its parent and call box.set_child(child)."""

    def __init__(self, parent, kit: Kit, bg="surface", fill="surface2", radius=9, pad=8, height=100):
        super().__init__(parent, bd=0, highlightthickness=0, bg=kit.c(bg))
        self.kit, self._bg, self._fill, self.r, self.pad = kit, bg, fill, radius, pad
        self.cv = tk.Canvas(self, height=height, highlightthickness=0, bd=0, bg=kit.c(bg))
        self.cv.pack(fill="both", expand=True)
        self.inner = self.cv
        self._child = None
        self._win = None
        kit.register(self)
        on_resize(self.cv, self._draw)

    def set_child(self, child):
        self._child = child
        self._win = self.cv.create_window(self.pad, self.pad, window=child, anchor="nw")
        self._draw()

    def apply_palette(self):
        self.configure(bg=self.kit.c(self._bg))
        self.cv.configure(bg=self.kit.c(self._bg))
        self._draw()

    def _draw(self):
        self.cv.delete("bgshape")
        W, H = self.cv.winfo_width(), self.cv.winfo_height()
        if W <= 2 or H <= 2:
            return
        round_rect(self.cv, 0, 0, W, H, self.r, fill=self.kit.c(self._fill), outline="", tags=("bgshape",))
        self.cv.tag_lower("bgshape")
        if self._win is not None:
            self.cv.itemconfigure(self._win, width=max(10, W - 2 * self.pad), height=max(10, H - 2 * self.pad))
