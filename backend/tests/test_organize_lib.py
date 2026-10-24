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
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))

    def run_plan(self):
        entries, comps = ol.scan(self.root, fake_identify)
        return ol.plan(self.root, entries, comps)

    def organize(self, selected=None):
        p = self.run_plan()
        res = ol.apply(p, self.root.parent / (self.root.name + ".journal.json"), selected=selected, on_line=lambda _l: None)
        return p, res

    def tree(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())


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

    def test_journal_is_written_after_every_move(self):
        self.put("a.ffpfsc", A); self.put("b.ffpfsc", B)
        p = self.run_plan()
        journal = self.root.parent / (self.root.name + ".journal.json")
        real, seen = ol._rename, []

        def spy(src, dst, *a, **k):
            seen.append(json.loads(journal.read_text())["ops"] if journal.exists() else None)
            return real(src, dst, *a, **k)
        ol._rename = spy
        try:
            ol.apply(p, journal, on_line=lambda _l: None)
        finally:
            ol._rename = real
        self.assertEqual(len(seen), 2)
        self.assertIsNotNone(seen[1], "a run killed now would leave no journal")
        self.assertTrue(any(op["op"] == "move" for op in seen[1]), seen[1])
        self.assertFalse(list(journal.parent.glob(journal.name + ".tmp")))

    def test_case_rename_cut_in_half_is_journaled_and_undone(self):
        src = self.put("alpha.ffpfsc", A)
        dst = self.root / "ALPHA.ffpfsc"
        p = ol.Plan(self.root, [ol.Move(src, dst, "PPSA00001", "Alpha")], [], 0)
        journal = self.root.parent / (self.root.name + ".journal.json")
        real = os.rename

        def failing(a, b, *x, **k):
            if str(a).endswith(".organize-case"):
                raise OSError(5, "drive gone")
            return real(a, b, *x, **k)
        ol.os.rename = failing
        try:
            res = ol.apply(p, journal, on_line=lambda _l: None)
        finally:
            ol.os.rename = real
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual([q.name for q in self.root.iterdir()], ["alpha.ffpfsc.organize-case"])
        back = ol.undo(journal, on_line=lambda _l: None)
        self.assertEqual(back["failed"], [])
        self.assertEqual([q.name for q in self.root.iterdir()], ["alpha.ffpfsc"])
        self.assertEqual(json.loads(src.read_text()), A)

    def test_plan_json_round_trip(self):
        self.put("a.ffpfsc", A); self.put("dl/x.rar", text="?")
        p = self.run_plan()
        q = ol.plan_from_json(json.loads(json.dumps(ol.plan_to_json(p))))
        self.assertEqual(ol.plan_to_json(q), ol.plan_to_json(p))


class Into(Tree):
    def lib(self):
        d = self.root / "library"
        d.mkdir(exist_ok=True)
        return d

    def test_folder_is_copied_into_its_title_folder_without_clutter(self):
        self.game_folder("src/unpacked", A); self.put("src/unpacked/._eboot.bin", text="\0")
        rc = ol.organize_into(self.root / "src" / "unpacked", self.lib(), fake_identify, mode="keep", on_line=lambda _l: None)
        self.assertEqual(rc, 0)
        got = sorted(p.relative_to(self.lib()).as_posix() for p in self.lib().rglob("*") if p.is_file())
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

    def test_progress_lines_are_read_by_the_app(self):
        for i in range(6):
            self.put(f"dl/g{i}.ffpfsc", dict(A, title=f"Game {i}", title_id=f"PPSA0000{i + 1}"))
        lines = []
        self.assertEqual(ol.organize_into(self.root / "dl", self.lib(), fake_identify, on_line=lines.append), 0)
        bars = [l for l in lines if l.startswith("[#")]
        pcts = [int(uc.PROGRESS_RE.search(b).group("pct")) for b in bars if uc.PROGRESS_RE.search(b)]
        self.assertTrue(len(pcts) == len(bars) and min(pcts) < 20 and pcts[-1] == 100, bars)

    def test_library_inside_the_source_is_refused(self):
        self.put("dl/a.ffpfsc", A)
        self.assertEqual(ol.organize_into(self.root / "dl", self.root / "dl" / "lib", fake_identify,
                                          on_line=lambda _l: None), 1)
        self.assertFalse((self.root / "dl" / "lib").exists())

    def test_an_overwrite_cut_short_gets_its_old_item_back(self):
        self.put(f"library/{FA}/Alpha Game [PPSA00001] [v01.000].ffpfsc.copy-old", A)
        lines = []
        rc = ol.organize_into(self.put("dl/a.ffpfsc", A), self.lib(), fake_identify, on_line=lines.append)
        names = sorted(p.name for p in (self.lib() / FA).iterdir())
        self.assertIn("Alpha Game [PPSA00001] [v01.000].ffpfsc", names)
        self.assertFalse(any(n.endswith(".copy-old") for n in names), names)
        self.assertTrue(any("took its name back" in l for l in lines), lines)
        self.assertIn(rc, (0, 4))

    def test_existing_name_follows_the_rule(self):
        self.put(f"library/{FA}/Alpha Game [PPSA00001] [v01.000].ffpfsc", text="old")
        src = self.put("dl/a.ffpfsc", A)
        self.assertEqual(ol.organize_into(src, self.lib(), fake_identify, on_line=lambda _l: None), 4)
        self.assertEqual((self.lib() / FA / "Alpha Game [PPSA00001] [v01.000].ffpfsc").read_text(), "old")
        ol.organize_into(src, self.lib(), fake_identify, if_exists="overwrite", on_line=lambda _l: None)
        self.assertEqual(json.loads((self.lib() / FA / "Alpha Game [PPSA00001] [v01.000].ffpfsc").read_text()), A)
        ol.organize_into(src, self.lib(), fake_identify, if_exists="keep", on_line=lambda _l: None)
        self.assertTrue((self.lib() / FA / "Alpha Game [PPSA00001] [v01.000] (2).ffpfsc").exists())
        self.assertEqual(sorted(p.name for p in (self.lib() / FA).iterdir() if p.name.startswith(".")), [])

    def test_backport_package_in_a_backport_folder_keeps_its_role(self):
        g = dict(A, fw="11.00")
        self.put("dl/UP0001-PPSA00001_00-X.pkg", g, )
        (self.root / "dl/UP0001-PPSA00001_00-X.pkg").write_text(json.dumps(g) + " " * 4000)
        self.put("dl/backport (4.xx to 10.xx)/UP0001-PPSA00001_00-X-4xx.pkg", g)
        self.put("dl/_SHA256/Tool.exe"); self.put("dl/_SHA256/UP0001.sha256"); self.put("dl/.DS_Store")
        lines = []
        rc = ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move", on_line=lines.append)
        self.assertEqual(rc, 0, lines)
        folder = "Alpha Game [PPSA00001] [v01.000.000]"
        got = sorted(p.relative_to(self.lib()).as_posix() for p in self.lib().rglob("*") if p.is_file())
        self.assertEqual(got, sorted([f"{folder}/Alpha Game [PPSA00001] [v01.000] [fw11.00].pkg",
                                      f"{folder}/backport (4.xx to 10.xx)/Alpha Game BACKPORT [fw11.00] [PPSA00001].pkg",
                                      f"{folder}/_SHA256/Tool.exe", f"{folder}/_SHA256/UP0001.sha256"]), lines)
        # the game, not the small backport, carries the game's name
        self.assertGreater((self.lib() / folder / "Alpha Game [PPSA00001] [v01.000] [fw11.00].pkg").stat().st_size, 4000)

    def test_an_unreadable_package_and_an_inner_archive_come_along(self):
        # what Organize can't read must not stay behind in an unpack that gets deleted
        self.put("dl/game.pkg", dict(A, fw="11.00"))
        self.put("dl/backport (4.xx to 10.xx)/game-4xx.pkg", text="not readable")
        self.put("dl/extras/bonus.part1.rar", text="r1"); self.put("dl/extras/bonus.part2.rar", text="r2")
        self.put("dl/release.sha256")
        lines = []
        self.assertEqual(ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move",
                                          on_line=lines.append), 0)
        got = sorted(p.relative_to(self.lib() / FA).as_posix() for p in self.lib().rglob("*") if p.is_file())
        self.assertEqual(got, sorted(["Alpha Game [PPSA00001] [v01.000] [fw11.00].pkg",
                                      "backport (4.xx to 10.xx)/game-4xx.pkg",
                                      "extras/bonus.part1.rar", "extras/bonus.part2.rar", "release.sha256"]), lines)
        self.assertEqual([p for p in (self.root / "dl").rglob("*") if p.is_file()], [])
        self.assertTrue(any("game-4xx.pkg" in l and "as it is" in l for l in lines), lines)

    def test_every_entry_goes_to_its_own_title(self):
        big = self.put("dl/1/a.pkg", A); big.write_text(json.dumps(A) + " " * 4000)
        self.put("dl/2/b.pkg", B)
        self.put("dl/3/a-update.pkg", dict(A, version="01.100.000", role="update"))
        self.assertEqual(ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move",
                                          on_line=lambda _l: None), 0)
        got = sorted(p.relative_to(self.lib()).as_posix() for p in self.lib().rglob("*") if p.is_file())
        fa = "Alpha Game [PPSA00001] [v01.100.000]"
        self.assertEqual(got, sorted([f"{fa}/Alpha Game [PPSA00001] [v01.000].pkg",
                                      f"{fa}/Alpha Game [PPSA00001] UPDATE [v01.100].pkg",
                                      f"{FB}/Beta Game [PPSA00002] [v01.000].pkg"]))
        read = lambda rel: json.loads((self.lib() / rel).read_text())
        self.assertEqual(read(f"{FB}/Beta Game [PPSA00002] [v01.000].pkg")["title_id"], "PPSA00002")
        self.assertEqual(read(f"{fa}/Alpha Game [PPSA00001] UPDATE [v01.100].pkg")["version"], "01.100.000")

    def test_a_companion_never_replaces_another(self):
        self.put("dl/game.pkg", A); self.put("dl/tools.ini", text="new one")
        self.put(f"library/{FA}/tools.ini", text="old")
        self.assertEqual(ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move",
                                          on_line=lambda _l: None), 0)
        self.assertEqual((self.lib() / FA / "tools.ini").read_text(), "old")
        self.assertEqual((self.lib() / FA / "tools (2).ini").read_text(), "new one")

    def test_a_companion_of_the_same_size_is_compared_by_content(self):
        self.put("dl/game.pkg", A); self.put("dl/a.sha256", text="1111")
        self.put(f"library/{FA}/a.sha256", text="2222")
        self.assertEqual(ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move",
                                          on_line=lambda _l: None), 0)
        self.assertEqual((self.lib() / FA / "a.sha256").read_text(), "2222")
        self.assertEqual((self.lib() / FA / "a (2).sha256").read_text(), "1111")

    def test_a_companion_that_cannot_come_along_fails_the_job(self):
        self.put("dl/game.pkg", A); self.put("dl/tools.ini")
        real = ol._bring_file
        ol._bring_file = lambda *a, **k: 1
        try:
            rc = ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move", on_line=lambda _l: None)
        finally:
            ol._bring_file = real
        self.assertEqual(rc, 1)
        self.assertTrue((self.root / "dl/tools.ini").exists())

    def test_bring_file_leaves_a_foreign_temp_alone(self):
        d = self.root / "x"; d.mkdir()
        (d / "f.copy-tmp").write_text("someone else's"); src = self.put("s/f", text="new")
        self.assertEqual(ol._bring_file(src, d / "f", move=False), 0)
        self.assertEqual((d / "f.copy-tmp").read_text(), "someone else's")
        self.assertEqual((d / "f").read_text(), "new")
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["f", "f.copy-tmp"])

    def test_several_backports_keep_their_folders(self):
        g = dict(A, fw="11.00")
        self.put("dl/game.pkg", g)
        self.put("dl/Backport 4.xx/game-4xx.pkg", g)
        self.put("dl/Backport 7.xx/game-7xx.pkg", g)
        self.put("dl/Backport 7.xx/game-7xx-fix.pkg", g)      # two in one folder: both stay
        self.assertEqual(ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move",
                                          on_line=lambda _l: None), 0)
        got = sorted(p.relative_to(self.lib() / FA).as_posix() for p in self.lib().rglob("*") if p.is_file())
        bp = "Alpha Game BACKPORT [fw11.00] [PPSA00001]"
        self.assertEqual(got, sorted(["Alpha Game [PPSA00001] [v01.000] [fw11.00].pkg",
                                      f"Backport 4.xx/{bp}.pkg", f"Backport 7.xx/{bp}.pkg",
                                      f"Backport 7.xx/{bp} (2).pkg"]))
        self.assertEqual([p for p in (self.root / "dl").rglob("*") if p.is_file()], [])

    def test_two_sources_never_share_one_name(self):
        big = self.put("dl/a/game.pkg", A); big.write_text(json.dumps(A) + " " * 4000)
        self.put("dl/b/game.pkg", A)
        lines = []
        self.assertEqual(ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move",
                                          on_line=lines.append), 0)
        names = sorted(p.name for p in (self.lib() / FA).iterdir())
        self.assertEqual(names, ["Alpha Game [PPSA00001] [v01.000] (2).pkg", "Alpha Game [PPSA00001] [v01.000].pkg"])
        self.assertGreater((self.lib() / FA / "Alpha Game [PPSA00001] [v01.000].pkg").stat().st_size, 4000)
        self.assertTrue(any(l.startswith("[WARN]") and "(2)" in l for l in lines), lines)
        self.assertFalse(any("skipped" in l for l in lines), lines)

    def test_companions_stay_when_the_source_holds_several_titles(self):
        self.put("dl/a.ffpfsc", A); self.put("dl/b.ffpfsc", B); self.put("dl/notes/readme.md")
        lines = []
        self.assertEqual(ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move",
                                          on_line=lines.append), 4)
        self.assertTrue((self.root / "dl/notes/readme.md").exists())
        self.assertFalse(any(p.name == "readme.md" for p in self.lib().rglob("*")))
        self.assertIn("[KEPT] 1 item(s) stay in the source: notes", lines)

    def test_names_that_differ_only_in_case_both_arrive(self):
        self.put("dl/g.pkg", A)
        self.put("dl/d1.pkg", dict(A, title="Alpha Game - x", role="dlc"))
        self.put("dl/d2.pkg", dict(A, title="Alpha Game - X", role="dlc"))
        for rule in ("skip", "overwrite"):
            lines = []
            out = self.root / f"lib-{rule}"
            rc = ol.organize_into(self.root / "dl", out, fake_identify, mode="keep", if_exists=rule,
                                  on_line=lines.append)
            self.assertEqual(rc, 0, lines)
            got = sorted(json.loads(p.read_text())["title"] for p in out.rglob("*DLC*.pkg"))
            self.assertEqual(got, ["Alpha Game - X", "Alpha Game - x"], lines)
            self.assertEqual(len({p.name.casefold() for p in out.rglob("*DLC*.pkg")}), 2)
            self.assertFalse([p for p in out.rglob("*") if p.name.endswith(".copy-old")])

    def test_items_left_in_the_source_give_code_4(self):
        self.put("dl/a.ffpfsc", A); self.put("dl/b.ffpfsc", B); self.put("dl/what.ffpfsc", text="unreadable")
        lines = []
        rc = ol.organize_into(self.root / "dl", self.lib(), fake_identify, mode="move", on_line=lines.append)
        self.assertEqual(rc, 4, lines)
        self.assertIn("[KEPT] 1 item(s) stay in the source: what.ffpfsc", lines)
        self.assertTrue((self.root / "dl/what.ffpfsc").exists())
        self.put("dl/a.ffpfsc", A)
        lines = []
        rc = ol.organize_into(self.root / "dl" / "a.ffpfsc", self.lib(), fake_identify, mode="move",
                              on_line=lines.append)
        self.assertEqual(rc, 4, lines)
        self.assertEqual([l for l in lines if l.startswith("[KEPT]")], ["[KEPT] 1 item(s) stay in the source: a.ffpfsc"])

    def test_kept_line_names_five_at_most(self):
        import copy_job
        self.assertEqual(copy_job.kept_line([f"f{i}" for i in range(7)]),
                         "[KEPT] 7 item(s) stay in the source: f0, f1, f2, f3, f4 and 2 more")


class Roles(unittest.TestCase):
    def test_role_from_the_name_or_a_folder_above(self):
        root = Path("/x/dl")
        self.assertEqual(ol.name_role(root / "Game-backport.pkg", root), "backport")
        self.assertEqual(ol.name_role(root / "backport (4.xx to 10.xx)" / "g-4xx.pkg", root), "backport")
        self.assertEqual(ol.name_role(root / "Update 1.02" / "g.pkg", root), "update")
        self.assertEqual(ol.name_role(root / "g.pkg", root), "")
        # folders at or above the root say nothing about the file
        self.assertEqual(ol.name_role(Path("/backport/dl/g.pkg"), Path("/backport/dl")), "")

    def test_scan_reads_the_backport_folder_too(self):
        d = Path(tempfile.mkdtemp(prefix="organize_")).resolve()
        try:
            f = d / "backport (4.xx to 10.xx)" / "g-4xx.pkg"
            f.parent.mkdir(parents=True); f.write_text(json.dumps(A))
            entries, _ = ol.scan(d, fake_identify)
            self.assertEqual([e.role for e in entries], ["backport"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Journals(unittest.TestCase):
    def test_newest_apply_first_also_within_one_second(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            a = ol.new_journal_path(folder); a.write_text('{"root": "/x", "ops": []}')
            b = ol.new_journal_path(folder); b.write_text('{"root": "/x", "ops": [{"op": "move"}]}')
            c = ol.new_journal_path(folder); c.write_text('{"root": "/x", "ops": []}')
            self.assertEqual(ol.journals(folder)[0], c)
            self.assertEqual(ol.describe(b)["moves"], 1)
            c.rename(c.with_name(c.stem + ".undone.json"))
            self.assertEqual(ol.journals(folder)[0], b)


if __name__ == "__main__":
    unittest.main()
