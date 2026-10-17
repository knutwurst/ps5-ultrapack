"""The fPKG tool never deletes or overwrites what is not its own: an in-place build frees only
real files outside sce_sys, a cancelled build leaves foreign packages alone, extracts fail on
missing metadata and a selective extract keeps existing files. Builds the homebrew sample
(set PS5_HBT_SAMPLE to its folder; skipped without it or without the built tool).

    python3 -m unittest backend.tests.test_pkg_tool_safety
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from backend import fpkg

HBT = Path(os.environ.get("PS5_HBT_SAMPLE",
                          "/var/folders/n8/wstw_pn97n57cgxvbssq18j00000gp/T/ffpfsc-fpkg-gui-tests/hbt"))
CONTENT_ID = "UP9000-PPSA99099_00-PROSPERO00000000"
PKG_NAME = CONTENT_ID + "-A0100-V0100.pkg"


def _tool_ready() -> bool:
    try:
        return fpkg.tool_path().is_file() and (HBT / "sce_sys" / "param.json").is_file()
    except FileNotFoundError:
        return False


def build_argv(src: Path, out: Path, temp: Path, *extra: str) -> list[str]:
    return [str(fpkg.tool_path()), "build", str(src), str(out), "--content-id", CONTENT_ID,
            "--title-id", "PPSA99099", "--version", "01.000.000", "--passcode", "0" * 32,
            "--mode", "kraken", "--kraken-backend", "builtin", "--temp", str(temp), *extra]


def run(argv: list[str]) -> tuple[int, str]:
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    return r.returncode, r.stdout + r.stderr


@unittest.skipUnless(_tool_ready(), "needs the built ffpfsc-pkg-tool and the homebrew sample")
class PkgToolSafety(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("TMPDIR") or None
        self.d = Path(tempfile.mkdtemp(prefix="pkgtool_", dir=base)).resolve()
        self.src = self.d / "src"
        shutil.copytree(HBT, self.src)
        self.out = self.d / "out"; self.out.mkdir()
        self.temp = self.d / "temp"; self.temp.mkdir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def build(self, *extra: str) -> tuple[int, str]:
        return run(build_argv(self.src, self.out, self.temp, *extra))

    def test_in_place_consume_frees_only_real_files_outside_sce_sys(self):
        # The library prints a "data NN% (i/n): /path -> ..." line per percent step, so files
        # of equal size past #999 get lines with "1,0xx/1,100"; the larger ones always get one.
        (self.src / "sce_sys" / "extra.bin").write_bytes(os.urandom(2 << 20))
        data = self.src / "data"; data.mkdir()
        for i in range(1100):
            (data / f"f{i:04d}.bin").write_bytes(os.urandom(8 << 10))
        (self.src / "with space.bin").write_bytes(os.urandom(1 << 20))
        outside = self.d / "outside"; outside.mkdir()
        (outside / "keep.bin").write_bytes(os.urandom(1 << 20))
        os.symlink(outside, self.src / "linked")
        rc, log = self.build("--stage-in-place", "--consume-source")
        self.assertEqual(rc, 0, log[-3000:])
        named = re.findall(r"\[inner\]\s+data\s+\d+%\s+\(([\d,]+)/[\d,]+\):\s+(/.+) -> ", log)
        self.assertTrue(any("," in i for i, _p in named), "no line past #999 in the log")
        self.assertTrue(any(p == "/with space.bin" for _i, p in named))
        self.assertTrue(any(p == "/sce_sys/extra.bin" for _i, p in named))
        self.assertTrue((outside / "keep.bin").exists(), "a file was deleted through a link")
        self.assertTrue((self.src / "sce_sys" / "param.json").exists())
        self.assertTrue((self.src / "sce_sys" / "extra.bin").exists(), "a file under sce_sys was freed")
        left = [p for _i, p in named
                if not p.startswith(("/sce_sys/", "/linked/")) and (self.src / p.lstrip("/")).exists()]
        self.assertEqual(left, [], "files the image already held were not freed")

    def test_a_cancelled_build_keeps_foreign_packages_and_removes_its_temps(self):
        stale = self.out / f".{PKG_NAME}.0123456789abcdef.build.tmp"
        stale.write_bytes(b"old")
        (self.src / "big.bin").write_bytes(os.urandom(400 << 20))
        proc = subprocess.Popen(build_argv(self.src, self.out, self.temp), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        foreign = self.out / "EP0000-CUSA00000_00-OTHERGAME0000000-A0100-V0100.pkg"
        log = []
        for line in proc.stdout:
            log.append(line)
            if "[inner]" in line and "data" in line:
                foreign.write_bytes(b"not ours")
                proc.send_signal(signal.SIGTERM)
                break
        log += proc.stdout.readlines()
        proc.stdout.close()
        rc = proc.wait(timeout=60)
        self.assertNotEqual(rc, 0, "".join(log)[-2000:])
        self.assertTrue(foreign.exists(), "a package the build did not write was deleted")
        self.assertFalse(stale.exists(), "a stale temp file of this package stayed")
        self.assertFalse((self.out / PKG_NAME).exists())
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.endswith(".tmp")], [])

    def test_extract_fails_when_the_metadata_cannot_be_added(self):
        rc, log = self.build()
        self.assertEqual(rc, 0, log[-2000:])
        dest = self.d / "x"
        (dest / "sce_sys" / "param.json").mkdir(parents=True)   # the merge cannot write it
        rc, log = run([str(fpkg.tool_path()), "extract-inner", str(self.out / PKG_NAME), str(dest)])
        self.assertNotEqual(rc, 0, log[-2000:])
        self.assertIn("[error]", log)

    def test_a_selective_extract_keeps_existing_files(self):
        rc, log = self.build()
        self.assertEqual(rc, 0, log[-2000:])
        dest = self.d / "x"; dest.mkdir()
        members = self.d / "members.txt"
        members.write_text("eboot.bin\nREADME.md\n", encoding="utf-8")
        (dest / "eboot.bin").write_bytes(b"mine")
        args = [str(fpkg.tool_path()), "extract-inner", str(self.out / PKG_NAME), str(dest), "--members", str(members)]
        rc, log = run(args)
        self.assertEqual(rc, 0, log[-2000:])
        self.assertEqual((dest / "eboot.bin").read_bytes(), b"mine")
        self.assertEqual((dest / "eboot (2).bin").read_bytes()[:4], b"\x54\x14\xF5\xEE")
        readme = (dest / "README.md").read_bytes()
        rc, log = run(args)                                     # the same bytes again: nothing new
        self.assertEqual(rc, 0, log[-2000:])
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["README.md", "eboot (2).bin", "eboot.bin"])
        self.assertEqual((dest / "README.md").read_bytes(), readme)

    def test_a_file_that_cannot_be_fake_signed_fails_the_build(self):
        (self.src / "broken.prx").write_bytes(b"\x7fELF" + b"\xff" * 60)
        rc, log = self.build()
        self.assertNotEqual(rc, 0, log[-2000:])
        self.assertIn("broken.prx", log)
        self.assertFalse((self.out / PKG_NAME).exists())


if __name__ == "__main__":
    unittest.main()
