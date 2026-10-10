"""Unit tests for ArchiveExtractor - the saved-password loop (a wrong first password
must not stop the right one from being tried), AES-encrypted zips (stdlib zipfile
cannot read them; they go through the 7-Zip CLI), multi-root archives (a base game
beside its patch) - and the settings redaction of the diagnostics export.

Headless: the GUI module is loaded with exec_module (importing it creates no Tk
window). Cases that need the native 7-Zip CLI are skipped when `7zz` is not on
PATH; the others author their archives with py7zr / zipfile and drive the CLI
code path with a fake tool script.

  /tmp/ps5venv/bin/python -m unittest backend/tests/test_archive_extractor.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

# Never touch the real profile: the app dir is computed (and an old one migrated) at
# import time, so point it at a scratch folder first.
os.environ.setdefault("PS5_FFPFSC_APP_DIR", tempfile.mkdtemp(prefix="ultrapack-test-profile-"))
_spec = importlib.util.spec_from_file_location("ultra_under_test", str(REPO / "PS5_UltraPack.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

SEVENZZ = shutil.which("7zz")
try:
    import py7zr
except ImportError:          # pragma: no cover - the venv has it
    py7zr = None

AE = m.ArchiveExtractor


def _make_game_tree(root: Path, name: str = "GAME") -> Path:
    game = root / name
    (game / "sce_sys").mkdir(parents=True)
    (game / "sce_sys" / "param.json").write_text('{"titleId": "PPSA00001"}')
    (game / "eboot.bin").write_bytes(os.urandom(64 * 1024))   # incompressible payload
    # A compressible member too: 7-Zip stores incompressible data as method 0 whatever
    # -mm says, so without this the Deflate64 zip would hold no method-9 member.
    (game / "data.bin").write_bytes(b"compressible line of payload\n" * 8000)
    return game


def _sevenzip(args, cwd):
    r = subprocess.run([SEVENZZ, *args], cwd=cwd, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"7zz {' '.join(args)} failed ({r.returncode}):\n{r.stdout}")


@contextlib.contextmanager
def _native_7z(value):
    """Temporarily make ArchiveExtractor._find_native_7z() return *value*."""
    orig = AE.__dict__["_find_native_7z"]
    AE._find_native_7z = staticmethod(lambda: value)
    try:
        yield
    finally:
        AE._find_native_7z = orig


@contextlib.contextmanager
def _forbid_py7zr():
    """Install a py7zr stand-in that fails loudly if the extractor touches it."""
    def _boom(*_a, **_k):
        raise AssertionError("py7zr must not be used after a CLI data/password error")
    stub = types.ModuleType("py7zr")
    stub.SevenZipFile = _boom
    saved = sys.modules.get("py7zr")
    sys.modules["py7zr"] = stub
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["py7zr"] = saved
        else:
            sys.modules.pop("py7zr", None)


class _Base(unittest.TestCase):
    """Shared fixtures: one game tree, the archives, a fake 7z CLI (sh wrappers)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ffpfsc-extractor-tests-"))
        cls.src = cls.tmp / "src"
        cls.src.mkdir()
        cls.game = _make_game_tree(cls.src)
        cls.archives = cls.tmp / "archives"
        cls.archives.mkdir()
        if SEVENZZ:
            _sevenzip(["a", "-pRIGHT", "-mhe=on", str(cls.archives / "he.7z"), "GAME"], cls.src)
            _sevenzip(["a", "-pRIGHT", str(cls.archives / "plain.7z"), "GAME"], cls.src)
            _sevenzip(["a", str(cls.archives / "nopw.7z"), "GAME"], cls.src)
            _sevenzip(["a", "-tzip", "-pRIGHT", "-mem=AES256", str(cls.archives / "aes.zip"), "GAME"], cls.src)
            _sevenzip(["a", "-tzip", "-pRIGHT", str(cls.archives / "zipcrypto.zip"), "GAME"], cls.src)
            _sevenzip(["a", "-tzip", "-mm=Deflate64", str(cls.archives / "deflate64.zip"), "GAME"], cls.src)
        if py7zr is not None:
            for name, pw, he in [("py_plain.7z", "RIGHT", False), ("py_he.7z", "RIGHT", True),
                                 ("py_nopw.7z", None, False)]:
                kw = {"password": pw} if pw else {}
                with py7zr.SevenZipFile(str(cls.archives / name), "w", **kw) as sz:
                    if he:
                        sz.set_encrypted_header(True)
                    sz.writeall(str(cls.game), "GAME")
        # Fake 7-Zip CLI: prints canned output and exits with the code of its mode.
        cls.fake = cls.tmp / "fake7z.py"
        cls.fake.write_text(
            "import sys\n"
            "mode = sys.argv[1]\n"
            "if mode == 'wrongpw':\n"
            "    print('  0%'); print('ERROR: Wrong password : GAME/eboot.bin')\n"
            "    print('Sub items Errors: 1'); sys.exit(2)\n"
            "if mode == 'data':\n"
            "    print('  0%'); print('ERROR: Data Error : passwords.txt'); sys.exit(2)\n"
            "if mode == 'cmdline':\n"
            "    print('Command Line Error:'); print('Unsupported switch postfix -bsp1'); sys.exit(7)\n"
            "if mode == 'prompt':\n"
            "    print('Enter password:'); print('Break signaled'); sys.exit(255)\n"
            "print('Everything is Ok'); sys.exit(0)\n")
        cls.wrappers = {}
        if os.name != "nt":
            for mode in ("wrongpw", "data", "cmdline", "prompt", "ok"):
                w = cls.tmp / f"fake7z_{mode}"
                w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{cls.fake}" {mode} "$@"\n')
                w.chmod(0o755)
                cls.wrappers[mode] = str(w)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.dest_root = Path(tempfile.mkdtemp(prefix="dest-", dir=self.tmp))
        self.logs = []

    def log(self, level, msg):
        self.logs.append((level, msg))

    def _log_text(self):
        return "\n".join(f"{lvl} {msg}" for lvl, msg in self.logs)

    def _assert_game_root(self, root: Path):
        self.assertTrue((root / "sce_sys" / "param.json").is_file(), f"no param.json under {root}")
        self.assertTrue((root / "eboot.bin").is_file())


# ── item 1: the password loop reaches the right password ──────────────────────

@unittest.skipUnless(SEVENZZ, "7zz not on PATH")
class NativeCliPasswordLoop(_Base):

    def test_header_encrypted_7z_wrong_then_right(self):
        root = AE.extract_with_passwords(self.archives / "he.7z", self.dest_root, ["WRONG", "RIGHT"],
                                         log_fn=self.log)
        self._assert_game_root(root)
        self.assertIn("did not match", self._log_text())

    def test_plain_7z_wrong_then_right_with_progress(self):
        progress = []
        root = AE.extract_with_passwords(self.archives / "plain.7z", self.dest_root, ["WRONG", "RIGHT"],
                                         log_fn=self.log, progress_fn=lambda p, n: progress.append(p))
        self._assert_game_root(root)
        self.assertTrue(progress and progress[-1] == 100)

    def test_all_wrong_ends_with_password_summary_not_a_hang(self):
        # Also covers the trailing no-password attempt: an empty -p must fail fast
        # (exit 2) instead of prompting for a password.
        with self.assertRaises(RuntimeError) as cm:
            AE.extract_with_passwords(self.archives / "he.7z", self.dest_root, ["A", "B"], log_fn=self.log)
        self.assertIn("none of the 2 saved password(s) worked", str(cm.exception))

    def test_password_switch_is_a_noop_on_unencrypted_7z(self):
        root = AE.extract_with_passwords(self.archives / "nopw.7z", self.dest_root, ["WRONG"], log_fn=self.log)
        self._assert_game_root(root)
        root = AE.extract_with_passwords(self.archives / "nopw.7z", self.dest_root, [], log_fn=self.log)
        self._assert_game_root(root)

    def test_cli_failure_message_carries_the_tool_output(self):
        with _forbid_py7zr():
            with self.assertRaises(m.ArchivePasswordError) as cm:
                AE.extract(self.archives / "plain.7z", self.dest_root, log_fn=self.log, password="WRONG")
        self.assertIn("Wrong password", str(cm.exception))
        self.assertTrue(AE._is_password_error(cm.exception))


class FakeCliClassification(_Base):
    """_run_extract_process / _sevenz decisions, driven by a fake tool - no 7zz needed."""

    def _run(self, mode):
        return AE._run_extract_process([sys.executable, str(self.fake), mode], "faketool", log_fn=self.log)

    def test_wrong_password_output_raises_password_error(self):
        with self.assertRaises(m.ArchivePasswordError) as cm:
            self._run("wrongpw")
        self.assertIn("Wrong password", str(cm.exception))
        self.assertTrue(AE._is_password_error(cm.exception))

    def test_prompt_for_missing_password_is_a_password_error(self):
        with self.assertRaises(m.ArchivePasswordError):
            self._run("prompt")

    def test_data_error_is_a_tool_error_with_exit_code(self):
        with self.assertRaises(m.ArchiveToolError) as cm:
            self._run("data")
        self.assertEqual(cm.exception.returncode, 2)
        self.assertIn("Data Error", str(cm.exception))
        # 'passwords.txt' in the output must not be misread as a password verdict
        self.assertFalse(AE._is_password_error(cm.exception))

    def test_command_line_error_keeps_exit_code_7(self):
        with self.assertRaises(m.ArchiveToolError) as cm:
            self._run("cmdline")
        self.assertEqual(cm.exception.returncode, 7)

    def test_success_returns_quietly(self):
        self.assertIsNone(self._run("ok"))

    @unittest.skipUnless(py7zr is not None and os.name != "nt", "needs py7zr and sh")
    def test_sevenz_does_not_retry_with_py7zr_after_a_data_error(self):
        with _native_7z(self.wrappers["data"]), _forbid_py7zr():
            with self.assertRaises(m.ArchiveToolError):
                AE.extract(self.archives / "py_plain.7z", self.dest_root, log_fn=self.log, password="RIGHT")

    @unittest.skipUnless(py7zr is not None and os.name != "nt", "needs py7zr and sh")
    def test_sevenz_falls_back_to_py7zr_only_on_command_line_error(self):
        with _native_7z(self.wrappers["cmdline"]):
            root = AE.extract_with_passwords(self.archives / "py_plain.7z", self.dest_root, ["RIGHT"],
                                             log_fn=self.log)
        self._assert_game_root(root)
        self.assertIn("falling back to py7zr", self._log_text())


@unittest.skipUnless(py7zr is not None, "py7zr not installed")
class Py7zrPasswordClassification(_Base):

    def test_wrong_then_right_password_plain(self):
        with _native_7z(None):
            root = AE.extract_with_passwords(self.archives / "py_plain.7z", self.dest_root, ["WRONG", "RIGHT"],
                                             log_fn=self.log)
        self._assert_game_root(root)

    def test_wrong_then_right_password_header_encrypted(self):
        with _native_7z(None):
            root = AE.extract_with_passwords(self.archives / "py_he.7z", self.dest_root, ["WRONG", "RIGHT"],
                                             log_fn=self.log)
        self._assert_game_root(root)

    def test_corrupt_archive_without_password_is_not_a_password_error(self):
        broken = self.tmp / "py_broken.7z"
        data = bytearray((self.archives / "py_nopw.7z").read_bytes())
        for i in range(48, 4096, 7):          # scramble the packed stream, keep the signature header
            data[i] ^= 0x5A
        broken.write_bytes(data)
        with _native_7z(None):
            with self.assertRaises(Exception) as cm:
                AE.extract_with_passwords(broken, self.dest_root, [], log_fn=self.log)
        self.assertNotIsInstance(cm.exception, m.ArchivePasswordError)
        self.assertNotIn("saved password", str(cm.exception))


# ── item 2: AES-encrypted zips ─────────────────────────────────────────────────

@unittest.skipUnless(SEVENZZ, "7zz not on PATH")
class AesZip(_Base):

    def test_aes_zip_wrong_then_right(self):
        root = AE.extract_with_passwords(self.archives / "aes.zip", self.dest_root, ["WRONG", "RIGHT"],
                                         log_fn=self.log)
        self._assert_game_root(root)
        self.assertIn("AES-encrypted ZIP", self._log_text())

    def test_zipcrypto_zip_still_uses_stdlib(self):
        with _native_7z(None):     # no CLI available: ZipCrypto must still work via zipfile
            root = AE.extract_with_passwords(self.archives / "zipcrypto.zip", self.dest_root, ["WRONG", "RIGHT"],
                                             log_fn=self.log)
        self._assert_game_root(root)

    def test_aes_zip_without_cli_is_a_clear_non_password_error(self):
        with _native_7z(None):
            with self.assertRaises(RuntimeError) as cm:
                AE.extract_with_passwords(self.archives / "aes.zip", self.dest_root, ["RIGHT"], log_fn=self.log)
        self.assertIn("AES-encrypted ZIP", str(cm.exception))
        self.assertIn("7zz", str(cm.exception))
        self.assertFalse(AE._is_password_error(cm.exception))

    def test_unsupported_zip_method_goes_through_the_cli(self):
        root = AE.extract_with_passwords(self.archives / "deflate64.zip", self.dest_root, [], log_fn=self.log)
        self._assert_game_root(root)
        self.assertIn("cannot read", self._log_text())


# ── item 3: _find_root with several game roots ─────────────────────────────────

class FindRoot(unittest.TestCase):

    def setUp(self):
        self.dest = Path(tempfile.mkdtemp(prefix="findroot-"))
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.dest, ignore_errors=True)

    def _game(self, rel):
        g = self.dest / rel
        (g / "sce_sys").mkdir(parents=True)
        (g / "sce_sys" / "param.json").write_text("{}")
        return g

    def test_single_nested_game(self):
        g = self._game("Release/GAME")
        self.assertEqual(AE._find_root(self.dest, log_fn=lambda *a: self.logs.append(a)), g)
        self.assertEqual(self.logs, [])

    def test_base_and_patch_side_by_side_return_their_parent(self):
        self._game("Release/PPSA00001-app")
        self._game("Release/PPSA00001-patch")
        root = AE._find_root(self.dest, log_fn=lambda *a: self.logs.append(a))
        self.assertEqual(root, self.dest / "Release")
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(self.logs[0][0], "WARN")
        self.assertIn("2 game roots", self.logs[0][1])

    def test_side_by_side_at_top_level_return_dest(self):
        self._game("PPSA00001-app")
        self._game("PPSA00001-patch")
        self.assertEqual(AE._find_root(self.dest), self.dest)

    def test_shallowest_level_wins_over_deeper_marker(self):
        g = self._game("GAME")
        self._game("GAME/extras/BONUS")
        self.assertEqual(AE._find_root(self.dest), g)

    def test_title_id_and_unwrap_fallbacks_unchanged(self):
        (self.dest / "PPSA12345-app").mkdir()
        self.assertEqual(AE._find_root(self.dest), self.dest / "PPSA12345-app")
        shutil.rmtree(self.dest / "PPSA12345-app")
        (self.dest / "only").mkdir()
        self.assertEqual(AE._find_root(self.dest), self.dest / "only")
        (self.dest / "second").mkdir()
        self.assertEqual(AE._find_root(self.dest), self.dest)


# ── item 4: diagnostics export never carries passwords ─────────────────────────

class SettingsRedaction(unittest.TestCase):

    def test_redaction(self):
        settings = {
            "archive_passwords": ["s3cret-one", "s3cret-two"],
            "password": "s3cret-field",
            "temp_folder": "/Volumes/T",
            "queue": [{"path": "/g/a", "password": "s3cret-item"},
                      {"path": "/g/b", "password": ""},
                      {"path": "/g/c"}],
            "nested": {"Default_Password": "s3cret-nested", "keep": 1},
        }
        out = m._redact_settings_for_export(settings)
        self.assertNotIn("archive_passwords", out)
        self.assertEqual(out["password"], "<redacted>")
        self.assertEqual(out["temp_folder"], "/Volumes/T")
        self.assertEqual([q.get("password") for q in out["queue"]], ["<redacted>", "", None])
        self.assertEqual(out["queue"][0]["path"], "/g/a")
        self.assertEqual(out["nested"], {"Default_Password": "<redacted>", "keep": 1})
        self.assertNotIn("s3cret", json.dumps(out))
        self.assertIn("s3cret", json.dumps(settings))   # the input itself is untouched


class PasswordErrorRecognition(unittest.TestCase):

    def test_new_types(self):
        self.assertTrue(AE._is_password_error(m.ArchivePasswordError("anything")))
        self.assertFalse(AE._is_password_error(
            m.ArchiveToolError("7zz exited with code 2 — extraction failed.\n  ERROR: Data Error : passwords.txt",
                               returncode=2)))
        self.assertFalse(AE._is_password_error(PermissionError("denied")))


class HeaderProbe(unittest.TestCase):
    """probe_header separates two answers: did a password open the header, and is the
    size in it worth trusting. Only the first may lead to a password prompt."""

    def test_a_damaged_zip_is_damaged_not_locked(self):
        with tempfile.TemporaryDirectory() as td:
            z = Path(td) / "broken.zip"
            z.write_bytes(b"PK\x03\x04" + b"\0" * 200)
            state, size, reason = AE.probe_header_state(z, ["pw"])
        self.assertEqual((state, size), ("damaged", 0))
        self.assertIn("damaged or incomplete", reason)

    def test_the_game_param_is_read_out_of_a_zip_alone(self):
        import json as _json
        pj = _json.dumps({"titleId": "PPSA00001", "localizedParameters": {
            "defaultLanguage": "en-US", "en-US": {"titleName": "Example Quest"}}}).encode()
        with tempfile.TemporaryDirectory() as td:
            z = Path(td) / "[site.example]-PPSA00001.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("Game/DLC/sce_sys/param.json", b'{"titleId": "PPSA99999"}')   # deeper: not the game
                zf.writestr("Game/sce_sys/param.json", pj)
                zf.writestr("Game/eboot.bin", b"x" * 100)
            ident = m.ident_from_param_bytes(AE.read_game_param(z))
        self.assertEqual((ident["title"], ident["title_id"]), ("Example Quest", "PPSA00001"))

    def test_a_solid_7z_is_not_read_ahead(self):
        # one member of a solid 7z costs decompressing everything before it: not worth it
        import py7zr
        with tempfile.TemporaryDirectory() as td:
            s = Path(td) / "game.7z"
            with py7zr.SevenZipFile(s, "w") as sz:
                sz.writestr(b'{"titleId": "PPSA00001"}', "Game/sce_sys/param.json")
                sz.writestr(b"y" * 2000, "Game/eboot.bin")
            self.assertIsNone(AE.read_game_param(s))

    def test_a_gap_in_a_part_set_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            for n in (1, 2, 4):
                (Path(td) / f"[A.B]-X.part{n:02d}.rar").write_bytes(b"x")
            self.assertEqual(AE.volume_gaps(Path(td) / "[A.B]-X.part01.rar"), ["[A.B]-X.part03.rar"])
            for n in ("rar", "r00", "r02"):
                (Path(td) / f"Old.{n}").write_bytes(b"x")
            self.assertEqual(AE.volume_gaps(Path(td) / "Old.rar"), ["Old.r01"])


    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.payload = self.root / "src"
        self.payload.mkdir()
        (self.payload / "game.pkg").write_bytes(os.urandom(300_000))

    def tearDown(self):
        self._td.cleanup()

    def _7z(self, name: str, password: str, header_encryption: bool) -> Path:
        out = self.root / name
        with py7zr.SevenZipFile(out, "w", password=password, header_encryption=header_encryption) as z:
            z.write(self.payload / "game.pkg", "Title/game.pkg")
        return out

    @unittest.skipIf(py7zr is None, "py7zr not installed")
    def test_the_right_password_among_wrong_ones_opens_an_encrypted_header(self):
        arc = self._7z("locked.7z", "the-right-one", header_encryption=True)
        self.assertEqual(AE.probe_header(arc, ["wrong-a", "wrong-b", "the-right-one"]), (True, 300_000))
        self.assertEqual(AE.probe_header(arc, ["wrong-a", "wrong-b"]), (False, 0))

    @unittest.skipIf(py7zr is None, "py7zr not installed")
    def test_a_readable_header_opens_whatever_the_passwords(self):
        arc = self._7z("clear.7z", "the-right-one", header_encryption=False)
        self.assertEqual(AE.probe_header(arc, ["wrong-a"]), (True, 300_000))
        self.assertEqual(AE.probe_header(arc, []), (True, 300_000))

    def test_an_odd_size_is_not_trusted_but_is_no_password_problem(self):
        self.assertEqual(AE.plausible_extracted_size(10, 1000), 0)
        self.assertEqual(AE.plausible_extracted_size(500, 1000), 500)
        self.assertEqual(AE.plausible_extracted_size(990, 1000), 990)      # stored ~1:1 archives count

    @unittest.skipIf(py7zr is None, "py7zr not installed")
    def test_a_new_archive_job_knows_whether_its_header_is_locked(self):
        arc = self._7z("job.7z", "the-right-one", header_encryption=True)
        m.save_settings({"archive_passwords": ["wrong-a", " the-right-one "]})
        try:
            item = m.GameItem.from_archive(arc)
            self.assertFalse(item.header_locked)
            self.assertEqual(item.extracted_size, 300_000)
            m.save_settings({"archive_passwords": ["wrong-a"]})
            item = m.GameItem.from_archive(arc)
            self.assertTrue(item.header_locked)
            self.assertEqual(item.extracted_size, 0)
        finally:
            m.save_settings({"archive_passwords": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
