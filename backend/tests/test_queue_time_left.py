# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
"""PYTHONPATH=. .venv/bin/python -m unittest backend.tests.test_queue_time_left"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="queue_time_left_"))
os.environ.setdefault("PS5_FFPFSC_APP_DIR", str(_SCRATCH / "app_dir"))   # before ultra_core loads
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ultra_core  # noqa: E402

GB = 1 << 30


def tearDownModule():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


class QueueTimeLeft(unittest.TestCase):
    def test_waiting_jobs_count_at_their_own_pace(self):
        # 25 min left on the running job, then 2 TB at 0.5 s/GB of one kind and 1 TB at 2 s/GB of another
        left = ultra_core.queue_time_left(1500, 0, None, [(2048 * GB, 0.5 / GB), (1024 * GB, 2.0 / GB)], None)
        self.assertAlmostEqual(left, 1500 + 1024 + 2048)

    def test_a_queue_of_terabytes_is_not_half_an_hour(self):
        # the old estimate showed the running job's 30 min for a 2.9 TB queue when nothing had finished
        left = ultra_core.queue_time_left(1800, 0, None, [(2900 * GB, None)], 14.0 / GB)
        self.assertGreater(left, 10 * 3600)

    def test_unknown_pace_gives_no_number(self):
        self.assertIsNone(ultra_core.queue_time_left(1800, 0, None, [(100 * GB, None)], None))

    def test_running_job_without_its_own_estimate(self):
        self.assertAlmostEqual(ultra_core.queue_time_left(None, 10 * GB, 3.0 / GB, [], None), 30)

    def test_rounding_steps(self):
        r = ultra_core.round_time_left
        self.assertEqual(r(25 * 60), 25)
        self.assertEqual(r(97 * 60), 95)            # 5 min steps past an hour
        self.assertEqual(r(3 * 3600 + 20 * 60), 210)  # 30 min steps past 3 h
        self.assertEqual(r(23.6 * 3600), 24 * 60)   # whole hours past 10 h


if __name__ == "__main__":
    unittest.main()
