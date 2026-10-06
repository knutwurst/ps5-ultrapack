"""Synthetic PS4 packages for tests: the header fields, entry table and param.sfo the app
reads. Not installable; there is no PFS image inside."""
import struct
from pathlib import Path

SFO_UTF8, SFO_INT = 0x0204, 0x0404


def make_sfo(fields: dict) -> bytes:
    keys = sorted(fields)
    key_blob = b"".join(k.encode() + b"\0" for k in keys)
    key_blob += b"\0" * (-len(key_blob) % 4)
    index, data = b"", b""
    koff = 0
    for k in keys:
        v = fields[k]
        if isinstance(v, int):
            raw, fmt, maxlen = struct.pack("<I", v), SFO_INT, 4
        else:
            raw = str(v).encode() + b"\0"
            fmt, maxlen = SFO_UTF8, (len(raw) + 3) & ~3
        index += struct.pack("<HHIII", koff, fmt, len(raw), maxlen, len(data))
        data += raw + b"\0" * (maxlen - len(raw))
        koff += len(k) + 1
    key_start = 0x14 + len(index)
    data_start = key_start + len(key_blob)
    head = b"\0PSF" + struct.pack("<IIII", 0x0101, key_start, data_start, len(keys))
    return head + index + key_blob + data


def make_pkg(path, *, content_id: str, content_type: int, sfo: dict) -> Path:
    path = Path(path)
    sfo_blob = make_sfo(sfo)
    table_off, sfo_off = 0x2000, 0x3000
    body = bytearray(sfo_off + len(sfo_blob))
    struct.pack_into(">I", body, 0x00, 0x7F434E54)              # "\x7FCNT"
    struct.pack_into(">I", body, 0x10, 1)                       # entry_count
    struct.pack_into(">H", body, 0x14, 1)
    struct.pack_into(">H", body, 0x16, 1)
    struct.pack_into(">I", body, 0x18, table_off)               # entry_table_offset
    body[0x40:0x40 + 36] = content_id.encode().ljust(36, b"\0")[:36]
    struct.pack_into(">I", body, 0x74, content_type)
    body[table_off:table_off + 32] = struct.pack(">IIIIII8x", 0x1000, 0, 0, 0, sfo_off, len(sfo_blob))
    body[sfo_off:sfo_off + len(sfo_blob)] = sfo_blob
    path.write_bytes(bytes(body))
    return path
