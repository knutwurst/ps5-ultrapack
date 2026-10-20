"""LZ4 asset packs: AMPR detection, packing with the default profile, the chain step and the
.exfat output. The packs are checked against the originals by unpacking them again."""
import filecmp
import io
import contextlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

import lz4_packs  # noqa: E402
from test_backport import _elf_with_imports  # noqa: E402

CLI = BACKEND / "cli.py"
HAVE_RUNTIME = (BACKEND / "native" / "ampr" / "libSceAmpr.sprx").is_file()


def _game(root: Path, ampr: bool = True) -> Path:
    """A small game folder: an eboot importing libSceAmpr (or not), data in subfolders, a
    config, a movie, sce_sys and a file in the game's own folder."""
    random.seed(7)
    words = [b"alpha", b"beta", b"gamma", b"vertex", b"texture", b"mesh", b"[level]"]
    lib = "libSceAmpr" if ampr else "libSceAgc"
    (root / "sce_sys").mkdir(parents=True)
    (root / "sce_sys" / "param.json").write_text('{"titleId": "PPSA99099"}')
    (root / "eboot.bin").write_bytes(_elf_with_imports([("mc36MRb8k1w", 0, lib, 0, lib)]))
    (root / "data" / "levels").mkdir(parents=True)
    for i in range(3):
        (root / "data" / "levels" / f"l{i}.bin").write_bytes(b" ".join(random.choice(words) for _ in range(60000)))
    (root / "data" / "[odd] name.bin").write_bytes(b"x " * 40000)
    (root / "data" / "config.json").write_text('{"a": 1}')
    (root / "movies").mkdir()
    (root / "movies" / "intro.bik").write_bytes(os.urandom(50000))
    (root / "movies" / "intro_de.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHallo\n" * 200)
    (root / "root.dat").write_bytes(b"r" * 50000)
    return root


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


@unittest.skipUnless(HAVE_RUNTIME, "run ./FETCH_AMPR_EMU.sh for the ampr_emu runtime")
class PackGame(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="lz4-test-"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_detects_ampr_from_the_eboot_imports(self):
        self.assertTrue(lz4_packs.uses_ampr(_game(self.d / "a")))
        self.assertFalse(lz4_packs.uses_ampr(_game(self.d / "b", ampr=False)))
        (self.d / "c").mkdir()
        self.assertIsNone(lz4_packs.uses_ampr(self.d / "c"))

    def test_packs_data_keeps_the_rest_loose_and_round_trips(self):
        game = _game(self.d / "app0")
        original = self.d / "original"
        shutil.copytree(game, original)
        with contextlib.redirect_stdout(io.StringIO()):
            numbers = lz4_packs.pack_game(game, workers=4)
        self.assertLess(numbers["stored_bytes"], numbers["logical_bytes"])
        now = _files(game)
        for packed in ("data/levels/l0.bin", "data/levels/l1.bin", "data/levels/l2.bin", "data/[odd] name.bin"):
            self.assertNotIn(packed, now)
        for loose in ("eboot.bin", "root.dat", "data/config.json", "movies/intro.bik", "movies/intro_de.srt",
                      "sce_sys/param.json",
                      "fakelib/libSceAmpr.sprx", "ampr_emu.index", "ampr_assets.index"):
            self.assertIn(loose, now)
        self.assertTrue(any(n.startswith("ampr_assets-") and n.endswith(".pak") for n in now))
        self.assertEqual((game / "fakelib" / "libSceAmpr.sprx").read_bytes(), lz4_packs.runtime_path().read_bytes())
        # unpacked again, every packed file is the original
        import ampr_pack
        out = self.d / "unpacked"
        ampr_pack.extract_packs(game / "ampr_assets.index", out, (), False, True)
        for rel in ("data/levels/l0.bin", "data/levels/l2.bin", "data/[odd] name.bin"):
            self.assertTrue(filecmp.cmp(out / rel, original / rel, shallow=False), rel)

    def test_unpack_brings_the_plain_game_back(self):
        game = _game(self.d / "app0")
        original = self.d / "original"
        shutil.copytree(game, original)
        with contextlib.redirect_stdout(io.StringIO()):
            lz4_packs.pack_game(game, workers=4)
            self.assertTrue(lz4_packs.is_packed(game))
            got = lz4_packs.unpack_game(game)
        self.assertEqual(got["files"], 4)
        self.assertFalse(lz4_packs.is_packed(game))
        self.assertEqual([n for n in _files(game) if n.startswith("ampr_assets")], [])
        extra = {"fakelib/libSceAmpr.sprx", "ampr_emu.index"}
        self.assertEqual(_files(game) - extra, _files(original))
        for rel in _files(original):
            self.assertTrue(filecmp.cmp(game / rel, original / rel, shallow=False), rel)

    def test_unpack_refuses_a_game_without_packs(self):
        with self.assertRaises(lz4_packs.Lz4Error):
            lz4_packs.unpack_game(_game(self.d / "app0"))

    def test_refuses_a_game_without_ampr(self):
        game = _game(self.d / "app0", ampr=False)
        with self.assertRaises(lz4_packs.Lz4Error):
            lz4_packs.pack_game(game)
        self.assertFalse((game / "fakelib").exists())

    def test_refuses_packs_that_save_almost_nothing(self):
        game = _game(self.d / "app0")
        for p in (game / "data").rglob("*.bin"):
            p.write_bytes(os.urandom(p.stat().st_size))
        before = _files(game)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(lz4_packs.Lz4Error) as e:
            lz4_packs.pack_game(game, workers=4)
        self.assertIn("not worth it", str(e.exception))
        self.assertTrue(before <= _files(game))
        self.assertFalse(any(n.startswith("ampr_assets") for n in _files(game)))

    def test_refuses_a_game_that_is_packed_already(self):
        game = _game(self.d / "app0")
        (game / "ampr_assets.index").write_bytes(b"x")
        with self.assertRaises(lz4_packs.Lz4Error):
            lz4_packs.pack_game(game)


@unittest.skipUnless(HAVE_RUNTIME, "run ./FETCH_AMPR_EMU.sh for the ampr_emu runtime")
class ChainLz4(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="lz4-chain-"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run(self, *argv):
        env = dict(os.environ, PYTHONUTF8="1")
        return subprocess.run([sys.executable, "-u", str(CLI), *map(str, argv), "--temp-dir", str(self.d / "tmp")],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                              timeout=600)

    def test_folder_to_exfat_with_lz4_leaves_the_source_alone(self):
        game = _game(self.d / "Game")
        before = {p: p.read_bytes() for p in game.rglob("*") if p.is_file()}
        (self.d / "tmp").mkdir()
        r = self._run(game, self.d / "out" / "Game.exfat", "--to", "exfat", "--lz4")
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-800:])
        self.assertEqual({p: p.read_bytes() for p in game.rglob("*") if p.is_file()}, before)
        image = self.d / "out" / "Game.exfat"
        self.assertTrue(image.is_file())
        import cli
        back = self.d / "back"
        self.assertTrue(cli._extract_exfat_to(image, back))
        inside = _files(back)
        self.assertIn("ampr_assets.index", inside)
        self.assertIn("fakelib/libSceAmpr.sprx", inside)
        self.assertNotIn("data/levels/l0.bin", inside)
        self.assertIn("root.dat", inside)
        self.assertEqual([p.name for p in (self.d / "tmp").rglob("chain-*")], [])

    def test_a_packed_exfat_unpacks_into_a_folder(self):
        game = _game(self.d / "Game")
        (self.d / "tmp").mkdir()
        r = self._run(game, self.d / "out" / "Game.exfat", "--to", "exfat", "--lz4")
        self.assertEqual(r.returncode, 0, r.stdout[-800:])
        r = self._run(self.d / "out" / "Game.exfat", self.d / "back" / "Game [unpacked]", "--to", "folder", "--lz4-unpack")
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-500:])
        back = self.d / "back" / "Game [unpacked]"
        self.assertEqual(_files(back) - {"fakelib/libSceAmpr.sprx", "ampr_emu.index"}, _files(game))
        self.assertTrue(filecmp.cmp(back / "data" / "levels" / "l1.bin", game / "data" / "levels" / "l1.bin", shallow=False))

    def test_lz4_refuses_an_ffpfsc_output(self):
        game = _game(self.d / "Game")
        (self.d / "tmp").mkdir()
        r = self._run(game, self.d / "out", "--to", "ffpfsc", "--lz4")
        self.assertEqual(r.returncode, 2, r.stdout[-800:])
        self.assertIn("LZ4 asset packs go into a folder or an .exfat", r.stdout)

    def test_a_game_without_ampr_fails_before_any_change(self):
        game = _game(self.d / "Game", ampr=False)
        (self.d / "tmp").mkdir()
        r = self._run(game, self.d / "out" / "Game.exfat", "--to", "exfat", "--lz4")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not load through AMPR", r.stdout)
        self.assertFalse((self.d / "out" / "Game.exfat").exists())

    def test_folder_to_exfat_without_changes(self):
        game = _game(self.d / "Game", ampr=False)
        (self.d / "tmp").mkdir()
        r = self._run(game, self.d / "out" / "Game.exfat", "--to", "exfat")
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])
        import cli
        back = self.d / "back"
        self.assertTrue(cli._extract_exfat_to(self.d / "out" / "Game.exfat", back))
        self.assertEqual(_files(back), _files(game))


class DefaultProfile(unittest.TestCase):
    def test_lists_the_top_files_and_escapes_glob_characters(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "a[1].dat").write_bytes(b"x")
            (d / "sub").mkdir()
            text = lz4_packs.default_profile(d)
            import tomllib
            cfg = tomllib.loads(text)
            loose = cfg["rule"][1]["include"]
            self.assertIn("a[[]1[]].dat", loose)
            self.assertNotIn("sub", loose)
            import fnmatch
            self.assertTrue(fnmatch.fnmatchcase("a[1].dat", "a[[]1[]].dat"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
