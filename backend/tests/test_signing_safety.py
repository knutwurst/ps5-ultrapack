"""python3 -m unittest backend.tests.test_signing_safety"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from backend import fake_sign, make_fself
from backend.tests.test_backport import _signable_elf


class TruncatedElf(unittest.TestCase):
    def test_a_short_segment_read_raises(self):
        elf = _signable_elf()
        with self.assertRaisesRegex(make_fself.ElfTruncatedError, "truncated"):
            make_fself.ElfFile(ignore_shdrs=True).load(io.BytesIO(elf[:0x200 + 16]))

    def test_a_truncated_eboot_fails_and_stays_untouched(self):
        cut = _signable_elf()[:0x200 + 16]
        with tempfile.TemporaryDirectory() as td:
            eboot = Path(td) / "eboot.bin"
            eboot.write_bytes(cut)
            lines: list[str] = []
            counts = fake_sign.fake_sign_tree(td, log=lines.append)
            self.assertEqual((counts["signed"], counts["skipped"], counts["failed"]), (0, 0, 1))
            self.assertEqual(eboot.read_bytes(), cut)
            self.assertTrue(any(l.startswith("[FAIL]") and "truncated" in l for l in lines), lines)
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ["eboot.bin"])

    def test_a_whole_elf_still_signs(self):
        with tempfile.TemporaryDirectory() as td:
            eboot = Path(td) / "eboot.bin"
            eboot.write_bytes(_signable_elf())
            counts = fake_sign.fake_sign_tree(td, log=lambda _m: None)
            self.assertEqual(counts["signed"], 1)
            self.assertEqual(eboot.read_bytes()[:4], make_fself.SignedElfFile.MAGIC)


if __name__ == "__main__":
    unittest.main()
