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
        fd, name = tempfile.mkstemp(suffix=".exfat")
        os.close(fd)
        self.img = Path(name)

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
                self.assertEqual(gate, m.space_shortfall(item, TEMP, OUT) is None, name)

    def test_shortfall_names_the_drive(self):
        item = dict(self._items())["folder, one drive"]
        self.free = {TEMP: 600 * GB, OUT: 10 * GB}
        label, folder, need, free = m.space_shortfall(item, TEMP, OUT)
        self.assertEqual((label, folder, free), ("Output drive", OUT, 10 * GB))
        self.assertGreater(need, free)
        self.free = {TEMP: 600 * GB, OUT: 600 * GB}
        self.assertIsNone(m.space_shortfall(item, TEMP, OUT))

    def test_disk_image_needs_no_temp_space(self):
        item = dict(self._items())["disk image"]
        self.free = {TEMP: 0, OUT: 200 * GB}
        self.assertTrue(m._space_preflight_ok(item, TEMP, OUT))
        labels = [label for label, _d, _n in m._space_requirements(item, TEMP, OUT)]
        self.assertEqual(labels, ["Output drive"])

    def test_unknown_size_is_gated_with_an_estimate(self):
        # an archive whose headers give no size: on-disk set x 1.05, like the router
        item = types.SimpleNamespace(size=5 * GB, extracted_size=0, source_kind="archive",
                                     operation="pack", path=None, _output_compressed=True)
        self.free = {TEMP: 0, OUT: 0}
        self.assertFalse(m._space_preflight_ok(item, TEMP, OUT))
        need = sum(n for _l, _d, n in m._space_requirements(item, TEMP, OUT))
        self.assertGreaterEqual(need, int(5 * GB * 1.05))
        self.free = {TEMP: 600 * GB, OUT: 600 * GB}
        self.assertTrue(m._space_preflight_ok(item, TEMP, OUT))

    def test_nothing_known_is_not_blocked(self):
        item = types.SimpleNamespace(size=0, extracted_size=0, source_kind="archive",
                                     operation="pack", path=None, _output_compressed=True)
        self.free = {TEMP: 0, OUT: 0}
        self.assertTrue(m._space_preflight_ok(item, TEMP, OUT))
        rows, ok, _banner = m._space_report(item, TEMP, OUT)
        self.assertTrue(ok)
        self.assertIn("Space needed", [r[0] for r in rows])

    def test_needs_on_one_drive_are_added_up(self):
        # image on temp, source and output on the output drive; then put both on one drive
        item = types.SimpleNamespace(size=100 * GB, extracted_size=100 * GB, operation="pack",
                                     source_kind="archive", path=None, _output_compressed=True,
                                     _image_only_on_temp=True)
        split = m._space_requirements(item, TEMP, OUT)
        self.assertEqual(len(split), 2)
        total = sum(n for _l, _d, n in split)
        one = m._space_requirements(item, OUT, OUT)
        self.assertEqual(len(one), 1)
        label, folder, need = one[0]
        self.assertEqual((folder, need), (OUT, total))
        self.assertIn("inner image", label)
        self.assertIn("Output drive", label)
        self.free = {OUT: max(n for _l, _d, n in split) + GB}     # each part fits, the sum does not
        self.assertFalse(m._space_preflight_ok(item, OUT, OUT))
        self.free = {OUT: total + GB}
        self.assertTrue(m._space_preflight_ok(item, OUT, OUT))

    def test_pool_split_on_one_drive_is_added_up(self):
        item = types.SimpleNamespace(size=100 * GB, extracted_size=100 * GB, operation="pack",
                                     source_kind="archive", path=None, _output_compressed=True,
                                     _extract_on_pool=True, _build_temp=POOL, _build_root=POOL)
        reqs = m._space_requirements(item, TEMP, OUT)
        self.assertEqual([d for _l, d, _n in reqs], [POOL, OUT])
        self.assertGreaterEqual(reqs[0][2], int(100 * GB * (1 + m.IMAGE_PEAK_FACTOR)) - 1)

    def _container(self, suffix: str, size: int) -> Path:
        fd, name = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        self.addCleanup(Path(name).unlink, missing_ok=True)
        with open(name, "wb") as f:
            f.truncate(size)
        return Path(name)

    def test_a_ffpfsc_that_is_unpacked_is_sized_from_the_container(self):
        src = self._container(".ffpfsc", 4096)
        item = m.GameItem.from_chain(src, to="ffpfsc", sign=True)
        item.size = item.extracted_size = 50 * GB                 # the container on disk
        reqs = dict((d, n) for _l, d, n in m._space_requirements(item, TEMP, OUT))
        sized = m.estimate_peak_space_needed(100 * GB, m._peak_factor_for(item), False)
        self.assertEqual(reqs[TEMP], sized)
        item.extracted_size = 140 * GB                            # a better size known
        reqs = dict((d, n) for _l, d, n in m._space_requirements(item, TEMP, OUT))
        self.assertEqual(reqs[TEMP], m.estimate_peak_space_needed(140 * GB, m._peak_factor_for(item), False))

    def test_a_pkg_whose_probe_failed_is_sized_from_the_package(self):
        src = self._container(".pkg", 4096)
        item = m.GameItem.from_chain(src, to="ffpfsc")
        item.size = item.extracted_size = 30 * GB
        item.pkg_content_size = 0
        reqs = dict((d, n) for _l, d, n in m._space_requirements(item, TEMP, OUT))
        self.assertEqual(reqs[TEMP], m.estimate_peak_space_needed(60 * GB, m._peak_factor_for(item), False))

    def _copy_item(self, mode: str):
        src = self._container(".ffpfsc", 4096)
        return types.SimpleNamespace(size=80 * GB, extracted_size=80 * GB, operation="copy",
                                     source_kind="inplace", path=src, archive_path=None,
                                     copy_mode=mode, _output_compressed=True)

    def test_same_drive_copy_needs_the_size_unless_it_moves_or_clones(self):
        m.same_drive = lambda a, b: True
        orig = m._drive_can_clone
        try:
            m._drive_can_clone = lambda d: False
            reqs = m._space_requirements(self._copy_item("keep"), TEMP, OUT)
            self.assertEqual([(d, n) for _l, d, n in reqs], [(OUT, int(80 * GB * 1.02))])
            self.assertEqual(m._space_requirements(self._copy_item("move"), TEMP, OUT), [])
            self.assertEqual(m._space_requirements(self._copy_item("organize"), TEMP, OUT), [])
            m._drive_can_clone = lambda d: True
            self.assertEqual(m._space_requirements(self._copy_item("keep"), TEMP, OUT), [])
            folder = self._copy_item("keep")
            folder.path = Path(tempfile.mkdtemp())          # copy_job copies a folder file by file
            self.addCleanup(folder.path.rmdir)
            self.assertEqual(len(m._space_requirements(folder, TEMP, OUT)), 1)
        finally:
            m._drive_can_clone = orig

    def test_a_ps4_copy_follows_its_after_job_rule(self):
        m.same_drive = lambda a, b: True
        orig = m._drive_can_clone
        try:
            m._drive_can_clone = lambda d: False
            item = self._copy_item("keep")
            item.content_kind = "ps4"
            self.assertEqual(len(m._space_requirements(item, TEMP, OUT)), 1)
            item.after_source = "delete"
            self.assertEqual(m._space_requirements(item, TEMP, OUT), [])
        finally:
            m._drive_can_clone = orig

    def test_safety_factor_does_not_shrink_the_output_term(self):
        orig = m.space_safety_factor
        try:
            m.space_safety_factor = lambda: 0.5
            need = m.estimate_peak_space_needed(100 * GB, 2.2, True)
            self.assertEqual(need, int(100 * GB * (2.2 * 0.5 + 1.05)))
            self.assertEqual(m.estimate_peak_space_needed(100 * GB, 2.2, False), int(100 * GB * 1.1))
        finally:
            m.space_safety_factor = orig


@unittest.skipUnless(sys.platform == "darwin", "APFS clones are a macOS feature")
class CloneProbe(unittest.TestCase):
    def test_a_missing_folder_cannot_clone(self):
        self.assertFalse(m._drive_can_clone(Path("/Volumes/NoSuchDrive_ffpfsc/x")))

    def test_the_answer_is_a_bool(self):
        self.assertIsInstance(m._drive_can_clone(Path(tempfile.gettempdir())), bool)

if __name__ == "__main__":
    unittest.main()
