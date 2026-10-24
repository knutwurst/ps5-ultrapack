"""English in the code, German in i18n_de.py; install() translates texts as they reach Tk"""
from __future__ import annotations

LANGUAGES = [("en", "English"), ("de", "Deutsch")]

_table: dict[str, str] = {}
_lang = "en"
_installed = False


def use(language: str) -> None:
    global _table, _lang
    _lang = language if language in dict(LANGUAGES) else "en"
    if _lang == "de":
        import sys
        from i18n_de import GERMAN
        _table = dict(GERMAN)
        if sys.platform != "darwin":          # Windows names these menu entries differently
            _table.update({"File": "Datei", "View": "Ansicht", "Paste": "Einfügen"})
    else:
        _table = {}


def language() -> str:
    return _lang


def t(text: str, **values) -> str:
    out = _table.get(text, text) if _table else text
    return out.format(**values) if values else out


def plural(count: int, one: str, many: str, **values) -> str:
    """{n} is the count unless values bring their own n"""
    return t(one if count == 1 else many, **{"n": count, **values})


_swap = str.maketrans(".,", ",.")


def number(text: str) -> str:
    """12.5 GB reads 12,5 GB in German."""
    return text.translate(_swap) if _lang == "de" else text


def _tr(value):
    if not isinstance(value, str) or not value:
        return value
    hit = _table.get(value)
    if hit is not None:
        return hit
    core = value.strip()
    if core != value and core in _table:          # a text padded with spaces for alignment
        return value.replace(core, _table[core], 1)
    return value


_KEYS = ("text", "label")


def install() -> None:
    """translates static texts as they reach Tk, only with a table loaded"""
    global _installed
    if _installed or not _table:
        return
    _installed = True
    import tkinter as tk

    options = tk.Misc._options

    def _options(self, cnf, kw=None):
        if kw:
            cnf = tk._cnfmerge((cnf, kw))
        elif cnf:
            cnf = tk._cnfmerge(cnf)
        if cnf and any(k in cnf for k in _KEYS):
            cnf = dict(cnf)
            for k in _KEYS:
                if k in cnf:
                    cnf[k] = _tr(cnf[k])
        return options(self, cnf)
    tk.Misc._options = _options

    title = tk.Wm.wm_title

    def wm_title(self, string=None):
        return title(self, _tr(string))
    tk.Wm.wm_title = tk.Wm.title = wm_title

    from tkinter import messagebox, commondialog, ttk
    show = messagebox._show

    def _show(title=None, message=None, _icon=None, _type=None, **options):
        if "detail" in options:
            options["detail"] = _tr(options["detail"])
        return show(_tr(title), _tr(message), _icon, _type, **options)
    messagebox._show = _show

    dshow = commondialog.Dialog.show

    def dialog_show(self, **options):
        if "title" in options:
            options["title"] = _tr(options["title"])
        if "title" in self.options:
            self.options["title"] = _tr(self.options["title"])
        for opts in (options, self.options):
            if opts.get("filetypes"):
                opts["filetypes"] = [(_tr(d), p) for d, p in opts["filetypes"]]
        return dshow(self, **options)
    commondialog.Dialog.show = dialog_show

    fmt = ttk._format_optdict

    def _format_optdict(optdict, script=False, ignore=None):
        if optdict and "text" in optdict:
            optdict = dict(optdict, text=_tr(optdict["text"]))
        return fmt(optdict, script, ignore)
    ttk._format_optdict = _format_optdict
