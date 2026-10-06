"""OS clutter (._*, .DS_Store, __MACOSX, …) is never extracted, copied, moved along or
packed: one list across the app, and the extractors drop it on the way in.

    PYTHONPATH=. /tmp/ps5venv/bin/python -m unittest backend.tests.test_junk_filter
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="junk_filter_"))
os.environ.setdefault("PS5_FFPFSC_APP_DIR", str(_SCRATCH / "app_dir"))   # before ultra_core loads
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))
import after_job  # noqa: E402
import cli  # noqa: E402
import ultra_core  # noqa: E402
from mkpfs import utils as mk_utils  # noqa: E402

REAL = ["Game/eboot.bin", "Game/sce_sys/param.json", "Game/data/level.bin"]
JUNK = ["__MACOSX/Game/._eboot.bin", "Game/.DS_Store", "Game/._eboot.bin",
        "Game/sce_sys/.DS_Store", "Game/Thumbs.db", "Game/.Spotlight-V100/store.db"]
CLEAN_TREE = ["Game", "Game/data", "Game/data/level.bin", "Game/eboot.bin",
              "Game/sce_sys", "Game/sce_sys/param.json"]


def make_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for m in REAL + JUNK:
            zf.writestr(m, b"x" * 8)
    return path


def plant(root: Path) -> None:
    for m in REAL + JUNK:
        p = root / m
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 8)


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


class OneList(unittest.TestCase):
    def test_every_module_names_the_same_clutter(self):
        self.assertEqual(set(cli._JUNK_NAMES), set(mk_utils.IGNORED_NAMES))
        self.assertEqual(set(cli._JUNK_NAMES), {n.lower() for n in ultra_core.FS_JUNK_NAMES})
        self.assertEqual(set(cli._JUNK_NAMES), set(after_job._JUNK_NAMES))
        cs = (ROOT / "backend/native/src/ffpfsc-pkg-tool/FsJunk.cs").read_text(encoding="utf-8")
        for n in cli._JUNK_NAMES:
            self.assertIn('"' + n.replace("\r", "\\r") + '"', cs, n)
        globbed = {g.lower() for g in cli._JUNK_GLOBS} | {g.lower() for g in ultra_core._COPYTREE_JUNK_GLOBS}
        self.assertTrue(set(cli._JUNK_NAMES) <= globbed, set(cli._JUNK_NAMES) - globbed)

    def test_the_rule(self):
        for n in ("._x", "._Game.rar", ".DS_Store", ".ds_store", "__MACOSX", "Thumbs.db", "$RECYCLE.BIN"):
            self.assertTrue(cli._is_junk_name(n), n)
            self.assertTrue(ultra_core.is_fs_junk_name(n), n)
            self.assertTrue(after_job._is_junk(n), n)
            self.assertTrue(mk_utils.is_ignored_name(n), n)
        for n in ("eboot.bin", "_DUPLEX_", ".nomedia", "sce_sys", "Icon.png", "_ffpfsc_temp"):
            self.assertFalse(cli._is_junk_name(n), n)
            self.assertFalse(ultra_core.is_fs_junk_name(n), n)
            self.assertFalse(after_job._is_junk(n), n)
            self.assertFalse(mk_utils.is_ignored_name(n), n)


class Extraction(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="case_", dir=_SCRATCH))
        self.zip = make_zip(self.tmp / "release.zip")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backend_zip_extraction_writes_no_clutter(self):
        dest = self.tmp / "out"
        cli._extract_archive_into(self.zip, dest)
        self.assertEqual(tree(dest), CLEAN_TREE)

    def test_app_zip_extraction_writes_no_clutter_and_finds_the_root(self):
        root = ultra_core.ArchiveExtractor.extract(self.zip, self.tmp / "ex", log_fn=lambda *a: None)
        self.assertEqual(root.name, "Game")
        self.assertEqual(tree(root.parent), CLEAN_TREE)

    def test_a_whole_extraction_is_swept(self):
        # what the RAR and 7z readers leave behind: clutter beside and below the game
        d = self.tmp / "ex2"
        plant(d)
        self.assertEqual(ultra_core.strip_fs_junk(d), 7)
        self.assertEqual(tree(d), CLEAN_TREE)
        d2 = self.tmp / "ex3"
        plant(d2)
        self.assertEqual(cli._strip_junk_files(d2), 7)
        self.assertEqual(tree(d2), CLEAN_TREE)

    def test_7z_extraction_ends_clean(self):
        try:
            import py7zr
        except ImportError:
            self.skipTest("py7zr is not installed")
        arc = self.tmp / "release.7z"
        with py7zr.SevenZipFile(arc, "w") as sz:
            for m in REAL + JUNK:
                sz.writestr(b"x" * 8, m)
        root = ultra_core.ArchiveExtractor.extract(arc, self.tmp / "ex7", log_fn=lambda *a: None)
        self.assertEqual(root.name, "Game")
        self.assertEqual(tree(root.parent), CLEAN_TREE)

    def test_unwrapping_a_nested_image_moves_no_clutter_up(self):
        # exFAT: macOS adds a "._<file>" beside a file the extractor just wrote and may take
        # it away again moments later. It is not moved into place, and one that vanished
        # between listing and moving is no error.
        out = self.tmp / "unpacked"
        out.mkdir()
        (out / "inner.ffpfs").write_bytes(b"")

        def fake_unpack(img, sub, _cmd, _cwd, overwrite=False):
            (Path(sub) / "Game").mkdir()
            (Path(sub) / "Game" / "eboot.bin").write_bytes(b"e")
            (Path(sub) / "ps5.kpf").write_bytes(b"k")
            (Path(sub) / "._ps5.kpf").write_bytes(b"\x00\x05\x16\x07")
            (Path(sub) / ".DS_Store").write_bytes(b"d")

        real = cli.unpack_pfs_image
        cli.unpack_pfs_image = fake_unpack
        try:
            cli._fully_unwrap(out, [], None)
        finally:
            cli.unpack_pfs_image = real
        self.assertEqual(tree(out), ["Game", "Game/eboot.bin", "ps5.kpf"])

    def test_the_patch_overlay_carries_none_of_it(self):
        game = self.tmp / "game"
        (game / "sce_sys").mkdir(parents=True)
        (game / "sce_sys" / "param.json").write_text('{"titleId": "PPSA00001"}')
        (game / "eboot.bin").write_bytes(b"old")
        patch = self.tmp / "patch"
        plant(patch)
        (patch / "Game" / "sce_sys" / "param.json").write_text('{"titleId": "PPSA00001"}')
        cli.overlay_patch(game, patch / "Game")
        self.assertFalse(any(ultra_core.is_fs_junk_name(p.name) for p in game.rglob("*")))
        self.assertEqual((game / "eboot.bin").read_bytes(), b"x" * 8)


def tearDownModule():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


class WrittenClutter(unittest.TestCase):
    """strip_written_clutter: only the job's own output and the sidecars beside it."""
    def test_file_output(self):
        import tempfile
        root = Path(tempfile.mkdtemp()); title = root / "Title [PPSA00001]"; title.mkdir()
        out = title / "Title.ffpfsc"; out.write_bytes(b"x")
        (title / "._Title.ffpfsc").write_bytes(b"\0\x05\x16\x07")
        (root / "._Title [PPSA00001]").write_bytes(b"\0\x05\x16\x07")
        (title / "._other.ffpfsc").write_bytes(b"x")          # not this job's: stays
        (root / "._Unrelated").write_bytes(b"x")              # beside another folder: stays
        self.assertEqual(ultra_core.strip_written_clutter(out), 2)
        self.assertEqual(sorted(p.name for p in root.rglob("._*")), ["._Unrelated", "._other.ffpfsc"])

    def test_folder_output(self):
        import tempfile
        root = Path(tempfile.mkdtemp()); out = root / "Game [extracted]"; (out / "sce_sys").mkdir(parents=True)
        (out / "sce_sys" / "._param.json").write_bytes(b"x"); (out / ".DS_Store").write_bytes(b"x")
        (root / "._Game [extracted]").write_bytes(b"x")
        self.assertEqual(ultra_core.strip_written_clutter(out), 3)
        self.assertFalse(list(root.rglob("._*")) or list(root.rglob(".DS_Store")))

    def test_nothing_to_do(self):
        self.assertEqual(ultra_core.strip_written_clutter(""), 0)
        self.assertEqual(ultra_core.strip_written_clutter("/nonexistent/x.ffpfsc"), 0)


if __name__ == "__main__":
    unittest.main()
