import io, re, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import ps4_sort
from ps4_fixture import make_pkg

CID = "UP0000-CUSA00001_00-SAMPLEGAME000000"


def pkg(d: Path, name, kind, ver, title="Sample Game"):
    ct, cat = {"game": (0x1A, "gd"), "update": (0x1A, "gp"), "dlc": (0x1B, "ac")}[kind]
    d.mkdir(parents=True, exist_ok=True)
    return make_pkg(d / name, content_id=CID, content_type=ct,
                    sfo={"TITLE": title, "TITLE_ID": "CUSA00001", "CATEGORY": cat, "APP_VER": ver})


class Sort(unittest.TestCase):
    def setUp(self):
        self.src = Path(tempfile.mkdtemp()); self.out = Path(tempfile.mkdtemp())
        pkg(self.src, "base.pkg", "game", "01.00")
        pkg(self.src, "patch.pkg", "update", "01.07")
        for i in range(5):
            pkg(self.src / "dlcs", f"dlc{i}.pkg", "dlc", "01.00", title=f"Sample Game - Item {i}")
        (self.src / "._base.pkg").write_bytes(b"junk")
        self.top = self.out / "Sample Game [CUSA00001] [v01.07]"

    def run_sort(self, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ps4_sort.sort_packages(self.src, self.out, **kw)
        return rc, buf.getvalue()

    def test_tree_and_progress(self):
        rc, log = self.run_sort()
        self.assertEqual(rc, 0, log)
        self.assertTrue((self.top / "Sample Game [CUSA00001] [v01.00].pkg").is_file())
        self.assertTrue((self.top / "Sample Game [CUSA00001] UPDATE [v01.07].pkg").is_file())
        dl = sorted(p.name for p in (self.top / "DLC Pack").iterdir())
        self.assertEqual(len(dl), 5)
        self.assertEqual(dl[0], "Sample Game DLC Item 0 [CUSA00001] [v01.07].pkg")
        self.assertFalse(any(p.name.startswith("._") for p in self.out.rglob("*")))
        self.assertTrue((self.src / "base.pkg").is_file())             # keep mode keeps the source
        pcts = [int(m) for m in re.findall(r"^\[#+\] (\d+)% ", log, re.M)]
        self.assertEqual(pcts, sorted(pcts)); self.assertEqual(pcts[-1], 100)   # one rising bar for the set
        self.assertEqual(log.count("[JOB] copy"), 1)
        self.assertIn(f"[OK] PS4 sorted: {self.top}", log)

    def test_if_exists(self):
        self.run_sort()
        game = self.top / "Sample Game [CUSA00001] [v01.00].pkg"
        game.write_bytes(b"old")
        rc, log = self.run_sort(if_exists="skip")
        self.assertEqual(game.read_bytes(), b"old"); self.assertIn("already there", log)
        rc, log = self.run_sort(if_exists="ask")
        self.assertEqual(game.read_bytes(), b"old"); self.assertIn("Ask", log)
        rc, log = self.run_sort(if_exists="keep")
        self.assertTrue(game.with_name(game.stem + " (2).pkg").is_file())
        rc, log = self.run_sort(if_exists="overwrite")
        self.assertNotEqual(game.read_bytes(), b"old")
        self.assertFalse(list(self.out.rglob("*.ps4sort-part")))

    def test_joins_the_library_folder(self):
        lib = self.out / "Sample Game [CUSA00001] [v01.62]"
        (lib / "DLC Pack").mkdir(parents=True)
        lone = Path(tempfile.mkdtemp())
        pkg(lone, "d.pkg", "dlc", "01.00", title="Sample Game - Skin")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ps4_sort.sort_packages(lone, self.out)
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertTrue((lib / "DLC Pack" / "Sample Game DLC Skin [CUSA00001] [v01.62].pkg").is_file())
        self.assertEqual(len([d for d in self.out.iterdir() if d.is_dir()]), 1)

    def test_non_ps4_package_is_reported(self):
        (self.src / "other.pkg").write_bytes(b"\x7fFIH" + b"\0" * 5000)
        rc, log = self.run_sort()
        self.assertEqual(rc, 0)
        self.assertIn("[WARN] not a PS4 package", log)

    def test_nothing_found(self):
        empty = Path(tempfile.mkdtemp())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(ps4_sort.sort_packages(empty, self.out), 1)


if __name__ == "__main__":
    unittest.main()
