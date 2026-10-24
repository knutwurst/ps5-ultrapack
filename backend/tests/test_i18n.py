import ast
import string
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import i18n  # noqa: E402
from i18n_de import GERMAN  # noqa: E402


def _fields(text):
    return {f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(text) if f}


def _translated_literals():
    out = {}
    for name in ("PS5_UltraPack.py", "ultra_core.py", "ui_kit.py"):
        tree = ast.parse((REPO / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("tr", "t", "plural"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        out.setdefault(arg.value, f"{name}:{node.lineno}")
    return out


class GermanTable(unittest.TestCase):
    def test_every_translated_text_has_a_german_entry(self):
        missing = {k: where for k, where in _translated_literals().items()
                   if k not in GERMAN and k.strip() and any(c.isalpha() for c in k)}
        self.assertEqual(missing, {}, "texts passed to tr()/plural() without a German entry")

    def test_translations_use_only_the_values_the_english_text_passes(self):
        bad = {k: sorted(_fields(v) - _fields(k)) for k, v in GERMAN.items() if not _fields(v) <= _fields(k)}
        self.assertEqual(bad, {})

    def test_translations_keep_line_breaks_and_edge_spaces(self):
        bad = [k for k, v in GERMAN.items()
               if k.count("\n") != v.count("\n") or (k.endswith(" ") != v.endswith(" "))
               or (k.startswith(" ") != v.startswith(" "))]
        self.assertEqual(bad, [])


class Lookup(unittest.TestCase):
    def tearDown(self):
        i18n.use("en")

    def test_english_is_the_text_itself(self):
        i18n.use("en")
        self.assertEqual(i18n.t("{n} jobs selected", n=3), "3 jobs selected")
        self.assertEqual(i18n.plural(1, "1 job", "{n} jobs"), "1 job")
        self.assertEqual(i18n.number("12.50 GB"), "12.50 GB")

    def test_german_lookup_plural_and_numbers(self):
        i18n.use("de")
        self.assertEqual(i18n.t("Settings"), "Einstellungen")
        self.assertEqual(i18n.plural(2, "Remove 1 job", "Remove {n} jobs"), "2 Jobs entfernen")
        self.assertEqual(i18n.plural(1200, "1 file", "{n} files", n=i18n.number("1,200")), "1.200 Dateien")
        self.assertEqual(i18n.number("12.50 GB"), "12,50 GB")
        self.assertEqual(i18n.t("not in the table"), "not in the table")

    def test_an_unknown_language_falls_back_to_english(self):
        i18n.use("xx")
        self.assertEqual(i18n.language(), "en")
        self.assertEqual(i18n.t("Settings"), "Settings")


if __name__ == "__main__":
    unittest.main()
