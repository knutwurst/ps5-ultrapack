"""Integrating a patch (backend/cli.py overlay_patch): what it replaces is kept with a README,
an old AMPR index goes when the patch brings its own emulator, release notes stay out, and a
patch for another game is refused.

    PYTHONPATH=. /tmp/ps5venv/bin/python -m unittest backend.tests.test_patch_overlay
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import cli  # noqa: E402


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def param(tid: str, version: str, sdk: str) -> str:
    return json.dumps({"titleId": tid, "contentVersion": version, "sdkVersion": sdk,
                       "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Example Quest"}}})


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="patch_overlay_"))
        self.game = self.tmp / "game"
        write(self.game / "sce_sys" / "param.json", param("PPSA00001", "01.300.300", "0x0C000000"))
        write(self.game / "eboot.bin", b"old eboot")
        write(self.game / "data" / "level.bin", b"level")
        write(self.game / "ampr_emu.index", b"old index")
        write(self.game / "fakelib" / "libSceAmpr.sprx", b"old emulator")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backport(self, tid="PPSA00001", version="01.300.300"):
        root = self.tmp / "patch" / "Example_backport_files"      # one wrapper folder, as shipped
        write(root / "sce_sys" / "param.json", param(tid, version, "0x04030000"))
        write(root / "eboot.bin", b"backported eboot")
        write(root / "fakelib" / "libSceAmpr.sprx", b"new emulator")
        write(root / "fakelib" / "libSceAgc.sprx", b"agc")
        write(root / "README.txt", b"notes")
        write(root / "SHA256SUMS", b"sums")
        return self.tmp / "patch"

    def run_overlay(self, patch, backup=True, **kw):
        bk = cli.new_patch_backup(self.tmp, "Example_backport_files.zip") if backup else None
        out = io.StringIO()
        with redirect_stdout(out):
            n = cli.overlay_patch(self.game, patch, bk, "Example_backport_files.zip", **kw)
        return n, bk, out.getvalue()

    def snapshot(self):
        return {p.relative_to(self.game).as_posix(): p.read_bytes()
                for p in sorted(self.game.rglob("*")) if p.is_file()}

    def test_backport_applied_originals_kept_with_readme(self):
        n, bk, log = self.run_overlay(self.backport())
        self.assertEqual((self.game / "eboot.bin").read_bytes(), b"backported eboot")
        self.assertEqual((self.game / "fakelib" / "libSceAmpr.sprx").read_bytes(), b"new emulator")
        self.assertEqual((bk / "app0" / "eboot.bin").read_bytes(), b"old eboot")
        self.assertEqual((bk / "app0" / "fakelib" / "libSceAmpr.sprx").read_bytes(), b"old emulator")
        self.assertEqual((bk / "app0" / "ampr_emu.index").read_bytes(), b"old index")
        readme = (bk / "README.txt").read_text()
        self.assertIn("Example_backport_files.zip", readme)
        self.assertIn("fakelib/libSceAgc.sprx", readme)            # listed as added
        self.assertIn("[PATCH-BACKUP]", log)
        self.assertEqual(bk.name, "Original files - Example_backport_files")

    def test_old_ampr_index_removed_and_notes_left_out(self):
        self.run_overlay(self.backport())
        self.assertFalse((self.game / "ampr_emu.index").exists())
        self.assertFalse((self.game / "README.txt").exists())
        self.assertFalse((self.game / "SHA256SUMS").exists())
        self.assertTrue((self.game / "data" / "level.bin").is_file())

    def test_patch_for_another_game_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.run_overlay(self.backport(tid="PPSA09999"))
        self.assertEqual((self.game / "eboot.bin").read_bytes(), b"old eboot")

    def test_backport_for_another_version_warns(self):
        _n, _bk, log = self.run_overlay(self.backport(version="01.200.000"))
        self.assertIn("backport made for version 01.200.000", log)

    def test_wrapper_is_still_descended_beside_sidecar_files(self):
        # On an exFAT temp drive macOS writes a `._<folder>` AppleDouble file beside the
        # wrapper folder an archive unpacks to; Finder leaves .DS_Store; a release may put its
        # notes beside the wrapper. None of these is content: the wrapper still has to be
        # descended, or the whole wrapper folder lands inside the game as a second game.
        patch = self.backport()
        write(patch / "._Example_backport_files", b"\x00\x05\x16\x07" + b"\x00" * 4092)
        write(patch / ".DS_Store", b"\x00" * 16)
        write(patch / "README.txt", b"notes beside the wrapper")
        write(patch / "__MACOSX" / "._eboot.bin", b"\x00" * 16)
        self.assertEqual(cli._patch_descend_wrapper(patch), patch / "Example_backport_files")
        self.assertTrue(cli.patch_brings_ampr(patch))
        self.run_overlay(patch)
        self.assertEqual((self.game / "eboot.bin").read_bytes(), b"backported eboot")
        self.assertFalse((self.game / "Example_backport_files").exists())
        self.assertFalse((self.game / "ampr_emu.index").exists())
        self.assertFalse(any(p.name.startswith("._") or p.name == ".DS_Store" for p in self.game.rglob("*")))

    def test_unresolved_wrapper_is_refused_instead_of_nested(self):
        # Two real folders at the top, one of them the game-shaped patch: no wrapper to
        # descend. The patch must not be copied into the game as a folder.
        patch = self.backport()
        write(patch / "Other content" / "thing.bin", b"?")
        with self.assertRaises(RuntimeError) as cm:
            self.run_overlay(patch)
        self.assertIn("Example_backport_files", str(cm.exception))
        self.assertEqual((self.game / "eboot.bin").read_bytes(), b"old eboot")
        self.assertFalse((self.game / "Example_backport_files").exists())

    def test_patch_that_only_adds_leaves_no_backup(self):
        patch = self.tmp / "addon"
        write(patch / "extra" / "new.bin", b"new")
        _n, bk, log = self.run_overlay(patch)
        self.assertFalse(bk.exists())
        self.assertNotIn("[PATCH-BACKUP]", log)


    def test_a_failed_overlay_puts_every_original_back(self):
        write(self.game / "zz", b"a file where the patch needs a folder")
        before = self.snapshot()
        patch = self.tmp / "half"
        write(patch / "data" / "level.bin", b"new level")
        write(patch / "eboot.bin", b"new eboot")
        write(patch / "new.bin", b"added")
        write(patch / "zz" / "blocker", b"cannot land")
        out = io.StringIO()
        bk = cli.new_patch_backup(self.tmp, "half.zip")
        with redirect_stdout(out), self.assertRaises(RuntimeError):
            cli.overlay_patch(self.game, patch, bk, "half.zip")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.game / "new.bin").exists())
        log = out.getvalue()
        self.assertIn("[PATCH-BACKUP]", log)
        self.assertLess(log.index("[PATCH-BACKUP]"), log.index("[ERROR]"))

    def test_restore_originals_undoes_a_finished_overlay(self):
        import patch_backup
        before = self.snapshot()
        _n, bk, log = self.run_overlay(self.backport())
        self.assertNotEqual(self.snapshot(), before)
        n = patch_backup.restore_originals(bk, self.game)
        self.assertGreater(n, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(patch_backup.restore_originals(bk, self.game), n, "running it twice is harmless")
        self.assertEqual(self.snapshot(), before)

    def test_restore_removes_what_an_add_only_patch_brought(self):
        import patch_backup
        before = self.snapshot()
        patch = self.tmp / "addon"
        write(patch / "extra" / "deep" / "new.bin", b"new")
        write(patch / "more.bin", b"more")
        _n, bk, log = self.run_overlay(patch, restorable=True)
        self.assertIn("[PATCH-BACKUP]", log)
        self.assertTrue((self.game / "extra" / "deep" / "new.bin").is_file())
        patch_backup.restore_originals(bk, self.game)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.game / "extra").exists())

    def test_restore_refuses_another_folder(self):
        import patch_backup
        _n, bk, _log = self.run_overlay(self.backport())
        other = self.tmp / "other"
        other.mkdir()
        with self.assertRaises(ValueError):
            patch_backup.restore_originals(bk, other)
        self.assertEqual(list(other.iterdir()), [])
        self.assertEqual(patch_backup.restore_originals(self.tmp / "missing", self.game), 0)


if __name__ == "__main__":
    unittest.main()
