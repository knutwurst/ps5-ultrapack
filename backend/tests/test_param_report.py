"""python3 -m unittest backend.tests.test_param_report"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "backend" / "cli.py"


def make_game(root: Path, name: str, tid: str, attribute: int, fw: str = "0x0910000000000000") -> Path:
    g = root / name
    (g / "sce_sys").mkdir(parents=True)
    (g / "sce_sys" / "param.json").write_text(json.dumps({
        "titleId": tid, "contentVersion": "01.000.000", "attribute": attribute,
        "requiredSystemSoftwareVersion": fw, "sdkVersion": "0x0850000000000000",
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": f"Example {name}"}},
    }), encoding="utf-8")
    (g / "eboot.bin").write_bytes(b"\x7fELF" + b"\0" * 60)
    return g


class ParamReport(unittest.TestCase):
    def test_hdr_bit_is_reported_per_game_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_game(root, "A", "PPSA00001", 0x20000000)
            make_game(root, "B", "PPSA00002", 0)
            before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            r = subprocess.run([sys.executable, str(CLI), "--param-report", str(root)],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            lines = r.stdout.splitlines()
            self.assertTrue(any(l.startswith("yes") and "PPSA00001" in l for l in lines), r.stdout)
            self.assertTrue(any(l.startswith("no") and "PPSA00002" in l for l in lines), r.stdout)
            self.assertIn("2 game(s): 1 declare HDR, 1 do not", r.stdout)
            row = next(l for l in lines if "PPSA00001" in l)
            self.assertIn(" 9.10 ", row, "required system software shown")
            self.assertIn(" 8.50 ", row, "SDK version shown")
            after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            self.assertEqual(before, after, "the report must not write anything next to the sources")


class IdentityHelpers(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(REPO / "backend"))
        import cli
        self.cli = cli

    def test_content_version_is_normalised(self):
        cv = self.cli._content_version
        self.assertEqual(cv("01.03"), "01.003.000")        # a masterVersion
        self.assertEqual(cv("01.003"), "01.003.000")
        self.assertEqual(cv("02.100.001"), "02.100.001")
        self.assertEqual(cv(""), "")

    def test_title_is_cleaned(self):
        self.assertEqual(self.cli._clean_title("\ufeff  My\tGame\x00  Two "), "My Game Two")

    def test_firmware_text(self):
        self.assertEqual(self.cli._firmware_text("0x0910000000000000"), "9.10")
        self.assertEqual(self.cli._firmware_text("0x1000000000000000"), "10.00")
        self.assertEqual(self.cli._firmware_text("bogus"), "")


if __name__ == "__main__":
    unittest.main()
