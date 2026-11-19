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


def gap_layout(ents, bs: int) -> bytes:
    """LibOrbisPkg's PFSBuilder: a block ends with zeros once the next entry might not fit"""
    out = bytearray()
    for e in ents:
        raw = e.to_bytes()
        out += raw
        if len(out) % bs > bs - 280:
            out += b"\x00" * (bs - len(out) % bs)
    return bytes(out)


class ReaderLayoutsTest(unittest.TestCase):
    """the Python reader takes all three layouts a directory can have"""
    ents = [pfs.Dirent(i + 10, 2, f"asset_texture_variant_{i:05d}.bin") for i in range(5000)]
    want = [e.name for e in ents]

    def parse(self, blob):
        parsed, errors = pfs.parse_image_dirents(blob, strict=True, block_size=BS)
        self.assertEqual(errors, [])
        return [d.name for d in parsed]

    def test_new_layout(self):
        self.assertEqual(self.parse(pfs.directory_blob(self.ents, BS)), self.want)

    def test_old_layout(self):
        self.assertEqual(self.parse(b"".join(e.to_bytes() for e in self.ents)), self.want)

    def test_gap_layout(self):
        blob = gap_layout(self.ents, BS)
        self.assertEqual(block_walk(blob, BS), self.want)       # what the libraries see
        self.assertEqual(self.parse(blob), self.want)
        self.assertFalse(pfs.dirents_cross_blocks(blob, BS))
        # without the block size it stops at the first gap: what the reader did before
        self.assertLess(len(pfs.parse_image_dirents(blob)[0]), len(self.want))


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


class LibraryVerdictTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(HERE.parent))
        import ps5_repair
        self.m = ps5_repair

    def row(self, check, level, msg="x"):
        return {"Check": check, "Level": level, "Message": msg}

    def test_half_read_package_is_never_fine(self):
        info = {"ident": {"content_type": 32}, "results": [self.row("header", "pass")], "error": "listing failed"}
        self.assertEqual(self.m.verdict(info)["state"], "error")

    def test_junk_only_game_is_rebuilt_addon_without_eboot_is_fine(self):
        game = {"ident": {"content_type": 32}, "results": [self.row("inner.junk", "fail", "3 OS junk file(s)")]}
        self.assertEqual(self.m.verdict(game)["state"], "rebuild")
        addon = {"ident": {"content_type": 33}, "results": [self.row("inner.eboot", "pass")]}
        self.assertEqual(self.m.verdict(addon)["state"], "ok")

    def test_update_package_is_never_rebuilt(self):
        upd = {"ident": {"content_type": 32, "package_type": "patch"},
               "results": [self.row("inner.junk", "fail", "3 OS junk file(s)")]}
        v = self.m.verdict(upd)
        self.assertEqual((v["state"], v["update"]), ("problem", True))


class FailedLogTest(unittest.TestCase):
    def test_kept_and_capped(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw.log"; raw.write_text("[error] boom\n")
            with mock.patch.object(ultra_core, "RAW_LOG_FILE", raw), \
                 mock.patch.object(ultra_core, "FAILED_LOGS_DIR", Path(td) / "failed"), \
                 mock.patch.object(ultra_core, "FAILED_LOGS_KEPT", 3):
                paths = [ultra_core.keep_failed_log(f"Game {i}/x") for i in range(5)]
                self.assertTrue(Path(paths[-1]).read_text().startswith("[error] boom"))
                self.assertEqual(len(list((Path(td) / "failed").glob("*.log"))), 3)
                self.assertNotIn("/x", Path(paths[-1]).name)


if __name__ == "__main__":
    unittest.main()
