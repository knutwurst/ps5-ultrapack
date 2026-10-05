"""CHAIN MODE (--to): any source → [patch → backport → sign] → any output, in one call.

Runs the real CLI on a tiny synthetic game (a raw ELF eboot with an SCE param
segment, a param.json, one data file) through the vendored mkpfs, so the container
paths are exercised for real:

    folder → folder            no changes  → refused ("nothing to do")
    folder → folder + backport → in place, SDK lowered
    folder → .ffpfsc           pass-through to the pack path
    .ffpfsc → folder + backport → unpacked into scratch, lowered, moved to the output
    .ffpfsc → .ffpfsc + backport → the user's case: unpack, lower, repack; source untouched
    .ffpfsc → .ffpfsc no changes → copy job

Plus the backport rules on a folder: SDK words from a firmware folder, the function
check that makes patched libraries mandatory, encrypted and fake-signed executables.

    python3 -m unittest backend.tests.test_chain_mode
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "backend" / "cli.py"
sys.path.insert(0, str(REPO / "backend" / "tests"))
from test_backport import (_elf_with_param, _elf_with_imports, _elf_with_exports,   # noqa: E402
                           _signable_elf, _fself, _encrypt_flag, _param_words, _fw_lib)
from backend import backport as bp         # noqa: E402

SDK_HIGH_PS5, SDK_HIGH_PS4 = 0x08000041, 0x11090001
SDK_761_PS5, SDK_761_PS4 = bp.SDK_TARGETS["7.61"]


def make_game(root: Path) -> Path:
    g = root / "Example [PPSA00001]"
    (g / "sce_sys").mkdir(parents=True)
    (g / "sce_sys" / "param.json").write_text(json.dumps({
        "titleId": "PPSA00001", "contentId": "UP0000-PPSA00001_00-EXAMPLE000000000",
        "contentVersion": "01.000.000", "attribute": 0,
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Example"}},
    }), encoding="utf-8")
    (g / "eboot.bin").write_bytes(_elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, SDK_HIGH_PS4, SDK_HIGH_PS5))
    (g / "data.bin").write_bytes(bytes(range(256)) * 64)
    return g


def sdk_of(eboot: Path) -> tuple[int, int]:
    d = eboot.read_bytes()
    base = 64 + 56          # ehdr + one phdr, as the fixture lays it out
    return struct.unpack_from("<I", d, base + 0x14)[0], struct.unpack_from("<I", d, base + 0x10)[0]


def run(*argv: str, timeout: int = 600) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-u", str(CLI), *argv], capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class ChainMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.root = Path(cls.td.name)
        cls.game = make_game(cls.root / "src")
        cls.temp = cls.root / "temp"; cls.temp.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_1_folder_to_folder_without_changes_is_refused(self):
        rc, log = run(str(self.game), str(self.root / "o1"), "--to", "folder")
        self.assertEqual(rc, 1, log)
        self.assertIn("Nothing to do", log)

    def test_2_folder_to_folder_with_backport_changes_in_place(self):
        game = make_game(self.root / "src2")
        rc, log = run(str(game), str(self.root / "o2"), "--to", "folder", "--backport-target", "7.61")
        self.assertEqual(rc, 0, log)
        self.assertIn("Changed in place", log)
        self.assertEqual(sdk_of(game / "eboot.bin"), (SDK_761_PS5, SDK_761_PS4))

    def test_3_folder_to_ffpfsc_passes_through_to_pack(self):
        out = self.root / "o3"; out.mkdir()
        rc, log = run(str(self.game), str(out), "--to", "ffpfsc", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-1500:])
        imgs = list(out.glob("*.ffpfsc"))
        self.assertEqual(len(imgs), 1, log[-800:])
        # The source folder was not changed (no transforms requested).
        self.assertEqual(sdk_of(self.game / "eboot.bin"), (SDK_HIGH_PS5, SDK_HIGH_PS4))
        type(self).ffpfsc = imgs[0]

    def test_4_ffpfsc_to_folder_with_backport(self):
        src = getattr(type(self), "ffpfsc", None) or self._pack_once()
        out = self.root / "o4"; out.mkdir()
        rc, log = run(str(src), str(out), "--to", "folder", "--backport-target", "7.61",
                      "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-1500:])
        dest = out / f"{src.stem}_extracted"
        eboot = next(dest.rglob("eboot.bin"), None)
        self.assertIsNotNone(eboot, f"no eboot under {dest}: {log[-800:]}")
        self.assertEqual(sdk_of(eboot), (SDK_761_PS5, SDK_761_PS4))
        self.assertTrue(src.is_file(), "the source image must be left in place")
        self.assertFalse(list((self.temp / "_ffpfsc_temp").glob("chain-*")), "scratch must be cleaned up")

    def test_5_ffpfsc_to_ffpfsc_with_backport_is_one_job(self):
        """The user's case: an existing .ffpfsc lowered to 7.61 and repacked, one call."""
        src = getattr(type(self), "ffpfsc", None) or self._pack_once()
        before = src.read_bytes()
        out = self.root / "o5"; out.mkdir()
        rc, log = run(str(src), str(out), "--to", "ffpfsc", "--backport-target", "7.61",
                      "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-1500:])
        built = list(out.glob("*.ffpfsc"))
        self.assertEqual(len(built), 1, log[-800:])
        self.assertEqual(src.read_bytes(), before, "the source image must be untouched")
        self.assertIn("CHAIN:", log); self.assertIn("backport 7.61", log)
        # Unpack the result and check the lowered SDK survived the repack.
        chk = self.root / "o5u"
        rc, log2 = run(str(built[0]), str(chk), "--unpack", "--overwrite", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log2[-1000:])
        eboot = next(chk.rglob("eboot.bin"), None)
        self.assertIsNotNone(eboot, log2[-800:])
        self.assertEqual(sdk_of(eboot), (SDK_761_PS5, SDK_761_PS4))

    def test_6_same_format_without_changes_is_a_copy(self):
        """Same container format, nothing to change → the copy job. By default the source
        stays (a clone on the same drive); --copy-mode move renames it into the output."""
        src = getattr(type(self), "ffpfsc", None) or self._pack_once()
        out = self.root / "o6"; out.mkdir()
        rc, log = run(str(src), str(out), "--to", "ffpfsc")
        self.assertEqual(rc, 0, log[-1000:])
        self.assertTrue((out / src.name).is_file(), log[-800:])
        self.assertTrue(src.is_file(), "keep (the default): the source stays")
        self.assertIn("[JOB] copy", log)
        out2 = self.root / "o6m"; out2.mkdir()
        rc, log = run(str(src), str(out2), "--to", "ffpfsc", "--copy-mode", "move")
        self.assertEqual(rc, 0, log[-1000:])
        self.assertTrue((out2 / src.name).is_file(), log[-800:])
        self.assertIn("move", log.lower())
        self.assertFalse(src.is_file(), "move: the source is renamed into the output")

    def test_7_sdk_of_reads_the_firmware_through_the_image(self):
        """The firmware a game needs, read from eboot.bin inside a packed image (headers
        only) and from a folder: the organized file name carries it as [fwN.NN]."""
        src = getattr(type(self), "ffpfsc", None)
        if not (src and src.is_file()):                   # test 6 moved it into its output
            src = self._pack_once()
        for target in (src, self.game):
            rc, log = run("--sdk-of", str(target))
            self.assertEqual(rc, 0, log[-800:])
            info = json.loads(next(l for l in log.splitlines() if l.startswith("SDK_JSON: "))[len("SDK_JSON: "):])
            self.assertEqual(info["fw"], "8.00")                # SDK_HIGH_PS5 = 0x08000041
            self.assertEqual((info["ps5"], info["ps4"]), (SDK_HIGH_PS5, SDK_HIGH_PS4))

    def _pack_once(self) -> Path:
        out = self.root / "o3"; out.mkdir(exist_ok=True)
        rc, log = run(str(self.game), str(out), "--to", "ffpfsc", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-1500:])
        img = next(out.glob("*.ffpfsc"))
        type(self).ffpfsc = img
        return img


class BackportRules(unittest.TestCase):
    """folder → folder + backport, the cheapest chain: every rule of the backport pass
    through the real CLI, without a container round trip."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.fw = self.root / "firmware"
        # 7.61 lacks one of the two functions the game uses; 9.60 has both.
        for name, words, nids in (("7.61", (0x10790001, 0x07610000), ["AAAAAAAAAAA"]),
                                  ("9.60", (0x11590001, 0x09600004), ["AAAAAAAAAAA", "BBBBBBBBBBB"])):
            _fw_lib(self.fw / name, *words)
            lib = self.fw / name / "system" / "common" / "lib" / "libSceX.sprx"
            lib.write_bytes(_elf_with_exports([(n, 0, "libSceX", 0, "libSceX") for n in nids]))

    def tearDown(self):
        self._td.cleanup()

    def game(self, name: str = "g") -> Path:
        g = make_game(self.root / name)
        (g / "sce_module").mkdir()
        (g / "sce_module" / "libGame.prx").write_bytes(_elf_with_imports([
            ("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX"),
            ("BBBBBBBBBBB", 0, "libSceX", 0, "libSceX"),
        ]))
        return g

    def backport(self, game: Path, target: str, *extra: str) -> tuple[int, str]:
        return run(str(game), str(self.root / "out"), "--to", "folder", "--backport-target", target, *extra)

    def test_a_firmware_folder_is_a_target_with_its_own_sdk_words(self):
        g = self.game()
        # A 10.xx game: 9.60 is lower than its SDK (the 8.00 default game would stay as it is).
        (g / "eboot.bin").write_bytes(_elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0x12090001, 0x10000040))
        rc, log = self.backport(g, "9.60", "--fw-libs-root", str(self.fw))
        self.assertEqual(rc, 0, log[-1500:])
        self.assertIn("Lowering the SDK is enough", log)
        self.assertEqual(sdk_of(g / "eboot.bin"), (0x09600004, 0x11590001))

    def test_missing_functions_make_patched_libraries_mandatory(self):
        g = self.game()
        before = (g / "eboot.bin").read_bytes()
        rc, log = self.backport(g, "7.61", "--fw-libs-root", str(self.fw))
        self.assertNotEqual(rc, 0, log[-1500:])
        self.assertIn("patched libraries for 7.61", log)
        self.assertEqual((g / "eboot.bin").read_bytes(), before, "refused before anything changed")

        libs = self.root / "patched" / "7.61"; libs.mkdir(parents=True)
        (libs / "libSceX.sprx").write_bytes(_elf_with_exports([("BBBBBBBBBBB", 0, "libSceX", 0, "libSceX")]))
        rc, log = self.backport(g, "7.61", "--fw-libs-root", str(self.fw),
                                "--backport-libs", str(self.root / "patched"))
        self.assertEqual(rc, 0, log[-1500:])
        self.assertIn("with your patched libraries", log)
        self.assertTrue((g / "fakelib" / "libSceX.sprx").is_file(), "the 7.61 set lands in fakelib/")
        self.assertEqual(sdk_of(g / "eboot.bin"), (SDK_761_PS5, SDK_761_PS4))

    def test_the_firmware_folder_as_patched_libraries_is_refused(self):
        g = self.game()
        before = (g / "eboot.bin").read_bytes()
        rc, log = self.backport(g, "9.60", "--fw-libs-root", str(self.fw), "--backport-libs", str(self.fw))
        self.assertNotEqual(rc, 0, log[-1500:])
        self.assertIn("original libraries", log)
        self.assertEqual((g / "eboot.bin").read_bytes(), before)
        self.assertFalse((g / "fakelib").exists())

    def test_without_firmware_files_the_check_is_skipped_with_a_warning(self):
        g = self.game()
        rc, log = self.backport(g, "7.61")
        self.assertEqual(rc, 0, log[-1500:])
        self.assertIn("were not checked", log)

    def test_an_encrypted_executable_stops_the_job(self):
        g = self.game()
        locked = _encrypt_flag(_fself(_signable_elf(), ps5=True))
        (g / "eboot.bin").write_bytes(locked)
        rc, log = self.backport(g, "7.61")
        self.assertNotEqual(rc, 0, log[-1500:])
        self.assertIn("encrypted", log)
        self.assertEqual((g / "eboot.bin").read_bytes(), locked)

    def test_a_fake_signed_executable_is_lowered_in_place(self):
        from backend import self_file
        g = self.game()
        (g / "eboot.bin").write_bytes(_fself(_signable_elf(0x12090001, 0x10000040), ps5=True))
        rc, log = self.backport(g, "7.61")
        self.assertEqual(rc, 0, log[-1500:])
        self.assertIn("fake-signed, changed in place", log)
        image = self_file.elf_image((g / "eboot.bin").read_bytes())
        self.assertEqual(_param_words(image), (SDK_761_PS4, SDK_761_PS5))

    def test_a_bad_target_is_rejected_by_the_parser(self):
        rc, log = self.backport(self.game(), "8.6")
        self.assertEqual(rc, 2, log[-800:])

    def _image_of(self, g: Path) -> Path:
        out = self.root / "img"
        rc, log = run(str(g), str(out), "--to", "ffpfsc", "--temp-dir", str(self.root / "t"))
        self.assertEqual(rc, 0, log[-1500:])
        return next(out.glob("*.ffpfsc"))

    def test_a_container_is_checked_before_it_is_unpacked(self):
        # the check reads only the executables out of the image: a refusal comes before the
        # whole game is unpacked, and nothing is left in the scratch
        img = self._image_of(self.game())
        rc, log = run(str(img), str(self.root / "out2"), "--to", "ffpfsc", "--backport-target", "7.61",
                      "--fw-libs-root", str(self.fw), "--temp-dir", str(self.root / "t"))
        self.assertNotEqual(rc, 0, log[-1500:])
        self.assertIn("on the executables of", log)
        self.assertIn("patched libraries for 7.61", log)
        self.assertNotIn("before the next step", log, "refused before the unpack")
        self.assertFalse(list((self.root / "t" / "_ffpfsc_temp").glob("chain-*")))
        rc, log = run(str(img), str(self.root / "out3"), "--to", "folder", "--backport-target", "9.60",
                      "--fw-libs-root", str(self.fw), "--temp-dir", str(self.root / "t"))
        self.assertEqual(rc, 0, log[-1500:])
        self.assertIn("Lowering the SDK is enough", log)

    def test_analyse_reads_a_container(self):
        img = self._image_of(self.game())
        rc, log = run("--backport-analyze", str(img), "--backport-target", "7.61", "--fw-libs-root", str(self.fw))
        self.assertEqual(rc, 1, log)
        self.assertIn("[verdict] The game uses 1 function(s) that 7.61 lacks (libSceX)", log)
        rc, log = run("--backport-analyze", str(img), "--backport-target", "9.60", "--fw-libs-root", str(self.fw))
        self.assertEqual(rc, 0, log)

    def test_analyse_prints_one_verdict_and_exits_by_it(self):
        g = self.game()
        rc, log = run("--backport-analyze", str(g), "--backport-target", "7.61", "--fw-libs-root", str(self.fw))
        self.assertEqual(rc, 1, log)
        self.assertIn("[verdict] The game uses 1 function(s) that 7.61 lacks (libSceX)", log)
        rc, log = run("--backport-analyze", str(g), "--backport-target", "9.60", "--fw-libs-root", str(self.fw))
        self.assertEqual(rc, 0, log)
        self.assertIn("[verdict] Firmware 9.60 has every function", log)


if __name__ == "__main__":
    unittest.main()
