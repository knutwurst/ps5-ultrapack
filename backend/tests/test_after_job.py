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
        # a download folder one level down is not next to it
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


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as td:
        try:
            (Path(td) / "l").symlink_to("t")
            return True
        except OSError:
            return False


SYMLINKS = _can_symlink()


class ActionTests(Base):
    def test_keep_does_nothing(self):
        g = self.game()
        self.assertEqual(aj.apply(aj.KEEP, [g]), [])
        self.assertTrue(g.is_dir())

    def test_delete_file_and_folder(self):
        g, f = self.game(), self.file("x/Game.pkg")
        aj.apply(aj.DELETE, [g, f])
        self.assertFalse(g.exists() or f.exists())

    @unittest.skipUnless(SYMLINKS, "this system does not allow symlinks")
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

    @unittest.skipUnless(SYMLINKS, "this system does not allow symlinks")
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

    def test_move_two_sources_of_one_name_both_arrive(self):
        a, b = self.file("x/Game.pkg"), self.file("y/Game.pkg", 200)
        dest = self.tmp / "done"
        for same in (None, lambda s, d: False):
            out = aj.apply(aj.MOVE, [a, b], dest=dest / str(bool(same)), same_device=same)
            self.assertEqual([Path(p).name for p in out], ["Game.pkg", "Game (2).pkg"])
            self.assertEqual(sorted(Path(p).stat().st_size for p in out), [100, 200])
            a, b = self.file("x/Game.pkg"), self.file("y/Game.pkg", 200)

    def test_move_names_that_differ_only_in_case_never_meet(self):
        a, b = self.file("x/game.pkg"), self.file("y/GAME.pkg", 200)
        out = aj.apply(aj.MOVE, [a, b], dest=self.tmp / "done")
        self.assertEqual(len({Path(p).name.casefold() for p in out}), 2)
        self.assertEqual(sorted(Path(p).stat().st_size for p in out), [100, 200])

    def test_free_names_keep_a_set_together_beside_a_twin(self):
        parts = [self.file(f"x/Game.part{i}.rar") for i in (1, 2)]
        other = self.file("y/Game.part1.rar")
        names = [t.name for t in aj._free_names(parts + [other], self.tmp / "done")]
        self.assertEqual(names, ["Game.part1.rar", "Game.part2.rar", "Game (2).part1.rar"])

    def test_cross_drive_never_removes_what_was_already_there(self):
        g = self.game()
        dest = self.tmp / "other_drive"
        (dest / "Game").mkdir(parents=True)
        (dest / "Game" / "mine.txt").write_text("the user's")
        with mock.patch.object(aj, "_free_names", return_value=[dest / "Game"]):
            with self.assertRaises(OSError):
                aj.apply(aj.MOVE, [g], dest=dest, same_device=lambda a, b: False)
        self.assertEqual((dest / "Game" / "mine.txt").read_text(), "the user's")
        self.assertTrue((g / "data" / "a.bin").is_file())

    def test_cross_drive_keeps_the_copy_when_the_source_vanished(self):
        import shutil
        g = self.game()
        dest = self.tmp / "other_drive"
        real = aj._copy_file
        calls = []

        def copy_then_lose_source(s, d, tick):
            real(s, d, tick)
            calls.append(d)
            if len(calls) == 2:
                shutil.rmtree(g)                     # the source drive went away mid-move
        with mock.patch.object(aj, "_copy_file", side_effect=copy_then_lose_source):
            with self.assertRaises(OSError):
                aj.apply(aj.MOVE, [g], dest=dest, same_device=lambda a, b: False)
        # all there is, under a name that says it is not complete
        self.assertEqual(sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()),
                         ["Game (incomplete)/data/a.bin", "Game (incomplete)/eboot.bin"])

    def test_cross_drive_copy_never_sits_under_the_final_name_half_done(self):
        g = self.game()
        dest = self.tmp / "other_drive"
        seen = []
        real = aj._copy_file

        def copy_and_look(s, d, tick):
            real(s, d, tick)
            seen.append(any(dest.glob("Game")))       # the final name is not there while copying
            if len(seen) == 2:
                raise KeyboardInterrupt               # the app quit half-way
        with mock.patch.object(aj, "_copy_file", side_effect=copy_and_look):
            with self.assertRaises(KeyboardInterrupt):
                aj.apply(aj.MOVE, [g], dest=dest, same_device=lambda a, b: False)
        self.assertEqual(seen, [False, False])
        self.assertFalse((dest / "Game").exists())
        self.assertEqual(list(dest.iterdir()) if dest.exists() else [], [])
        self.assertTrue((g / "data" / "a.bin").is_file())

    def test_cross_drive_move_clears_its_own_cut_off_copy(self):
        g = self.game()
        dest = self.tmp / "other_drive"; dest.mkdir()
        stale = dest / ".Game.part-0123abcd"; stale.mkdir(); (stale / "eboot.bin").write_bytes(b"old")
        other = dest / ".Game.part-notours"; other.mkdir()
        aj.apply(aj.MOVE, [g], dest=dest, same_device=lambda a, b: False)
        self.assertTrue((dest / "Game" / "eboot.bin").is_file())
        self.assertFalse(stale.exists())
        self.assertTrue(other.exists(), "only the app's own names go")

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "needs a folder this user cannot read")
    def test_cross_drive_unreadable_folder_keeps_the_source(self):
        g = self.game(files=(("eboot.bin", 300), ("data/a.bin", 700), ("locked/b.bin", 50)))
        os.chmod(g / "locked", 0)
        try:
            with self.assertRaises(OSError):
                aj.apply(aj.MOVE, [g], dest=self.tmp / "other_drive", same_device=lambda a, b: False)
        finally:
            os.chmod(g / "locked", 0o755)
        self.assertTrue((g / "locked" / "b.bin").is_file())
        self.assertTrue((g / "eboot.bin").is_file() and (g / "data" / "a.bin").is_file())

    def test_move_needs_a_folder(self):
        with self.assertRaises(OSError):
            aj.apply(aj.MOVE, [self.game()], dest=None)

    def clutter(self, g):
        (g / ".DS_Store").write_bytes(b"j" * 50)
        (g / "data" / "._a.bin").write_bytes(b"j" * 50)
        (g / "__MACOSX").mkdir()
        (g / "__MACOSX" / "._x").write_bytes(b"j" * 50)

    def test_move_to_another_drive_leaves_clutter_behind(self):
        g = self.game()
        self.clutter(g)
        dest = self.tmp / "other_drive"
        out = aj.apply(aj.MOVE, [g], dest=dest, same_device=lambda a, b: False)
        moved = Path(out[0])
        self.assertFalse(g.exists())
        self.assertEqual(sorted(p.relative_to(moved).as_posix() for p in moved.rglob("*")),
                         ["data", "data/a.bin", "eboot.bin"])

    def test_move_on_the_same_drive_arrives_without_clutter(self):
        g = self.game()
        self.clutter(g)
        out = aj.apply(aj.MOVE, [g], dest=self.tmp / "done")
        moved = Path(out[0])
        self.assertEqual(sorted(p.relative_to(moved).as_posix() for p in moved.rglob("*")),
                         ["data", "data/a.bin", "eboot.bin"])

    def test_size_counts_no_clutter(self):
        g = self.game()
        self.clutter(g)
        self.assertEqual(aj.size_of([g]), 1000)


class ReleaseFolderTests(Base):
    def find(self, folder):
        return ultra_core.find_job_sources(Path(folder))

    def test_folder_named_after_the_set_goes_whole(self):
        rel = self.tmp / "dl" / "[site]-PPSA00001"
        parts = [self.file(f"dl/[site]-PPSA00001/[site]-PPSA00001.part{i}.rar") for i in (1, 2)]
        self.file("dl/[site]-PPSA00001/readme.nfo")
        self.assertEqual(aj.release_folder(parts, list_sources=self.find, title_id="PPSA00001"), rel)

    def test_folder_named_after_the_title_goes_whole(self):
        pkg = self.file("dl/Example Quest/UP0001-PPSA00001_00-EXAMPLE0000000000-A0100-V0100.pkg")
        self.file("dl/Example Quest/cover.jpg")
        self.assertEqual(aj.release_folder([pkg], list_sources=self.find, title="EXAMPLE QUEST"), pkg.parent)

    def test_two_games_in_one_folder_stay_apart(self):
        a = [self.file(f"dl/Mixed PPSA00001/A-PPSA00001.part{i}.rar") for i in (1, 2)]
        self.file("dl/Mixed PPSA00001/B-PPSA00002.part1.rar")
        self.assertIsNone(aj.release_folder(a, list_sources=self.find, title_id="PPSA00001"))

    def test_unrelated_name_or_protected_folder_is_not_taken(self):
        a = [self.file("dl/Stuff/A-PPSA00001.part1.rar")]
        self.assertIsNone(aj.release_folder(a, list_sources=self.find, title_id="PPSA00009"))
        b = [self.file("root/B-PPSA00002/B-PPSA00002.rar")]
        self.assertIsNone(aj.release_folder(b, list_sources=self.find, title_id="PPSA00002",
                                            protected=[self.tmp / "root" / "B-PPSA00002"]))

    def test_picked_folder_may_be_the_release_folder_but_not_one_above(self):
        parts = [self.file("root/G-PPSA00003/G-PPSA00003.rar")]
        rel = self.tmp / "root" / "G-PPSA00003"
        self.assertEqual(aj.release_folder(parts, list_sources=self.find, title_id="PPSA00003",
                                           protected=[rel], may_be=rel), rel)
        self.assertIsNone(aj.release_folder(parts, list_sources=self.find, title_id="PPSA00003",
                                            protected=[rel, self.tmp / "root"], may_be=self.tmp / "root"))

    def test_prune_removes_empty_folders_up_to_a_protected_one(self):
        deep = self.tmp / "root" / "batch" / "game"
        deep.mkdir(parents=True)
        (deep / ".DS_Store").write_bytes(b"x")
        removed = aj.prune_empty_dirs([deep], protected=[self.tmp / "root"])
        self.assertEqual(removed, [deep, deep.parent])
        self.assertTrue((self.tmp / "root").is_dir())

    def test_prune_keeps_a_folder_with_content(self):
        d = self.tmp / "root" / "keep"
        self.file("root/keep/notes.docx")
        self.assertEqual(aj.prune_empty_dirs([d], protected=[self.tmp / "root"]), [])

    def test_sweep_sidecars_only_when_nothing_else_is_left(self):
        rel = self.tmp / "dl" / "PPSA00001"
        self.file("dl/PPSA00001/readme.nfo"); self.file("dl/PPSA00001/check.sfv")
        self.assertTrue(aj.sweep_sidecars(rel))
        self.assertFalse(rel.exists())
        rel2 = self.tmp / "dl" / "PPSA00002"
        self.file("dl/PPSA00002/readme.nfo"); self.file("dl/PPSA00002/mine.docx")
        self.assertFalse(aj.sweep_sidecars(rel2))
        self.assertTrue((rel2 / "mine.docx").is_file())


@unittest.skipUnless(sys.platform == "win32", "the Windows Recycle Bin")
class RecycleBinGuardTest(Base):
    def test_too_big_for_the_bin_is_not_deleted(self):
        f = self.file("big.bin", 10)
        with mock.patch.object(aj, "_recycle_bin_limit", return_value=("X:\\", 5)):
            with self.assertRaises(OSError) as cm:
                aj.move_to_trash(f)
        self.assertTrue(f.exists())
        self.assertIn("not deleted", str(cm.exception))

    def test_no_bin_on_the_drive_is_not_deleted(self):
        f = self.file("any.bin", 10)
        with mock.patch.object(aj, "_recycle_bin_limit", return_value=("X:\\", 0)):
            with self.assertRaises(OSError):
                aj.move_to_trash(f)
        self.assertTrue(f.exists())

    def test_the_local_drive_has_a_bin(self):
        root, limit = aj._recycle_bin_limit(_SCRATCH)
        self.assertGreater(limit, 0, root)


@unittest.skipUnless(sys.platform in ("darwin", "win32") and os.environ.get("PS5_TEST_REAL_TRASH") == "1",
                     "puts a scratch file into the real Trash; set PS5_TEST_REAL_TRASH=1 to run")
class RealTrashTest(Base):
    def test_scratch_file_goes_to_the_trash(self):
        f = self.file("ps5-ultrapack-trash-test.txt", 10)
        aj.move_to_trash(f)
        self.assertFalse(f.exists())


def tearDownModule():
    import shutil
    shutil.rmtree(_SCRATCH, ignore_errors=True)



class CaseTwinSets(unittest.TestCase):
    def test_two_volumes_that_differ_only_in_case_get_names_and_never_hang(self):
        import tempfile as _tf, shutil as _sh
        d = Path(_tf.mkdtemp())
        try:
            src = d / "src"; src.mkdir(); dest = d / "dest"; dest.mkdir()
            a, b = src / "Game.rar", src / "game.rar"
            a.write_text("a")
            try:
                b.write_text("b")
            except OSError:
                self.skipTest("this filesystem cannot hold both names")
            if len(list(src.iterdir())) < 2:
                self.skipTest("case-insensitive filesystem: only one name exists")
            names = aj._free_names([a, b], dest)
            self.assertEqual(len({n.name.casefold() for n in names}), 2, names)
        finally:
            _sh.rmtree(d, ignore_errors=True)

    def test_the_name_search_has_an_end(self):
        names = aj._free_names([Path("/nonexistent/x/Game.rar")], Path("/nonexistent/dest"))
        self.assertEqual(names[0].name, "Game.rar")

if __name__ == "__main__":
    unittest.main()
