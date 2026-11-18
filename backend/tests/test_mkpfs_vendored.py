# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
from __future__ import annotations

import io
import os
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from mkpfs import consts, game_metadata, pfs  # noqa: E402

BLOCK = consts.PFSC_LOGICAL_BLOCK_SIZE

FMT_UTF8 = 0x0204
FMT_U32 = 0x0404


def build_sfo(entries: list[tuple[str, object, int]]) -> bytes:
    keys = b""
    datas = b""
    index = b""
    rows = []
    for key, value, fmt in entries:
        payload = struct.pack("<I", value) if fmt == FMT_U32 else str(value).encode() + b"\0"
        rows.append((len(keys), fmt, len(payload), len(datas)))
        keys += key.encode() + b"\0"
        datas += payload
    key_table = 0x14 + 16 * len(entries)
    data_table = key_table + len(keys)
    for key_offset, fmt, length, data_offset in rows:
        index += struct.pack("<HHIII", key_offset, fmt, length, length, data_offset)
    header = b"\x00PSF" + struct.pack("<I", 0x0101) + struct.pack("<III", key_table, data_table, len(entries))
    return header + index + keys + datas


class ParseSfoTests(unittest.TestCase):
    def test_parses_string_and_integer_entries(self):
        data = build_sfo([
            ("TITLE_ID", "CUSA00001", FMT_UTF8),
            ("APP_VER", "01.02", FMT_UTF8),
            ("PARENTAL_LEVEL", 5, FMT_U32),
        ])
        parsed = game_metadata._parse_sfo(data)
        self.assertEqual(parsed["TITLE_ID"], "CUSA00001")
        self.assertEqual(parsed["APP_VER"], "01.02")
        self.assertEqual(parsed["PARENTAL_LEVEL"], "5")

    def test_rejects_non_sfo_and_truncated_input(self):
        self.assertEqual(game_metadata._parse_sfo(b"not an sfo"), {})
        data = build_sfo([("TITLE_ID", "CUSA00001", FMT_UTF8)])
        self.assertEqual(game_metadata._parse_sfo(data[:0x14 + 8]), {})

    def test_index_struct_format_is_two_u16(self):
        # two little-endian u16s; any other width misreads every entry
        self.assertEqual(struct.calcsize("<HH"), 4)
        src = Path(game_metadata.__file__).read_text(encoding="utf-8")
        self.assertIn('struct.unpack_from("<HH", data, entry_base)', src)


class PfscBlockThreadsTests(unittest.TestCase):
    """pass 2 compresses big files on threads; the bytes must match the one-thread path"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def _mixed(self) -> Path:
        rnd = random.Random(5)
        parts = []
        for i in range(60):
            kind = i % 5
            if kind == 0:
                parts.append(bytes(BLOCK))
            elif kind == 1:
                parts.append(rnd.randbytes(BLOCK))
            elif kind == 2:
                parts.append(rnd.randbytes(BLOCK - 2600) + bytes(2600))    # gain under the 5 % threshold
            elif kind == 3:
                parts.append(rnd.randbytes(BLOCK - 4000) + bytes(4000))    # gain over it
            else:
                parts.append(rnd.randbytes(BLOCK // 2) + bytes(BLOCK // 2))
        parts.append(b"short last block " * 300)
        return self._write("mixed.bin", b"".join(parts))

    def _encode(self, path: Path, workers: int) -> tuple[bytes, tuple]:
        out = io.BytesIO()
        result = pfs._encode_pfsc_into_handle(
            out=out, base_offset=0, source_path=path, threshold_gain=5, min_file_gain=0,
            zlib_level=7, logical_block_size=BLOCK, block_worker_count=workers)
        return out.getvalue()[:result[0]], result

    def _analyze(self, path: Path, workers: int) -> tuple:
        return pfs._analyze_pfsc_file_storage(
            abs_path=path, threshold_gain=5, min_file_gain=0, zlib_level=7,
            logical_block_size=BLOCK, block_worker_count=workers)

    def test_threads_write_the_same_bytes_as_one_thread(self):
        path = self._mixed()
        one_bytes, one_result = self._encode(path, 1)
        self.assertTrue(one_result[1])
        for workers in (2, 4):
            data, result = self._encode(path, workers)
            self.assertEqual(result, one_result)
            self.assertEqual(data, one_bytes)
        with mock.patch.dict(os.environ, {"MKPFS_PFSC_WINDOW_FACTOR": "1"}):
            data, result = self._encode(path, 3)
        self.assertEqual(data, one_bytes)

    def test_threaded_payload_decodes_to_the_source(self):
        path = self._mixed()
        data, _result = self._encode(path, 4)
        lbs, count, boff, _data_off, _size = pfs._parse_pfsc_header(data[:consts.PFSC_HEADER_SIZE])
        offsets = struct.unpack_from(f"<{count + 1}Q", data, boff)
        plain = b"".join(pfs._decode_pfsc_block(data[offsets[i]:offsets[i + 1]], lbs, i) for i in range(count))
        source = path.read_bytes()
        # the header counts whole blocks, the inode holds the real size
        self.assertEqual(plain[:len(source)], source)
        self.assertEqual(plain[len(source):], bytes(len(plain) - len(source)))

    def test_threaded_analysis_matches_one_thread_and_the_real_write(self):
        path = self._mixed()
        one = self._analyze(path, 1)
        self.assertEqual(self._analyze(path, 4), one)
        self.assertEqual(one[0], self._encode(path, 4)[1][0])

    def test_incompressible_file_stays_raw_on_threads(self):
        path = self._write("noise.bin", random.Random(9).randbytes(20 * BLOCK + 77))
        self.assertEqual(self._encode(path, 4)[1], self._encode(path, 1)[1])
        self.assertFalse(self._encode(path, 4)[1][1])
        self.assertEqual(self._analyze(path, 4), self._analyze(path, 1))


if __name__ == "__main__":
    unittest.main()
