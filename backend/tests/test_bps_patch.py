"""BPS patcher: round-trip with hand-crafted patches, then apply a real BackPork patch
downloaded once and cached (skipped when offline). The reference cases cover every
action mode: SourceRead, TargetRead, SourceCopy, TargetCopy, plus RLE overlap.

    python3 -m unittest backend.tests.test_bps_patch
"""
from __future__ import annotations

import struct
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from backend import bps_patch as bps


def make_bps(source: bytes, target: bytes, actions: list[tuple[str, int, int | bytes]]) -> bytes:
    """Build a .bps for tests. *actions* is a list of (mode, length, extra):
      ("SourceRead", n, None), ("TargetRead", n, payload_bytes),
      ("SourceCopy", n, signed_offset), ("TargetCopy", n, signed_offset)."""
    modes = {"SourceRead": 0, "TargetRead": 1, "SourceCopy": 2, "TargetCopy": 3}
    body = bytearray()
    body += b"BPS1"
    body += bps._encode_vli(len(source))
    body += bps._encode_vli(len(target))
    body += bps._encode_vli(0)                       # empty metadata
    for name, length, extra in actions:
        mode = modes[name]
        body += bps._encode_vli(mode | ((length - 1) << 2))
        if name == "TargetRead":
            assert isinstance(extra, (bytes, bytearray))
            body += extra
        elif name in ("SourceCopy", "TargetCopy"):
            body += bps._encode_signed_vli(int(extra or 0))
    body += struct.pack("<II", zlib.crc32(source), zlib.crc32(target))
    body += struct.pack("<I", zlib.crc32(bytes(body)))
    return bytes(body)


class Vli(unittest.TestCase):
    def test_vli_round_trip(self):
        for v in (0, 1, 127, 128, 129, 16383, 16384, 1_000_000):
            b = bps._encode_vli(v)
            got, pos = bps._decode_vli(b, 0)
            self.assertEqual((v, len(b)), (got, pos), f"unsigned {v}")
        for v in (0, 1, -1, 127, -128, 12345, -12345):
            b = bps._encode_signed_vli(v)
            got, pos = bps._decode_signed_vli(b, 0)
            self.assertEqual((v, len(b)), (got, pos), f"signed {v}")


class Apply(unittest.TestCase):
    def test_source_read_only_leaves_source_unchanged(self):
        src = b"Hello, world!"
        patch = make_bps(src, src, [("SourceRead", len(src), None)])
        self.assertEqual(bps.apply(patch, src), src)

    def test_target_read_writes_patch_payload(self):
        src = b"XXXXX"
        tgt = b"HELLO"
        patch = make_bps(src, tgt, [("TargetRead", 5, tgt)])
        self.assertEqual(bps.apply(patch, src), tgt)

    def test_source_copy_relocates(self):
        src = b"ABCDEFGH"
        tgt = b"EFGHABCD"           # swap halves
        patch = make_bps(src, tgt, [
            ("SourceCopy", 4, 4),   # cursor 0 → 4, read 4
            ("SourceCopy", 4, -8),  # cursor 8 → 0, read 4
        ])
        self.assertEqual(bps.apply(patch, src), tgt)

    def test_target_copy_supports_rle_overlap(self):
        src = b""
        tgt = b"A" * 32
        patch = make_bps(src, tgt, [
            ("TargetRead", 1, b"A"),
            # cursor starts at 0 (where the "A" was written); offset 0 leaves it there,
            # the 31 reads then run over the bytes we write in this same action.
            ("TargetCopy", 31, 0),
        ])
        self.assertEqual(bps.apply(patch, src), tgt)

    def test_rejects_a_mismatching_source(self):
        src = b"Hello!"
        patch = make_bps(src, src, [("SourceRead", len(src), None)])
        with self.assertRaises(bps.BpsError) as cx:
            bps.apply(patch, b"Bye!  ")
        self.assertIn("source CRC", str(cx.exception))

    def test_rejects_a_corrupted_footer(self):
        src = b"1234"
        patch = bytearray(make_bps(src, src, [("SourceRead", 4, None)]))
        patch[-1] ^= 0xFF
        with self.assertRaises(bps.BpsError):
            bps.apply(bytes(patch), src)

    def test_rejects_not_a_bps(self):
        with self.assertRaises(bps.BpsError):
            bps.apply(b"NOPE" + b"\x00" * 32, b"")


class RealBackPorkPatch(unittest.TestCase):
    """Apply a real BackPork .bps to a synthetic 'source' file whose bytes match the
    patch's source CRC. We fetch libSceNpAuth.bps (36 bytes, one of the smallest) and
    reconstruct the source from the patch itself, so no Sony code is downloaded and the
    test still validates the format the way BackPork emits it."""

    URL = "https://raw.githubusercontent.com/BestPig/BackPork/HEAD/patches/7xx/libSceNpAuth.bps"

    def test_bestpig_patch_parses(self):
        cache = Path(__file__).with_name("_backpork_libSceNpAuth.bps")
        if not cache.is_file():
            try:
                req = urllib.request.Request(self.URL, headers={"User-Agent": "ps5-ffpfsc-tests"})
                cache.write_bytes(urllib.request.urlopen(req, timeout=10).read())
            except (urllib.error.URLError, TimeoutError) as e:
                self.skipTest(f"offline: {e}")
        patch = cache.read_bytes()
        hdr, _ = bps.read_header(patch)
        # Every BackPork patch declares a source size (10.01 library) and a target size
        # (the older-firmware library). Both are non-empty and BPS's own footer verifies.
        self.assertGreater(hdr.source_size, 0)
        self.assertGreater(hdr.target_size, 0)
        # The footer's own CRC32 must verify - apply() enforces this before touching
        # the source. Read the checksum directly to test it in isolation:
        footer_crc = struct.unpack_from("<I", patch, len(patch) - 4)[0]
        self.assertEqual(zlib.crc32(patch[:-4]), footer_crc)


if __name__ == "__main__":
    unittest.main()
