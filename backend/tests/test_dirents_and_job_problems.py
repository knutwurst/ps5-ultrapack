# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
"""Directory entries stay inside their block; failed jobs name their error, checks their warnings."""
import struct
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

from mkpfs import pfs  # noqa: E402
import ultra_core  # noqa: E402

BS = 65536


def block_walk(blob: bytes, bs: int) -> list[str]:
    """how a reader that walks a directory block by block sees it (LibProsperoPkg's PfsReader)"""
    names = []
    for start in range(0, len(blob), bs):
        blk = blob[start:start + bs].ljust(bs, b"\0")
        off = 0
        while off + 16 <= bs:
            _ino, _typ, nl, es = struct.unpack_from("<Iiii", blk, off)
            if es == 0:
                break
            if off + es > bs:
                raise ValueError(f"entry at {start + off} runs into the next block")
            names.append(blk[off + 16:off + 16 + nl].decode("ascii"))
            off += es
    return names


class DirectoryBlobTest(unittest.TestCase):
    def entries(self, n):
        return [pfs.Dirent(i + 10, 2, f"asset_texture_variant_{i:05d}.bin") for i in range(n)]

    def test_small_folder_is_unchanged(self):
        ents = self.entries(20)
        self.assertEqual(pfs.directory_blob(ents, BS), b"".join(e.to_bytes() for e in ents))

    def test_no_entry_crosses_a_block(self):
        ents = self.entries(5000)
        blob = pfs.directory_blob(ents, BS)
        self.assertGreater(len(blob), 2 * BS)
        self.assertFalse(pfs.dirents_cross_blocks(blob, BS))
        self.assertEqual(block_walk(blob, BS), [e.name for e in ents])
        parsed, errors = pfs.parse_image_dirents(blob, strict=True)
        self.assertEqual(errors, [])
        self.assertEqual([d.name for d in parsed], [e.name for e in ents])

    def test_old_layout_is_detected(self):
        ents = self.entries(5000)
        old = b"".join(e.to_bytes() for e in ents)
        self.assertTrue(pfs.dirents_cross_blocks(old, BS))
        with self.assertRaises(ValueError):
            block_walk(old, BS)


class JobProblemsTest(unittest.TestCase):
    def test_specific_error_beats_the_wrapper(self):
        lines = ["[error] EndOfStreamException: Unable to read beyond the end of the stream.",
                 "[ERROR] fPKG build failed (rc=1)."]
        err, warn = ultra_core.job_problems(lines)
        self.assertEqual(err, "EndOfStreamException: Unable to read beyond the end of the stream.")
        self.assertEqual(warn, "")

    def test_failed_checks_are_the_error(self):
        lines = ["  [ok  ]  header                    content_id=X",
                 "  [FAIL]  license.license.dat       missing (applicationDrmType 'upgradable')",
                 "summary: 37 passed, 0 warned, 1 failed",
                 "[ERROR] [ERROR] The package failed validation (1 check(s)): ..."]
        err, _warn = ultra_core.job_problems(lines)
        self.assertEqual(err, "license.license.dat: missing (applicationDrmType 'upgradable')")

    def test_check_warnings(self):
        lines = ["  [WARN]  param.titleId             title id PPSA01911 differs from PPSA01903 in the content id",
                 "[WARN] Queue finished - 1 of 8 job(s) done, 1 failed."]
        err, warn = ultra_core.job_problems(lines)
        self.assertEqual(err, "")
        self.assertEqual(warn, "param.titleId: title id PPSA01911 differs from PPSA01903 in the content id")

    def test_problem_lines(self):
        self.assertTrue(ultra_core.is_problem_line("[error] x"))
        self.assertTrue(ultra_core.is_problem_line("  [FAIL]  a  b"))
        self.assertFalse(ultra_core.is_problem_line("  [ok  ]  a  b"))


class InPlaceSidecarTest(unittest.TestCase):
    def test_in_place_strips_only_its_own_sidecar(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            game = Path(td) / "Game [PPSA00001]"; game.mkdir()
            out = game / "Game.pkg"; out.write_bytes(b"P")
            (game / "._Game.pkg").write_bytes(b"s")
            (Path(td) / "._Game [PPSA00001]").write_bytes(b"s")     # the folder's, not ours
            self.assertEqual(ultra_core.strip_written_clutter(out, own_only=True), 1)
            self.assertTrue((Path(td) / "._Game [PPSA00001]").exists())
            self.assertFalse((game / "._Game.pkg").exists())


if __name__ == "__main__":
    unittest.main()
