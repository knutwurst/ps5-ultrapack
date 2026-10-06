"""Read PS4 packages (.pkg, title ids CUSA…): identity from the header and param.sfo.

Big-endian header: magic "\\x7FCNT" @0, entry_count @0x10, entry_table_offset @0x18,
content_id @0x40 (36 chars), content_type @0x74 (0x1A GD, 0x1B AC, 0x1C AL, 0x1E DP),
pfs_flags @0x408, pfs_image_offset @0x410, pfs_image_size @0x418. Entries are 32 bytes:
id, filename_offset, flags1, flags2, offset, size, 8 pad; param.sfo is entry 0x1000 and is
stored in the clear. The package content itself is read by the package tool (ps4-list,
ps4-extract), not here."""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = 0x7F434E54
ENTRY_PARAM_SFO = 0x1000
CT_GD, CT_AC, CT_AL, CT_DP = 0x1A, 0x1B, 0x1C, 0x1E


class Ps4PackageError(Exception):
    """The file is not a readable PS4 package."""


@dataclass
class Ps4Header:
    content_id: str
    content_type: int
    entry_count: int
    entry_table_offset: int
    pfs_flags: int
    pfs_image_offset: int
    pfs_image_size: int


@dataclass
class Ps4Identity:
    title: str
    title_id: str
    content_id: str
    kind: str          # game | update | dlc | other
    version: str       # APP_VER, else VERSION
    app_ver: str
    content_type: int


def read_header(path) -> Ps4Header:
    try:
        with open(path, "rb") as f:
            h = f.read(0x1000)
    except OSError as e:
        raise Ps4PackageError(f"cannot read {path}: {e}") from e
    if len(h) < 0x420 or struct.unpack_from(">I", h, 0)[0] != MAGIC:
        raise Ps4PackageError("not a CNT package")
    cid = h[0x40:0x40 + 36].split(b"\0", 1)[0].decode("ascii", "replace")
    return Ps4Header(
        content_id=cid,
        content_type=struct.unpack_from(">I", h, 0x74)[0],
        entry_count=struct.unpack_from(">I", h, 0x10)[0],
        entry_table_offset=struct.unpack_from(">I", h, 0x18)[0],
        pfs_flags=struct.unpack_from(">Q", h, 0x408)[0],
        pfs_image_offset=struct.unpack_from(">Q", h, 0x410)[0],
        pfs_image_size=struct.unpack_from(">Q", h, 0x418)[0],
    )


def is_ps4_package(path) -> bool:
    """True for a CNT package whose content id carries a CUSA title id. Never raises."""
    try:
        h = read_header(path)
    except Exception:
        return False
    return len(h.content_id) == 36 and h.content_id[7:11] == "CUSA"


def _read_entry(path, h: Ps4Header, entry_id: int) -> bytes:
    if not 0 < h.entry_count < 10000:
        raise Ps4PackageError("implausible entry count")
    with open(path, "rb") as f:
        f.seek(h.entry_table_offset)
        table = f.read(32 * h.entry_count)
        if len(table) < 32 * h.entry_count:
            raise Ps4PackageError("entry table cut short")
        for i in range(h.entry_count):
            eid, _fn, _f1, _f2, off, size = struct.unpack_from(">IIIIII", table, i * 32)
            if eid == entry_id:
                if size > 16 << 20:
                    raise Ps4PackageError("entry too large")
                f.seek(off)
                data = f.read(size)
                if len(data) < size:
                    raise Ps4PackageError("entry cut short")
                return data
    raise Ps4PackageError(f"entry 0x{entry_id:X} not found")


def parse_sfo(data: bytes) -> dict:
    if len(data) < 0x14 or data[:4] != b"\0PSF":
        raise Ps4PackageError("param.sfo has no PSF magic")
    key_start, data_start, count = struct.unpack_from("<III", data, 8)
    out: dict = {}
    try:
        for i in range(count):
            koff, fmt, length, _maxlen, doff = struct.unpack_from("<HHIII", data, 0x14 + i * 16)
            kend = data.index(b"\0", key_start + koff)
            key = data[key_start + koff:kend].decode("ascii", "replace")
            raw = data[data_start + doff:data_start + doff + length]
            if fmt == 0x0404:
                out[key] = struct.unpack_from("<I", raw.ljust(4, b"\0"))[0]
            else:
                out[key] = raw.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    except (struct.error, ValueError) as e:
        raise Ps4PackageError(f"param.sfo is damaged: {e}") from e
    return out


def _kind(category: str, content_type: int) -> str:
    c = (category or "").lower()
    if c == "gd":
        return "game"
    if c == "gp":
        return "update"
    if c == "ac" or content_type in (CT_AC, CT_AL):
        return "dlc"
    return "other"


def read_identity(path) -> Ps4Identity:
    h = read_header(path)
    sfo = parse_sfo(_read_entry(path, h, ENTRY_PARAM_SFO))
    app_ver = str(sfo.get("APP_VER") or "")
    tid = str(sfo.get("TITLE_ID") or h.content_id[7:16])
    return Ps4Identity(
        title=str(sfo.get("TITLE") or ""),
        title_id=tid.upper(),
        content_id=str(sfo.get("CONTENT_ID") or h.content_id),
        kind=_kind(str(sfo.get("CATEGORY") or ""), h.content_type),
        version=app_ver or str(sfo.get("VERSION") or ""),
        app_ver=app_ver,
        content_type=h.content_type,
    )
