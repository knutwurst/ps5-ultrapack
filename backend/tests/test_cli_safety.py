"""backend/cli.py never loses, overwrites or consumes what the user owns, and every failure
exits non-zero with an [ERROR] line.

    PYTHONPATH=. .venv/bin/python backend/tests/test_cli_safety.py
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

import cli  # noqa: E402
from test_backport import _signable_elf  # noqa: E402
from test_chain_mode import make_game as make_elf_game  # noqa: E402

CLI = BACKEND / "cli.py"
TITLE_ID = "ABCD01234"
MARKER = b"previous build - must survive"


def make_game(root: Path, name: str = "game") -> Path:
    game = root / name
    (game / "sce_sys").mkdir(parents=True)
    (game / "sce_sys" / "param.json").write_text(json.dumps({
        "titleId": TITLE_ID, "contentId": f"UP0000-{TITLE_ID}_00-TESTTESTTEST0000",
        "contentVersion": "01.000.000", "titleName": "Test"}), encoding="utf-8")
    (game / "eboot.bin").write_bytes(os.urandom(50_000))
    (game / "data.bin").write_bytes(b"\0" * 100_000)
    return game


def run_cli(*argv: str, env: dict | None = None, timeout: int = 300) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-u", str(CLI), *argv], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, errors="replace", timeout=timeout,
                       env={**os.environ, **(env or {})})
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def run_main(*argv: str, fpkg_module=None) -> tuple[int, str]:
    """cli.main() in this process (so a fake package tool can stand in)."""
    saved_argv, saved_mod = sys.argv, sys.modules.get("fpkg")
    if fpkg_module is not None:
        sys.modules["fpkg"] = fpkg_module
    sys.argv = ["cli.py", *argv]
    out, code = io.StringIO(), 0
    try:
        with redirect_stdout(out):
            cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = saved_argv
        if fpkg_module is not None:
            if saved_mod is None:
                sys.modules.pop("fpkg", None)
            else:
                sys.modules["fpkg"] = saved_mod
    return code, out.getvalue()


def tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*"))


class Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cli_safety_")).resolve()
        self.temp = self.root / "temp"
        self.temp.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        for dirpath, dirnames, _f in os.walk(self.root):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def zip_of(self, game: Path, extra: dict | None = None) -> Path:
        z = self.root / "G.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for p in game.rglob("*"):
                if p.is_file():
                    zf.write(p, "Game/" + p.relative_to(game).as_posix())
            for name, data in (extra or {}).items():
                zf.writestr(name, data)
        return z


# ── 1: a nested image that cannot be unwrapped fails the job ─────────────────────
class UnwrapFailures(Case):
    def test_unreadable_nested_exfat_raises(self):
        d = self.root / "unpacked"
        d.mkdir()
        (d / "inner.exfat").write_bytes(b"not an exfat volume" * 100)
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            cli._fully_unwrap(d, None, None)

    def test_several_nested_images_raise(self):
        d = self.root / "unpacked"
        d.mkdir()
        (d / "a.exfat").write_bytes(b"x")
        (d / "b.ffpfs").write_bytes(b"y")
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            cli._fully_unwrap(d, None, None)


# ── 2 + 3: the user's folder keeps its clutter and its extras ───────────────────
class UserFolderUntouched(Case):
    def setUp(self):
        super().setUp()
        self.game = make_game(self.root)
        (self.game / "._eboot.bin").write_bytes(b"\x00\x05\x16\x07" + b"\0" * 60)
        (self.game / ".Trashes").mkdir()
        (self.game / ".Trashes" / "x").write_bytes(b"trash")
        (self.game / "$RECYCLE.BIN").mkdir()
        (self.game / "$RECYCLE.BIN" / "y").write_bytes(b"bin")
        (self.game / "_bundle_").mkdir()
        (self.game / "_bundle_" / "readme.txt").write_text("group notes")
        (self.game / "info.nfo").write_text("nfo")
        self.before = tree(self.game)

    def _pack(self, target: Path, *extra: str) -> tuple[int, str]:
        return run_cli(str(self.game), str(target), "--pack", "--temp-dir", str(self.temp), *extra)

    def test_pack_leaves_the_source_folder_as_it_was(self):
        rc, log = self._pack(self.out / "G.ffpfsc")
        self.assertEqual(rc, 0, log[-2000:])
        self.assertEqual(tree(self.game), self.before)
        # the extras are kept out of the image and copied beside it
        self.assertEqual((self.out / "_bundle_" / "readme.txt").read_text(), "group notes")
        self.assertEqual((self.out / "info.nfo").read_text(), "nfo")
        self.assertFalse([p for p in self.root.iterdir() if "extras" in p.name], list(self.root.iterdir()))

    def test_via_exfat_and_uncompressed_leave_the_source_as_it_was(self):
        rc, log = self._pack(self.out / "G.ffpfsc", "--via-exfat")
        self.assertEqual(rc, 0, log[-2000:])
        self.assertEqual(tree(self.game), self.before)
        rc, log = self._pack(self.out / "G.ffpfs", "--no-compress")
        self.assertEqual(rc, 0, log[-2000:])
        self.assertEqual(tree(self.game), self.before)

    def test_a_failed_pack_puts_the_extras_back(self):
        target = self.out / "G.ffpfsc"
        target.write_bytes(MARKER)
        (self.out / "G.ffpfsc.partial.tmp").mkdir()        # makes pass 2 fail
        rc, log = self._pack(target, "--overwrite")
        self.assertNotEqual(rc, 0, log[-2000:])
        self.assertIn("[ERROR]", log)
        self.assertNotIn("[SUCCESS]", log)
        self.assertEqual(tree(self.game), self.before)
        self.assertEqual(target.read_bytes(), MARKER)
        self.assertFalse((self.out / "_bundle_").exists())
        self.assertFalse([p for p in self.root.iterdir() if "extras" in p.name])

    def test_fpkg_build_from_a_folder_leaves_its_clutter(self):
        fake = types.ModuleType("fpkg")
        fake.is_available = lambda: True

        def build(src, out_dir, **kw):
            (Path(out_dir) / "test.pkg").write_bytes(b"x" * 16)
            return 0
        fake.build = build
        fake.validate = lambda pkg, on_line=None, **kw: 0
        rc, log = run_main(str(self.game), str(self.out), "--fpkg-build", str(self.game),
                           "--temp-dir", str(self.temp), fpkg_module=fake)
        self.assertEqual(rc, 0, log[-2000:])
        self.assertTrue((self.game / "._eboot.bin").is_file())
        self.assertTrue((self.game / ".Trashes" / "x").is_file())

    def test_fake_sign_tool_leaves_clutter(self):
        rc, log = run_cli("--fake-sign", str(self.game))
        self.assertEqual(rc, 0, log[-2000:])
        self.assertEqual(tree(self.game), self.before)


# ── 4: Look inside → Extract ───────────────────────────────────────────────────
class PfsExtract(Case):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="cli_safety_img_")).resolve()
        game = make_game(cls.base)
        cls.image = cls.base / "G.ffpfs"
        rc, log = run_cli(str(game), str(cls.image), "--pack", "--no-compress",
                          "--temp-dir", str(cls.base / "t"))
        assert rc == 0, log[-2000:]
        cls.eboot = (game / "eboot.bin").read_bytes()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def extract(self, members, dest):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.extract_pfs_members(self.image, members, dest)
        return rc, out.getvalue()

    def test_an_existing_different_file_is_kept_and_the_new_one_gets_a_free_name(self):
        dest = self.root / "pick"
        dest.mkdir()
        (dest / "eboot.bin").write_bytes(MARKER)
        rc, log = self.extract(["eboot.bin"], dest)
        self.assertEqual(rc, 0, log)
        self.assertEqual((dest / "eboot.bin").read_bytes(), MARKER)
        self.assertEqual((dest / "eboot (2).bin").read_bytes(), self.eboot)
        self.assertIn("[INFO]", log)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["eboot (2).bin", "eboot.bin"])

    def test_an_existing_identical_file_is_left_alone(self):
        dest = self.root / "pick"
        dest.mkdir()
        (dest / "eboot.bin").write_bytes(self.eboot)
        rc, log = self.extract(["eboot.bin"], dest)
        self.assertEqual(rc, 0, log)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["eboot.bin"])

    def test_names_that_could_leave_the_folder_are_refused(self):
        dest = self.root / "pick" / "inner"
        dest.mkdir(parents=True)
        for bad in ("../eboot.bin", "/etc/x", "C:/x", "c:x", "a\\..\\b", "sce_sys/../../x", "x:y"):
            rc, log = self.extract([bad], dest)
            self.assertEqual(rc, 1, (bad, log))
            self.assertIn("[ERROR]", log, bad)
        self.assertEqual(tree(self.root / "pick"), ["inner"])

    def test_a_failed_write_leaves_no_temp_file(self):
        dest = self.root / "pick"
        dest.mkdir()
        pfs, _c = cli._import_mkpfs()
        real = pfs.iter_inode_logical_blocks

        def broken(*a, **kw):
            yield b"half"
            raise OSError("read error")
        pfs.iter_inode_logical_blocks = broken
        try:
            with redirect_stdout(io.StringIO()), self.assertRaises(OSError):
                cli.extract_pfs_members(self.image, ["eboot.bin"], dest)
        finally:
            pfs.iter_inode_logical_blocks = real
        self.assertEqual(list(dest.iterdir()), [])


# ── 5: a finished build whose rename fails is kept ──────────────────────────────
class CommitFailure(Case):
    def test_the_partial_is_kept_and_named(self):
        src = self.root / "in.ffpfs"
        src.write_bytes(b"inner")
        final = self.out / "G.ffpfsc"
        final.write_bytes(MARKER)
        fake_mkpfs = [sys.executable, "-c", "import sys; open(sys.argv[-1], 'wb').write(b'new build')"]
        real = os.replace

        def failing(a, b, *rest, **kw):
            if str(a).endswith(".partial"):
                raise OSError("device busy")
            return real(a, b, *rest, **kw)
        os.replace = failing
        out = io.StringIO()
        try:
            with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                cli.compress_file_to_ffpfsc(src, final, fake_mkpfs, None, replace_existing=True)
        finally:
            os.replace = real
        partial = self.out / "G.ffpfsc.partial"
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(final.read_bytes(), MARKER)
        self.assertEqual(partial.read_bytes(), b"new build")
        self.assertIn(str(partial), next(l for l in out.getvalue().splitlines() if l.startswith("[ERROR]")))


# ── 7 + 8 + 9: chain jobs on exFAT sources and folder outputs ───────────────────
class ChainSources(Case):
    def test_exfat_to_ffpfs_builds_an_uncompressed_ffpfs(self):
        from mkpfs.exfat_writer import write_exfat_image
        img = write_exfat_image(make_game(self.root), self.root / "G.exfat")
        target = self.out / "G.ffpfs"
        neighbour = self.out / "G.ffpfsc"
        neighbour.write_bytes(MARKER)
        rc, log = run_cli(str(img), str(target), "--to", "ffpfs", "--overwrite", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-2000:])
        self.assertTrue(target.is_file(), log[-1500:])
        self.assertEqual(neighbour.read_bytes(), MARKER, "the .ffpfsc beside it is not touched")
        with open(target, "rb") as fh:
            pfs, _c = cli._import_mkpfs()
            pfs.parse_image_header(fh)                 # a plain PFS image, not a PFSC

    def test_backport_on_an_exfat_wrapped_ffpfsc_goes_on_to_the_unpack(self):
        game = make_elf_game(self.root / "src")
        img = self.out / "E.ffpfsc"
        rc, log = run_cli(str(game), str(img), "--pack", "--via-exfat", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-2000:])
        dest = self.root / "res" / "E [extracted]"
        rc, log = run_cli(str(img), str(dest), "--to", "folder", "--backport-target", "7.61",
                          "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-2000:])
        self.assertTrue((dest / "eboot.bin").is_file(), tree(dest.parent))

    def test_folder_output_rerun_replaces_instead_of_nesting(self):
        img = self.out / "G.ffpfsc"
        rc, log = run_cli(str(make_game(self.root)), str(img), "--pack", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-2000:])
        dest = self.root / "res" / "G [extracted]"
        args = (str(img), str(dest), "--to", "folder", "--sign", "--temp-dir", str(self.temp))
        rc, log = run_cli(*args)
        self.assertEqual(rc, 0, log[-2000:])
        (dest / "mine.txt").write_text("old run")
        rc, log = run_cli(*args)
        self.assertEqual(rc, 1, log[-2000:])
        self.assertIn("--overwrite", log)
        self.assertTrue((dest / "mine.txt").is_file())
        rc, log = run_cli(*args, "--overwrite")
        self.assertEqual(rc, 0, log[-2000:])
        self.assertTrue((dest / "eboot.bin").is_file())
        self.assertFalse((dest / "mine.txt").exists())
        self.assertFalse(any("extracted" in p.name for p in dest.iterdir()), tree(dest))
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["G [extracted]"])

    def test_folder_output_failure_is_an_error_line(self):
        img = self.out / "G.ffpfsc"
        rc, log = run_cli(str(make_game(self.root)), str(img), "--pack", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-2000:])
        blocker = self.root / "blocker"
        blocker.write_bytes(b"a file, not a folder")
        rc, log = run_cli(str(img), str(blocker / "G [extracted]"), "--to", "folder", "--sign",
                          "--temp-dir", str(self.temp))
        self.assertEqual(rc, 1, log[-2000:])
        self.assertIn("[ERROR]", log)
        self.assertNotIn("Traceback", log)
        self.assertFalse(list((self.temp / "_ffpfsc_temp").glob("chain-*")))


# ── 10 + 13 + 14: the pack path's own files ─────────────────────────────────────
class PackPath(Case):
    def test_keep_pfs_failure_leaves_no_partial(self):
        game = make_game(self.root)
        real = cli.shutil.move

        def failing(src, dst, *a, **kw):
            if str(src).endswith(".ffpfs") and "_ffpfsc_inner" in str(src):
                Path(dst).write_bytes(b"half")
                raise OSError("disk full")
            return real(src, dst, *a, **kw)
        cli.shutil.move = failing
        try:
            rc, log = run_main(str(game), str(self.out / "G.ffpfsc"), "--pack", "--keep-pfs",
                               "--temp-dir", str(self.temp))
        finally:
            cli.shutil.move = real
        self.assertEqual(rc, 0, log[-2000:])
        self.assertIn("[WARN]", log)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["G.ffpfsc"])

    def test_success_removes_only_its_own_inner_image(self):
        inner = self.temp / "_ffpfsc_inner"
        inner.mkdir()
        other = inner / "PPSA99999.ffpfs"
        other.write_bytes(b"another job's pass-1 image")
        rc, log = run_cli(str(make_game(self.root)), str(self.out / "G.ffpfsc"), "--pack",
                          "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-2000:])
        self.assertTrue(other.is_file())
        self.assertFalse((inner / f"{TITLE_ID}.ffpfs").exists())

    def test_a_failed_fake_sign_fails_the_job(self):
        game = make_game(self.root)
        mod = game / "sce_module"
        mod.mkdir()
        (mod / "lib.prx").write_bytes(_signable_elf())
        os.chmod(mod, stat.S_IRUSR | stat.S_IXUSR)        # the signer cannot write its temp file
        rc, log = run_cli(str(game), str(self.out / "G.ffpfsc"), "--pack", "--fake-sign-first",
                          "--temp-dir", str(self.temp))
        self.assertEqual(rc, 1, log[-2000:])
        self.assertIn("[ERROR]", log)
        self.assertNotIn("[SUCCESS]", log)
        self.assertFalse((self.out / "G.ffpfsc").exists())


# ── 11: fPKG extract ────────────────────────────────────────────────────────────
class FpkgExtract(Case):
    def fake(self, rc=0):
        fake = types.ModuleType("fpkg")
        fake.is_available = lambda: True
        fake.list_inner = lambda pkg: {"entries": [{"type": "file", "size": 4}]}
        calls = []

        def extract(src, out_dir, **kw):
            calls.append(out_dir)
            (Path(out_dir) / "eboot.bin").write_bytes(b"new!")
            (Path(out_dir) / "._eboot.bin").write_bytes(b"junk")
            return rc
        fake.extract = extract
        fake.calls = calls
        return fake

    def setUp(self):
        super().setUp()
        self.pkg = self.root / "G.pkg"
        self.pkg.write_bytes(b"pkg")
        self.dest = self.root / "lib" / "G [extracted]"

    def test_an_existing_folder_needs_overwrite(self):
        self.dest.mkdir(parents=True)
        (self.dest / "mine.txt").write_text("mine")
        fake = self.fake()
        rc, log = run_main("x", str(self.dest), "--fpkg-extract", str(self.pkg), fpkg_module=fake)
        self.assertEqual(rc, 1, log)
        self.assertIn("--overwrite", log)
        self.assertEqual(fake.calls, [])
        self.assertEqual(tree(self.dest), ["mine.txt"])

    def test_an_empty_folder_is_filled_and_overwrite_replaces(self):
        self.dest.mkdir(parents=True)          # the GUI creates it before the run
        rc, log = run_main("x", str(self.dest), "--fpkg-extract", str(self.pkg), fpkg_module=self.fake())
        self.assertEqual(rc, 0, log)
        self.assertEqual(tree(self.dest), ["eboot.bin"])
        (self.dest / "mine.txt").write_text("old")
        rc, log = run_main("x", str(self.dest), "--fpkg-extract", str(self.pkg), "--overwrite",
                           fpkg_module=self.fake())
        self.assertEqual(rc, 0, log)
        self.assertEqual(tree(self.dest), ["eboot.bin"])
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["G [extracted]"])

    def test_a_failed_extract_keeps_the_old_folder(self):
        self.dest.mkdir(parents=True)
        (self.dest / "mine.txt").write_text("old")
        fake = self.fake(rc=1)
        rc, log = run_main("x", str(self.dest), "--fpkg-extract", str(self.pkg), "--overwrite",
                           fpkg_module=fake)
        self.assertEqual(rc, 1, log)
        self.assertNotEqual(fake.calls, [self.dest])
        self.assertEqual(tree(self.dest), ["mine.txt"])
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["G [extracted]"])


# ── 12 + 18: temp folders ───────────────────────────────────────────────────────
class TempFolders(Case):
    def linked_tmp(self) -> dict:
        real = self.root / "realtmp"
        real.mkdir()
        link = self.root / "linktmp"
        link.symlink_to(real, target_is_directory=True)
        return {"TMPDIR": str(link), "TEMP": str(link), "TMP": str(link)}

    def test_patch_mode_without_a_temp_dir(self):
        game = make_game(self.root)
        patch = self.root / "patch"
        (patch / "sce_sys").mkdir(parents=True)
        shutil.copy2(game / "sce_sys" / "param.json", patch / "sce_sys" / "param.json")
        (patch / "eboot.bin").write_bytes(b"patched eboot")
        rc, log = run_cli(str(game), str(self.out / "P.ffpfsc"), "--patch", str(patch), env=self.linked_tmp())
        self.assertEqual(rc, 0, log[-2000:])
        self.assertNotIn("Traceback", log)

    def test_chain_scratch_without_a_temp_dir_is_known_as_ours(self):
        z = self.zip_of(make_game(self.root))
        rc, log = run_cli(str(z), str(self.out / "G.ffpfsc"), "--to", "ffpfsc", "--sign", env=self.linked_tmp())
        self.assertEqual(rc, 0, log[-2000:])
        self.assertIn("The unpacked copy is ours", log)

    def test_every_scratch_lives_in_the_app_folder(self):
        app = self.temp / "_ffpfsc_temp"
        game = make_game(self.root)
        for target, extra in ((self.out / "G.ffpfsc", ()), (self.out / "U.ffpfs", ("--no-compress",)),
                              (self.out / "Z.ffpfsc", ())):
            src = self.zip_of(game) if target.stem == "Z" else game
            rc, log = run_cli(str(src), str(target), "--pack", "--temp-dir", str(self.temp), *extra)
            self.assertEqual(rc, 0, log[-2000:])
            folders = re.findall(r"--temp-folder (\S+)", log)
            self.assertTrue(folders, log[-1500:])
            for f in folders:
                self.assertTrue(Path(f).is_relative_to(app), (target.name, f))
            if target.stem == "Z":
                found = re.search(r"\[OK\] Found game source at (.+)", log).group(1)
                self.assertTrue(Path(found).is_relative_to(app), found)
        # the app folder this run made goes again once it is empty: nothing loose is left
        self.assertEqual(sorted(p.name for p in self.temp.iterdir() if p.name != "_ffpfsc_inner"), [])


# ── 17: an archive's other files come along ─────────────────────────────────────
class ChainArchiveExtras(Case):
    def test_files_beside_the_game_are_copied_next_to_the_output(self):
        z = self.zip_of(make_game(self.root), {"DLC Pack/dlc.bin": b"dlc", "notes.txt": b"read me",
                                               "__MACOSX/._notes.txt": b"junk"})
        rc, log = run_cli(str(z), str(self.out / "G.ffpfsc"), "--to", "ffpfsc", "--sign",
                          "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-2000:])
        self.assertEqual((self.out / "DLC Pack" / "dlc.bin").read_bytes(), b"dlc")
        self.assertEqual((self.out / "notes.txt").read_bytes(), b"read me")
        self.assertFalse((self.out / "__MACOSX").exists())
        self.assertIn("[INFO]", log)
        self.assertIn("DLC Pack", log)


if __name__ == "__main__":
    unittest.main()
