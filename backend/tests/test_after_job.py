"""Unit tests for backend/after_job.py: which sources may be touched after a job, and the
keep / Trash / move / delete actions, on scratch folders only."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRATCH = Path(tempfile.mkdtemp(prefix="after_job_test_"))
os.environ.setdefault("PS5_FFPFSC_APP_DIR", str(_SCRATCH / "app_dir"))   # before ultra_core loads
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import after_job as aj  # noqa: E402
import ultra_core  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="case_", dir=_SCRATCH))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def game(self, name="Game", files=(("eboot.bin", 300), ("data/a.bin", 700))):
        root = self.tmp / "src" / name
        for rel, size in files:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"G" * size)
        return root

    def file(self, rel, size=100):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"F" * size)
        return p


class RefusalTests(Base):
    def test_plain_source_may_go(self):
        g = self.game()
        self.assertIsNone(aj.refusal([g], output=self.tmp / "out" / "Game.ffpfsc"))

    def test_output_inside_the_source_stays(self):
        g = self.game()
        self.assertIn("holds the job's output", aj.refusal([g], output=g / "Game.ffpfsc"))

    def test_source_inside_the_output_stays(self):
        out = self.tmp / "out"
        g = self.file("out/Game/Game.pkg")
        self.assertIn("inside the job's output", aj.refusal([g], output=out))

    def test_source_next_to_its_output_stays(self):
        # a library .ffpfsc and the .pkg built from it, in one folder: both stay
        lib = self.file("lib/Game [PPSA00001]/Game.ffpfsc")
        out = lib.parent / "Game.pkg"
        out.write_bytes(b"P")
        self.assertIn("next to its output", aj.refusal([lib], output=out))
        # one level down (a download folder inside the output folder) is not next to it
        dl = self.file("lib/Downloads/Other.ffpfsc")
        self.assertIsNone(aj.refusal([dl], output=self.tmp / "lib" / "Other.pkg"))

    def test_same_file_as_output_stays(self):
        g = self.file("lib/Game.ffpfsc")
        self.assertIsNotNone(aj.refusal([g], output=g))

    def test_another_waiting_job_needs_it(self):
        g = self.game()
        self.assertIn("another job", aj.refusal([g], output=self.tmp / "o.pkg", others=[g]))
        self.assertIn("another job", aj.refusal([g], output=self.tmp / "o.pkg", others=[g / "eboot.bin"]))

    def test_app_folders_are_protected(self):
        g = self.game()
        self.assertIn("app's own folder", aj.refusal([g], protected=[g / "_ffpfsc_temp"]))
        self.assertIn("app's own folder", aj.refusal([g], protected=[self.tmp]))

    def test_drive_and_home_stay(self):
        self.assertIn("whole drive", aj.refusal([Path("/")]))
        self.assertIn("home folder", aj.refusal([Path.home().parent]))

    def test_missing_source(self):
        self.assertIn("no longer there", aj.refusal([self.tmp / "gone"]))

    def test_destination_inside_the_source(self):
        g = self.game()
        self.assertIn("destination folder is inside", aj.refusal([g], dest=g / "done"))

    def test_nothing_to_act_on(self):
        self.assertIsNotNone(aj.refusal([]))


class ArchiveSetTests(Base):
    def test_every_part_and_nothing_else(self):
        parts = [self.file(f"dl/Game.part{i}.rar") for i in (1, 2, 3)]
        self.file("dl/Game Update.part1.rar")
        self.file("dl/Game.nfo")
        self.assertEqual(sorted(ultra_core.archive_set_parts(parts[0])), sorted(parts))

    def test_single_archive(self):
        z = self.file("dl/Game.zip")
        self.assertEqual(ultra_core.archive_set_parts(z), [z])

    def test_old_style_volumes(self):
        parts = [self.file("dl/Game.rar")] + [self.file(f"dl/Game.r{i:02d}") for i in range(3)]
        self.assertEqual(sorted(ultra_core.archive_set_parts(parts[0])), sorted(parts))


class ActionTests(Base):
    def test_keep_does_nothing(self):
        g = self.game()
        self.assertEqual(aj.apply(aj.KEEP, [g]), [])
        self.assertTrue(g.is_dir())

    def test_delete_file_and_folder(self):
        g, f = self.game(), self.file("x/Game.pkg")
        aj.apply(aj.DELETE, [g, f])
        self.assertFalse(g.exists() or f.exists())

    def test_delete_a_link_keeps_its_target(self):
        g = self.game()
        link = self.tmp / "link"
        link.symlink_to(g)
        aj.apply(aj.DELETE, [link])
        self.assertFalse(os.path.lexists(link))
        self.assertTrue((g / "eboot.bin").is_file())

    def test_trash_calls_the_trash_for_every_part(self):
        parts = [self.file(f"dl/Game.part{i}.rar") for i in (1, 2)]
        seen = []
        aj.apply(aj.TRASH, parts, trash=seen.append)
        self.assertEqual(seen, parts)

    def test_move_same_drive_numbers_a_taken_name(self):
        g = self.game()
        dest = self.tmp / "done"
        (dest / "Game").mkdir(parents=True)
        out = aj.apply(aj.MOVE, [g], dest=dest)
        self.assertEqual(out, [str(dest / "Game (2)")])
        self.assertTrue((dest / "Game (2)" / "data" / "a.bin").is_file())
        self.assertFalse(g.exists())

    def test_move_archive_set_keeps_one_number(self):
        parts = [self.file(f"dl/Game.part{i}.rar") for i in (1, 2)]
        dest = self.tmp / "done"
        dest.mkdir()
        (dest / "Game.part1.rar").write_bytes(b"old")
        out = aj.apply(aj.MOVE, parts, dest=dest)
        self.assertEqual(sorted(Path(p).name for p in out), ["Game (2).part1.rar", "Game (2).part2.rar"])
        self.assertEqual((dest / "Game.part1.rar").read_bytes(), b"old")

    def test_move_to_another_drive_copies_checks_then_removes(self):
        g = self.game()
        (g / "link").symlink_to("eboot.bin")
        dest = self.tmp / "other_drive"
        seen = []
        out = aj.apply(aj.MOVE, [g], dest=dest, same_device=lambda a, b: False,
                       on_progress=lambda d, t: seen.append((d, t)))
        moved = Path(out[0])
        self.assertFalse(g.exists())
        self.assertEqual((moved / "data" / "a.bin").read_bytes(), b"G" * 700)
        self.assertEqual(os.readlink(moved / "link"), "eboot.bin")
        self.assertEqual(seen[-1][0], seen[-1][1])

    def test_failed_copy_leaves_the_source(self):
        g = self.game()
        dest = self.tmp / "other_drive"
        with mock.patch.object(aj, "_copy_file", side_effect=OSError("drive gone")):
            with self.assertRaises(OSError):
                aj.apply(aj.MOVE, [g], dest=dest, same_device=lambda a, b: False)
        self.assertTrue((g / "data" / "a.bin").is_file())
        self.assertFalse((dest / "Game").exists())

    def test_move_needs_a_folder(self):
        with self.assertRaises(OSError):
            aj.apply(aj.MOVE, [self.game()], dest=None)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("PS5_TEST_REAL_TRASH") == "1",
                     "puts a scratch file into the real Trash; set PS5_TEST_REAL_TRASH=1 to run")
class RealTrashTest(Base):
    def test_scratch_file_goes_to_the_trash(self):
        f = self.file("ps5-ultrapack-trash-test.txt", 10)
        aj.move_to_trash(f)
        self.assertFalse(f.exists())


def tearDownModule():
    import shutil
    shutil.rmtree(_SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
