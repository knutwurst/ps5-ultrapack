"""Organize: library names for every format, the scan of a folder, the plan, apply and undo.

The containers in these tests are small files whose content is their identity as JSON; the
readers handed to organize_lib parse that instead of opening a real image or package (the
pipeline suite runs the real readers)."""
import json, os, shutil, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent)); sys.path.insert(0, str(HERE.parent))
import ultra_core as uc


def lib(key, kind, role, version, title="Sample Game", tid="PPSA00001", fw="", ps4=None):
    return uc.LibItem(Path(key), kind, role, title, tid, version, fw, ps4)


def ps4id(kind, version, title="Sample Game", tid="CUSA00001"):
    return NS(kind=kind, version=version, app_ver=version, title=title, title_id=tid)


class Layout(unittest.TestCase):
    def place(self, items, known=None):
        return {p.name: (f, s, n) for p, f, s, n in uc.library_layout(items, known)}

    def test_ps5_title_folder_has_the_highest_version(self):
        got = self.place([lib("a.ffpfsc", "ffpfsc", "game", "01.000.000", fw="4.03"),
                          lib("u.pkg", "pkg", "update", "01.300.000"),
                          lib("g", "folder", "game", "01.000.000")])
        folder = "Sample Game [PPSA00001] [v01.300.000]"
        self.assertEqual(got["a.ffpfsc"], (folder, "", "Sample Game [PPSA00001] [v01.000] [fw4.03].ffpfsc"))
        self.assertEqual(got["u.pkg"], (folder, "", "Sample Game [PPSA00001] UPDATE [v01.300].pkg"))
        self.assertEqual(got["g"], (folder, "", "Sample Game [PPSA00001] [v01.000.000]"))

    def test_ps5_game_package_is_named_like_a_build(self):
        got = self.place([lib("x.pkg", "pkg", "game", "01.002.000", fw="7.00")])
        self.assertEqual(got["x.pkg"][2], "Sample Game [PPSA00001] [v01.002] [fw7.00].pkg")
        got = self.place([lib("x.exfat", "exfat", "game", "01.002.000")])
        self.assertEqual(got["x.exfat"][2], "Sample Game [PPSA00001] [v01.002].exfat")

    def test_ps5_dlc_and_dlc_pack(self):
        dl = [lib(f"d{i}.pkg", "pkg", "dlc", "01.000.000", title=f"Sample Game - Item {i}") for i in range(4)]
        got = self.place([lib("g.ffpfsc", "ffpfsc", "game", "01.100.000")] + dl[:3])
        self.assertEqual(got["d0.pkg"], ("Sample Game [PPSA00001] [v01.100.000]", "",
                                         "Sample Game DLC Item 0 [PPSA00001] [v01.100].pkg"))
        got = self.place([lib("g.ffpfsc", "ffpfsc", "game", "01.100.000")] + dl)
        self.assertEqual({s for k, (f, s, n) in got.items() if k.startswith("d")}, {"DLC Pack"})

    def test_ps5_backport_and_archive(self):
        got = self.place([lib("b.pkg", "pkg", "backport", "01.000.000", fw="4.03"),
                          lib("Release.part1.rar", "archive", "game", "01.000.000")])
        self.assertEqual(got["b.pkg"][2], "Sample Game BACKPORT [fw4.03] [PPSA00001].pkg")
        self.assertEqual(got["Release.part1.rar"], ("Sample Game [PPSA00001] [v01.000.000]", "", "Release.part1.rar"))

    def test_known_folder_is_never_lowered(self):
        known = {"PPSA00001": uc.Ps4Library("Old Name [PPSA00001] [v01.500.000]", "01.500.000", True)}
        got = self.place([lib("a.ffpfsc", "ffpfsc", "game", "01.000.000"),
                          lib("d.pkg", "pkg", "dlc", "01.000.000", title="Sample Game - Skin")], known)
        self.assertEqual(got["a.ffpfsc"][0], "Sample Game [PPSA00001] [v01.500.000]")
        self.assertEqual(got["d.pkg"][1], "DLC Pack")

    def test_ps4_matches_ps4_layout_and_folders(self):
        items = [lib("g.pkg", "pkg", "game", "01.00", tid="CUSA00001", ps4=ps4id("game", "01.00")),
                 lib("u.pkg", "pkg", "update", "01.07", tid="CUSA00001", ps4=ps4id("update", "01.07")),
                 lib("unpacked", "folder", "game", "01.00", tid="CUSA00001", ps4=ps4id("game", "01.00")),
                 lib("set.zip", "archive", "game", "01.00", tid="CUSA00001", ps4=ps4id("game", "01.00"))]
        got = self.place(items)
        folder = "Sample Game [CUSA00001] [v01.07]"
        self.assertEqual(got["g.pkg"], (folder, "", "Sample Game [CUSA00001] [v01.00].pkg"))
        self.assertEqual(got["u.pkg"], (folder, "", "Sample Game [CUSA00001] UPDATE [v01.07].pkg"))
        self.assertEqual(got["unpacked"], (folder, "", "Sample Game [CUSA00001] [v01.00]"))
        self.assertEqual(got["set.zip"], (folder, "", "set.zip"))


import organize_lib as ol

A = {"title": "Alpha Game", "title_id": "PPSA00001", "version": "01.000.000"}
B = {"title": "Beta Game", "title_id": "PPSA00002", "version": "01.000.000"}
FA = "Alpha Game [PPSA00001] [v01.000.000]"
FB = "Beta Game [PPSA00002] [v01.000.000]"


def fake_identify(path, kind):
    """The test containers carry their identity as JSON (a folder in sce_sys/param.json)."""
    f = path / "sce_sys" / "param.json" if kind == "folder" else path
    try:
        d = json.loads(f.read_text())
    except Exception:
        return None
    if d.get("ps4"):
        d["ps4"] = NS(**d["ps4"])
    return d


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="organize_")).resolve()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        for j in self.root.parent.glob(self.root.name + ".journal*.json"):
            j.unlink()

    def put(self, rel, ident=None, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ident) if ident is not None else text)
        return p

    def game_folder(self, rel, ident):
        self.put(f"{rel}/sce_sys/param.json", ident)
        self.put(f"{rel}/eboot.bin", text="elf")

    def snapshot(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))

    def run_plan(self):
        entries, comps = ol.scan(self.root, fake_identify)
        return ol.plan(self.root, entries, comps)

    def organize(self, selected=None):
        p = self.run_plan()
        res = ol.apply(p, self.root.parent / (self.root.name + ".journal.json"), selected=selected, on_line=lambda _l: None)
        return p, res

    def tree(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


class Scan(Tree):
    def test_clutter_is_never_listed(self):
        self.put("._x.pkg", text="\0"); self.put(".DS_Store"); self.put("__MACOSX/a.pkg", A)
        self.put("Rel/._g.ffpfsc", text="\0"); self.put("Rel/g.ffpfsc", A); self.put("Rel/g.ffpfsc.copy-tmp", A)
        entries, comps = ol.walk(self.root)
        self.assertEqual([e.path.name for e in entries], ["g.ffpfsc"])
        self.assertEqual(comps, [])

    def test_game_folder_is_one_entry_and_sets_are_grouped(self):
        self.game_folder("G", A); self.put("G/sce_sys/inner.pkg", B)
        self.put("D/s.part1.rar", A); self.put("D/s.part2.rar", text="y"); self.put("D/notes/a.txt")
        entries, comps = ol.walk(self.root)
        self.assertEqual([(e.path.name, e.kind) for e in entries], [("s.part1.rar", "archive"), ("G", "folder")])
        self.assertEqual([p.name for p in entries[0].parts], ["s.part1.rar", "s.part2.rar"])
        self.assertEqual(comps, [self.root / "D" / "notes"])

    def test_unreadable_gets_a_note(self):
        self.put("x.ffpfsc", text="not json")
        entries, _ = ol.scan(self.root, fake_identify)
        self.assertFalse(entries[0].known)
        self.assertTrue(entries[0].note)


class Organize(Tree):
    def test_release_folder_of_one_game_is_renamed(self):
        self.put("Some.Release-GRP/g.ffpfsc", A); self.put("Some.Release-GRP/info.nfo")
        self.put("Some.Release-GRP/._g.ffpfsc", text="\0")
        p, res = self.organize()
        self.assertEqual(res["failed"], [])
        self.assertEqual(self.tree(), [f"{FA}/Alpha Game [PPSA00001] [v01.000].ffpfsc", f"{FA}/info.nfo"])

    def test_two_games_in_one_folder_are_split(self):
        self.put("dl/a.part1.rar", A); self.put("dl/a.part2.rar", text="y"); self.put("dl/b.zip", B)
        self.put("dl/a.nfo"); self.put("dl/readme.txt")
        p, res = self.organize()
        self.assertEqual(self.tree(), sorted([f"{FA}/a.part1.rar", f"{FA}/a.part2.rar", f"{FA}/a.nfo",
                                              f"{FB}/b.zip", "dl/readme.txt"]))
        self.assertIn(self.root / "dl" / "readme.txt", [s.path for s in p.stays])

    def test_unclear_archives_stay(self):
        self.put("dl/a.zip", A); self.put("dl/x.rar", text="?")
        before = self.snapshot()
        p, res = self.organize()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({s.path.name for s in p.stays}, {"a.zip", "x.rar"})

    def test_loose_image_package_and_folder_share_one_title_folder(self):
        self.put("g.ffpfsc", A); self.put("g.pkg", dict(A, role="game", fw="7.00")); self.game_folder("unpacked", A)
        self.organize()
        self.assertEqual({p.relative_to(self.root).parts[0] for p in self.root.iterdir()}, {FA})
        self.assertEqual(sorted(p.name for p in (self.root / FA).iterdir()),
                         ["Alpha Game [PPSA00001] [v01.000.000]", "Alpha Game [PPSA00001] [v01.000] [fw7.00].pkg",
                          "Alpha Game [PPSA00001] [v01.000].ffpfsc"])

    def test_loose_game_folder_named_like_its_title_folder(self):
        self.game_folder(FA, A)
        before = self.snapshot()
        p, res = self.organize()
        self.assertEqual(res["failed"], [])
        self.assertEqual(self.tree(), [f"{FA}/{FA}/eboot.bin", f"{FA}/{FA}/sce_sys/param.json"])
        self.assertEqual(self.run_plan().moves, [])
        ol.undo(self.root.parent / (self.root.name + ".journal.json"), on_line=lambda _l: None)
        self.assertEqual(self.snapshot(), before)

    def test_existing_title_folder_is_joined_and_raised(self):
        self.put(f"{FA}/Alpha Game [PPSA00001] [v01.000].ffpfsc", A); self.put(f"{FA}/notes.txt")
        self.put("new/u.pkg", dict(A, version="01.300.000", role="update"))
        self.organize()
        new = "Alpha Game [PPSA00001] [v01.300.000]"
        self.assertEqual(self.tree(), sorted([f"{new}/Alpha Game [PPSA00001] [v01.000].ffpfsc", f"{new}/notes.txt",
                                              f"{new}/Alpha Game [PPSA00001] UPDATE [v01.300].pkg"]))

    def test_companion_in_the_root_goes_by_its_name(self):
        self.put("a.ffpfsc", A); self.put("b.ffpfsc", B)
        self.put("Alpha Game manual.pdf"); self.put("shopping.txt")
        p, _ = self.organize()
        self.assertIn(f"{FA}/Alpha Game manual.pdf", self.tree())
        self.assertIn("shopping.txt", self.tree())

    def test_same_name_twice_both_stay(self):
        self.put("x/one.ffpfsc", A); self.put("y/two.ffpfsc", A)
        p, _ = self.organize()
        self.assertEqual({s.path.name for s in p.stays}, {"one.ffpfsc", "two.ffpfsc"})
        self.assertEqual(self.tree().count(f"{FA}/Alpha Game [PPSA00001] [v01.000].ffpfsc"), 0)

    def test_a_tidy_library_has_nothing_to_do(self):
        self.put(f"{FA}/Alpha Game [PPSA00001] [v01.000].ffpfsc", A)
        self.put(f"{FB}/Beta Game [PPSA00002] [v01.000].ffpfsc", B)
        p = self.run_plan()
        self.assertEqual((p.moves, p.in_place), ([], 2))

    def test_ps4_set_gets_a_dlc_pack(self):
        def ps4(kind, ver, title="Gamma Game"):
            return {"title": title, "title_id": "CUSA00003", "version": ver, "role": kind, "platform": "ps4",
                    "ps4": {"kind": kind, "version": ver, "app_ver": ver, "title": title, "title_id": "CUSA00003"}}
        self.put("in/g.pkg", ps4("game", "01.00")); self.put("in/u.pkg", ps4("update", "01.05"))
        for i in range(4):
            self.put(f"in/d{i}.pkg", ps4("dlc", "01.00", title=f"Gamma Game - Item {i}"))
        self.organize()
        folder = "Gamma Game [CUSA00003] [v01.05]"
        self.assertEqual(self.tree(), sorted([f"{folder}/Gamma Game [CUSA00003] [v01.00].pkg",
                                              f"{folder}/Gamma Game [CUSA00003] UPDATE [v01.05].pkg"]
                                             + [f"{folder}/DLC Pack/Gamma Game DLC Item {i} [CUSA00003] [v01.05].pkg"
                                                for i in range(4)]))

    def test_undo_restores_the_tree(self):
        self.put("dl/a.part1.rar", A); self.put("dl/a.part2.rar", text="y"); self.put("dl/b.zip", B)
        self.put("Rel/g.ffpfsc", B); self.game_folder("x/y/unpacked", A); self.put("x/y/keep.txt")
        before = self.snapshot()
        self.organize()
        self.assertNotEqual(self.snapshot(), before)
        res = ol.undo(self.root.parent / (self.root.name + ".journal.json"), on_line=lambda _l: None)
        self.assertEqual(res["failed"], [])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.root.parent / (self.root.name + ".journal.undone.json")).exists())

    def test_only_the_selected_moves_run(self):
        self.put("a.ffpfsc", A); self.put("b.ffpfsc", B)
        p = self.run_plan()
        idx = next(i for i, m in enumerate(p.moves) if m.src.name == "a.ffpfsc")
        ol.apply(p, self.root.parent / (self.root.name + ".journal.json"), selected={idx}, on_line=lambda _l: None)
        self.assertEqual(self.tree(), sorted([f"{FA}/Alpha Game [PPSA00001] [v01.000].ffpfsc", "b.ffpfsc"]))

    def test_plan_json_round_trip(self):
        self.put("a.ffpfsc", A); self.put("dl/x.rar", text="?")
        p = self.run_plan()
        q = ol.plan_from_json(json.loads(json.dumps(ol.plan_to_json(p))))
        self.assertEqual(ol.plan_to_json(q), ol.plan_to_json(p))


class Into(Tree):
    """The "Organize" job output: one source written into a library."""
    def lib(self):
        d = self.root / "library"
        d.mkdir(exist_ok=True)
        return d

    def test_folder_is_copied_into_its_title_folder_without_clutter(self):
        self.game_folder("src/unpacked", A); self.put("src/unpacked/._eboot.bin", text="\0")
        rc = ol.organize_into(self.root / "src" / "unpacked", self.lib(), fake_identify, mode="keep", on_line=lambda _l: None)
        self.assertEqual(rc, 0)
        got = sorted(str(p.relative_to(self.lib())) for p in self.lib().rglob("*") if p.is_file())
        self.assertEqual(got, [f"{FA}/{FA}/eboot.bin", f"{FA}/{FA}/sce_sys/param.json"])
        self.assertTrue((self.root / "src" / "unpacked" / "eboot.bin").exists())

    def test_image_joins_and_raises_the_title_folder(self):
        self.put(f"library/{FA}/Alpha Game [PPSA00001] [v01.000].ffpfsc", A)
        src = self.put("dl/new.ffpfsc", dict(A, version="01.200.000"))
        rc = ol.organize_into(src, self.lib(), fake_identify, mode="move", on_line=lambda _l: None)
        self.assertEqual(rc, 0)
        new = "Alpha Game [PPSA00001] [v01.200.000]"
        self.assertEqual(sorted(p.name for p in (self.lib() / new).iterdir()),
                         ["Alpha Game [PPSA00001] [v01.000].ffpfsc", "Alpha Game [PPSA00001] [v01.200].ffpfsc"])
        self.assertFalse(src.exists())

    def test_library_inside_the_source_is_refused(self):
        self.put("dl/a.ffpfsc", A)
        self.assertEqual(ol.organize_into(self.root / "dl", self.root / "dl" / "lib", fake_identify,
                                          on_line=lambda _l: None), 1)
        self.assertFalse((self.root / "dl" / "lib").exists())

    def test_existing_name_follows_the_rule(self):
        self.put(f"library/{FA}/Alpha Game [PPSA00001] [v01.000].ffpfsc", text="old")
        src = self.put("dl/a.ffpfsc", A)
        self.assertEqual(ol.organize_into(src, self.lib(), fake_identify, on_line=lambda _l: None), 0)
        self.assertEqual((self.lib() / FA / "Alpha Game [PPSA00001] [v01.000].ffpfsc").read_text(), "old")
        ol.organize_into(src, self.lib(), fake_identify, if_exists="overwrite", on_line=lambda _l: None)
        self.assertEqual(json.loads((self.lib() / FA / "Alpha Game [PPSA00001] [v01.000].ffpfsc").read_text()), A)
        ol.organize_into(src, self.lib(), fake_identify, if_exists="keep", on_line=lambda _l: None)
        self.assertTrue((self.lib() / FA / "Alpha Game [PPSA00001] [v01.000] (2).ffpfsc").exists())
        self.assertEqual(sorted(p.name for p in (self.lib() / FA).iterdir() if p.name.startswith(".")), [])


if __name__ == "__main__":
    unittest.main()
