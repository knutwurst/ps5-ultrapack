"""One game per download folder takes the rest of the folder along (ultra_core.plan_releases):
the add-ons are copied next to its output instead of becoming jobs of their own.

    PYTHONPATH=. .venv/bin/python -m unittest backend.tests.test_release_plan
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="release_plan_"))
os.environ.setdefault("PS5_FFPFSC_APP_DIR", str(_SCRATCH / "app_dir"))   # before ultra_core loads
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ultra_core  # noqa: E402


def tearDownModule():
    shutil.rmtree(_SCRATCH, ignore_errors=True)


def _zip(path: Path, members: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


GAME = {"Game/eboot.bin": b"e", "Game/sce_sys/param.json": b"{}"}


def _role(p):
    return {"addon": "addon", "game": "game"}.get(Path(p).stem.split("-")[0], "unknown")


def plan(root):
    jobs, addons = ultra_core.plan_releases(root, ultra_core.find_job_sources(root), pkg_role=_role)
    return {Path(s).relative_to(root).as_posix(): b for s, b in jobs}, sorted(
        Path(a).relative_to(root).as_posix() for a in addons)


class OneGamePerFolder(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_SCRATCH))

    def test_the_game_takes_the_addon_folder_along(self):
        rel = self.root / "Some Game [PPSA00001]"
        _zip(rel / "Some Game.zip", GAME)
        _zip(rel / "Addon" / "dlc.zip", {"dlc/content.pkg": b"p"})
        (rel / "Addon" / "addon-pack.pkg").write_bytes(b"p")
        (rel / "Addon" / "read me.txt").write_text("x")
        (rel / "release.nfo").write_text("x")
        jobs, addons = plan(self.root)
        self.assertEqual(list(jobs), ["Some Game [PPSA00001]/Some Game.zip"])
        b = jobs["Some Game [PPSA00001]/Some Game.zip"]
        self.assertEqual(b["release"], str(rel))
        self.assertEqual([Path(e).name for e in b["extras"]], ["Addon"])      # the .nfo is a note
        self.assertEqual(addons, ["Some Game [PPSA00001]/Addon/addon-pack.pkg",
                                  "Some Game [PPSA00001]/Addon/dlc.zip"])

    def test_the_folder_picked_itself_can_be_the_release(self):
        rel = self.root / "Only Game"
        _zip(rel / "g.zip", GAME)
        _zip(rel / "Extras" / "bonus.zip", {"art/cover.png": b"x"})
        jobs, addons = plan(rel)
        self.assertEqual(list(jobs), ["g.zip"])
        self.assertEqual(addons, ["Extras/bonus.zip"])

    def test_two_games_each_get_a_job_and_the_addons_stay(self):
        rel = self.root / "Two"
        _zip(rel / "a.zip", GAME)
        _zip(rel / "b.zip", GAME)
        _zip(rel / "Addon" / "dlc.zip", {"x.pkg": b"p"})
        jobs, addons = plan(self.root)
        self.assertEqual(sorted(jobs), ["Two/a.zip", "Two/b.zip"])
        self.assertTrue(all(b is None for b in jobs.values()))
        self.assertEqual(addons, ["Two/Addon/dlc.zip"])

    def test_a_package_that_may_be_a_game_keeps_its_job(self):
        rel = self.root / "Maybe"
        _zip(rel / "g.zip", GAME)
        (rel / "Addon").mkdir()
        (rel / "Addon" / "game-full.pkg").write_bytes(b"p")    # pkg_role says game
        jobs, addons = plan(self.root)
        self.assertEqual(sorted(jobs), ["Maybe/Addon/game-full.pkg", "Maybe/g.zip"])
        self.assertTrue(all(b is None for b in jobs.values()))
        self.assertEqual(addons, [])

    def test_an_unreadable_archive_is_not_taken_for_an_addon(self):
        rel = self.root / "Locked"
        _zip(rel / "g.zip", GAME)
        (rel / "Addon").mkdir()
        (rel / "Addon" / "x.zip").write_bytes(b"not a zip")
        jobs, addons = plan(self.root)
        self.assertEqual(sorted(jobs), ["Locked/Addon/x.zip", "Locked/g.zip"])

    def test_many_release_folders_side_by_side(self):
        for n in ("A", "B"):
            _zip(self.root / n / f"{n}.zip", GAME)
            _zip(self.root / n / "Addon" / "d.zip", {"d.pkg": b"p"})
        _zip(self.root / "C" / "C.zip", GAME)                  # nothing else: a plain job
        jobs, addons = plan(self.root)
        self.assertEqual(sorted(jobs), ["A/A.zip", "B/B.zip", "C/C.zip"])
        self.assertIsNotNone(jobs["A/A.zip"])
        self.assertIsNone(jobs["C/C.zip"])
        self.assertEqual(addons, ["A/Addon/d.zip", "B/Addon/d.zip"])

    def test_a_package_archive_beside_the_game_may_be_a_game(self):
        rel = self.root / "Pkg Beside"
        _zip(rel / "g.zip", GAME)
        _zip(rel / "full.zip", {"Full Game.pkg": b"p"})
        _zip(rel / "Addon" / "dlc.zip", {"dlc.pkg": b"p"})
        jobs, addons = plan(self.root)
        self.assertEqual(sorted(jobs), ["Pkg Beside/full.zip", "Pkg Beside/g.zip"])
        self.assertEqual(addons, ["Pkg Beside/Addon/dlc.zip"])

    def test_a_package_release_takes_its_addon_folder_along(self):
        rel = self.root / "Pkg Release"
        _zip(rel / "game.zip", {"Game.pkg": b"p"})
        _zip(rel / "Addon" / "dlc.zip", {"dlc.pkg": b"p"})
        jobs, addons = plan(self.root)
        self.assertEqual(list(jobs), ["Pkg Release/game.zip"])
        self.assertEqual([Path(e).name for e in jobs["Pkg Release/game.zip"]["extras"]], ["Addon"])
        self.assertEqual(addons, ["Pkg Release/Addon/dlc.zip"])

    def test_no_bundle_below_a_release_with_several_games(self):
        rel = self.root / "Multi"
        _zip(rel / "a.zip", GAME)
        _zip(rel / "b.zip", GAME)
        _zip(rel / "Addon" / "dlc.zip", {"x.pkg": b"p"})
        (rel / "Addon" / "art").mkdir()
        (rel / "Addon" / "art" / "cover.bin").write_bytes(b"x")
        jobs, addons = plan(self.root)
        self.assertEqual(sorted(jobs), ["Multi/a.zip", "Multi/b.zip"])
        self.assertTrue(all(b is None for b in jobs.values()))
        self.assertEqual(addons, ["Multi/Addon/dlc.zip"])

    def test_a_loose_game_beside_other_release_folders_takes_nothing(self):
        _zip(self.root / "loose.zip", GAME)
        _zip(self.root / "R" / "r.zip", GAME)
        jobs, addons = plan(self.root)
        self.assertEqual(sorted(jobs), ["R/r.zip", "loose.zip"])
        self.assertTrue(all(b is None for b in jobs.values()))


def _ps5_pkg(ct: int, cnt: int = 0x1000) -> bytes:
    b = bytearray(cnt + 0x100)
    b[:4] = b"\x7fFIH"
    b[0x58:0x60] = cnt.to_bytes(8, "little")
    b[cnt:cnt + 4] = b"\x7fCNT"
    b[cnt + 0x74:cnt + 0x78] = ct.to_bytes(4, "big")
    return bytes(b)


def _ps4_pkg(ct: int) -> bytes:
    b = bytearray(0x400)
    b[:4] = b"\x7fCNT"
    b[0x74:0x78] = ct.to_bytes(4, "big")
    return bytes(b)


class PackageHeaders(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(dir=_SCRATCH))

    def test_loose_packages(self):
        for data, want in ((_ps5_pkg(32), "game"), (_ps5_pkg(33), "addon"), (_ps5_pkg(34), "addon"),
                           (_ps5_pkg(7), ""), (_ps4_pkg(0x1A), "game"), (_ps4_pkg(0x1B), "addon"),
                           (_ps4_pkg(0x1E), "addon"), (b"nope", "")):
            f = self.d / "x.pkg"
            f.write_bytes(data)
            self.assertEqual(ultra_core.package_role(f), want, data[:4])

    def test_packages_inside_a_zip(self):
        dlc = _zip(self.d / "dlc.zip", {"a/one.pkg": _ps5_pkg(33), "a/two.pkg": _ps5_pkg(34)})
        mix = _zip(self.d / "mix.zip", {"g.pkg": _ps5_pkg(32), "d.pkg": _ps5_pkg(33)})
        far = _zip(self.d / "far.zip", {"big.pkg": _ps5_pkg(33, cnt=ultra_core.PKG_HEADER_BUDGET + 0x1000)[:0x400]})
        self.assertEqual(ultra_core.ArchiveExtractor.package_role(dlc), "addon")
        self.assertEqual(ultra_core.ArchiveExtractor.package_role(mix), "game")
        self.assertEqual(ultra_core.ArchiveExtractor.package_role(far), "")

    def test_a_dlc_archive_beside_the_game_goes_along(self):
        rel = self.d / "R"
        _zip(rel / "g.zip", GAME)
        _zip(rel / "dlc.zip", {"d.pkg": _ps5_pkg(33)})        # read from its header: an add-on
        _zip(rel / "full.zip", {"f.pkg": _ps5_pkg(32)})       # a game
        jobs, addons = plan(self.d)
        self.assertEqual(sorted(jobs), ["R/full.zip", "R/g.zip"])
        self.assertEqual(addons, ["R/dlc.zip"])


if __name__ == "__main__":
    unittest.main()
