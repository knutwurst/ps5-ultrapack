"""Unit tests for the bundled UnRAR module (backend/unrar: rarfile.py over the _unrar C++ extension)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from unrar import rarfile  # noqa: E402
except ImportError as exc:                     # extension not built: skip loudly, do not crash
    rarfile = None
    IMPORT_ERROR: str | None = str(exc)
else:
    IMPORT_ERROR = None

SAMPLE_RAR = Path(__file__).resolve().parents[1] / "test_data" / "sample.rar"
RAR_CLI = shutil.which("rar")
NOT_BUILT = (f"unrar extension not importable ({IMPORT_ERROR}); build it with: "
             "cd backend/unrar && python3 setup.py build_ext --inplace")


def _skip(reason: str) -> None:
    print(f"SKIP test_unrar: {reason}", file=sys.stderr)
    raise unittest.SkipTest(reason)


@unittest.skipIf(rarfile is None, NOT_BUILT)
class RarfileApiTests(unittest.TestCase):
    """No fixture needed."""

    def test_api_surface(self):
        for name in ("RarFile", "RarInfo", "BadRarFile", "RarWrongPassword",
                     "NeedFirstVolume", "RarExtractionCancelled"):
            self.assertTrue(hasattr(rarfile, name), name)

    def test_bad_archive_raises_badrarfile(self):
        with tempfile.NamedTemporaryFile(suffix=".rar", delete=False) as f:
            f.write(b"not a rar file")
            bad_path = f.name
        try:
            with self.assertRaises(rarfile.BadRarFile):
                rarfile.RarFile(bad_path).infolist()
        finally:
            os.unlink(bad_path)


@unittest.skipIf(rarfile is None, NOT_BUILT)
class RarfileArchiveTests(unittest.TestCase):
    """Listing and extraction against a real archive: the shipped fixture, or one
    generated with the `rar` tool when that is available."""

    fixture: Path
    expected: dict[str, int]          # member -> size; known only for a generated fixture
    _tmp: tempfile.TemporaryDirectory | None = None

    @classmethod
    def setUpClass(cls):
        cls.expected = {}
        if SAMPLE_RAR.is_file():
            cls.fixture = SAMPLE_RAR
            return
        if RAR_CLI is None:
            _skip(f"no fixture: {SAMPLE_RAR} is absent and no `rar` command line tool is on "
                  "PATH to generate one")
        cls._tmp = tempfile.TemporaryDirectory(prefix="unrar_fixture_")
        root = Path(cls._tmp.name)
        src = root / "src"
        (src / "sub").mkdir(parents=True)
        files = {"hello.txt": b"hello, rar\n" * 100,
                 "sub/data.bin": bytes(range(256)) * 64,
                 "empty.txt": b""}
        for rel, blob in files.items():
            (src / rel).write_bytes(blob)
        cls.fixture = root / "sample.rar"
        proc = subprocess.run([RAR_CLI, "a", "-r", "-idq", str(cls.fixture), "hello.txt", "sub", "empty.txt"],
                              cwd=src, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not cls.fixture.is_file():
            cls._tmp.cleanup()
            cls._tmp = None
            _skip(f"`rar a` failed (rc={proc.returncode}): {(proc.stdout + proc.stderr)[-300:]}")
        cls.expected = {rel: len(blob) for rel, blob in files.items()}
        print(f"test_unrar: generated fixture {cls.fixture} with {RAR_CLI}", file=sys.stderr)

    @classmethod
    def tearDownClass(cls):
        if cls._tmp is not None:
            cls._tmp.cleanup()

    def test_list(self):
        rf = rarfile.RarFile(self.fixture)
        names = rf.namelist()
        infos = rf.infolist()
        self.assertGreater(len(names), 0)
        self.assertEqual(len(infos), len(names))
        for info in infos:
            self.assertIsInstance(info.filename, str)
            self.assertGreaterEqual(info.file_size, 0)
            self.assertIsInstance(info.isdir(), bool)
        if self.expected:
            got = {os.path.normpath(i.filename): i.file_size for i in infos if not i.isdir()}
            self.assertEqual(got, {os.path.normpath(k): v for k, v in self.expected.items()})

    def test_extractall(self):
        rf = rarfile.RarFile(self.fixture)
        with tempfile.TemporaryDirectory() as tmpdir:
            rf.extractall(tmpdir)
            for info in rf.infolist():
                if info.isdir():
                    continue
                extracted = Path(tmpdir) / info.filename
                self.assertTrue(extracted.is_file(), info.filename)
                self.assertEqual(extracted.stat().st_size, info.file_size, info.filename)


@unittest.skipIf(rarfile is None, NOT_BUILT)
class MultiPartSets(unittest.TestCase):
    """A multi-part RAR 5 set with encrypted headers, brackets in its name and a dot in its
    password, built with the `rar` tool: a password that works opens it among wrong ones,
    a wrong one reads as locked, and a damaged or incomplete set reads as damaged, with a
    reason, instead of asking for a password no one can give."""

    PASSWORD = "EXAMPLE.ORG"
    NAME = "[EXAMPLE.ORG]-PPSA00001"

    @classmethod
    def setUpClass(cls):
        if RAR_CLI is None:
            _skip("no `rar` command line tool on PATH to build a multi-part set")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from ultra_core import ArchiveExtractor
        cls.AE = ArchiveExtractor
        cls._tmp = tempfile.TemporaryDirectory(prefix="unrar_sets_")
        root = Path(cls._tmp.name)
        game = root / "src" / "PPSA00001"
        (game / "sce_sys").mkdir(parents=True)
        (game / "sce_sys" / "param.json").write_text('{"titleId": "PPSA00001"}')
        (game / "eboot.bin").write_bytes(b"\x7fELF" + os.urandom(400_000))
        cls.sets = {}
        for kind in ("ok", "missing", "damaged"):
            d = root / kind
            d.mkdir()
            proc = subprocess.run([RAR_CLI, "a", "-ma5", f"-hp{cls.PASSWORD}", "-v120k", "-m1", "-idq",
                                   str(d / f"{cls.NAME}.rar"), "PPSA00001"],
                                  cwd=game.parent, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                _skip(f"`rar a` failed (rc={proc.returncode}): {(proc.stdout + proc.stderr)[-300:]}")
            cls.sets[kind] = d / f"{cls.NAME}.part1.rar"
        (root / "missing" / f"{cls.NAME}.part2.rar").unlink()
        part = root / "damaged" / f"{cls.NAME}.part3.rar"
        blob = bytearray(part.read_bytes())
        blob[len(blob) // 2:len(blob) // 2 + 4096] = bytes(4096)
        part.write_bytes(blob)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_saved_password_opens_it_among_wrong_ones(self):
        state, size, _ = self.AE.probe_header_state(self.sets["ok"], ["wrong", " ", self.PASSWORD])
        self.assertEqual(state, "open")
        self.assertGreater(size, 400_000)

    def test_a_wrong_password_reads_as_locked(self):
        self.assertEqual(self.AE.probe_header_state(self.sets["ok"], ["wrong"])[0], "locked")
        self.assertEqual(self.AE.probe_header_state(self.sets["ok"], [])[0], "locked")

    def test_a_missing_part_is_damaged_and_named(self):
        state, _size, reason = self.AE.probe_header_state(self.sets["missing"], [self.PASSWORD])
        self.assertEqual(state, "damaged")
        self.assertIn(f"{self.NAME}.part2.rar", reason)

    def _extract(self, arc: Path, passwords) -> str:
        """The error message of a failed extraction, run on a thread: a missing part must
        end with an error, not wait for the part to appear (UnRAR's volume callback)."""
        import threading
        out = {}
        def work():
            with tempfile.TemporaryDirectory() as td:
                try:
                    self.AE.extract_with_passwords(arc, Path(td), passwords)
                    out["msg"] = ""
                except Exception as e:
                    out["msg"] = str(e)
        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(60)
        self.assertFalse(th.is_alive(), "extraction hangs")
        return out["msg"]

    def test_extraction_names_the_damage_not_a_password(self):
        msg = self._extract(self.sets["missing"], ["wrong", self.PASSWORD])
        self.assertIn("part2.rar", msg)
        self.assertNotIn("password", msg.lower())
        msg = self._extract(self.sets["damaged"], [self.PASSWORD])
        self.assertIn("damaged", msg)
        self.assertNotIn("password", msg.lower())

    def test_a_part_that_vanishes_during_extraction_ends_with_an_error(self):
        # the listing has read the whole set; then a part goes away (a drive that drops
        # out or sleeps). UnRAR asks for it; the answer must be "give up", or it waits
        # for the part forever.
        import threading
        root = Path(tempfile.mkdtemp(prefix="unrar_vanish_"))
        try:
            for f in self.sets["ok"].parent.iterdir():
                shutil.copy2(f, root / f.name)
            rf = rarfile.RarFile(root / self.sets["ok"].name, pwd=self.PASSWORD)
            rf.infolist()
            (root / f"{self.NAME}.part3.rar").unlink()
            out = {}
            def work():
                try:
                    rf.extractall(root / "out")
                    out["err"] = None
                except Exception as e:
                    out["err"] = e
            th = threading.Thread(target=work, daemon=True)
            th.start()
            th.join(60)
            self.assertFalse(th.is_alive(), "extraction waits for the missing part forever")
            self.assertIsInstance(out["err"], rarfile.BadRarFile)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_game_param_is_read_alone_before_extraction(self):
        data = self.AE.read_game_param(self.sets["ok"], ["wrong", self.PASSWORD])
        self.assertEqual(data, b'{"titleId": "PPSA00001"}')
        self.assertIsNone(self.AE.read_game_param(self.sets["ok"], ["wrong"]))

    def test_a_solid_set_is_not_read_ahead(self):
        root = Path(tempfile.mkdtemp(prefix="unrar_solid_"))
        try:
            game = self.sets["ok"].parents[1] / "src"
            proc = subprocess.run([RAR_CLI, "a", "-ma5", "-s", f"-p{self.PASSWORD}", "-idq",
                                   str(root / "solid.rar"), "PPSA00001"], cwd=game, capture_output=True, timeout=120)
            self.assertEqual(proc.returncode, 0)
            with self.assertRaises(rarfile.SolidArchive):
                rarfile.RarFile(root / "solid.rar", pwd=self.PASSWORD).read("PPSA00001/sce_sys/param.json")
            self.assertIsNone(self.AE.read_game_param(root / "solid.rar", [self.PASSWORD]))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_right_password_extracts_the_set(self):
        self.assertEqual(self._extract(self.sets["ok"], ["wrong", self.PASSWORD]), "")


if __name__ == "__main__":
    unittest.main()
