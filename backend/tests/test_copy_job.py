"""Unit tests for backend/copy_job.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import copy_job  # noqa: E402


class LineSink:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    def joined(self) -> str:
        return "\n".join(self.lines)


class RunCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="copy_job_test_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_src(self, name: str = "game.ffpfsc", size: int = 1024) -> Path:
        p = self.tmp / "src" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"X" * size)
        return p

    def test_folder_copy_replaces_a_partial_one_left_behind(self):
        src = self.tmp / "src" / "game"
        (src / "sce_sys").mkdir(parents=True)
        (src / "eboot.bin").write_bytes(b"E" * 100)
        dst = self.tmp / "out" / "game"
        stale = dst.with_name("game.copy-tmp")
        stale.mkdir(parents=True)
        (stale / "half.bin").write_bytes(b"h")          # an earlier run that was cut off
        rc = copy_job.run_copy_tree(src, dst, mode=copy_job.KEEP, on_line=LineSink())
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in dst.iterdir()), ["eboot.bin", "sce_sys"])
        self.assertFalse(stale.exists())

    # ── same-drive fast path ─────────────────────────────────────────────────

    def test_same_drive_keep_leaves_the_source(self):
        # The default: nothing is taken away. On APFS (the test's temp drive) a clone.
        src = self._make_src()
        dst_dir = self.tmp / "dst"
        sink = LineSink()

        rc = copy_job.run_copy(src, dst_dir, on_line=sink)

        self.assertEqual(rc, 0, sink.joined())
        self.assertTrue(src.is_file(), "keep must leave the source")
        self.assertEqual((dst_dir / "game.ffpfsc").read_bytes(), src.read_bytes())
        self.assertIn("[PHASE] Writing Final Image", sink.joined())
        self.assertIn("[SUCCESS] Copied", sink.joined())

    def test_same_drive_keep_copies_where_it_cannot_clone(self):
        src = self._make_src(size=CHUNK_SIZE_FOR_TEST + 5)
        dst_dir = self.tmp / "dst"
        sink = LineSink()
        with mock.patch.object(copy_job, "_clone", return_value=False):
            rc = copy_job.run_copy(src, dst_dir, on_line=sink)
        self.assertEqual(rc, 0, sink.joined())
        self.assertTrue(src.is_file())
        self.assertEqual((dst_dir / "game.ffpfsc").read_bytes(), src.read_bytes())
        self.assertIn("same-drive copy", sink.joined())

    def test_same_drive_move_and_organize_rename(self):
        for mode in ("move", "organize"):
            src = self._make_src(f"{mode}.ffpfsc")
            dst_dir = self.tmp / f"dst_{mode}"
            sink = LineSink()
            rc = copy_job.run_copy(src, dst_dir, mode=mode, on_line=sink)
            self.assertEqual(rc, 0, sink.joined())
            self.assertFalse(src.exists(), f"{mode}: same drive is a rename")
            self.assertTrue((dst_dir / f"{mode}.ffpfsc").is_file())
            self.assertIn("same-drive move", sink.joined())

    def test_unknown_mode_is_refused(self):
        src = self._make_src()
        self.assertEqual(copy_job.run_copy(src, self.tmp / "dst", mode="delete"), 1)
        self.assertTrue(src.is_file())

    def test_not_enough_space_stops_before_writing(self):
        src = self._make_src(size=1000)
        dst_dir = self.tmp / "dst"
        sink = LineSink()
        usage = mock.Mock(free=10)
        with mock.patch.object(copy_job, "_same_device", return_value=False), \
                mock.patch.object(copy_job.shutil, "disk_usage", return_value=usage):
            rc = copy_job.run_copy(src, dst_dir, mode="move", on_line=sink)
        self.assertEqual(rc, 1, sink.joined())
        self.assertIn("not enough space", sink.joined())
        self.assertTrue(src.is_file())
        self.assertFalse((dst_dir / "game.ffpfsc").exists())

    def test_dst_name_override(self):
        src = self._make_src()
        dst_dir = self.tmp / "dst"

        rc = copy_job.run_copy(src, dst_dir, dst_name="renamed.ffpfsc")

        self.assertEqual(rc, 0)
        self.assertTrue((dst_dir / "renamed.ffpfsc").is_file())
        self.assertFalse((dst_dir / "game.ffpfsc").exists())

    # ── safety: src == dst ────────────────────────────────────────────────────

    def test_src_equals_dst_skips_with_code_2(self):
        src = self._make_src()
        # Same folder, same name → resolves-same must catch it.
        sink = LineSink()
        rc = copy_job.run_copy(src, src.parent, on_line=sink)
        self.assertEqual(rc, 2, sink.joined())
        self.assertTrue(src.exists(), "src must NOT be deleted on the same-file guard")

    # ── safety: collision ─────────────────────────────────────────────────────

    def test_collision_skips_with_code_3(self):
        src = self._make_src("game.ffpfsc", size=100)
        dst_dir = self.tmp / "dst"
        dst_dir.mkdir()
        (dst_dir / "game.ffpfsc").write_bytes(b"Y" * 999)  # different file, same name

        sink = LineSink()
        rc = copy_job.run_copy(src, dst_dir, on_line=sink)

        self.assertEqual(rc, 3, sink.joined())
        self.assertTrue(src.exists(), "src must survive a collision skip")
        self.assertEqual((dst_dir / "game.ffpfsc").read_bytes(), b"Y" * 999,
                         "existing target must be untouched on a collision skip")

    # ── validation: unsupported suffix ────────────────────────────────────────

    def test_unsupported_suffix_rejected(self):
        bad = self.tmp / "src.bin"
        bad.write_bytes(b"x")
        rc = copy_job.run_copy(bad, self.tmp / "dst")
        self.assertEqual(rc, 1)

    def test_missing_source_rejected(self):
        rc = copy_job.run_copy(self.tmp / "nope.ffpfsc", self.tmp / "dst")
        self.assertEqual(rc, 1)

    # ── cross-drive path (simulated via monkeypatch) ─────────────────────────

    def test_cross_drive_copy_emits_progress_and_keeps_source_by_default(self):
        # Force _same_device to return False so the cross-drive branch runs
        # inside a single tmp filesystem (portable test).
        src = self._make_src("game.pkg", size=CHUNK_SIZE_FOR_TEST * 3 + 111)
        dst_dir = self.tmp / "dst"

        original = copy_job._same_device
        copy_job._same_device = lambda a, b: False
        try:
            sink = LineSink()
            rc = copy_job.run_copy(src, dst_dir, on_line=sink)
        finally:
            copy_job._same_device = original

        self.assertEqual(rc, 0, sink.joined())
        self.assertTrue(src.exists(), "keep (the default) must keep the source")
        self.assertTrue((dst_dir / "game.pkg").is_file())
        self.assertEqual((dst_dir / "game.pkg").read_bytes(), src.read_bytes())
        # Progress markers should have fired
        joined = sink.joined()
        self.assertIn("[####]", joined)
        self.assertIn("% copy", joined)
        self.assertIn("[SUCCESS] Copied", joined)

    def test_cross_drive_move_deletes_source_organize_keeps_it(self):
        for mode, gone in (("move", True), ("organize", False)):
            src = self._make_src(f"{mode}.ffpfs", size=CHUNK_SIZE_FOR_TEST + 7)
            dst_dir = self.tmp / f"dst_{mode}"
            with mock.patch.object(copy_job, "_same_device", return_value=False):
                rc = copy_job.run_copy(src, dst_dir, mode=mode)
            self.assertEqual(rc, 0)
            self.assertEqual(not src.exists(), gone, f"{mode}: source removed after a cross-drive copy?")
            self.assertTrue((dst_dir / f"{mode}.ffpfs").is_file())

    def test_cross_drive_write_failure_leaves_no_visible_target(self):
        # Simulate: force cross-drive, then make the write fail by making the
        # destination folder read-only after mkdir. We fail cleanly and no
        # half-written target sits under the final name.
        src = self._make_src(size=8)
        dst_dir = self.tmp / "dst"
        dst_dir.mkdir()

        original_same = copy_job._same_device
        original_open = open  # not used in copy_job - we monkey the module's open
        copy_job._same_device = lambda a, b: False

        # Replace copy_job's open with one that raises on the destination write
        real_open = open

        def evil_open(path, mode="r", *args, **kwargs):
            if "w" in mode and str(path).endswith(".copy-tmp"):
                raise OSError("simulated write failure")
            return real_open(path, mode, *args, **kwargs)

        original_builtin_open = copy_job.open if hasattr(copy_job, "open") else None
        copy_job.open = evil_open
        try:
            sink = LineSink()
            rc = copy_job.run_copy(src, dst_dir, on_line=sink)
        finally:
            copy_job._same_device = original_same
            if original_builtin_open is None:
                del copy_job.open
            else:
                copy_job.open = original_builtin_open

        self.assertEqual(rc, 1, sink.joined())
        self.assertTrue(src.exists(), "source must remain on write failure")
        self.assertFalse((dst_dir / "game.ffpfsc").exists(),
                         "no half-written target should be visible")


CHUNK_SIZE_FOR_TEST = copy_job.CHUNK


if __name__ == "__main__":
    unittest.main(verbosity=2)
