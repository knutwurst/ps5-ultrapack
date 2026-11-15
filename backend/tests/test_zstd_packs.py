# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
import filecmp
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

import zstd_packs as Z  # noqa: E402

CLI = BACKEND / "cli.py"
HAVE_RUNTIME = (BACKEND / "native" / "ampr-ng" / "libSceAmpr.sprx").is_file()
CHUNK = Z.CHUNK_SIZE


def _words(n: int, seed: int) -> bytes:
    rnd = random.Random(seed)
    words = [b"alpha", b"beta", b"vertex", b"texture", b"mesh", b"[level]", b"shader"]
    out = bytearray()
    while len(out) < n:
        out += rnd.choice(words) + b" "
    return bytes(out[:n])


def _game(root: Path) -> Path:
    files = {
        "eboot.bin": os.urandom(6000),
        "sce_sys/param.json": b'{"titleId": "PPSA99099"}',
        "sce_module/libc.prx": os.urandom(4000),
        "sce_module/libSceFios2.sprx": os.urandom(3000),
        "fakelib/libSceAgc.sprx": os.urandom(2000),
        "dlc_emu.ini": b"[dlc]\n",
        "data/levels/l0.bin": _words(700_000, 1),
        "data/levels/l1.bin": _words(CHUNK, 2),
        "data/levels/l2.bin": _words(CHUNK + 1, 3),
        "data/noise.bin": os.urandom(CHUNK + 4096),
        "data/empty.bin": b"",
        "data/[odd] näme.dat": _words(90_000, 4),
        "Movies/Intro.BK2": _words(300_000, 5),
        "root.dat": b"r" * 50_000,
        "lib/plugin.self": os.urandom(1000),
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


class Format(unittest.TestCase):
    def test_fnv1a64_matches_the_reference_vectors(self):
        self.assertEqual(Z.fnv1a64(b""), 0xcbf29ce484222325)
        self.assertEqual(Z.fnv1a64(b"a"), 0xaf63dc4c8601ec8c)
        self.assertEqual(Z.fnv1a64(b"foobar"), 0x85944171f73967e8)

    def test_chunk_word_bit_layout(self):
        w = Z.chunk_word(64, 4095, CHUNK, Z.CODEC_ZSTD)
        self.assertEqual(w & 0xFFFFFFFF, 64)
        self.assertEqual((w >> 32) & 0xFFF, 4095)
        self.assertEqual((w >> 44) & 0x3FFFF, CHUNK - 1)
        self.assertEqual(w >> 62, 1)
        self.assertEqual(Z.split_word(w), (64, 4095, CHUNK, 1))
        self.assertEqual(Z.split_word(Z.chunk_word(0xFFFFFFFF, 0, 1, 0)), (0xFFFFFFFF, 0, 1, 0))

    def test_volume_names(self):
        self.assertEqual(Z.volume_name(0), "ampr_zstd-000.pak")
        self.assertEqual(Z.volume_name(999), "ampr_zstd-999.pak")
        self.assertEqual(Z.volume_name(1000), "ampr_zstd-1000.pak")
        self.assertTrue(Z.is_pack_name("AMPR_ZSTD-001.PAK"))
        self.assertTrue(Z.is_pack_name("ampr_zstd.index"))
        self.assertFalse(Z.is_pack_name("ampr_zstd-01.pak"))
        self.assertFalse(Z.is_pack_name("data/ampr_zstd-000.pak"))

    def test_always_loose_follows_ps5_dump_forge(self):
        for rel in ("eboot.bin", "sub/EBOOT.BIN", "a/b.prx", "x.SPRX", "y.elf", "z.self", "dlc_emu.ini",
                    "sce_sys/param.json", "SCE_SYS/icon0.png", "sce_module/libc.prx", "fakelib/x.bin",
                    "fakelib_7.61/y", "ampr_emu.index", "fakelib/libSceAmpr.sprx", "ampr_commands.bin",
                    "ampr_emu.log", "ampr_assets.index", "ampr_zstd-003.pak"):
            self.assertTrue(Z.always_loose(rel), rel)
        for rel in ("data/a.bin", "sce_sys", "Movies/x.bk2", "config.json", "data/sce_sys/x", "fakelibx.bin"):
            self.assertFalse(Z.always_loose(rel), rel)

    def test_path_rules(self):
        self.assertTrue(Z.valid_path("data/level1.bin"))
        for bad in ("", "/abs", "a\\b", "a\0b", "a//b", "./a", "a/../b", "a/"):
            self.assertFalse(Z.valid_path(bad), bad)
        self.assertTrue(Z.reachable("data/a.bin"))
        self.assertFalse(Z.reachable("data/tab\there.bin"))
        self.assertFalse(Z.reachable("x" * 256))
        self.assertFalse(Z.reachable("/".join(["d" * 200] * 6)))

    def test_raw_when_zstd_does_not_shrink(self):
        enc = Z._Encoder(5)
        raw = os.urandom(CHUNK)
        self.assertEqual(enc.encode(raw), (Z.CODEC_RAW, raw))
        codec, frame = enc.encode(_words(CHUNK, 9))
        self.assertEqual(codec, Z.CODEC_ZSTD)
        Z.check_frame(frame, CHUNK)
        self.assertEqual(Z.decode_chunk(codec, frame, CHUNK), _words(CHUNK, 9))

    def test_frames_carry_size_checksum_and_a_small_window(self):
        enc = Z._Encoder(19)
        for n in (1, 1000, CHUNK - 1, CHUNK):
            codec, frame = enc.encode(b"\0" * n if n > 100 else b"a" * n)
            if codec == Z.CODEC_RAW:
                continue
            self.assertEqual(frame[:4], Z.FRAME_MAGIC)
            self.assertTrue(frame[4] & 0x04, "checksum flag")
            self.assertFalse(frame[4] & 0x03, "no dictionary")
            Z.check_frame(frame, n)

    def test_a_reused_encoder_writes_the_same_frames_as_a_fresh_one(self):
        data = [_words(CHUNK, s) for s in range(4)]
        reused = Z._Encoder(5)
        self.assertEqual([reused.encode(d) for d in data], [Z._Encoder(5).encode(d) for d in data])

    def test_damaged_frames_are_refused(self):
        codec, frame = Z._Encoder(5).encode(_words(CHUNK, 3))
        bad = bytearray(frame)
        bad[len(bad) // 2] ^= 0xFF
        with self.assertRaises(Z.ZstdPackError):
            Z.decode_chunk(codec, bytes(bad), CHUNK)
        with self.assertRaises(Z.ZstdPackError):
            Z.decode_chunk(codec, frame, CHUNK - 1)
        with self.assertRaises(Z.ZstdPackError):
            Z.decode_chunk(2, frame, CHUNK)
        with self.assertRaises(Z.ZstdPackError):
            Z.decode_chunk(Z.CODEC_RAW, b"short", 6)


class PackAndRead(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="zstd-test-"))
        self.src = self.d / "src"
        self.files = {
            "data/a.bin": _words(600_000, 1),
            "data/exact.bin": _words(CHUNK, 2),
            "data/plus.bin": _words(CHUNK + 1, 3),
            "data/noise.bin": os.urandom(2 * CHUNK + 5),
            "data/empty.bin": b"",
            "Ü/ä b.dat": _words(10_000, 4),
        }
        for rel, data in self.files.items():
            p = self.src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        self.listing = [(rel, len(data)) for rel, data in self.files.items()]

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_round_trip_and_layout(self):
        out = self.d / "packs"
        idx = Z.pack_files(self.src, self.listing, out, level=5, threads=4)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["ampr_zstd-000.pak", "ampr_zstd.index"])
        keys = [(Z.fnv1a64(f.path.encode()), f.path.encode()) for f in idx.files]
        self.assertEqual(keys, sorted(keys))
        empty = next(f for f in idx.files if f.path == "data/empty.bin")
        self.assertEqual(empty.chunk_count, 0)
        with Z.Archive(self.src, out) as arc:
            for rel, data in self.files.items():
                self.assertEqual(arc.read(rel), data, rel)
            arc.compare_with(self.src)
            codecs = [Z.split_word(w)[3] for w in arc.index.chunks]
            self.assertIn(Z.CODEC_RAW, codecs)
            self.assertIn(Z.CODEC_ZSTD, codecs)
        back = self.d / "back"
        with Z.Archive(self.src, out) as arc:
            arc.extract_to(back)
        for rel in self.files:
            self.assertTrue(filecmp.cmp(back / rel, self.src / rel, shallow=False), rel)

    def test_the_index_header_and_crcs(self):
        out = self.d / "packs"
        idx = Z.pack_files(self.src, self.listing, out, level=9, threads=1)
        data = (out / Z.INDEX_NAME).read_bytes()
        self.assertEqual(data[:8], b"AMPRZPK1")
        self.assertEqual(struct.unpack_from("<II", data, 8), (1, 128))
        self.assertEqual((data[16], data[17]), (18, 9))
        self.assertEqual(struct.unpack_from("<I", data, 112)[0], zlib.crc32(data[128:]))
        head = bytearray(data[:128])
        head[112:120] = bytes(8)
        self.assertEqual(struct.unpack_from("<I", data, 116)[0], zlib.crc32(bytes(head)))
        vol = (out / "ampr_zstd-000.pak").read_bytes()
        self.assertEqual(vol[:8], b"AMPRZDT1")
        self.assertEqual(vol[24:40], idx.archive_id)
        self.assertEqual(struct.unpack_from("<Q", vol, 40)[0], len(vol) - 64)
        self.assertEqual(struct.unpack_from("<I", vol, 60)[0], zlib.crc32(vol[:60] + bytes(4)))
        self.assertEqual(len(vol), idx.volume_sizes[0])

    def test_volumes_split_and_a_file_continues_in_the_next(self):
        out = self.d / "packs"
        idx = Z.pack_files(self.src, self.listing, out, level=1, threads=2, volume_limit=64 + CHUNK + 1000)
        self.assertGreater(len(idx.volume_sizes), 2)
        self.assertTrue(all(s <= 64 + CHUNK + 1000 for s in idx.volume_sizes))
        with Z.Archive(self.src, out) as arc:
            arc.compare_with(self.src)

    def test_the_same_archive_id_gives_the_same_bytes(self):
        a = Z.pack_files(self.src, self.listing, self.d / "a", archive_id=b"\1" * 16, threads=1)
        b = Z.pack_files(self.src, self.listing, self.d / "b", archive_id=b"\1" * 16, threads=8)
        self.assertEqual(a.to_bytes(), b.to_bytes())
        self.assertEqual((self.d / "a" / "ampr_zstd-000.pak").read_bytes(), (self.d / "b" / "ampr_zstd-000.pak").read_bytes())

    def test_bad_lists_are_refused(self):
        with self.assertRaises(Z.ZstdPackError):
            Z.pack_files(self.src, self.listing + [self.listing[0]], self.d / "x")
        with self.assertRaises(Z.ZstdPackError):
            Z.pack_files(self.src, [("data", 0), ("data/a.bin", 600_000)], self.d / "x")
        with self.assertRaises(Z.ZstdPackError):
            Z.pack_files(self.src, [("../a", 1)], self.d / "x")
        with self.assertRaises(Z.ZstdPackError):
            Z.pack_files(self.src, self.listing, self.d / "x", level=23)
        with self.assertRaises(Z.ZstdPackError):
            Z.pack_files(self.src, [("data/a.bin", 600_001)], self.d / "x")

    def test_every_damage_to_the_index_is_found(self):
        out = self.d / "packs"
        Z.pack_files(self.src, self.listing, out, threads=1)
        good = (out / Z.INDEX_NAME).read_bytes()

        def fix_crcs(b: bytearray) -> bytes:
            struct.pack_into("<I", b, 112, zlib.crc32(bytes(b[128:])))
            h = bytearray(b[:128]); h[112:120] = bytes(8)
            struct.pack_into("<I", b, 116, zlib.crc32(bytes(h)))
            return bytes(b)

        def poked(at: int, value: bytes, crcs: bool = True) -> bytes:
            b = bytearray(good)
            b[at:at + len(value)] = value
            return fix_crcs(b) if crcs else bytes(b)

        cases = {
            "magic": poked(0, b"AMPRZPK2"),
            "version": poked(8, struct.pack("<I", 2)),
            "chunk shift 16": poked(16, b"\x10"),
            "level 0": poked(17, b"\x00"),
            "level 23": poked(17, b"\x17"),
            "reserved 18": poked(18, b"\x01"),
            "reserved 104": poked(104, b"\x01"),
            "reserved 120": poked(120, b"\x01"),
            "header crc": poked(116, b"\0\0\0\0", crcs=False),
            "payload crc": poked(200, b"\xff", crcs=False),
            "file count": poked(40, struct.pack("<Q", 7)),
            "total": poked(96, struct.pack("<Q", 1)),
            "path hash": poked(128, b"\0" * 8),
            "first chunk": poked(128 + 16, struct.pack("<I", 9)),
            "flags": poked(128 + 26, struct.pack("<H", 2)),
            "file reserved": poked(128 + 28, b"\x01"),
        }
        self.assertIsInstance(Z.parse_index(good), Z.Index)
        for name, data in cases.items():
            with self.assertRaises(Z.ZstdPackError, msg=name):
                Z.parse_index(data)
        idx = Z.parse_index(good)
        chunks_off = 128 + 32 * len(idx.files)
        moved = bytearray(good)
        w = struct.unpack_from("<Q", good, chunks_off + 8)[0]
        struct.pack_into("<Q", moved, chunks_off + 8, w + 1)          # a gap after the first chunk
        with self.assertRaises(Z.ZstdPackError):
            Z.parse_index(fix_crcs(moved))

    def test_a_damaged_or_foreign_volume_is_found(self):
        out = self.d / "packs"
        Z.pack_files(self.src, self.listing, out, threads=1)
        vol = out / "ampr_zstd-000.pak"
        good = vol.read_bytes()
        vol.write_bytes(good[:24] + b"\x02" * 16 + good[40:])     # another archive id
        with self.assertRaises(Z.ZstdPackError):
            Z.Archive(self.src, out)
        vol.write_bytes(good + b"x")
        with self.assertRaises(Z.ZstdPackError):
            Z.Archive(self.src, out)
        b = bytearray(good)
        b[200] ^= 0xFF                                            # inside the first chunk
        vol.write_bytes(bytes(b))
        with self.assertRaises(Z.ZstdPackError):
            with Z.Archive(self.src, out) as arc:
                arc.compare_with(self.src)

    def test_ampr_index_reader(self):
        sys.path.insert(0, str(BACKEND / "ampr_pack"))
        import build_ampr_index
        out = self.d / "ampr_emu.index"
        self.assertEqual(build_ampr_index.build_index_local(self.src, out, False), 0)
        rows = Z.read_ampr_index(out.read_bytes())
        self.assertEqual(rows["/app0/data/a.bin"], 600_000)
        self.assertEqual(rows["/app0/Ü/ä b.dat"], 10_000)


@unittest.skipUnless(HAVE_RUNTIME, "run ./FETCH_AMPR_EMU.sh for the ampr-emu-ng runtime")
class PackGame(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="zstd-game-"))
        self.log = []

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_pack_then_unpack_gives_the_game_back(self):
        game = _game(self.d / "app0")
        orig = self.d / "orig"
        shutil.copytree(game, orig)
        r = Z.pack_game(game, level=5, threads=4, log=self.log.append)
        self.assertGreater(r["logical_bytes"], r["stored_bytes"])
        inside = _files(game)
        self.assertEqual(inside, {
            "eboot.bin", "sce_sys/param.json", "sce_module/libc.prx", "sce_module/libSceFios2.sprx",
            "fakelib/libSceAgc.sprx", "fakelib/libSceAmpr.sprx", "dlc_emu.ini", "data/empty.bin",
            "lib/plugin.self", "ampr_emu.index", "ampr_zstd.index", "ampr_zstd-000.pak"})
        self.assertTrue(filecmp.cmp(game / "fakelib" / "libSceAmpr.sprx", Z.runtime_path(), shallow=False))
        self.assertTrue(Z.is_packed(game))
        rows = Z.read_ampr_index((game / "ampr_emu.index").read_bytes())
        with Z.Archive(game) as arc:
            for rec in arc.index.files:
                self.assertEqual(rows["/app0/" + rec.path], rec.size, rec.path)
            arc.compare_with(orig)
            self.assertEqual({r.path for r in arc.index.files}, {
                "data/levels/l0.bin", "data/levels/l1.bin", "data/levels/l2.bin", "data/noise.bin",
                "data/[odd] näme.dat", "Movies/Intro.BK2", "root.dat"})
        self.assertFalse((self.d / ".app0.zstd-work").exists())

        Z.unpack_game(game, log=self.log.append)
        self.assertFalse(Z.is_packed(game))
        self.assertEqual(_files(game) - {"fakelib/libSceAmpr.sprx", "ampr_emu.index"}, _files(orig))
        for rel in _files(orig):
            self.assertTrue(filecmp.cmp(game / rel, orig / rel, shallow=False), rel)
        rows = Z.read_ampr_index((game / "ampr_emu.index").read_bytes())
        self.assertEqual(rows["/app0/data/levels/l0.bin"], 700_000)

    def test_refusals_leave_the_game_as_it_was(self):
        game = _game(self.d / "app0")
        (game / "ampr_assets.index").write_bytes(b"x")
        with self.assertRaises(Z.ZstdPackError):
            Z.pack_game(game, log=self.log.append)
        (game / "ampr_assets.index").unlink()
        (game / "ampr_zstd-000.pak").write_bytes(b"x")
        with self.assertRaises(Z.ZstdPackError):
            Z.pack_game(game, log=self.log.append)
        (game / "ampr_zstd-000.pak").unlink()
        (game / "eboot.bin").unlink()
        with self.assertRaises(Z.ZstdPackError):
            Z.pack_game(game, log=self.log.append)

    def test_a_game_that_does_not_shrink_stays_as_it_is(self):
        game = self.d / "app0"
        (game / "data").mkdir(parents=True)
        (game / "eboot.bin").write_bytes(os.urandom(100))
        for i in range(3):
            (game / "data" / f"n{i}.bin").write_bytes(os.urandom(CHUNK))
        before = _files(game)
        with self.assertRaises(Z.ZstdPackError) as e:
            Z.pack_game(game, log=self.log.append)
        self.assertIn("not worth it", str(e.exception))
        self.assertTrue(before <= _files(game))
        self.assertFalse(Z.is_packed(game))
        self.assertFalse((self.d / ".app0.zstd-work").exists())

    @unittest.skipIf(sys.platform == "win32", "Windows file names cannot hold a tab")
    def test_paths_ampr_emu_ng_cannot_reach_stay_loose(self):
        game = _game(self.d / "app0")
        (game / "data" / "tab\there.bin").write_bytes(_words(100_000, 8))
        Z.pack_game(game, log=self.log.append)
        self.assertTrue((game / "data" / "tab\there.bin").is_file())
        with Z.Archive(game) as arc:
            self.assertNotIn("data/tab\there.bin", {r.path for r in arc.index.files})


@unittest.skipUnless(HAVE_RUNTIME, "run ./FETCH_AMPR_EMU.sh for the ampr-emu-ng runtime")
class ChainZstd(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="zstd-chain-"))
        (self.d / "tmp").mkdir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run(self, *argv):
        env = dict(os.environ, PYTHONUTF8="1")
        return subprocess.run([sys.executable, "-u", str(CLI), *map(str, argv), "--temp-dir", str(self.d / "tmp")],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                              timeout=600)

    def test_folder_to_exfat_with_zstd_and_back(self):
        game = _game(self.d / "Game")
        before = {p: p.read_bytes() for p in game.rglob("*") if p.is_file()}
        r = self._run(game, self.d / "out" / "Game.exfat", "--to", "exfat", "--zstd", "--zstd-level", "9")
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-800:])
        self.assertEqual({p: p.read_bytes() for p in game.rglob("*") if p.is_file()}, before)
        import cli
        back = self.d / "back"
        self.assertTrue(cli._extract_exfat_to(self.d / "out" / "Game.exfat", back))
        inside = _files(back)
        self.assertIn("ampr_zstd.index", inside)
        self.assertIn("fakelib/libSceAmpr.sprx", inside)
        self.assertNotIn("data/levels/l0.bin", inside)
        self.assertIn("eboot.bin", inside)
        with Z.Archive(back) as arc:
            self.assertEqual(arc.index.level, 9)
            arc.compare_with(game)
        self.assertEqual([p.name for p in (self.d / "tmp").rglob("chain-*")], [])

        r = self._run(self.d / "out" / "Game.exfat", self.d / "unp" / "Game [unpacked]", "--to", "folder", "--zstd-unpack")
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-500:])
        unp = self.d / "unp" / "Game [unpacked]"
        self.assertEqual(_files(unp) - {"fakelib/libSceAmpr.sprx", "ampr_emu.index"}, _files(game))
        self.assertTrue(filecmp.cmp(unp / "data" / "noise.bin", game / "data" / "noise.bin", shallow=False))

    def test_folder_to_folder_with_zstd(self):
        game = _game(self.d / "Game")
        r = self._run(game, self.d / "out" / "Game [zstd]", "--to", "folder", "--zstd")
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-800:])
        out = self.d / "out" / "Game [zstd]"
        self.assertTrue(Z.is_packed(out), r.stdout[-800:])
        with Z.Archive(out) as arc:
            arc.compare_with(game)

    def test_zstd_refuses_other_outputs_and_lz4(self):
        game = _game(self.d / "Game")
        r = self._run(game, self.d / "out", "--to", "ffpfsc", "--zstd")
        self.assertEqual(r.returncode, 2, r.stdout[-800:])
        self.assertIn("zstd packs go into a folder or an .exfat", r.stdout)
        r = self._run(game, self.d / "out" / "g.exfat", "--to", "exfat", "--zstd", "--lz4")
        self.assertEqual(r.returncode, 2, r.stdout[-800:])
        r = self._run(game, self.d / "out" / "g.exfat", "--to", "exfat", "--zstd", "--zstd-level", "30")
        self.assertEqual(r.returncode, 2, r.stdout[-800:])


class PkgCheck(unittest.TestCase):
    def test_packed_files_count_as_present_in_the_ampr_check(self):
        import ps5_repair
        d = Path(tempfile.mkdtemp(prefix="zstd-pkgcheck-"))
        try:
            (d / "data").mkdir()
            (d / "data" / "a.bin").write_bytes(_words(300_000, 1))
            idx = Z.pack_files(d, [("data/a.bin", 300_000)], d / "p", threads=1)
            zindex = (d / "p" / Z.INDEX_NAME).read_bytes()
            sizes = {"eboot.bin": 10, "ampr_zstd.index": len(zindex), "ampr_zstd-000.pak": idx.volume_sizes[0]}
            index = {"/app0/eboot.bin": 10, "/app0/data/a.bin": 300_000}
            self.assertEqual(ps5_repair.ampr_mismatch(index, ps5_repair._with_zstd_packed(sizes, zindex)), [])
            self.assertEqual(ps5_repair.ampr_mismatch(index, sizes), ["ampr_zstd-000.pak", "ampr_zstd.index", "data/a.bin"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
