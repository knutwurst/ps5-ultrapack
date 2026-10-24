"""python3 -m unittest backend.tests.test_chain_items"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
# never touch the real profile: the app dir is set (and migrated) at import time
os.environ.setdefault("PS5_FFPFSC_APP_DIR", tempfile.mkdtemp(prefix="ultrapack-test-profile-"))
import ultra_core as uc  # noqa: E402


def make_folder(root: Path) -> Path:
    g = root / "Example [PPSA00001]"
    (g / "sce_sys").mkdir(parents=True)
    (g / "sce_sys" / "param.json").write_text(json.dumps({
        "titleId": "PPSA00001", "contentVersion": "01.000.000",
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Example"}}}))
    (g / "eboot.bin").write_bytes(b"\x7fELF" + b"\0" * 60)
    return g


class FromChain(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_folder_source_carries_stats_and_chain_fields(self):
        g = make_folder(self.root)
        it = uc.GameItem.from_chain(g, to="ffpfsc", sign=True, backport_target="7.61",
                                    backport_libs_root=self.root / "libs", output_path=self.root / "out")
        self.assertEqual(it.operation, "chain")
        self.assertEqual(it.chain_to, "ffpfsc")
        self.assertTrue(it.chain_sign)
        self.assertEqual(it.backport_target, "7.61")
        self.assertEqual(it.title_id, "PPSA00001")
        self.assertEqual(it.source_kind, "inplace")
        self.assertEqual(uc.chain_source_kind(it), "folder")
        self.assertEqual(uc.chain_changes(it), ["backport 7.61", "sign"])
        self.assertEqual(uc.chain_summary(it), "Backport to 7.61, sign, then build .ffpfsc")

    def test_container_source(self):
        img = self.root / "Example [PPSA00002].ffpfsc"
        img.write_bytes(b"\0" * 4096)
        it = uc.GameItem.from_chain(img, to="ffpfsc", backport_target="7.61")
        self.assertEqual(uc.chain_source_kind(it), "ffpfsc")
        self.assertEqual(it.size, 4096)
        self.assertTrue(uc.chain_needs_unpack(it), "a change on a container means unpack into scratch")
        self.assertEqual(uc.chain_summary(it), "Backport to 7.61, then build .ffpfsc")
        self.assertEqual(uc._peak_factor_for(it), uc.PATCH_PEAK_FACTOR)

    def test_pkg_title_id_from_content_id_name(self):
        pkg = self.root / "UP0000-PPSA00003_00-EXAMPLE000000000-A0100-V0100.pkg"
        pkg.write_bytes(b"\0" * 64)
        it = uc.GameItem.from_chain(pkg, to="folder")
        self.assertEqual(it.title_id, "PPSA00003")
        self.assertEqual(uc.chain_summary(it), "Unpack to folder")
        self.assertTrue(uc.chain_needs_unpack(it))

    def test_pkg_space_uses_the_game_size(self):
        # pkg size is only a floor until its directory is read: conservative factor till then
        pkg = self.root / "UP0000-PPSA00004_00-EXAMPLE000000000-A0100-V0100.pkg"
        pkg.write_bytes(b"\0" * 1000)
        it = uc.GameItem.from_chain(pkg, to="ffpfsc")
        self.assertEqual(uc._peak_factor_for(it), uc.PATCH_PEAK_FACTOR)
        self.assertEqual(uc._build_size_of(it), 1000)
        it.pkg_content_size = 1800
        it.extracted_size = 1800
        self.assertEqual(uc._peak_factor_for(it), uc.PKG_UNPACK_PEAK_FACTOR)
        self.assertEqual(uc._build_size_of(it), 1800)
        need = uc._space_requirements(it, self.root, self.root / "elsewhere")[0][2]
        self.assertGreaterEqual(need, 2 * 1800, "the decoded image and the files sit side by side")

    def test_pass_through_needs_no_unpack(self):
        ff = self.root / "a.ffpfs"; ff.write_bytes(b"\0" * 64)
        it = uc.GameItem.from_chain(ff, to="ffpfsc")
        self.assertFalse(uc.chain_needs_unpack(it), ".ffpfs → .ffpfsc is native to the pack path")
        self.assertEqual(uc.chain_summary(it), "Build .ffpfsc")

    def test_summary_edge_cases(self):
        g = make_folder(self.root)
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(g, to="folder")), "Nothing to do")
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(g, to="folder", sign=True)), "Sign in place")
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(g, to="pkg")), "Build .pkg")
        img = self.root / "b.ffpfsc"; img.write_bytes(b"\0" * 64)
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(img, to="ffpfsc")), "Copy or move (same format)")
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(img, to="folder", sign=True)),
                         "Sign, then unpack to folder")
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(img, to="pkg", patch_source=self.root / "p")),
                         "Integrate patch, then build .pkg")

    def test_archive_source_is_a_placeholder(self):
        z = self.root / "game.zip"
        import zipfile
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("x/eboot.bin", b"\x7fELF")
        it = uc.GameItem.from_chain(z, to="ffpfsc")
        self.assertEqual(it.operation, "chain")
        self.assertEqual(uc.chain_source_kind(it), "archive")
        self.assertEqual(it.status, "Pending Extract")
        self.assertIsNotNone(it.archive_path)


def make_source_tree(root: Path) -> Path:
    conv = root / "Convert"
    rars = conv / "Set A"
    rars.mkdir(parents=True)
    for n in (1, 2, 3):
        (rars / f"Set A.part{n}.rar").write_bytes(b"Rar!")
    (rars / "._Set A.part1.rar").write_bytes(b"x")
    (conv / "Title [PPSA00001] [v01.000.000].pkg").write_bytes(b"\x7fCNT")
    misc = conv / "Misc"
    misc.mkdir()
    (misc / "PPSA00002-Compressed.7z").write_bytes(b"7z")
    (misc / "SHA-256.txt").write_text("abc", encoding="utf-8")
    old = conv / "Old"
    old.mkdir()
    (old / "game.rar").write_bytes(b"Rar!")
    (old / "game.r00").write_bytes(b"Rar!")
    (old / "game.r01").write_bytes(b"Rar!")
    game = conv / "Game [PPSA00003]"
    (game / "sce_sys").mkdir(parents=True)
    (game / "eboot.bin").write_bytes(b"\x7fELF")
    (game / "dlc.pkg").write_bytes(b"\x7fCNT")
    (conv / ".hidden.ffpfsc").write_bytes(b"x")
    scratch = conv / "_ffpfsc_temp"
    scratch.mkdir()
    (scratch / "leftover.ffpfsc").write_bytes(b"x")
    return conv


class FindJobSources(unittest.TestCase):
    def test_one_entry_per_source_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as td:
            conv = make_source_tree(Path(td))
            found = [s.relative_to(conv).as_posix() for s in uc.find_job_sources(conv)]
            self.assertEqual(found, [
                "Game [PPSA00003]",
                "Misc/PPSA00002-Compressed.7z",
                "Old/game.rar",
                "Set A/Set A.part1.rar",
                "Title [PPSA00001] [v01.000.000].pkg",
            ])

    def test_progress_counts_and_stop_ends_the_walk(self):
        with tempfile.TemporaryDirectory() as td:
            conv = make_source_tree(Path(td))
            seen = []
            found = uc.find_job_sources(conv, progress=seen.append)
            self.assertEqual(seen, list(range(1, len(found) + 1)))
            calls = []
            part = uc.find_job_sources(conv, stop=lambda: calls.append(1) or len(calls) > 2)
            self.assertLess(len(part), len(found))

    def test_every_source_becomes_a_chain_job(self):
        with tempfile.TemporaryDirectory() as td:
            conv = make_source_tree(Path(td))
            items = [uc.GameItem.from_chain(s, to="ffpfsc", backport_target="10.xx")
                     for s in uc.find_job_sources(conv)]
            self.assertTrue(all(i.operation == "chain" and i.chain_to == "ffpfsc"
                                and i.backport_target == "10.xx" for i in items))
            kinds = [uc.chain_source_kind(i) for i in items]
            self.assertEqual(kinds, ["folder", "archive", "archive", "archive", "pkg"])
            self.assertEqual([i.status for i in items],
                             ["Queued", "Pending Extract", "Pending Extract", "Pending Extract", "Queued"])


if __name__ == "__main__":
    unittest.main()
