"""The output Auto: what an archive holds, read from its member names, and the output the
Settings table picks for each kind of source.

    PYTHONPATH=. .venv/bin/python -m unittest backend.tests.test_auto_output
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

_SCRATCH = Path(tempfile.mkdtemp(prefix="auto_output_"))
os.environ.setdefault("PS5_FFPFSC_APP_DIR", str(_SCRATCH / "app_dir"))   # before ultra_core loads
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ultra_core  # noqa: E402

kind = ultra_core.content_kind_from_members


def tearDownModule():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


class ContentFromNames(unittest.TestCase):
    def test_a_game_folder_needs_param_json_and_eboot_side_by_side(self):
        self.assertEqual(kind(["Game/", "Game/eboot.bin", "Game/sce_sys/param.json"]), "folder")
        self.assertEqual(kind(["eboot.bin", "sce_sys/param.json"]), "folder")
        self.assertEqual(kind(["A/eboot.bin", "B/sce_sys/param.json"]), "")

    def test_images_and_packages(self):
        self.assertEqual(kind(["x/Game [PPSA00001].pkg"]), "pkg")
        self.assertEqual(kind(["Game.exfat"]), "exfat")
        self.assertEqual(kind(["Game.ffpkg"]), "ffpkg")
        self.assertEqual(kind(["Game.ffpfs"]), "ffpfs")
        self.assertEqual(kind(["Game.ffpfs", "Game.FFPFSC"]), "ffpfsc")

    def test_the_order_the_unpacked_archive_is_classified_in(self):
        # an image beats a game folder, a game folder beats a package
        self.assertEqual(kind(["g/eboot.bin", "g/sce_sys/param.json", "g.exfat"]), "exfat")
        self.assertEqual(kind(["g/eboot.bin", "g/sce_sys/param.json", "update.pkg"]), "folder")

    def test_junk_and_unknown(self):
        self.assertEqual(kind(["__MACOSX/._Game.pkg", "._Game.exfat", "readme.txt"]), "")
        self.assertEqual(kind(["inner.rar"]), "")
        self.assertEqual(kind([]), "")


class Table(unittest.TestCase):
    def test_defaults(self):
        a = ultra_core.auto_actions()
        self.assertEqual(a["folder"], "ffpfsc")
        self.assertEqual(a["exfat"], "ffpfsc")
        self.assertEqual(a["pkg"], "organize")
        self.assertEqual(set(a), set(ultra_core.AUTO_SOURCE_KINDS))

    def test_settings_override_and_bad_values_are_dropped(self):
        a = ultra_core.auto_actions({"pkg": "folder", "exfat": "nonsense", "zzz": "pkg"})
        self.assertEqual(a["pkg"], "folder")
        self.assertEqual(a["exfat"], "ffpfsc")
        self.assertNotIn("zzz", a)

    def test_a_folder_never_goes_to_a_folder(self):
        self.assertEqual(ultra_core.auto_actions({"folder": "folder"})["folder"], "ffpfsc")

    def test_unknown_kind_is_treated_as_a_folder(self):
        self.assertEqual(ultra_core.auto_target("weird", {"folder": "exfat"}), "exfat")


class FromArchives(unittest.TestCase):
    def test_zip(self):
        z = _SCRATCH / "pkg.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("Some Game/Some Game.pkg", b"\0" * 64)
        self.assertEqual(ultra_core.ArchiveExtractor.content_kind(z), "pkg")

    def test_7z_with_a_game_folder(self):
        try:
            import py7zr
        except ImportError:
            self.skipTest("py7zr is not installed")
        src = _SCRATCH / "g7" / "Game"
        (src / "sce_sys").mkdir(parents=True)
        (src / "sce_sys" / "param.json").write_text("{}")
        (src / "eboot.bin").write_bytes(b"x")
        a = _SCRATCH / "game.7z"
        with py7zr.SevenZipFile(a, "w") as f:
            f.writeall(src, "Game")
        self.assertEqual(ultra_core.ArchiveExtractor.content_kind(a), "folder")

    def test_unreadable_archive(self):
        bad = _SCRATCH / "bad.zip"
        bad.write_bytes(b"not a zip")
        self.assertEqual(ultra_core.ArchiveExtractor.content_kind(bad), "")


class QueueLabel(unittest.TestCase):
    def test_the_content_replaces_the_archive_kind(self):
        it = SimpleNamespace(path="", archive_path=_SCRATCH / "x.rar", origin_archive=None,
                             source_kind="archive", archive_content="pkg")
        self.assertEqual(ultra_core.source_label(it), ".pkg")
        it.archive_content = "folder"
        self.assertEqual(ultra_core.source_label(it), "Folder")
        it.archive_content = ""
        self.assertEqual(ultra_core.source_label(it), ".rar")

    def test_after_the_unpack(self):
        it = SimpleNamespace(path=str(_SCRATCH / "unpacked"), archive_path=None,
                             origin_archive=str(_SCRATCH / "x.7z"), source_kind="inplace",
                             archive_content="exfat")
        self.assertEqual(ultra_core.source_label(it), ".exfat")


if __name__ == "__main__":
    unittest.main()
