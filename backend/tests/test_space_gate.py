"""The pre-flight space gate and the Drive Space Diagnostics dialog must agree."""
from __future__ import annotations

import os
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("PS5_FFPFSC_APP_DIR", tempfile.mkdtemp(prefix="ffpfsc-space-test-"))
sys.path.insert(0, str(REPO))
import ultra_core as m  # noqa: E402  (the Tk-free core; no GUI import needed)

GB = 1024 ** 3
TEMP, OUT, POOL = Path("/virtual/temp"), Path("/virtual/out"), Path("/virtual/pool")


class SpaceGateAgreesWithDialog(unittest.TestCase):
    def setUp(self):
        self.free = {}
        self._orig = (m.get_free_space, m.same_drive)
        m.get_free_space = lambda d: self.free.get(Path(d), 0)
        m.same_drive = lambda a, b: Path(a) == Path(b)
        self.img = Path(tempfile.mkstemp(suffix=".exfat")[1])

    def tearDown(self):
        m.get_free_space, m.same_drive = self._orig
        self.img.unlink(missing_ok=True)

    def _items(self):
        base = dict(size=100 * GB, extracted_size=150 * GB, operation="pack", _output_compressed=True)
        yield "archive, one drive", types.SimpleNamespace(**base, source_kind="archive", path=None)
        yield "archive, image on temp", types.SimpleNamespace(**base, source_kind="archive", path=None,
                                                                _image_only_on_temp=True)
        yield "archive, pool split", types.SimpleNamespace(**base, source_kind="archive", path=None,
                                                             _extract_on_pool=True, _build_temp=POOL,
                                                             _build_root=TEMP)
        yield "folder, one drive", types.SimpleNamespace(**base, source_kind="folder", path=Path("/virtual/game"))
        yield "disk image", types.SimpleNamespace(size=120 * GB, extracted_size=0, operation="pack",
                                                  source_kind="inplace", path=self.img, archive_path=None,
                                                  _output_compressed=True)

    def test_dialog_verdict_equals_gate_verdict(self):
        rnd = random.Random(1115)
        for name, item in self._items():
            for _ in range(200):
                self.free = {TEMP: rnd.randint(0, 600) * GB, OUT: rnd.randint(0, 600) * GB,
                             POOL: rnd.randint(0, 600) * GB}
                gate = m._space_preflight_ok(item, TEMP, OUT)
                _rows, dialog_ok, banner = m._space_report(item, TEMP, OUT)
                self.assertEqual(gate, dialog_ok, f"{name}: gate={gate} dialog={dialog_ok} free={self.free}")
                self.assertEqual(dialog_ok, banner.startswith("✓"), banner)

    def test_disk_image_needs_no_temp_space(self):
        item = dict(self._items())["disk image"]
        self.free = {TEMP: 0, OUT: 200 * GB}
        self.assertTrue(m._space_preflight_ok(item, TEMP, OUT))
        labels = [label for label, _d, _n in m._space_requirements(item, TEMP, OUT)]
        self.assertEqual(labels, ["Output drive"])

    def test_unknown_size_is_not_blocked(self):
        item = types.SimpleNamespace(size=5 * GB, extracted_size=0, source_kind="archive",
                                     operation="pack", path=None, _output_compressed=True)
        self.free = {TEMP: 0, OUT: 0}
        self.assertTrue(m._space_preflight_ok(item, TEMP, OUT))
        rows, ok, _banner = m._space_report(item, TEMP, OUT)
        self.assertTrue(ok)
        self.assertIn("Space needed", [r[0] for r in rows])


if __name__ == "__main__":
    unittest.main()
