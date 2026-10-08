"""The time a job still needs: the running step's own time left (the backend's, or its pace
so far), then each later step at the speed this Mac has shown for it before, else in the
proportion the progress bands give it to the running step."""
import os, sys, tempfile, unittest
from pathlib import Path

os.environ.setdefault("PS5_FFPFSC_APP_DIR", tempfile.mkdtemp(prefix="time_left_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ultra_core as uc

PHASES = ["Extracting", "Creating Temp PFS", "Compressing"]
WIDTHS = {"Extracting": 28, "Creating Temp PFS": 24, "Compressing": 34}
GB = 10**9


class ParseEta(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(uc.eta_seconds("38m 08s"), 2288)
        self.assertEqual(uc.eta_seconds("1h 05m"), 3900)
        self.assertEqual(uc.eta_seconds("45s"), 45)
        self.assertEqual(uc.eta_seconds("ETA 2m 00s"), 120)
        for none in ("—", "", None, "soon"):
            self.assertIsNone(uc.eta_seconds(none))


class TimeLeft(unittest.TestCase):
    def test_the_last_step_is_its_own_time_left(self):
        # compressing is the last long step: its own time left is the job's (it was 11 min
        # from the bands while the compress alone needed 38)
        self.assertEqual(uc.job_time_left(PHASES, "Compressing", 2288, 60, 128 * GB, {}, WIDTHS), 2288)

    def test_later_steps_at_the_speed_seen_before(self):
        left = uc.job_time_left(PHASES, "Creating Temp PFS", 300, 500, 128 * GB, {"Compressing": 55e6}, WIDTHS)
        self.assertAlmostEqual(left, 300 + 128 * GB / 55e6, delta=1)

    def test_without_a_speed_the_bands_scale_the_running_step(self):
        left = uc.job_time_left(PHASES, "Creating Temp PFS", 300, 900, 128 * GB, {}, WIDTHS)
        self.assertAlmostEqual(left, 300 + (900 + 300) * 34 / 24, delta=1)

    def test_nothing_to_go_by(self):
        self.assertIsNone(uc.job_time_left(PHASES, "Reading Game", 30, 10, GB, {}, WIDTHS))
        self.assertIsNone(uc.job_time_left(PHASES, "Compressing", None, 10, GB, {}, WIDTHS))


if __name__ == "__main__":
    unittest.main()
