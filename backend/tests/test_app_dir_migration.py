"""Settings-folder migration after the rename to PS5 UltraPack."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUPPORT = Path("Library") / "Application Support"
NEW = "PS5_UltraPack"
ULTRA = "PS5_FFPFSC_ULTRA_BIZKUT"
PRO = "PS5_FFPFSC_PRO_BIZKUT"


@unittest.skipUnless(sys.platform == "darwin", "the settings folder lives under ~/Library on macOS")
class AppDirMigration(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="appdir_mig_"))
        self.support = self.home / SUPPORT
        self.support.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _profile(self, name: str, marker: str) -> Path:
        d = self.support / name
        d.mkdir()
        (d / "settings.json").write_text(f'{{"marker": "{marker}"}}', encoding="utf-8")
        return d

    def _import_core(self, env_app_dir: str | None = None) -> str:
        """Import ultra_core with HOME=self.home; return the APP_DIR it settled on."""
        env = {k: v for k, v in os.environ.items() if k != "PS5_FFPFSC_APP_DIR"}
        env["HOME"] = str(self.home)
        if env_app_dir is not None:
            env["PS5_FFPFSC_APP_DIR"] = env_app_dir
        out = subprocess.run(
            [sys.executable, "-c", "import ultra_core; print(ultra_core.APP_DIR)"],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip().splitlines()[-1]

    def _marker(self, name: str) -> str:
        return (self.support / name / "settings.json").read_text(encoding="utf-8")

    def test_moves_the_ultra_folder(self):
        self._profile(ULTRA, "ultra")
        self.assertEqual(self._import_core(), str(self.support / NEW))
        self.assertIn('"ultra"', self._marker(NEW))
        self.assertFalse((self.support / ULTRA).exists())

    def test_moves_the_pro_folder_when_no_ultra_one_exists(self):
        self._profile(PRO, "pro")
        self._import_core()
        self.assertIn('"pro"', self._marker(NEW))
        self.assertFalse((self.support / PRO).exists())

    def test_prefers_the_newer_ultra_folder_over_pro(self):
        self._profile(ULTRA, "ultra")
        self._profile(PRO, "pro")
        self._import_core()
        self.assertIn('"ultra"', self._marker(NEW))
        self.assertTrue((self.support / PRO).is_dir(), "the older PRO folder stays untouched")

    def test_an_existing_new_folder_wins(self):
        self._profile(NEW, "new")
        self._profile(ULTRA, "ultra")
        self._import_core()
        self.assertIn('"new"', self._marker(NEW))
        self.assertIn('"ultra"', self._marker(ULTRA))

    def test_env_override_skips_the_migration(self):
        self._profile(ULTRA, "ultra")
        override = self.home / "isolated-profile"
        self.assertEqual(self._import_core(str(override)), str(override))
        self.assertTrue((self.support / ULTRA).is_dir())
        self.assertFalse((self.support / NEW).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
