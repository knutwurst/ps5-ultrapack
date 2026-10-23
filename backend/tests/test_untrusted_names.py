"""Names read from downloaded images and packages never leave the target folder.

    PYTHONPATH=. .venv/bin/python backend/tests/test_untrusted_names.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from mkpfs.exfat_writer import write_exfat_image  # noqa: E402
from mkpfs.pfs import extract_exfat_image  # noqa: E402

PLAIN = "AAAAAAAAAAAAAAA"


def _image_with_name(tmp: Path, name: str) -> Path:
    """An exFAT image whose one file is called *name* (same length as PLAIN)."""
    assert len(name) == len(PLAIN)
    src = tmp / "src"
    src.mkdir()
    (src / PLAIN).write_bytes(b"x")
    img = Path(write_exfat_image(src, tmp / "test.exfat"))
    data = bytearray(img.read_bytes())
    old, new = PLAIN.encode("utf-16-le"), name.encode("utf-16-le")
    assert data.count(old) == 1
    img.write_bytes(data.replace(old, new))
    return img


class ExfatNames(unittest.TestCase):
    def test_parent_reference_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            img = _image_with_name(tmp, "../../../ESC_XX")
            res = extract_exfat_image(img, tmp / "a" / "b" / "c" / "out")
            self.assertTrue(res.errors, "the bad name must fail the extraction")
            self.assertEqual([p for p in tmp.rglob("ESC_XX")], [])

    def test_separator_and_control_characters_are_refused(self):
        for bad in ("AAAAAA/AAAAAAAA", "AAAAAA\nAAAAAAAA", "..\x00AAAAAAAAAAAA"[:15]):
            with self.subTest(name=repr(bad)), tempfile.TemporaryDirectory() as t:
                tmp = Path(t)
                img = _image_with_name(tmp, bad)
                out = tmp / "out"
                res = extract_exfat_image(img, out)
                self.assertTrue(res.errors)
                self.assertFalse(any(out.rglob("*")) if out.exists() else False)

    def test_other_odd_names_stay_inside(self):
        # legal on macOS, so the app's own writer may store them: readable, and never outside
        for odd in ("AAAAAA\\AAAAAAAA", "AAAAAAA:AAAAAAA", "what?AAAAAAAAAA", "..\\..\\AAAAAAAAA"):
            with self.subTest(name=repr(odd)), tempfile.TemporaryDirectory() as t:
                tmp = Path(t)
                img = _image_with_name(tmp, odd)
                out = tmp / "a" / "out"
                extract_exfat_image(img, out)
                inside = {p.resolve() for p in out.rglob("*")} if out.exists() else set()
                everything = {p.resolve() for p in tmp.rglob("*") if p.is_file() and p.suffix != ".exfat"
                              and "src" not in p.parts}
                self.assertTrue(everything <= inside, everything - inside)

    def test_normal_names_still_extract(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            img = _image_with_name(tmp, "Plain name 01.x")
            res = extract_exfat_image(img, tmp / "out")
            self.assertEqual(res.errors, [])
            self.assertTrue((tmp / "out" / "Plain name 01.x").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=1)
