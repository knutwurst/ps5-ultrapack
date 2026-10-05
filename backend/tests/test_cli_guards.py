"""Guards in backend/cli.py and backend/copy_job.py that protect the user's files.

Covers: refusing to build onto the source itself, a stale pass-1 image not
blocking a rebuild, overwrite-by-swap (the previous file survives a failed
build), --keep-pfs never clobbering an existing .ffpfs, the ZIP source context
manager reporting only real extraction failures, the copy job detecting a short
cross-drive write, fPKG validation running before the success line, and the
mkpfs resolver using nothing but the bundled package.

The pack tests drive the real cli.py + vendored mkpfs on a tiny synthetic game
(well under a second each) exactly the way the GUI does: stdin closed.

    /tmp/ps5venv/bin/python -m unittest backend.tests.test_cli_guards
"""

from __future__ import annotations

import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import cli  # noqa: E402
import copy_job  # noqa: E402

CLI = BACKEND / "cli.py"
TITLE_ID = "ABCD01234"
MARKER = b"previous build - must survive"


def make_game(root: Path) -> Path:
    """A minimal PS5-style game folder: sce_sys/param.json + eboot.bin + one data file."""
    game = root / "game"
    (game / "sce_sys").mkdir(parents=True)
    (game / "sce_sys" / "param.json").write_text(json.dumps({
        "titleId": TITLE_ID,
        "contentId": f"UP0000-{TITLE_ID}_00-TESTTESTTEST0000",
        "contentVersion": "01.000.000",
        "titleName": "Test",
    }), encoding="utf-8")
    (game / "eboot.bin").write_bytes(os.urandom(200_000))
    (game / "data.bin").write_bytes(b"\0" * 300_000)
    return game


def run_cli(*argv: str, temp_dir: Path) -> subprocess.CompletedProcess:
    """Run cli.py the way the GUI does: stdin closed, output captured."""
    return subprocess.run(
        [sys.executable, str(CLI), *argv, "--temp-dir", str(temp_dir)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
    )


class PackGuardTests(unittest.TestCase):
    def setUp(self):
        # resolve(): cli.py prints resolved paths, and macOS maps /var -> /private/var.
        self.root = Path(tempfile.mkdtemp(prefix="cli_guards_")).resolve()
        self.game = make_game(self.root)
        self.out = self.root / "out"
        self.out.mkdir()
        self.temp = self.root / "temp"
        self.temp.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _pack(self, output: Path, *extra: str) -> subprocess.CompletedProcess:
        return run_cli(str(self.game), str(output), "--pack", *extra, temp_dir=self.temp)

    # ── item 2: source == output ─────────────────────────────────────────────

    def test_same_path_overwrite_is_refused_and_the_image_survives(self):
        image = self.out / "X.ffpfs"
        first = self._pack(image, "--no-compress", "--overwrite")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = image.read_bytes()
        self.assertGreater(len(before), 0)

        same = run_cli(str(image), str(image), "--pack", "--no-compress", "--overwrite",
                       temp_dir=self.temp)

        self.assertNotEqual(same.returncode, 0, same.stdout)
        self.assertIn(f"[ERROR] Output path is the source itself: {image}", same.stdout)
        self.assertTrue(image.is_file(), "the source image must not be deleted")
        self.assertEqual(image.read_bytes(), before, "the source image must be untouched")

    # ── item 1: stale pass-1 inner image ─────────────────────────────────────

    def test_stale_pass1_image_does_not_block_a_rebuild(self):
        inner_dir = self.temp / "_ffpfsc_inner"
        inner_dir.mkdir()
        (inner_dir / f"{TITLE_ID}.ffpfs").write_bytes(b"\0" * 100)   # left by a crash
        (inner_dir / f"{TITLE_ID}.ffpfs.tmp").write_bytes(b"\0" * 10)

        res = self._pack(self.out / "G.ffpfsc", "--overwrite")

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("EOFError", res.stdout + res.stderr)
        self.assertNotIn("Overwrite?", res.stdout)
        self.assertTrue((self.out / "G.ffpfsc").is_file())

    # ── item 3: overwrite swaps the finished build in ────────────────────────

    def test_overwrite_replaces_the_output_only_with_a_finished_build(self):
        target = self.out / "G.ffpfsc"
        target.write_bytes(MARKER)

        res = self._pack(target, "--overwrite")

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotEqual(target.read_bytes(), MARKER, "the new build must replace the old file")
        self.assertIn(f"[OK] Compression complete: {target}", res.stdout,
                      "the GUI marker must name the FINAL path, not the staging file")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["G.ffpfsc"],
                         "no .partial staging files may remain")

    def test_failed_overwrite_build_keeps_the_previous_file(self):
        target = self.out / "G.ffpfsc"
        target.write_bytes(MARKER)
        # A directory where mkpfs wants to open its temp file makes pass 2 fail
        # deterministically (the same effect as ENOSPC or an unplugged drive).
        (self.out / "G.ffpfsc.partial.tmp").mkdir()

        res = self._pack(target, "--overwrite")

        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("[ERROR]", res.stdout)
        self.assertEqual(target.read_bytes(), MARKER, "a failed build must not touch the old file")
        self.assertFalse((self.out / "G.ffpfsc.partial").exists())

    def test_uncompressed_copy_route_replaces_an_existing_output(self):
        src_image = self.out / "X.ffpfs"
        first = self._pack(src_image, "--no-compress", "--overwrite")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        target = self.out / "Y.ffpfs"
        target.write_bytes(MARKER)

        res = run_cli(str(src_image), str(target), "--pack", "--no-compress", "--overwrite",
                      temp_dir=self.temp)

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(target.read_bytes(), src_image.read_bytes())
        self.assertFalse((self.out / "Y.ffpfs.partial").exists())

    # ── item 6: --keep-pfs picks a free name ─────────────────────────────────

    def test_keep_pfs_does_not_clobber_an_existing_ffpfs(self):
        kept = self.out / f"{TITLE_ID}.ffpfs"
        kept.write_bytes(MARKER)

        res = self._pack(self.out / "G.ffpfsc", "--overwrite", "--keep-pfs")

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(kept.read_bytes(), MARKER, "the existing .ffpfs must be left alone")
        alt = self.out / f"{TITLE_ID} (2).ffpfs"
        self.assertTrue(alt.is_file(), sorted(p.name for p in self.out.iterdir()))
        self.assertGreater(alt.stat().st_size, len(MARKER))
        self.assertIn(f"keeping the intermediate image as {alt.name}", res.stdout)

    def test_free_sibling_name(self):
        p = self.out / f"{TITLE_ID}.ffpfs"
        self.assertEqual(cli._free_sibling_name(p), p)
        p.write_bytes(b"1")
        self.assertEqual(cli._free_sibling_name(p), self.out / f"{TITLE_ID} (2).ffpfs")
        (self.out / f"{TITLE_ID} (2).ffpfs").write_bytes(b"2")
        self.assertEqual(cli._free_sibling_name(p), self.out / f"{TITLE_ID} (3).ffpfs")


class ZipSourceTests(unittest.TestCase):
    """Item 7: only the extraction is inside the try; the body's errors are its own."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cli_zip_"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_body_runtime_error_is_not_reported_as_a_zip_failure(self):
        archive = self.root / "src.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.txt", "hello")
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(RuntimeError):
            with cli._extracted_zip_source(archive, temp_root=self.root) as src_dir:
                self.assertTrue((src_dir / "a.txt").is_file())
                raise RuntimeError("boom from the pack body")
        self.assertNotIn("ZIP extraction failed", out.getvalue())

    def test_corrupt_zip_is_reported_as_a_zip_failure(self):
        archive = self.root / "bad.zip"
        archive.write_bytes(b"this is not a zip archive")
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            with cli._extracted_zip_source(archive, temp_root=self.root):
                self.fail("must not yield for a corrupt archive")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[ERROR] ZIP extraction failed", out.getvalue())


class _DroppingWriter:
    """Claims to write every chunk but silently drops the second one, so the bytes
    on disk fall short of the source size while the caller's own count is complete."""

    def __init__(self, raw):
        self._raw = raw
        self._calls = 0

    def write(self, b):
        self._calls += 1
        if self._calls == 2:
            return len(b)
        return self._raw.write(b)

    def flush(self):
        self._raw.flush()

    def fileno(self):
        return self._raw.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._raw.__exit__(*exc)


class CopyJobIntegrityTests(unittest.TestCase):
    """Item 4: a cross-drive move verifies the copy before the source is deleted."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="copy_guard_"))
        self.src = self.root / "src" / "game.pkg"
        self.src.parent.mkdir()
        self.src.write_bytes(b"P" * (copy_job.CHUNK * 3 + 111))
        self.dst_dir = self.root / "dst"
        self._same_device = copy_job._same_device
        copy_job._same_device = lambda a, b: False      # force the cross-drive path

    def tearDown(self):
        copy_job._same_device = self._same_device
        shutil.rmtree(self.root, ignore_errors=True)

    def test_short_write_is_detected_and_the_source_survives(self):
        real_open = open

        def dropping_open(path, mode="r", *args, **kwargs):
            fh = real_open(path, mode, *args, **kwargs)
            if "w" in mode and str(path).endswith(".copy-tmp"):
                return _DroppingWriter(fh)
            return fh

        lines: list[str] = []
        copy_job.open = dropping_open
        try:
            rc = copy_job.run_copy(self.src, self.dst_dir, on_line=lines.append)
        finally:
            del copy_job.open

        joined = "\n".join(lines)
        self.assertEqual(rc, 1, joined)
        self.assertIn("short copy", joined)
        self.assertTrue(self.src.is_file(), "the source must survive a failed copy")
        self.assertFalse((self.dst_dir / "game.pkg").exists(), "no truncated target may appear")
        self.assertFalse((self.dst_dir / "game.pkg.copy-tmp").exists(), "the temp file is removed")

    def test_data_is_synced_before_the_source_is_deleted(self):
        events: list[str] = []
        real_fsync = copy_job._fsync_file

        def recording_fsync(fd):
            events.append("fsync")
            real_fsync(fd)

        real_unlink = Path.unlink
        src = self.src

        def recording_unlink(self_path, *a, **kw):
            if self_path == src:
                events.append("unlink")
            return real_unlink(self_path, *a, **kw)

        copy_job._fsync_file = recording_fsync
        Path.unlink = recording_unlink
        try:
            rc = copy_job.run_copy(self.src, self.dst_dir, mode="move", on_line=lambda _line: None)
        finally:
            copy_job._fsync_file = real_fsync
            Path.unlink = real_unlink

        self.assertEqual(rc, 0)
        self.assertEqual(events, ["fsync", "unlink"])
        self.assertFalse(self.src.exists())
        self.assertEqual((self.dst_dir / "game.pkg").stat().st_size, copy_job.CHUNK * 3 + 111)


class FpkgBuildOrderTests(unittest.TestCase):
    """Item 5: the checklist runs first; SUCCESS/OK markers follow; failures get a WARN."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cli_fpkg_")).resolve()
        self.game = make_game(self.root)
        self.out = self.root / "out"
        self.temp = self.root / "temp"
        self.temp.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run_with_fake_tool(self, validate_rc: int) -> str:
        fake = types.ModuleType("fpkg")
        fake.is_available = lambda: True

        def build(src_dir, out_dir, **kw):
            kw["on_line"]("[stage 5/5] build finished")
            (Path(out_dir) / "test.pkg").write_bytes(b"x" * 16)
            return 0

        def validate(pkg, on_line=None, **kw):
            on_line("Summary: 1 failed" if validate_rc else "Summary: 0 failed")
            return validate_rc

        fake.build = build
        fake.validate = validate
        saved_mod = sys.modules.get("fpkg")
        saved_argv = sys.argv
        out = io.StringIO()
        sys.modules["fpkg"] = fake
        # OUTPUT is the second positional (as the GUI passes it); --fpkg-build names the source.
        sys.argv = ["cli.py", str(self.game), str(self.out), "--fpkg-build", str(self.game),
                    "--temp-dir", str(self.temp)]
        try:
            with redirect_stdout(out):
                cli.main()
        finally:
            sys.argv = saved_argv
            if saved_mod is None:
                sys.modules.pop("fpkg", None)
            else:
                sys.modules["fpkg"] = saved_mod
        return out.getvalue()

    def test_validation_precedes_success_and_failures_are_flagged(self):
        log = self._run_with_fake_tool(validate_rc=1)
        summary = log.index("Summary: 1 failed")
        success = log.index("[SUCCESS] fPKG built.")
        complete = log.index(f"[OK] fPKG complete: {self.out / 'test.pkg'}")
        warn = log.index("[WARN] Validation reported failures")
        self.assertLess(summary, success)
        self.assertLess(success, complete)
        self.assertLess(complete, warn)

    def test_clean_validation_emits_no_warning(self):
        log = self._run_with_fake_tool(validate_rc=0)
        self.assertIn("[SUCCESS] fPKG built.", log)
        self.assertIn("[OK] fPKG complete:", log)
        self.assertNotIn("[WARN] Validation reported failures", log)


class MkpfsResolverTests(unittest.TestCase):
    """Item 8: only the bundled package; a missing one is a hard, explicit error."""

    def test_uses_the_bundled_package_only(self):
        with redirect_stdout(io.StringIO()):
            cmd, cwd = cli._locate_mkpfs()
        self.assertEqual(cmd, [sys.executable, "-m", "mkpfs"])
        self.assertEqual(cwd, str(BACKEND))
        # No runtime pip install, no PATH lookup, no sibling-directory scan left in the code.
        src = inspect.getsource(cli._locate_mkpfs)
        self.assertNotIn('"pip"', src)
        self.assertNotIn("subprocess.run", src)
        self.assertNotIn("which(", src)
        self.assertNotIn("iterdir", src)

    def test_missing_bundle_exits_with_a_clear_error(self):
        saved = cli._BUNDLED_MKPFS
        cli._BUNDLED_MKPFS = str(BACKEND / "definitely-missing" / "__main__.py")
        out = io.StringIO()
        try:
            with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                cli._locate_mkpfs()
        finally:
            cli._BUNDLED_MKPFS = saved
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[ERROR] Bundled MkPFS package not found at", out.getvalue())
        self.assertIn("the installation is incomplete", out.getvalue())


class SilentStepProgress(unittest.TestCase):
    """A full .pkg extract prints nothing until it is done; the backend measures the files
    already on disk and prints mkpfs-style bars the GUI reads (stage, %, speed, ETA)."""

    PROGRESS = __import__("re").compile(r"\[(?P<bar>[#\-]{4,})\]\s*(?P<pct>\d{1,3})%\s*(?P<label>.*)")

    def test_bars_follow_the_bytes_on_disk(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            chunk = b"x" * 400_000

            def work():
                for i in range(5):
                    (folder / f"part{i}.bin").write_bytes(chunk)
                    time.sleep(0.25)
                return 0

            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli._run_with_folder_progress(work, folder, 5 * len(chunk), "extract", interval=0.2)
            self.assertEqual(rc, 0)
            bars = [self.PROGRESS.search(l) for l in out.getvalue().splitlines()]
            bars = [b for b in bars if b]
            self.assertGreaterEqual(len(bars), 3, out.getvalue())
            pcts = [int(b.group("pct")) for b in bars]
            self.assertEqual(pcts, sorted(pcts))
            self.assertEqual(pcts[-1], 100)
            self.assertTrue(all(b.group("label").startswith("extract") for b in bars))
            self.assertTrue(any("MB/s" in b.group("label") and "ETA" in b.group("label") for b in bars[:-1]),
                            out.getvalue())

    def test_a_failing_step_still_raises(self):
        def work():
            raise RuntimeError("tool crashed")
        with tempfile.TemporaryDirectory() as td, redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                cli._run_with_folder_progress(work, Path(td), 100, "extract", interval=0.05)

    def test_the_label_says_where_and_how_much(self):
        line = cli._progress_line(5 * 1024 ** 3, 20 * 1024 ** 3, 10,
                                  "extract the .pkg to SAMSUNG: {done} of {total} GB")
        m = self.PROGRESS.search(line)
        self.assertEqual(int(m.group("pct")), 25)
        self.assertTrue(m.group("label").startswith("extract the .pkg to SAMSUNG: 5.0 of 20.0 GB @ "), line)
        self.assertEqual(cli._drive_label(Path("/Volumes/SAMSUNG/_ffpfsc_temp")), "SAMSUNG")
        self.assertEqual(cli._drive_label(Path("/tmp")), "the system drive")

    def test_the_label_names_the_extract_step(self):
        # the package tool's temp files tell the step: outer temp, inner temp being
        # written under a second name, then the finished inner image
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            self.assertEqual(cli._pkg_extract_step(folder), 0)
            (folder / ".libprospero-outer-ab.tmp").write_bytes(b"x")
            self.assertEqual(cli._pkg_extract_step(folder), 1)
            (folder / "..libprospero-inner-cd.tmp.ef.tmp").write_bytes(b"x")
            self.assertEqual(cli._pkg_extract_step(folder), 2)
            (folder / "..libprospero-inner-cd.tmp.ef.tmp").rename(folder / ".libprospero-inner-cd.tmp")
            (folder / ".libprospero-outer-ab.tmp").unlink()
            self.assertEqual(cli._pkg_extract_step(folder), 3)
            line = cli._progress_line(150 * 1024 ** 3, 200 * 1024 ** 3, 60,
                                      lambda: cli._pkg_extract_label(folder))
            m = self.PROGRESS.search(line)
            self.assertIn("step 3 of 3: write the game files (150.0 of 200.0 GB written)", m.group("label"))
            self.assertTrue(m.group("label").startswith("extract the .pkg on "), line)
            self.assertNotIn("compress", m.group("label"))   # the GUI would read it as Compressing
            pkg = folder / "t.pkg"; pkg.write_bytes(b"\0" * 1000)
            self.assertEqual(cli._pkg_extract_plan(pkg, 5000), 11000)
            self.assertEqual(cli._pkg_extract_plan(pkg, 0), 0)

    def test_the_writers_counter_wins_over_file_sizes(self):
        # the .pkg tool creates each file at its full size first, then fills it: the size
        # on disk says 100 % while the data is still coming. The tool's own write counter
        # is the truth; the folder size is only the fallback.
        import time
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            with open(folder / "presized.bin", "wb") as f:
                f.truncate(1_000_000)                 # "done" by size from the start
            counter = iter([100_000, 300_000, 500_000, 700_000, 900_000] + [900_000] * 50)

            def work():
                time.sleep(1.0)
                return 0

            out = io.StringIO()
            with redirect_stdout(out):
                cli._run_with_folder_progress(work, folder, 1_000_000, "extract", interval=0.15,
                                              measure=lambda: next(counter))
            pcts = [int(b.group("pct")) for b in map(self.PROGRESS.search, out.getvalue().splitlines()) if b]
            self.assertTrue(pcts and pcts[0] <= 30, pcts)
            self.assertEqual(pcts, sorted(pcts))

            out = io.StringIO()
            with redirect_stdout(out):
                cli._run_with_folder_progress(work, folder, 2_000_000, "extract", interval=0.15,
                                              measure=lambda: None)
            pcts = [int(b.group("pct")) for b in map(self.PROGRESS.search, out.getvalue().splitlines()) if b]
            self.assertTrue(pcts and pcts[0] == 50, pcts)   # no counter: the size on disk

    @unittest.skipUnless(sys.platform == "darwin" or __import__("importlib").util.find_spec("psutil"),
                         "needs proc_pid_rusage or psutil")
    def test_the_write_counter_reads_a_running_process(self):
        import subprocess, time
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "w.bin"
            code = ("import os,sys,time\nf=open(sys.argv[1],'wb')\n"
                    "for i in range(8):\n f.write(os.urandom(1<<20)); f.flush(); os.fsync(f.fileno()); time.sleep(0.1)\n"
                    "time.sleep(0.5)")
            proc = subprocess.Popen([sys.executable, "-c", code, str(target)])
            seen = []
            while proc.poll() is None:
                seen.append(cli._proc_bytes_written(proc.pid)); time.sleep(0.1)
            vals = [v for v in seen if v is not None]
            self.assertTrue(vals, seen)
            self.assertEqual(vals, sorted(vals))
            self.assertGreaterEqual(max(vals), 8 << 20)
        self.assertIsNone(cli._proc_bytes_written(2 ** 22 + 12345))

    @unittest.skipUnless(sys.platform == "darwin" or __import__("importlib").util.find_spec("psutil"),
                         "needs proc_pid_rusage or psutil")
    def test_a_coarse_tool_bar_gets_a_byte_meter(self):
        # MkPFS pass 1 counts whole files; the meter counts the bytes the process wrote,
        # and stays quiet until 1 % is written (the tool's scan/read bars come first)
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "img.bin"
            code = ("import os,sys,time\ntime.sleep(1.2)\nf=open(sys.argv[1],'wb')\n"
                    "for i in range(6):\n f.write(os.urandom(1<<20)); f.flush(); os.fsync(f.fileno()); time.sleep(0.3)")
            proc = subprocess.Popen([sys.executable, "-c", code, str(target)])
            out = io.StringIO()
            with redirect_stdout(out):
                cli._meter_process(proc, 6 << 20, "write the PFS image on TEST: {done} of {total} GB", interval=0.2)
            self.assertIsNotNone(proc.poll())
            bars = [b for b in map(self.PROGRESS.search, out.getvalue().splitlines()) if b]
            self.assertTrue(bars, out.getvalue())
            pcts = [int(b.group("pct")) for b in bars]
            self.assertEqual(pcts, sorted(pcts))
            self.assertGreaterEqual(pcts[0], 1)
            self.assertTrue(all(b.group("label").startswith("write the PFS image on TEST") for b in bars))
            # no counter: no bars, the tool keeps its own
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
            saved = cli._proc_bytes_written
            cli._proc_bytes_written = lambda _pid: None
            try:
                out = io.StringIO()
                with redirect_stdout(out):
                    cli._meter_process(proc, 100, "x", interval=0.1)
            finally:
                cli._proc_bytes_written = saved
                proc.wait()
            self.assertEqual(out.getvalue(), "")

    def test_the_tool_process_reaches_the_meter(self):
        import fpkg
        got = []
        rc = fpkg._run([sys.executable, "-c", "print('hi')"], on_line=lambda l: None,
                       on_start=lambda proc: got.append(proc.pid))
        self.assertEqual(rc, 0)
        self.assertEqual(len(got), 1)

    def test_eta_and_rate_use_mkpfs_units(self):
        self.assertEqual(cli._fmt_eta(57), "57s")
        self.assertEqual(cli._fmt_eta(5052), "84.2m")
        self.assertEqual(cli._fmt_rate(812.4 * 1024 * 1024), "812.40 MB/s")
        self.assertEqual(cli._fmt_rate(1.5 * 1024 ** 3), "1.50 GB/s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
