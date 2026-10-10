"""Guards for the vendored MkPFS copy under backend/mkpfs."""
from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from mkpfs import game_metadata  # noqa: E402

FMT_UTF8 = 0x0204
FMT_U32 = 0x0404


def build_sfo(entries: list[tuple[str, object, int]]) -> bytes:
    """Minimal PSF 1.1 file: header, index, key table, data table."""
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
        # The index entry starts with two little-endian u16 fields; the format string
        # must stay exactly this wide or every entry is misread.
        self.assertEqual(struct.calcsize("<HH"), 4)
        src = Path(game_metadata.__file__).read_text(encoding="utf-8")
        self.assertIn('struct.unpack_from("<HH", data, entry_base)', src)


if __name__ == "__main__":
    unittest.main()
