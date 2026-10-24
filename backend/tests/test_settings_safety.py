from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("PS5_FFPFSC_APP_DIR", tempfile.mkdtemp(prefix="ffpfsc-settings-test-"))
sys.path.insert(0, str(REPO))
import ultra_core as m  # noqa: E402


class _Profile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="profile-"))
        self._saved = (m.APP_DIR, m.SETTINGS_FILE, m.HISTORY_FILE)
        m.APP_DIR = self.dir
        m.SETTINGS_FILE = self.dir / "settings.json"
        m.HISTORY_FILE = self.dir / "history.json"

    def tearDown(self):
        m.APP_DIR, m.SETTINGS_FILE, m.HISTORY_FILE = self._saved
        for p in self.dir.rglob("*"):
            try:
                p.chmod(0o644)
            except OSError:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def _aside(self, stem: str) -> list:
        return sorted(p.name for p in self.dir.iterdir() if p.name.startswith(stem + ".corrupt-"))


PROFILE = {"queue": [{"path": "/x/game"}], "archive_passwords": ["pw"], "temp_pool": ["/t"]}


class SettingsSafety(_Profile):

    def test_a_locked_file_is_not_replaced_by_one_key(self):
        m.SETTINGS_FILE.write_text(json.dumps(PROFILE), encoding="utf-8")
        before = m.SETTINGS_FILE.read_bytes()
        locked = PermissionError(13, "in use")
        with mock.patch.object(m, "_SETTINGS_READ_TRIES", 2), \
                mock.patch.object(m, "_SETTINGS_SAVE_TRIES", 3), \
                mock.patch.object(m.time, "sleep", lambda _s: None), \
                mock.patch.object(Path, "read_text", side_effect=locked):
            self.assertEqual(m.load_settings(), {})
            self.assertFalse(m.settings_readable())
            self.assertFalse(m.save_settings({"last_source_dir": "/y"}))   # not written, no exception
        self.assertEqual(m.SETTINGS_FILE.read_bytes(), before)
        self.assertEqual(m.load_settings(), PROFILE)
        self.assertTrue(m.settings_readable())

    def test_save_waits_longer_than_a_plain_read(self):
        m.SETTINGS_FILE.write_text(json.dumps(PROFILE), encoding="utf-8")
        real = Path.read_text
        fails = {"left": 4}

        def flaky(self_path, *a, **k):
            if self_path == m.SETTINGS_FILE and fails["left"] > 0:
                fails["left"] -= 1
                raise PermissionError(13, "in use")
            return real(self_path, *a, **k)

        with mock.patch.object(m, "_SETTINGS_READ_TRIES", 2), \
                mock.patch.object(m, "_SETTINGS_SAVE_TRIES", 8), \
                mock.patch.object(m.time, "sleep", lambda _s: None), \
                mock.patch.object(Path, "read_text", autospec=True, side_effect=flaky):
            m.save_settings({"last_source_dir": "/y"})
        self.assertEqual(m.load_settings(), dict(PROFILE, last_source_dir="/y"))

    def test_a_bom_is_fine(self):
        m.SETTINGS_FILE.write_bytes(b"\xef\xbb\xbf" + json.dumps(PROFILE).encode("utf-8"))
        self.assertEqual(m.load_settings(), PROFILE)
        self.assertTrue(m.settings_readable())
        m.save_settings({"x": 1})
        self.assertEqual(m.load_settings(), dict(PROFILE, x=1))
        self.assertEqual(self._aside("settings.json"), [])

    def test_json_that_is_not_an_object_is_kept_aside(self):
        m.SETTINGS_FILE.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(m.load_settings(), {})
        self.assertFalse(m.settings_readable())
        m.save_settings({"x": 1})
        aside = self._aside("settings.json")
        self.assertEqual(len(aside), 1, aside)
        self.assertEqual((self.dir / aside[0]).read_text(encoding="utf-8"), "[1, 2, 3]")
        self.assertEqual(m.load_settings(), {"x": 1})
        self.assertTrue(m.settings_readable())

    def test_corrupt_file_is_still_kept_aside(self):
        m.SETTINGS_FILE.write_text("{broken", encoding="utf-8")
        self.assertEqual(m.load_settings(), {})
        self.assertFalse(m.settings_readable())
        m.save_settings({"x": 1})
        self.assertEqual(len(self._aside("settings.json")), 1)
        self.assertEqual(m.load_settings(), {"x": 1})

    def test_a_missing_file_is_readable(self):
        self.assertEqual(m.load_settings(), {})
        self.assertTrue(m.settings_readable())


class HistorySafety(_Profile):

    def test_save_is_atomic(self):
        m.save_history([{"name": "A"}])
        before = m.HISTORY_FILE.read_bytes()
        with mock.patch.object(m.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                m.save_history([{"name": "A"}, {"name": "B"}])
        self.assertEqual(m.HISTORY_FILE.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["history.json"], "no temp file left")

    def test_a_corrupt_history_is_kept_aside(self):
        m.HISTORY_FILE.write_text('[{"name": "A"}, {broken', encoding="utf-8")
        self.assertEqual(m.load_history(), [])
        aside = self._aside("history.json")
        self.assertEqual(len(aside), 1, aside)
        self.assertIn("broken", (self.dir / aside[0]).read_text(encoding="utf-8"))
        m.save_history([{"name": "B"}])
        self.assertEqual(m.load_history(), [{"name": "B"}])
        self.assertEqual(len(self._aside("history.json")), 1)

    def test_a_locked_history_is_not_overwritten(self):
        m.save_history([{"name": "A"}, {"name": "B"}])
        before = m.HISTORY_FILE.read_bytes()
        with mock.patch.object(m, "_SETTINGS_READ_TRIES", 2), \
                mock.patch.object(m.time, "sleep", lambda _s: None), \
                mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "in use")):
            hist = m.load_history()
            hist.append({"name": "C"})
            m.save_history(hist)
        self.assertEqual(m.HISTORY_FILE.read_bytes(), before)
        m.save_history(m.load_history() + [{"name": "C"}])
        self.assertEqual([h["name"] for h in m.load_history()], ["A", "B", "C"])


class AppTmpDirs(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tmpdirs-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _dir(self, rel: str, files=()) -> Path:
        d = self.root / rel
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_bytes(b"x")
        return d

    def test_user_folders_named_like_tmp_are_left_alone(self):
        for name in ("tmp_archive", "tmpdownload", "tmpRenders1"):
            d = self._dir(name, ["holiday.mov"])
            self.assertFalse(m.is_app_tmp_dir_in(d), name)

    def test_inside_an_app_scratch_folder_any_tmp_name_counts(self):
        for parent in m.SCRATCH_DIR_NAMES:
            d = self._dir(f"{parent}/tmpab12cd34", ["whatever.bin"])
            self.assertTrue(m.is_app_tmp_dir_in(d), parent)

    def test_loose_tmp_folders_count_only_with_backend_leftovers(self):
        self.assertTrue(m.is_app_tmp_dir_in(self._dir("tmpab12cd34")))                 # empty
        self.assertTrue(m.is_app_tmp_dir_in(self._dir("tmpab12cd35", ["x.ffpfs", "y.ffpfsc.partial",
                                                                      ".DS_Store"])))
        d = self._dir("tmpab12cd36")
        (d / "mkpfs-0001").mkdir()
        (d / "ffpfsc_spool_abc").mkdir()
        self.assertTrue(m.is_app_tmp_dir_in(d))
        self.assertFalse(m.is_app_tmp_dir_in(self._dir("tmpab12cd37", ["eboot.bin"])))
        self.assertFalse(m.is_app_tmp_dir_in(self._dir("tmp_photos", [])))            # name rule first
        self.assertFalse(m.is_app_tmp_dir_in(self.root / "tmpmissing1"))

    def test_the_old_name_check_is_unchanged(self):
        self.assertTrue(m._is_app_tmp_dir("tmpab12cd34"))
        self.assertFalse(m._is_app_tmp_dir("tmp_notes"))


class SameDrive(unittest.TestCase):

    def test_missing_folders_use_their_existing_ancestor(self):
        here = Path(tempfile.mkdtemp(prefix="samedrive-"))
        try:
            self.assertTrue(m.same_drive(here / "not" / "made" / "yet", here))
        finally:
            shutil.rmtree(here, ignore_errors=True)

    @unittest.skipUnless(sys.platform == "darwin", "macOS volume layout")
    def test_missing_folders_on_another_drive_are_not_the_same(self):
        here = Path(tempfile.mkdtemp(prefix="samedrive-"))
        try:
            # /dev is its own fs; a missing deep folder must not match via an empty drive letter
            self.assertFalse(m.same_drive(Path("/dev/no/such/folder"), here / "a" / "b"))
            self.assertFalse(m.same_drive(Path("/Volumes/NoSuchDrive_ffpfsc/x/y"), here / "a"))
            self.assertTrue(m.same_drive(Path("/Volumes/NoSuchDrive_ffpfsc/x"),
                                         Path("/Volumes/NoSuchDrive_ffpfsc/y/z")))
        finally:
            shutil.rmtree(here, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
