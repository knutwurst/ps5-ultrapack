"""FpkgProgress: the package tool's log -> the GUI's phase markers and metered bars."""
import importlib.util, re, sys, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cli", HERE.parent / "cli.py")
cli = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(HERE.parent)); spec.loader.exec_module(cli)


class Replay:
    def __init__(self):
        self.out, self.now = [], 0.0
        self.fp = cli.FpkgProgress(lambda n: self.out.append(f"[PHASE] {n}"),
                                   out=self.out.append, clock=lambda: self.now)

    def feed(self, *lines, dt=0.0):
        for l in lines:
            self.now += dt
            self.fp.line(l)

    def bars(self):
        return [l for l in self.out if re.match(r"\[[#-]{20}\]", l)]

    def phases(self):
        return [l[8:] for l in self.out if l.startswith("[PHASE] ")]


class KrakenMeter(unittest.TestCase):
    def setUp(self):
        self.r = Replay()
        self.r.feed("[stage] staged in place: /x (the caller's working copy)",
                    "[+00:00:00.021] Source scan: deferred to inner-tree preparation (single-pass mode).",
                    "[+00:00:00.050] [stage 1/5] Building and compressing the inner pfs_image.dat...",
                    "[+00:00:00.052]  [inner] Preparing 3 inner files (1,000 bytes)...",
                    "[+00:00:00.112]  [inner] Prepared inner tree: 3 files, 1 directories.",
                    "[+00:00:00.118]  [inner] Planning nwonly inner image: 3 files, 1,000,000,000 uncompressed bytes.",
                    "[+00:00:00.129]  [inner] Compressing and writing AFID-ordered inner data with 8 built-in Kraken worker(s)...")

    def test_late_source_scan_is_not_a_bar(self):
        """The tool scans after staging; that must not credit 100 % to the running phase."""
        self.r.feed("[+00:00:00.022] Source scan: 3 files.")
        self.assertNotIn("source scan", " ".join(self.r.bars()))
        self.assertEqual(self.r.phases()[-1], "Creating Temp PFS")

    def test_bytes_speed_and_eta(self):
        self.r.feed("[+00:00:00.188]  [inner]   processing large file: /big.bin (600,000,000 bytes)")
        self.r.feed("[+00:00:10.000]  [inner]     Kraken level -4:  50% of /big.bin", dt=10.0)
        bar = self.r.bars()[-1]
        self.assertTrue(bar.startswith("[######--------------] 30% inner image (Kraken) @ 30.0 MB/s ETA 23s"), bar)
        self.r.feed("[+00:00:20.000]  [inner]   data  60% (1/3): /big.bin -> 400,000,000 bytes (Kraken, ratio 66.7 %)", dt=10.0)
        bar = self.r.bars()[-1]
        self.assertIn("60% inner image (Kraken) @ 30.0 MB/s ETA 13s", bar)
        # a small file without its own size line is sized from output and ratio
        self.r.feed("[+00:00:30.000]  [inner]   data  70% (2/3): /small.bin -> 100,000,000 bytes (Kraken, ratio 50.0 %)", dt=10.0)
        self.assertIn("80% inner image (Kraken)", self.r.bars()[-1])
        self.assertEqual(self.r.fp.done, 800_000_000)

    def test_library_percent_is_the_floor(self):
        self.r.feed("[+00:00:01.000]  [inner]   data  40% (1/3): /odd.bin -> 1 bytes (raw)", dt=1.0)
        self.assertIn("40% inner image (Kraken)", self.r.bars()[-1])

    def test_inner_complete_and_outer_pass(self):
        self.r.feed("[+00:10:00.000] [stage 1/5] Inner image complete: 900,000,000 bytes in 00:10:00.")
        self.assertIn("100% inner image (Kraken)", self.r.bars()[-1])
        self.r.feed("[+00:10:01.000] [stage 2/5] Generating NAPS file, block and integrity tables...",
                    "[+00:10:02.000] [stage 3/5] Building plaintext/no-auth outer PFS...",
                    "[+00:10:03.000] [stage 3/5] Writing and hashing outer-PFS data (8 worker(s)): started (94.25 GiB total).",
                    "[+00:20:00.000] [stage 3/5] Writing and hashing outer-PFS data (8 worker(s)): 59% (55.77 GiB / 94.25 GiB; 71.4 MiB/s).")
        self.assertEqual(self.r.phases()[-1], "Compressing")
        bar = self.r.bars()[-1]
        m = re.search(r"59% outer PFS @ ([\d.]+) MB/s ETA (\d+)s", bar)
        self.assertTrue(m, bar)
        self.assertAlmostEqual(float(m.group(1)), 74.9, delta=0.2)          # 71.4 MiB/s in MB/s
        self.assertAlmostEqual(int(m.group(2)), 38.48 * 2**30 / (71.4 * 2**20), delta=2)
        self.r.feed("[+00:30:00.000] [stage 3/5] Outer PFS complete: 1 bytes, 2 digest blocks in 00:20:00.",
                    "[+00:30:01.000] [stage 4/5] Writing CNT bodies and outer image (3 entries)...",
                    "[+00:31:00.000] [stage 4/5] CNT image complete: 10 bytes in 00:00:59.",
                    "[+00:31:01.000] [stage 5/5] Finalization inputs ready: 3 content records.",
                    "[+00:31:02.000] [finalize] NAPS plaintext integrity tables (SHA3/ihsh/rhsh): 60% (2,740 / 4,567 blocks).",
                    "[+00:32:00.000] Build finished in 00:32:00; output 1 bytes (0.0 GiB), warnings=0.")
        self.assertEqual(self.r.phases()[-1], "Writing Final Image")
        labels = [b.split("% ", 1)[1] for b in self.r.bars()[-5:]]
        self.assertEqual(labels, ["CNT image", "CNT image", "finalize", "finalize (FIH digests)", "finalized .pkg written"])
        self.assertIn("80% finalize (FIH digests)", self.r.bars()[-2])


if __name__ == "__main__":
    unittest.main()
