from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("PS5_FFPFSC_APP_DIR", tempfile.mkdtemp(prefix="ffpfsc-ramtemp-test-"))
sys.path.insert(0, str(REPO))
import ultra_core as m  # noqa: E402

GB = 1024 ** 3


@unittest.skipUnless(sys.platform.startswith("linux"), "RAM temp lives in /dev/shm, Linux only")
class RamTemp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.shm = Path(self._td.name) / "shm"
        self.shm.mkdir()
        self._orig = (m.SHM_DIR, m._mem_available)
        m.SHM_DIR = self.shm
        m._mem_available = lambda: 64 * GB
        m.save_settings({"ram_temp_gb": 0})

    def tearDown(self):
        m.SHM_DIR, m._mem_available = self._orig
        m.save_settings({"ram_temp_gb": 0})
        self._td.cleanup()

    def test_off_by_default(self):
        self.assertIsNone(m.ram_temp_root())
        self.assertFalse(m.is_ram_temp(self.shm / f"PS5_UltraPack-{os.getuid()}"))
        self.assertEqual(m.ram_temp_free(), 0)

    def test_the_budget_is_the_free_space(self):
        m.save_settings({"ram_temp_gb": 8})
        root = m.ram_temp_root()
        self.assertEqual(root, self.shm / f"PS5_UltraPack-{os.getuid()}")
        root.mkdir()
        disk = m.shutil.disk_usage(str(self.shm)).free
        self.assertEqual(m.get_free_space(root / "_ultrapack_temp"), min(8 * GB, disk))
        (root / "piece").write_bytes(b"x" * (1 << 20))
        self.assertEqual(m.get_free_space(root), min(8 * GB - m.get_folder_size(root), disk))

    def test_free_ram_caps_it(self):
        m.save_settings({"ram_temp_gb": 32})
        m.ram_temp_root().mkdir()
        m._mem_available = lambda: m.RAM_TEMP_RESERVE + 1 * GB
        self.assertEqual(m.get_free_space(m.ram_temp_root()), min(1 * GB, m.shutil.disk_usage(str(self.shm)).free))
        m._mem_available = lambda: m.RAM_TEMP_RESERVE - 1
        self.assertEqual(m.get_free_space(m.ram_temp_root()), 0)

    def test_it_counts_as_fast_and_other_paths_are_untouched(self):
        m.save_settings({"ram_temp_gb": 8})
        root = m.ram_temp_root()
        root.mkdir()
        self.assertEqual(m.drive_type_cached(root), "SSD")
        self.assertEqual(m.get_drive_type(root / "_ultrapack_temp"), "SSD")
        other = self.shm / "someone-else"
        other.mkdir()
        self.assertFalse(m.is_ram_temp(other))
        self.assertEqual(m.get_free_space(other), m.shutil.disk_usage(str(other)).free)
        self.assertFalse(m.is_ram_temp(Path(self._td.name)))


if __name__ == "__main__":
    unittest.main()
