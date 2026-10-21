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
        rel = self.root / "Only Game [PPSA00002]"
        _zip(rel / "PPSA00002-game.zip", GAME)
        _zip(rel / "Extras" / "bonus.zip", {"art/cover.png": b"x"})
        jobs, addons = plan(rel)
        self.assertEqual(list(jobs), ["PPSA00002-game.zip"])
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
        for n in ("Alpha", "Bravo"):
            _zip(self.root / n / f"{n}.zip", GAME)
            _zip(self.root / n / "Addon" / "d.zip", {"d.pkg": b"p"})
        _zip(self.root / "Charlie" / "Charlie.zip", GAME)      # nothing else: a plain job
        jobs, addons = plan(self.root)
        self.assertEqual(sorted(jobs), ["Alpha/Alpha.zip", "Bravo/Bravo.zip", "Charlie/Charlie.zip"])
        self.assertIsNotNone(jobs["Alpha/Alpha.zip"])
        self.assertIsNone(jobs["Charlie/Charlie.zip"])
        self.assertEqual(addons, ["Alpha/Addon/d.zip", "Bravo/Addon/d.zip"])

    def test_the_title_id_inside_the_archive_names_the_folder(self):
        rel = self.root / "Some Title [PPSA00012] [v01.000]"
        _zip(rel / "release-part.zip", {"PPSA00012-app0/eboot.bin": b"e", "PPSA00012-app0/sce_sys/param.json": b"{}"})
        _zip(rel / "Addon" / "dlc.zip", {"d.pkg": b"p"})
        jobs, addons = plan(self.root)
        self.assertEqual(list(jobs), ["Some Title [PPSA00012] [v01.000]/release-part.zip"])
        self.assertIsNotNone(jobs["Some Title [PPSA00012] [v01.000]/release-part.zip"])
        self.assertEqual(addons, ["Some Title [PPSA00012] [v01.000]/Addon/dlc.zip"])

    def test_a_folder_not_named_after_the_game_takes_nothing(self):
        # a drive or a collection: one loose game beside a series folder is no download folder
        drive = self.root / "Drive"
        _zip(drive / "PPSA00009-game.zip", GAME)
        (drive / "Some Series S01").mkdir(parents=True)
        (drive / "Some Series S01" / "e01.mkv").write_bytes(b"v")
        jobs, addons = plan(drive)
        self.assertEqual(jobs, {"PPSA00009-game.zip": None})
        self.assertEqual(addons, [])

    def test_a_package_archive_beside_the_game_may_be_a_game(self):
        rel = self.root / "Pkg Beside"
        _zip(rel / "g.zip", GAME)
        _zip(rel / "full.zip", {"Full Game.pkg": b"p"})
        _zip(rel / "Addon" / "dlc.zip", {"dlc.pkg": b"p"})
        jobs, addons = plan(self.root)
        self.assertEqual(sorted(jobs), ["Pkg Beside/full.zip", "Pkg Beside/g.zip"])
        self.assertEqual(addons, ["Pkg Beside/Addon/dlc.zip"])

    def test_a_package_release_takes_its_addon_folder_along(self):
        rel = self.root / "Pkg Release [PPSA00003]"
        _zip(rel / "PPSA00003.zip", {"Game.pkg": b"p"})
        _zip(rel / "Addon" / "dlc.zip", {"dlc.pkg": b"p"})
        jobs, addons = plan(self.root)
        self.assertEqual(list(jobs), ["Pkg Release [PPSA00003]/PPSA00003.zip"])
        self.assertEqual([Path(e).name for e in jobs["Pkg Release [PPSA00003]/PPSA00003.zip"]["extras"]], ["Addon"])
        self.assertEqual(addons, ["Pkg Release [PPSA00003]/Addon/dlc.zip"])

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
                           (_ps5_pkg(7), ""), (_ps4_pkg(0x1A), ""), (_ps4_pkg(0x1B), "addon"),
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


class AddonOnlyTitleFolder(unittest.TestCase):
    """DLC organized without their game: the title folder takes the game's name."""
    def _dlc(self, title, version="01.000.000"):
        return ultra_core.LibItem(Path(f"/x/{title}.pkg"), "pkg", "dlc", title, "PPSA10621", version, "", None)

    def test_named_after_the_game_from_the_title_list(self):
        got = ultra_core.library_layout([self._dlc("Some Game - Costume Pack")], title_of=lambda t: "Some Game")
        self.assertEqual(got[0][1], "Some Game [PPSA10621] [v01.000.000]")
        self.assertEqual(got[0][3], "Some Game DLC Costume Pack [PPSA10621] [v01.000].pkg")

    def test_an_existing_title_folder_keeps_its_name(self):
        lib = ultra_core.Ps4Library("Some Game [PPSA10621] [v01.012.000]", "01.012.000", False)
        got = ultra_core.library_layout([self._dlc("Some Game - Costume Pack")], {"PPSA10621": lib},
                                        title_of=lambda t: "Elsewhere")
        self.assertEqual(got[0][1], "Some Game [PPSA10621] [v01.012.000]")

    def test_without_the_list_the_package_names_it(self):
        got = ultra_core.library_layout([self._dlc("Some Game - Costume Pack")], title_of=lambda t: None)
        self.assertEqual(got[0][1], "Some Game - Costume Pack [PPSA10621] [v01.000.000]")

    def test_the_game_itself_decides_when_it_is_there(self):
        game = ultra_core.LibItem(Path("/x/g.pkg"), "pkg", "game", "Some Game", "PPSA10621", "01.012.000", "", None)
        got = ultra_core.library_layout([self._dlc("Some Game - Costume Pack"), game], title_of=lambda t: "Other")
        self.assertEqual({g[1] for g in got}, {"Some Game [PPSA10621] [v01.012.000]"})


if __name__ == "__main__":
    unittest.main()
