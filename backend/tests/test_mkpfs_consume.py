"""MkPFS --consume-source: pass 1 frees each larger file of the caller's own copy once the
image holds it. What stays: sce_sys, small files, links. The image is the same either way."""
import os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PY = sys.executable


def pack(src: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-m", "mkpfs", "pack", "folder", "--raw", "--no-ampr-index", "--no-compress",
                           "--no-adjust-output-file-extension", "--version", "PS5", "--inode-bits", "32",
                           "--block-size", "auto", *extra, str(src), str(out)],
                          cwd=BACKEND, capture_output=True, text=True)


class Consume(unittest.TestCase):
    def test_the_space_check_counts_what_stays(self):
        sys.path.insert(0, str(BACKEND))
        from mkpfs import cli as mcli
        if self.links:
            (self.d / "game" / "data" / "link.bin").unlink()
        need = mcli.consuming_space_need(self.d / "game", block_size=65536)
        # kept files in whole blocks (sce_sys and the small eboot), the largest freed file
        kept = (3 << 20) + 65536 + 65536
        largest = max(2 << 20, len(b"pattern" * (500 << 10)))
        self.assertGreaterEqual(need, kept + largest)
        self.assertLess(need, kept + largest + (4 << 20))

    def test_the_space_check_covers_the_blocks_of_many_tiny_files(self):
        sys.path.insert(0, str(BACKEND))
        from mkpfs import cli as mcli
        g = self.d / "tiny"
        (g / "sce_sys").mkdir(parents=True)
        (g / "sce_sys" / "param.json").write_text('{"titleId": "PPSA00001"}')
        for i in range(40):
            d = g / f"d{i:02d}"; d.mkdir()
            for j in range(50):
                (d / f"f{j:02d}.txt").write_bytes(b"x")
        (g / "big.bin").write_bytes(os.urandom(2 << 20))
        r = pack(g, self.d / "tiny.ffpfs", "--no-verify-structure")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        image = (self.d / "tiny.ffpfs").stat().st_size
        need = mcli.consuming_space_need(g, block_size=65536)
        # at the peak the image holds everything but the freed file is gone
        self.assertGreaterEqual(need, image - (2 << 20) + (2 << 20))
        self.assertLess(need, image + (8 << 20))

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="consume_"))
        g = self.d / "game"
        (g / "sce_sys").mkdir(parents=True)
        (g / "sce_sys" / "param.json").write_text('{"titleId": "PPSA00001"}')
        (g / "sce_sys" / "big.dat").write_bytes(os.urandom(3 << 20))   # big, but metadata
        (g / "eboot.bin").write_bytes(b"\x7fELF" + b"\0" * 1000)        # small
        (g / "data").mkdir()
        (g / "data" / "a.bin").write_bytes(os.urandom(2 << 20))
        (g / "data" / "b.bin").write_bytes(b"pattern" * (500 << 10))
        outside = self.d / "outside.bin"
        outside.write_bytes(os.urandom(2 << 20))
        try:
            os.symlink(outside, g / "data" / "link.bin")     # Windows: needs a right most users lack
            self.links = True
        except OSError:
            self.links = False
        self.ref = self.d / "ref"
        shutil.copytree(g, self.ref, symlinks=True)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def files(self, root: Path) -> list:
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink())

    def test_frees_the_larger_files_and_keeps_the_rest(self):
        r = pack(self.d / "game", self.d / "img.ffpfs", "--consume-source", "--no-verify-structure")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        if self.links:
            # a link may point at a file freed earlier: with one in the tree nothing goes
            self.assertNotIn("[consume] freed", r.stdout)
            self.assertEqual(self.files(self.d / "game"), self.files(self.ref))
            self.assertTrue((self.d / "outside.bin").exists())
            (self.d / "game" / "data" / "link.bin").unlink(); (self.ref / "data" / "link.bin").unlink()
            r = pack(self.d / "game", self.d / "img2.ffpfs", "--consume-source", "--no-verify-structure")
            self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
            (self.d / "img2.ffpfs").rename(self.d / "img.ffpfs")
        self.assertIn("[consume] freed", r.stdout)
        self.assertEqual(self.files(self.d / "game"), ["eboot.bin", "sce_sys/big.dat", "sce_sys/param.json"])
        # the image holds every file byte for byte (a full check against the untouched copy)
        v = subprocess.run([PY, "-m", "mkpfs", "verify", "--source-dir", str(self.ref), str(self.d / "img.ffpfs")],
                           cwd=BACKEND, capture_output=True, text=True)
        self.assertIn("Errors:                0", v.stdout, v.stdout[-2000:])

    def test_a_check_against_the_source_keeps_it(self):
        r = pack(self.d / "game", self.d / "img.ffpfs", "--consume-source", "--verify")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertNotIn("[consume] freed", r.stdout)
        self.assertEqual(self.files(self.d / "game"), self.files(self.ref))

    def test_a_signed_image_frees_nothing(self):
        if self.links:                       # a link alone already stops the freeing
            (self.d / "game" / "data" / "link.bin").unlink(); (self.ref / "data" / "link.bin").unlink()
        r = pack(self.d / "game", self.d / "img.ffpfs", "--consume-source", "--no-verify-structure", "--signed")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertNotIn("[consume] freed", r.stdout)
        self.assertEqual(self.files(self.d / "game"), self.files(self.ref))

    def test_without_the_flag_nothing_goes(self):
        r = pack(self.d / "game", self.d / "img.ffpfs", "--no-verify-structure")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertEqual(self.files(self.d / "game"), self.files(self.ref))


if __name__ == "__main__":
    unittest.main()
