# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("PS5_FFPFSC_APP_DIR", tempfile.mkdtemp(prefix="ffpfsc-build-info-test-"))
sys.path.insert(0, str(REPO))
import ultra_core as m  # noqa: E402


class BuildInfo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, data):
        (self.base / "build_info.json").write_text(data if isinstance(data, str) else json.dumps(data),
                                                   encoding="utf-8")

    def test_ci_build_names_repo_and_commit(self):
        self.write({"repo": "knutwurst/ps5-ultrapack", "commit": "a4d9532"})
        self.assertEqual(m.build_info(self.base), {"repo": "knutwurst/ps5-ultrapack", "commit": "a4d9532"})

    def test_local_build_has_no_file(self):
        self.assertIsNone(m.build_info(self.base))

    def test_running_from_source_is_a_local_build(self):
        self.assertFalse(getattr(sys, "frozen", False))
        self.assertIsNone(m.build_info())

    def test_broken_or_odd_files_count_as_local(self):
        for data in ("{not json", "[]", {"repo": "x/y"}, {"repo": "", "commit": "abc1234"}, {"repo": "<>", "commit": "zz"}):
            self.write(data)
            self.assertIsNone(m.build_info(self.base), data)

    def test_values_are_cleaned_and_cut(self):
        self.write({"repo": "evil/<b>repo</b>\n", "commit": "ABCDEF0123456789abcdef0123"})
        self.assertEqual(m.build_info(self.base), {"repo": "evil/brepo/b", "commit": "0123456"})


class LegalFiles(unittest.TestCase):
    def test_license_is_gpl3_and_notice_has_the_attribution(self):
        self.assertIn("GNU GENERAL PUBLIC LICENSE\n                       Version 3",
                      (REPO / "LICENSE").read_text(encoding="utf-8"))
        notice = (REPO / "NOTICE").read_text(encoding="utf-8")
        self.assertIn("Copyright (C) 2024-2026 Knutwurst", notice)
        self.assertIn("PS5 UltraPack · Copyright © 2024-2026 Knutwurst", notice)

    def test_every_spec_ships_license_and_notice(self):
        for spec in ("PS5_UltraPack_macos.spec", "PS5_UltraPack_windows.spec", "PS5_UltraPack_linux.spec"):
            self.assertIn('datas += [("LICENSE", "."), ("NOTICE", ".")]', (REPO / spec).read_text(encoding="utf-8"),
                          spec)


if __name__ == "__main__":
    unittest.main()
