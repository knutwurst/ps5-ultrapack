# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
"""PYTHONPATH=. .venv/bin/python -m unittest backend.tests.test_source_presence"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="source_presence_"))
os.environ.setdefault("PS5_FFPFSC_APP_DIR", str(_SCRATCH / "app_dir"))   # before ultra_core loads
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ultra_core  # noqa: E402


def tearDownModule():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


class SourcePresence(unittest.TestCase):
    # only an unplugged drive means "waits for its drive"; a missing file or a pending
    # macOS permission prompt is something else
    def test_a_present_file_is_ok(self):
        f = _SCRATCH / "game.ffpfsc"; f.write_bytes(b"x")
        self.assertEqual(ultra_core.source_presence(f), "ok")

    def test_a_missing_file_on_the_system_drive_is_gone(self):
        self.assertEqual(ultra_core.source_presence(_SCRATCH / "nope.ffpfsc"), "gone")

    @unittest.skipIf(sys.platform == "win32", "posix mount layout")
    def test_an_unplugged_drive_is_offline(self):
        self.assertEqual(ultra_core.source_presence("/Volumes/__no_such_drive__/Game/x.ffpfsc"), "offline")
        self.assertEqual(ultra_core.source_presence("/media/nobody/__no_such_drive__/x.ffpfsc"), "offline")

    @unittest.skipUnless(sys.platform == "darwin" and any(Path("/Volumes").iterdir()), "needs a mounted volume")
    def test_a_missing_file_on_a_connected_drive_is_gone(self):
        vol = next(p for p in Path("/Volumes").iterdir() if os.access(p, os.R_OK))
        self.assertEqual(ultra_core.source_presence(vol / "__no_such_folder__" / "x.ffpfsc"), "gone")

    @unittest.skipIf(sys.platform == "win32" or os.geteuid() == 0, "needs posix modes, not root")
    def test_no_permission_is_denied_not_offline(self):
        d = _SCRATCH / "locked"; d.mkdir(); (d / "game.ffpfsc").write_bytes(b"x")
        os.chmod(d, 0)
        try:
            self.assertEqual(ultra_core.source_presence(d / "game.ffpfsc"), "denied")
        finally:
            os.chmod(d, 0o755)


if __name__ == "__main__":
    unittest.main()
