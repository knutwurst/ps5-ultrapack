# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
"""PYTHONPATH=. .venv/bin/python -m unittest backend.tests.test_free_space_cache"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

_SCRATCH = Path(tempfile.mkdtemp(prefix="free_space_cache_"))
os.environ.setdefault("PS5_FFPFSC_APP_DIR", str(_SCRATCH / "app_dir"))   # before ultra_core loads
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ultra_core  # noqa: E402

_Usage = namedtuple("_Usage", "total used free")


def tearDownModule():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


class QuickFreeSpace(unittest.TestCase):
    # a busy exFAT drive took 47 s for one free-space query; the window must not wait for it
    def setUp(self):
        with ultra_core._free_lock:
            ultra_core._free_cache.clear()
            ultra_core._free_busy.clear()
        self.gate = threading.Event()

    def slow_usage(self, path):
        self.gate.wait(5)
        return _Usage(100, 40, 60)

    def test_quick_mode_never_waits_and_fills_in_later(self):
        with mock.patch.object(ultra_core.shutil, "disk_usage", self.slow_usage):
            t = time.monotonic()
            with ultra_core.quick_free_space():
                with self.assertRaises(ultra_core.FreeSpacePending):
                    ultra_core.get_free_space(_SCRATCH)
            self.assertLess(time.monotonic() - t, 0.5)
            self.gate.set()
            for _ in range(100):
                if not ultra_core._free_busy:
                    break
                time.sleep(0.02)
            with ultra_core.quick_free_space():
                self.assertEqual(ultra_core.get_free_space(_SCRATCH), 60)

    def test_one_measurement_at_a_time_per_drive(self):
        calls = []

        def usage(path):
            calls.append(path)
            self.gate.wait(5)
            return _Usage(100, 40, 60)
        with mock.patch.object(ultra_core.shutil, "disk_usage", usage):
            with ultra_core.quick_free_space():
                for _ in range(5):
                    with self.assertRaises(ultra_core.FreeSpacePending):
                        ultra_core.get_free_space(_SCRATCH)
            self.gate.set()
            time.sleep(0.1)
        self.assertEqual(len(calls), 1)

    def test_outside_quick_mode_it_measures(self):
        self.gate.set()
        with mock.patch.object(ultra_core.shutil, "disk_usage", self.slow_usage):
            self.assertEqual(ultra_core.get_free_space(_SCRATCH), 60)


if __name__ == "__main__":
    unittest.main()
