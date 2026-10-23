"""Replacing the keystone of a PS4 fake package: the file check, the tool command on a real
package (skipped without one in unzipped/ps4/), and the library sort that writes it."""
import filecmp
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fpkg  # noqa: E402
import ps4pkg  # noqa: E402
import ps4_sort  # noqa: E402
from ps4_fixture import make_keystone, make_default_keystone, make_pkg  # noqa: E402


def _sample(kind: str):
    """The first git-ignored sample package of *kind* (game, update) in unzipped/ps4/."""
    for p in sorted((REPO / "unzipped" / "ps4").glob("*.pkg")):
        try:
            if ps4pkg.read_identity(p).kind == kind:
                return p
        except Exception:
            pass
    return None


GAME, UPDATE = _sample("game"), _sample("update")


def _clone(src: Path, dst: Path) -> None:
    """A copy that costs no space on APFS."""
    if sys.platform == "darwin" and subprocess.run(["cp", "-c", str(src), str(dst)]).returncode == 0:
        return
    shutil.copyfile(src, dst)


def _run(*args) -> tuple[int, str]:
    lines = []
    rc = fpkg._run([str(fpkg.tool_path()), *map(str, args)], on_line=lines.append)
    return rc, "\n".join(lines)


def _general_digests(pkg: Path) -> dict:
    """{name: (stored, computed)} for the general digests that hang on the content image. The
    formulas are the ones Sony's packages carry, checked against untouched samples."""
    with open(pkg, "rb") as f:
        b = f.read(0x10000)
        n, tbl = int.from_bytes(b[0x10:0x14], "big"), int.from_bytes(b[0x18:0x1c], "big")
        ents = {}
        for i in range(n):
            e = b[tbl + 32 * i:tbl + 32 * i + 24]
            ents[int.from_bytes(e[0:4], "big")] = (int.from_bytes(e[16:20], "big"), int.from_bytes(e[20:24], "big"))

        def h(eid):
            f.seek(ents[eid][0])
            return hashlib.sha256(f.read(ents[eid][1])).digest()
        gd = ents[0x80][0]
        playgo = hashlib.sha256(b"".join(h(e) for e in (0x1001, 0x1002, 0x1003, 0x1008, 0x1009, 0x100A) if e in ents)).digest()
    content = hashlib.sha256(b[0x40:0x64] + bytes(12) + b[0x70:0x78] + b[0x440:0x460] + b[gd + 0xa0:gd + 0xc0]).digest()
    return {"content": (b[gd + 0x20:gd + 0x40], content), "game": (b[gd + 0x40:gd + 0x60], b[0x440:0x460]),
            "header": (b[gd + 0x60:gd + 0x80], hashlib.sha256(b[0:0x40] + b[0x400:0x480]).digest()),
            "playgo": (b[gd + 0xe0:gd + 0x100], playgo)}


def _fingerprint(out: str) -> str:
    return re.search(r"current fingerprint=([0-9A-F]{64})", out).group(1)


class KeystoneFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, data: bytes) -> Path:
        p = self.tmp / "keystone"
        p.write_bytes(data)
        return p

    def test_a_well_formed_keystone_passes(self):
        self.assertIsNone(ps4pkg.keystone_problem(self._file(make_keystone())))

    def test_wrong_size_header_and_checksum_are_named(self):
        ks = make_keystone()
        self.assertIn("96 bytes", ps4pkg.keystone_problem(self._file(ks + b"\0")))
        self.assertIn("does not start", ps4pkg.keystone_problem(self._file(b"x" + ks[1:])))
        self.assertIn("checksum", ps4pkg.keystone_problem(self._file(ks[:-1] + bytes([ks[-1] ^ 1]))))

    def test_a_missing_file_is_reported(self):
        self.assertIn("cannot read", ps4pkg.keystone_problem(self.tmp / "none"))


@unittest.skipUnless(GAME, "no real PS4 game package in unzipped/ps4/")
class ToolOnARealPackage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(dir=REPO / "local-build" if (REPO / "local-build").is_dir() else None))
        cls.ks = cls.tmp / "new.keystone"
        cls.ks.write_bytes(make_keystone(bytes(range(100, 132))))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_swap_reads_back_changes_only_the_keystone_and_restores(self):
        pkg, undo = self.tmp / "game.pkg", self.tmp / "game.undo"
        _clone(GAME, pkg)
        rc, out = _run("ps4-keystone", pkg)
        self.assertEqual(rc, 0, out)
        old = _fingerprint(out)
        rc, out = _run("ps4-keystone", pkg, self.ks, "--undo", undo)
        self.assertEqual(rc, 0, out)
        self.assertIn("[ok] keystone replaced", out)
        rc, out = _run("ps4-keystone", pkg)
        self.assertEqual(_fingerprint(out), bytes(range(100, 132)).hex().upper())
        self.assertNotEqual(old, _fingerprint(out))
        before = {k: a == b for k, (a, b) in _general_digests(GAME).items()}
        self.assertEqual(before, {"content": True, "game": True, "header": True, "playgo": True})
        self.assertEqual({k: a == b for k, (a, b) in _general_digests(pkg).items()}, before)
        # every file of the package is the same, but the keystone (and the chunk hashes)
        a, b = self.tmp / "a", self.tmp / "b"
        self.assertEqual(fpkg.extract(GAME, a, on_line=lambda _l: None), 0)
        self.assertEqual(fpkg.extract(pkg, b, on_line=lambda _l: None), 0)
        diff = []

        def walk(c):
            diff.extend(os.path.join(c.left, n) for n in c.diff_files + c.left_only + c.right_only)
            for sub in c.subdirs.values():
                walk(sub)
        walk(filecmp.dircmp(a, b))
        self.assertEqual(sorted(Path(x).relative_to(a).as_posix() for x in diff),
                         ["sce_sys/keystone", "sce_sys/playgo-chunk.sha"])
        self.assertEqual((b / "sce_sys" / "keystone").read_bytes(), self.ks.read_bytes())
        # the same keystone again changes nothing; the undo file brings the original back
        rc, out = _run("ps4-keystone", pkg, self.ks)
        self.assertIn("already has this keystone", out)
        self.assertEqual(fpkg.ps4_keystone_restore(pkg, undo, on_line=lambda _l: None), 0)
        self.assertTrue(filecmp.cmp(pkg, GAME, shallow=False))

    def test_a_bad_keystone_leaves_the_package_alone(self):
        pkg, bad = self.tmp / "bad.pkg", self.tmp / "bad.keystone"
        _clone(GAME, pkg)
        bad.write_bytes(make_keystone()[:-1] + b"\0")
        rc, out = _run("ps4-keystone", pkg, bad)
        self.assertEqual(rc, 1, out)
        self.assertTrue(filecmp.cmp(pkg, GAME, shallow=False))

    def test_the_default_keystone_is_recognised(self):
        pkg, dks = self.tmp / "default.pkg", self.tmp / "default.keystone"
        _clone(GAME, pkg)
        dks.write_bytes(make_default_keystone())
        self.assertEqual(fpkg.ps4_keystone_info(GAME)["default"], False)
        self.assertEqual(_run("ps4-keystone", pkg, dks)[0], 0)
        self.assertEqual(fpkg.ps4_keystone_info(pkg)["default"], True)
        self.assertIn("zero_passcode=true", _run("ps4-keystone", pkg)[1])

    @unittest.skipUnless(UPDATE, "no real PS4 update package in unzipped/ps4/")
    def test_an_update_has_no_keystone_to_report(self):
        self.assertIsNone(fpkg.ps4_keystone_info(UPDATE))

    def test_an_undo_file_only_fits_its_own_package(self):
        pkg, undo, other = self.tmp / "own.pkg", self.tmp / "own.undo", self.tmp / "other.pkg"
        _clone(GAME, pkg)
        self.assertEqual(_run("ps4-keystone", pkg, self.ks, "--undo", undo)[0], 0)
        self.assertEqual(undo.read_bytes()[:10], b"PS4KSUNDO1")
        other.write_bytes(GAME.read_bytes()[:4096] + bytes(64))          # another size: not its package
        self.assertEqual(fpkg.ps4_keystone_restore(other, undo, on_line=lambda _l: None), 1)
        headless = self.tmp / "headless.undo"
        headless.write_bytes(undo.read_bytes()[10 + 36 + 8:])            # the ranges without the head
        self.assertNotEqual(fpkg.ps4_keystone_restore(pkg, headless, on_line=lambda _l: None), 0)
        self.assertEqual(fpkg.ps4_keystone_restore(pkg, undo, on_line=lambda _l: None), 0)
        self.assertTrue(filecmp.cmp(pkg, GAME, shallow=False))

    def test_a_failed_swap_leaves_no_undo_file(self):
        # a keystone that cannot be written (the package is read-only) leaves nothing behind
        pkg, undo = self.tmp / "ro.pkg", self.tmp / "ro.undo"
        _clone(GAME, pkg)
        os.chmod(pkg, 0o444)
        try:
            rc, out = _run("ps4-keystone", pkg, self.ks, "--undo", undo)
        finally:
            os.chmod(pkg, 0o644)
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(undo.exists(), "an undo file stayed")
        self.assertTrue(filecmp.cmp(pkg, GAME, shallow=False))

    def test_sfo_values_change_in_place_and_restore(self):
        pkg, undo = self.tmp / "sfo.pkg", self.tmp / "sfo.undo"
        _clone(GAME, pkg)
        old = ps4pkg.read_sfo(GAME)
        rc, out = _run("ps4-sfo", pkg, "--set", f"APP_VER={old['VERSION']}", "--set", f"VERSION={old['APP_VER']}",
                       "--undo", undo)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"[sfo] APP_VER: {old['APP_VER']} -> {old['VERSION']}", out)
        new = ps4pkg.read_sfo(pkg)
        self.assertEqual((new["APP_VER"], new["VERSION"]), (old["VERSION"], old["APP_VER"]))
        self.assertEqual({k: v for k, v in new.items() if k not in ("APP_VER", "VERSION")},
                         {k: v for k, v in old.items() if k not in ("APP_VER", "VERSION")})
        # the param digest follows the new param.sfo, the digests that hang on the image stay valid
        h = ps4pkg.read_header(pkg)
        self.assertEqual(ps4pkg.general_digests(pkg)[6],
                         hashlib.sha256(ps4pkg._read_entry(pkg, h, ps4pkg.ENTRY_PARAM_SFO)).digest())
        self.assertEqual({k: a == b for k, (a, b) in _general_digests(pkg).items()},
                         {"content": True, "game": True, "header": True, "playgo": True})
        self.assertEqual(fpkg.ps4_keystone_restore(pkg, undo, on_line=lambda _l: None), 0)
        self.assertTrue(filecmp.cmp(pkg, GAME, shallow=False))

    def test_sfo_refuses_what_it_cannot_reseal(self):
        pkg, undo = self.tmp / "sfo-bad.pkg", self.tmp / "sfo-bad.undo"
        _clone(GAME, pkg)
        for args in (["--set", "CATEGORY=gp"], ["--set", "APP_VER=" + "9" * 40], ["--set", "NO_SUCH_KEY=1"]):
            rc, out = _run("ps4-sfo", pkg, *args, "--undo", undo)
            self.assertEqual(rc, 1, out)
            self.assertFalse(undo.exists(), f"{args}: an undo file stayed")
        self.assertTrue(filecmp.cmp(pkg, GAME, shallow=False))

    @unittest.skipUnless(UPDATE, "no real PS4 update package in unzipped/ps4/")
    def test_an_update_has_no_keystone(self):
        rc, out = _run("ps4-keystone", UPDATE)
        self.assertEqual(rc, 1)
        self.assertIn("no keystone", out)

    def test_sort_writes_the_keystone_into_the_library_copy(self):
        src, lib, undo = self.tmp / "src", self.tmp / "lib", self.tmp / "undo"
        src.mkdir()
        _clone(GAME, src / GAME.name)
        lines = []
        rc = ps4_sort.sort_packages(src / GAME.name, lib, on_line=lines.append, keystone=self.ks, undo_dir=undo)
        self.assertEqual(rc, 0, "\n".join(lines))
        placed = next(lib.rglob("*.pkg"))
        _rc, out = _run("ps4-keystone", placed)
        self.assertEqual(_fingerprint(out), bytes(range(100, 132)).hex().upper())
        self.assertTrue(filecmp.cmp(src / GAME.name, GAME, shallow=False))   # the source stays as it was
        self.assertEqual(len(list(undo.glob("*.undo"))), 1)
        pct = [int(m.group(1)) for m in map(re.compile(r"^\[#+\]\s*(\d+)%").match, lines) if m]
        self.assertEqual(pct, sorted(pct), "one rising bar over copy and keystone")

    @unittest.skipUnless(UPDATE, "no real PS4 update package in unzipped/ps4/")
    def test_sort_refuses_a_keystone_for_an_update(self):
        lib = self.tmp / "lib2"
        lines = []
        rc = ps4_sort.sort_packages(UPDATE, lib, on_line=lines.append, keystone=self.ks)
        self.assertEqual(rc, 1)
        self.assertFalse(lib.exists() and any(lib.rglob("*.pkg")))


class KeystoneText(unittest.TestCase):
    def test_fingerprint_full_hex_and_file_give_the_same_keystone(self):
        ks = make_keystone(bytes(range(100, 132)))
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "keystone"
            f.write_bytes(ks)
            for text in (ks[0x20:0x40].hex(), ks.hex().upper(), str(f), " ".join(ks[0x20:0x40].hex()[i:i + 2] for i in range(0, 64, 2))):
                self.assertEqual(ps4pkg.keystone_from_text(text), (ks, None), text)

    def test_the_default_keystone_and_rubbish_are_refused(self):
        from ps4_fixture import make_default_keystone
        self.assertIn("default keystone", ps4pkg.keystone_from_text(make_default_keystone().hex())[1])
        self.assertIn("hex digits", ps4pkg.keystone_from_text("12345")[1])
        self.assertIn("checksum", ps4pkg.keystone_from_text((make_keystone()[:-1] + b"\0").hex())[1])


class VersionAndSaveFolder(unittest.TestCase):
    def test_a_merged_game_is_named_by_its_file(self):
        merged = {"CATEGORY": "gd", "APP_VER": "01.00", "VERSION": "01.81"}
        self.assertEqual(ps4pkg.version_problem(Path("/x/Game [CUSA00001] [v01.81].pkg"), merged),
                         {"app_ver": "01.00", "version": "01.81", "named": True})
        self.assertTrue(ps4pkg.version_problem(Path("/x/Game v1.81/game.pkg"), merged)["named"])
        self.assertFalse(ps4pkg.version_problem(Path("/x/game.pkg"), merged)["named"])
        self.assertFalse(ps4pkg.version_problem(Path("/x/Game [v01.81] [v01.00].pkg"), merged)["named"])

    def test_no_problem_without_a_higher_master_version(self):
        for sfo in ({"CATEGORY": "gd", "APP_VER": "01.81", "VERSION": "01.00"},
                    {"CATEGORY": "gd", "APP_VER": "01.00", "VERSION": "01.00"},
                    {"CATEGORY": "gp", "APP_VER": "01.00", "VERSION": "01.81"},
                    {"CATEGORY": "gd", "APP_VER": "01.00"}):
            self.assertIsNone(ps4pkg.version_problem(Path("/x/Game [v01.81].pkg"), sfo), sfo)

    def test_an_update_saving_elsewhere_gets_the_games_folder(self):
        import ps4_keystone_job
        with tempfile.TemporaryDirectory() as d:
            cid = "UP0000-CUSA00001_00-SAMPLEGAME000000"
            other = make_pkg(Path(d) / "a.pkg", content_id=cid, content_type=0x1A,
                             sfo={"TITLE_ID": "CUSA00001", "CATEGORY": "gp", "INSTALL_DIR_SAVEDATA": "CUSA00002"})
            same = make_pkg(Path(d) / "b.pkg", content_id=cid, content_type=0x1A,
                            sfo={"TITLE_ID": "CUSA00001", "CATEGORY": "gp", "INSTALL_DIR_SAVEDATA": "CUSA00001"})
            none = make_pkg(Path(d) / "c.pkg", content_id=cid, content_type=0x1A,
                            sfo={"TITLE_ID": "CUSA00001", "CATEGORY": "gp"})
            game = {"TITLE_ID": "CUSA00001", "CATEGORY": "gd"}
            self.assertEqual(ps4pkg.save_folder(game), "CUSA00001")
            self.assertEqual(ps4pkg.save_folder(dict(game, INSTALL_DIR_SAVEDATA="CUSA00003")), "CUSA00003")
            self.assertEqual(ps4_keystone_job.plan_update(other, game), {"save_folder": ("CUSA00002", "CUSA00001")})
            self.assertEqual(ps4_keystone_job.plan_update(same, game), {})
            self.assertEqual(ps4_keystone_job.plan_update(none, game), {})
            # the game saves elsewhere, the update names no folder: said, not changed
            moved = dict(game, INSTALL_DIR_SAVEDATA="CUSA00009")
            self.assertEqual(ps4_keystone_job.plan_update(none, moved), {"save_folder_note": ("CUSA00001", "CUSA00009")})


@unittest.skipUnless(GAME and UPDATE, "no real PS4 game and update in unzipped/ps4/")
class KeystoneJob(unittest.TestCase):
    def setUp(self):
        import ps4_keystone_job
        self.job = ps4_keystone_job
        self.tmp = Path(tempfile.mkdtemp(dir=REPO / "local-build" if (REPO / "local-build").is_dir() else None))
        self.base, self.upd = self.tmp / "game.pkg", self.tmp / "update.pkg"
        _clone(GAME, self.base)
        _clone(UPDATE, self.upd)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_binds_the_update_and_undo_restores_both(self):
        lines = []
        fp = bytes(range(100, 132))
        rc = self.job.run(self.base, fp.hex(), [self.upd], self.tmp / "undo", on_line=lines.append)
        self.assertEqual(rc, 0, "\n".join(lines))
        info = fpkg.ps4_keystone_info(self.base)
        self.assertEqual(info["fingerprint"], fp.hex().upper())
        g, u = ps4pkg.general_digests(self.base), ps4pkg.general_digests(self.upd)
        self.assertEqual((u[10], u[12]), (g[1], g[2]))
        self.assertEqual({k: a == b for k, (a, b) in _general_digests(self.upd).items()}["header"], True)
        pct = [int(m.group(1)) for m in map(re.compile(r"^\[#+\]\s*(\d+)%").match, lines) if m]
        self.assertEqual(pct, sorted(pct))
        self.assertEqual(pct[-1], 100)
        journal = self.job.last_journal(self.tmp / "undo")
        self.assertIsNotNone(journal)
        self.assertEqual(self.job.undo(journal, on_line=lambda _l: None), 0)
        self.assertTrue(filecmp.cmp(self.base, GAME, shallow=False))
        self.assertTrue(filecmp.cmp(self.upd, UPDATE, shallow=False))
        self.assertIsNone(self.job.last_journal(self.tmp / "undo"))

    @unittest.skipUnless(GAME and ps4pkg.version_problem(GAME), "the sample game has no higher master version")
    def test_version_fix_alone_and_undo(self):
        old = ps4pkg.read_sfo(GAME)
        lines = []
        rc = self.job.run(self.base, None, [], self.tmp / "undo", on_line=lines.append, fix_version=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        new = ps4pkg.read_sfo(self.base)
        self.assertEqual((new["APP_VER"], new["VERSION"]), (old["VERSION"], old["APP_VER"]))
        self.assertIsNone(ps4pkg.version_problem(self.base))
        # a retry once the version is right goes on with the other steps
        self.assertEqual(self.job.run(self.base, None, [self.upd], self.tmp / "undo2", on_line=lines.append,
                                      fix_version=True), 0, "\n".join(lines))
        self.assertEqual(fpkg.ps4_keystone_info(self.base), fpkg.ps4_keystone_info(GAME))
        self.assertEqual(self.job.undo(self.job.last_journal(self.tmp / "undo"), on_line=lambda _l: None), 0)
        self.assertTrue(filecmp.cmp(self.base, GAME, shallow=False))

    def test_a_failed_update_ends_with_a_warning(self):
        lines = []
        os.chmod(self.upd, 0o444)
        try:
            rc = self.job.run(self.base, bytes(range(100, 132)).hex(), [self.upd], self.tmp / "undo", on_line=lines.append)
        finally:
            os.chmod(self.upd, 0o644)
        self.assertEqual(rc, 1, "\n".join(lines))
        self.assertTrue(lines[-1].startswith("[WARN] PS4 repair:"), lines[-1])
        self.assertFalse(any(x.startswith("[OK] PS4 repair") for x in lines))
        self.assertEqual(self.job.undo(self.job.last_journal(self.tmp / "undo"), on_line=lambda _l: None), 0)
        self.assertTrue(filecmp.cmp(self.base, GAME, shallow=False))

    def test_a_run_stopped_in_a_step_can_be_undone(self):
        real = fpkg.ps4_keystone

        def dies_after_writing(*a, **k):
            real(*a, **k)                 # the package is changed, then the process is gone
            raise SystemExit(-15)
        fpkg.ps4_keystone = dies_after_writing
        try:
            with self.assertRaises(SystemExit):
                self.job.run(self.base, bytes(range(100, 132)).hex(), [], self.tmp / "undo", on_line=lambda _l: None)
        finally:
            fpkg.ps4_keystone = real
        self.assertFalse(filecmp.cmp(self.base, GAME, shallow=False))
        journal = self.job.last_journal(self.tmp / "undo")
        self.assertIsNotNone(journal, "the step must be in the journal before it starts")
        self.assertEqual(self.job.undo(journal, on_line=lambda _l: None), 0)
        self.assertTrue(filecmp.cmp(self.base, GAME, shallow=False))

    def test_undo_goes_back_one_run_at_a_time(self):
        undo_root = self.tmp / "undo"
        fp1, fp2 = bytes(range(100, 132)), bytes(range(140, 172))
        self.assertEqual(self.job.run(self.base, fp1.hex(), [], undo_root, on_line=lambda _l: None), 0)
        self.assertEqual(self.job.run(self.base, fp2.hex(), [], undo_root, on_line=lambda _l: None), 0)
        self.assertEqual(len(self.job.journals(undo_root)), 2)
        self.assertEqual(self.job.describe(self.job.last_journal(undo_root))["base"], str(self.base))
        self.assertEqual(self.job.undo(self.job.last_journal(undo_root), on_line=lambda _l: None), 0)
        self.assertEqual(fpkg.ps4_keystone_info(self.base)["fingerprint"], fp1.hex().upper())
        self.assertEqual(self.job.undo(self.job.last_journal(undo_root), on_line=lambda _l: None), 0)
        self.assertTrue(filecmp.cmp(self.base, GAME, shallow=False))
        self.assertIsNone(self.job.last_journal(undo_root))

    def test_wrong_inputs_change_nothing(self):
        lines = []
        self.assertEqual(self.job.run(self.upd, make_keystone().hex(), [], self.tmp / "u", on_line=lines.append), 1)
        self.assertIn("no game package", "\n".join(lines))
        self.assertEqual(self.job.run(self.base, make_keystone().hex(), [self.base], self.tmp / "u", on_line=lines.append), 1)
        self.assertIn("records no base package", "\n".join(lines))
        self.assertEqual(self.job.run(self.base, "rubbish", [], self.tmp / "u", on_line=lines.append), 1)
        self.assertEqual(self.job.run(self.base, "", [], self.tmp / "u", on_line=lines.append), 1)
        self.assertIn("nothing to do", "\n".join(lines))
        self.assertEqual(self.job.run(self.upd, None, [], self.tmp / "u", on_line=lines.append, fix_version=True), 1)
        self.assertTrue(filecmp.cmp(self.base, GAME, shallow=False))
        self.assertTrue(filecmp.cmp(self.upd, UPDATE, shallow=False))


if __name__ == "__main__":
    unittest.main()
