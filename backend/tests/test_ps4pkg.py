import sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
import ps4pkg
from ps4_fixture import make_pkg

CID = "UP0000-CUSA00001_00-SAMPLEGAME000000"
REAL = sorted((HERE.parent.parent / "unzipped" / "ps4").glob("*.pkg"))   # git-ignored samples, optional


class Identity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _pkg(self, name, content_type, category, app_ver="01.00", title="Sample Game", cid=CID):
        sfo = {"TITLE": title, "TITLE_ID": cid[7:16], "CONTENT_ID": cid, "CATEGORY": category, "VERSION": "01.00"}
        if app_ver is not None:
            sfo["APP_VER"] = app_ver
        return make_pkg(self.tmp / name, content_id=cid, content_type=content_type, sfo=sfo)

    def test_game(self):
        i = ps4pkg.read_identity(self._pkg("g.pkg", 0x1A, "gd"))
        self.assertEqual((i.kind, i.title, i.title_id, i.version), ("game", "Sample Game", "CUSA00001", "01.00"))

    def test_update(self):
        i = ps4pkg.read_identity(self._pkg("u.pkg", 0x1A, "gp", app_ver="01.07"))
        self.assertEqual((i.kind, i.version), ("update", "01.07"))

    def test_dlc_without_app_ver_uses_version(self):
        for ct in (0x1B, 0x1C):
            i = ps4pkg.read_identity(self._pkg(f"d{ct}.pkg", ct, "ac", app_ver=None, title="Sample Game - Extra Pack"))
            self.assertEqual((i.kind, i.version, i.app_ver), ("dlc", "01.00", ""))

    def test_is_ps4_package(self):
        self.assertTrue(ps4pkg.is_ps4_package(self._pkg("g.pkg", 0x1A, "gd")))
        ps5 = self._pkg("p.pkg", 0x1A, "gd", cid="UP0000-PPSA00001_00-SAMPLEGAME000000")
        self.assertFalse(ps4pkg.is_ps4_package(ps5))
        junk = self.tmp / "junk.pkg"; junk.write_bytes(b"\x00" * 4096)
        self.assertFalse(ps4pkg.is_ps4_package(junk))
        self.assertFalse(ps4pkg.is_ps4_package(self.tmp / "missing.pkg"))

    def test_unreadable_raises(self):
        junk = self.tmp / "junk.pkg"; junk.write_bytes(b"\x7fCNT" + b"\x00" * 60)
        with self.assertRaises(ps4pkg.Ps4PackageError):
            ps4pkg.read_identity(junk)

    @unittest.skipUnless(REAL, "no real PS4 packages in unzipped/ps4/")
    def test_real_packages(self):
        for p in REAL:
            self.assertTrue(ps4pkg.is_ps4_package(p), p.name)
            i = ps4pkg.read_identity(p)
            self.assertIn(i.kind, ("game", "update", "dlc"), p.name)
            self.assertTrue(i.title and i.title_id.startswith("CUSA") and i.version, i)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(REAL, "no real PS4 packages in unzipped/ps4/")
class RealThroughBackend(unittest.TestCase):
    """Look inside and Unpack through cli.py, as the GUI calls them, on the real samples."""
    def run_cli(self, *args):
        import subprocess
        p = subprocess.run([sys.executable, str(HERE.parent / "cli.py"), *map(str, args)], capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr

    def test_list_extract_unpack(self):
        import json
        work = Path(tempfile.mkdtemp())
        for pkg in REAL:
            rc, out = self.run_cli("--list-image", pkg)
            doc = json.loads(next(l for l in out.splitlines() if l.startswith("PFSBROWSE_JSON:")).split(":", 1)[1])
            paths = {e["path"] for e in doc["entries"]}
            self.assertIn("sce_sys/param.sfo", paths, pkg.name)
            members = work / "m.txt"; members.write_text("sce_sys/param.sfo\n")
            dest = work / ("m_" + pkg.stem)
            rc, out = self.run_cli("--extract-from", pkg, "--dest", dest, "--members-file", members)
            self.assertEqual(rc, 0, out)
            self.assertEqual(ps4pkg.parse_sfo((dest / "sce_sys" / "param.sfo").read_bytes())["TITLE_ID"],
                             ps4pkg.read_identity(pkg).title_id)
            if pkg.stat().st_size > 8 << 20:
                continue        # never unpack a whole game in a test; the small DLCs show the full path
            full = work / ("full_" + pkg.stem)
            rc, out = self.run_cli("placeholder", full, "--fpkg-extract", pkg)
            self.assertEqual(rc, 0, out)
            files = {p.relative_to(full).as_posix() for p in full.rglob("*") if p.is_file()}
            self.assertEqual(files, {e["path"] for e in doc["entries"] if e["type"] == "file"})

    def test_game_executable_is_decrypted(self):
        """eboot.bin of a real game or update reads back as a PS4 SELF (members only)."""
        import json
        work = Path(tempfile.mkdtemp())
        games = [p for p in REAL if ps4pkg.read_identity(p).kind in ("game", "update")]
        if not games:
            self.skipTest("no real PS4 game or update in unzipped/ps4/")
        for pkg in games:
            members = work / "m.txt"; members.write_text("eboot.bin\n")
            dest = work / ("e_" + pkg.stem)
            rc, out = self.run_cli("--extract-from", pkg, "--dest", dest, "--members-file", members)
            self.assertEqual(rc, 0, out)
            self.assertEqual((dest / "eboot.bin").read_bytes()[:4], bytes.fromhex("4f153d1d"), pkg.name)
