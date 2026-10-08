"""The line under a job's name reads the same whatever became of the source: the archive's kind,
Folder, or the container's type; never a placeholder glyph for the title id.

    PYTHONPATH=. /tmp/ps5venv/bin/python -m unittest backend.tests.test_queue_labels
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_SCRATCH = Path(tempfile.mkdtemp(prefix="queue_labels_"))
os.environ.setdefault("PS5_FFPFSC_APP_DIR", str(_SCRATCH / "app_dir"))   # before ultra_core loads
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ultra_core  # noqa: E402


def item(**kw):
    base = dict(path="", archive_path=None, origin_archive=None, source_kind="inplace",
                title_id="", archive_title_id="")
    base.update(kw)
    return SimpleNamespace(**base)


class SourceLabel(unittest.TestCase):
    def test_a_folder_that_was_moved_away_is_still_a_folder(self):
        gone = _SCRATCH / "gone" / "Example Title 1.000 ppsa00001"
        self.assertEqual(ultra_core.source_label(item(path=str(gone))), "Folder")
        self.assertEqual(ultra_core.source_label(item(path=str(_SCRATCH / "gone" / "PPSA00001-app0"))), "Folder")

    def test_a_folder_on_disk(self):
        d = _SCRATCH / "Example Title v1.100 ppsa00002"
        d.mkdir(parents=True)
        self.assertEqual(ultra_core.source_label(item(path=str(d))), "Folder")

    def test_an_archive_reads_as_its_kind_also_once_it_is_unpacked(self):
        self.assertEqual(ultra_core.source_label(item(path=str(_SCRATCH / "x" / "PPSA00003-app0"),
                                                      origin_archive="/dl/release.part1.rar")), ".rar")
        self.assertEqual(ultra_core.source_label(item(path="/dl/release.rar", archive_path="/dl/release.rar",
                                                      source_kind="archive")), ".rar")
        for name, label in (("set.part01.rar", ".rar"), ("set.r00", ".rar"), ("set.RAR", ".rar"),
                            ("set.7z", ".7z"), ("set.7z.001", ".7z"), ("set.zip", ".zip"), ("set.z01", ".zip"),
                            ("set.zip.001", ".zip")):
            self.assertEqual(ultra_core.source_label(item(archive_path=f"/dl/{name}", source_kind="archive")), label, name)
        self.assertEqual(ultra_core.source_label(item(source_kind="archive")), "Archive")   # nothing to go by

    def test_containers_keep_their_type_on_disk_or_not(self):
        for suf in (".ffpfsc", ".ffpfs", ".pkg", ".exfat", ".ffpkg"):
            self.assertEqual(ultra_core.source_label(item(path=str(_SCRATCH / f"gone/Example [PPSA00004]{suf}"))), suf)
        f = _SCRATCH / "Example [PPSA00005] [v01.000].ffpfsc"
        f.write_bytes(b"x")
        self.assertEqual(ultra_core.source_label(item(path=str(f))), ".ffpfsc")

    def test_never_file_or_a_piece_of_the_folder_name(self):
        for p in ("", str(_SCRATCH / "gone" / "Example.of.the.End.1.000 ppsa00006")):
            label = ultra_core.source_label(item(path=p))
            self.assertEqual(label, "Folder", p)


class ShownTitleId(unittest.TestCase):
    def test_placeholders_are_never_shown(self):
        for glyph in ("📦", "💾", "📤", "Unknown", ""):
            self.assertEqual(ultra_core.shown_title_id(item(title_id=glyph)), "")

    def test_the_archive_id_wins_once_known(self):
        self.assertEqual(ultra_core.shown_title_id(item(title_id="📦", archive_title_id="PPSA00007")), "PPSA00007")
        self.assertEqual(ultra_core.shown_title_id(item(title_id="PPSA00008")), "PPSA00008")


def tearDownModule():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
