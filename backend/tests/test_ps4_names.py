import sys, unittest
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
import ultra_core as uc


def ident(kind, version, title="Sample Game", tid="CUSA00001", app_ver=None):
    return NS(kind=kind, version=version, app_ver=version if app_ver is None else app_ver, title=title, title_id=tid)


class Layout(unittest.TestCase):
    def test_game_update_dlc(self):
        got = uc.ps4_layout([(Path("g.pkg"), ident("game", "01.00")),
                             (Path("u.pkg"), ident("update", "01.07")),
                             (Path("d.pkg"), ident("dlc", "01.00", title="Sample Game - Extra Pack"))])
        self.assertEqual({p.name: (f, s, n) for p, f, s, n in got}, {
            "g.pkg": ("Sample Game [CUSA00001] [v01.07]", "", "Sample Game [CUSA00001] [v01.00].pkg"),
            "u.pkg": ("Sample Game [CUSA00001] [v01.07]", "", "Sample Game [CUSA00001] UPDATE [v01.07].pkg"),
            "d.pkg": ("Sample Game [CUSA00001] [v01.07]", "", "Sample Game DLC Extra Pack [CUSA00001] [v01.07].pkg"),
        })

    def test_dlc_pack_from_four(self):
        dl = [(Path(f"d{i}.pkg"), ident("dlc", "01.00", title=f"Sample Game: Item {i}")) for i in range(4)]
        got = uc.ps4_layout([(Path("g.pkg"), ident("game", "01.00"))] + dl)
        self.assertEqual({s for p, f, s, n in got if p.name.startswith("d")}, {"DLC Pack"})
        self.assertIn("Sample Game DLC Item 0 [CUSA00001] [v01.00].pkg", {n for p, f, s, n in got})
        got3 = uc.ps4_layout([(Path("g.pkg"), ident("game", "01.00"))] + dl[:3])
        self.assertEqual({s for p, f, s, n in got3}, {""})

    def test_lone_dlc_keeps_its_version_or_none(self):
        (_, f, _, n), = uc.ps4_layout([(Path("d.pkg"), ident("dlc", "01.02", title="Sample Game - Skin"))])
        self.assertEqual(n, "Sample Game DLC Skin [CUSA00001] [v01.02].pkg")
        self.assertEqual(f, "Sample Game [CUSA00001] [v01.02]")
        (_, f, _, n), = uc.ps4_layout([(Path("d.pkg"), ident("dlc", "", title="Sample Game - Skin"))])
        self.assertEqual(n, "Sample Game DLC Skin [CUSA00001].pkg")
        self.assertEqual(f, "Sample Game [CUSA00001]")

    def test_dlc_name_not_repeating_the_game(self):
        got = uc.ps4_layout([(Path("g.pkg"), ident("game", "01.00", title="Sample Game™")),
                             (Path("d.pkg"), ident("dlc", "01.00", title="Imperial Pack"))])
        names = {p.name: n for p, f, s, n in got}
        self.assertEqual(names["g.pkg"], "Sample Game [CUSA00001] [v01.00].pkg")
        self.assertEqual(names["d.pkg"], "Sample Game DLC Imperial Pack [CUSA00001] [v01.00].pkg")

    def test_titles_grouped_by_id(self):
        got = uc.ps4_layout([(Path("a.pkg"), ident("game", "01.00", title="Game A", tid="CUSA00001")),
                             (Path("b.pkg"), ident("game", "01.00", title="Game B", tid="CUSA00002"))])
        self.assertEqual({f for p, f, s, n in got}, {"Game A [CUSA00001] [v01.00]", "Game B [CUSA00002] [v01.00]"})

    def test_other_kind_keeps_its_title(self):
        (_, f, s, n), = uc.ps4_layout([(Path("t.pkg"), ident("other", "01.00", title="Sample Theme"))])
        self.assertEqual((s, n), ("", "Sample Theme.pkg"))

    def test_joins_an_existing_title_folder(self):
        known = {"CUSA00001": uc.Ps4Library("Sample Game [CUSA00001] [v01.62]", "01.62", has_dlc_pack=True)}
        got = uc.ps4_layout([(Path("d.pkg"), ident("dlc", "01.00", title="Sample Game - Skin"))], known=known)
        self.assertEqual(got, [(Path("d.pkg"), "Sample Game [CUSA00001] [v01.62]", "DLC Pack",
                                "Sample Game DLC Skin [CUSA00001] [v01.62].pkg")])
        # an update newer than the folder keeps the folder name (folders are never renamed)
        known = {"CUSA00001": uc.Ps4Library("Sample Game [CUSA00001] [v01.05]", "01.05", has_dlc_pack=False)}
        got = uc.ps4_layout([(Path("u.pkg"), ident("update", "01.07"))], known=known)
        self.assertEqual(got[0][1:], ("Sample Game [CUSA00001] [v01.05]", "", "Sample Game [CUSA00001] UPDATE [v01.07].pkg"))
        # the library's own title spelling wins for the file names too
        known = {"CUSA00001": uc.Ps4Library("Sample Game Remastered [CUSA00001] [v01.00]", "01.00", has_dlc_pack=False)}
        got = uc.ps4_layout([(Path("d.pkg"), ident("dlc", "", title="Sample Game - Skin"))], known=known)
        self.assertEqual(got[0][3], "Sample Game Remastered DLC Skin [CUSA00001] [v01.00].pkg")

    def test_scan_library(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        (root / "Sample Game [CUSA00001] [v01.62]" / "DLC Pack").mkdir(parents=True)
        (root / "Other [CUSA00002]").mkdir()
        (root / "._Sample Game [CUSA00003] [v01.00]").mkdir()
        got = uc.scan_ps4_library(root)
        self.assertEqual(got["CUSA00001"], uc.Ps4Library("Sample Game [CUSA00001] [v01.62]", "01.62", True))
        self.assertEqual(got["CUSA00002"], uc.Ps4Library("Other [CUSA00002]", "", False))
        self.assertNotIn("CUSA00003", got)


if __name__ == "__main__":
    unittest.main()
