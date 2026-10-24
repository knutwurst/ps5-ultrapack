"""BPS binary patch format (byuu, 2014) - apply a .bps to a source file."""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass


BPS_MAGIC = b"BPS1"
# system libs are a few MB, a bigger target means a hostile or broken patch
MAX_TARGET_SIZE = 1 << 30


class BpsError(ValueError):
    pass


def _decode_vli(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 1
    while True:
        if pos >= len(data):
            raise BpsError("truncated variable-length integer")
        b = data[pos]; pos += 1
        value += (b & 0x7F) * shift
        if b & 0x80:
            return value, pos
        shift <<= 7
        value += shift


def _decode_signed_vli(data: bytes, pos: int) -> tuple[int, int]:
    raw, pos = _decode_vli(data, pos)
    magnitude = raw >> 1
    return (-magnitude if raw & 1 else magnitude), pos


def _encode_vli(value: int) -> bytes:
    """tests only, the patcher never writes a BPS"""
    if value < 0:
        raise ValueError("vli is unsigned")
    out = bytearray()
    while True:
        seven = value & 0x7F
        value >>= 7
        if value == 0:
            out.append(seven | 0x80)
            return bytes(out)
        out.append(seven)
        value -= 1


def _encode_signed_vli(value: int) -> bytes:
    return _encode_vli((abs(value) << 1) | (1 if value < 0 else 0))


@dataclass(frozen=True)
class BpsHeader:
    source_size: int
    target_size: int
    metadata: bytes


def read_header(patch: bytes) -> tuple[BpsHeader, int]:
    if len(patch) < 4 or patch[:4] != BPS_MAGIC:
        raise BpsError("not a BPS patch (magic 'BPS1' missing)")
    pos = 4
    src, pos = _decode_vli(patch, pos)
    tgt, pos = _decode_vli(patch, pos)
    metasz, pos = _decode_vli(patch, pos)
    if pos + metasz > len(patch):
        raise BpsError("metadata length exceeds the patch")
    meta = patch[pos:pos + metasz]
    return BpsHeader(source_size=src, target_size=tgt, metadata=meta), pos + metasz


def apply(patch: bytes, source: bytes, *, verify_source: bool = True) -> bytes:
    """verify_source=False skips the source CRC; leave it on for BackPork's 10.01 libs"""
    hdr, pos = read_header(patch)
    if len(source) != hdr.source_size:
        raise BpsError(f"source is {len(source)} bytes, patch expects {hdr.source_size}")
    if hdr.target_size > MAX_TARGET_SIZE:
        raise BpsError(f"patch declares a target size of {hdr.target_size} bytes, more than "
                       f"any library can be")
    if len(patch) < pos + 12:  # 3 x u32 footer
        raise BpsError("patch is shorter than its footer")
    footer_start = len(patch) - 12
    src_crc, tgt_crc, patch_crc = struct.unpack_from("<III", patch, footer_start)
    if zlib.crc32(patch[:-4]) != patch_crc:
        raise BpsError("patch is corrupted (footer CRC mismatch)")
    if verify_source and zlib.crc32(source) != src_crc:
        raise BpsError("source does not match the patch (source CRC mismatch)")

    out = bytearray()
    src_cur = 0
    tgt_cur = 0
    while pos < footer_start:
        action, pos = _decode_vli(patch, pos)
        mode = action & 0b11
        length = (action >> 2) + 1
        if len(out) + length > hdr.target_size:
            raise BpsError("an action writes past the declared target size")
        if mode == 0:                                        # SourceRead
            if len(out) + length > hdr.source_size:
                raise BpsError("SourceRead overruns the source")
            out.extend(source[len(out):len(out) + length])
        elif mode == 1:                                      # TargetRead
            if pos + length > footer_start:
                raise BpsError("TargetRead payload extends past the actions")
            out.extend(patch[pos:pos + length])
            pos += length
        elif mode == 2:                                      # SourceCopy
            off, pos = _decode_signed_vli(patch, pos)
            src_cur += off
            if src_cur < 0 or src_cur + length > len(source):
                raise BpsError("SourceCopy escapes the source")
            out.extend(source[src_cur:src_cur + length])
            src_cur += length
        else:                                                # 3 = TargetCopy
            off, pos = _decode_signed_vli(patch, pos)
            tgt_cur += off
            if tgt_cur < 0 or tgt_cur >= len(out):
                raise BpsError("TargetCopy starts past the target written so far")
            # byte by byte on purpose: an overlapping run (RLE) reads what we just wrote
            for i in range(length):
                out.append(out[tgt_cur + i])
            tgt_cur += length

    if len(out) != hdr.target_size:
        raise BpsError(f"actions produced {len(out)} bytes, target_size is {hdr.target_size}")
    if zlib.crc32(out) != tgt_crc:
        raise BpsError("patch applied cleanly but the target CRC does not match - the "
                       "source may be an intermediate variant the patch was not built for")
    return bytes(out)


def apply_file(patch_path, source_path, target_path) -> None:
    from pathlib import Path
    Path(target_path).write_bytes(apply(Path(patch_path).read_bytes(), Path(source_path).read_bytes()))
