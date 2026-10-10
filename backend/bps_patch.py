"""BPS binary patch format (byuu, 2014) - apply a .bps to a source file.

The format is a small delta encoding used by, among others, BestPig's BackPork patches
that turn a PS5 10.01 system library into the version an older firmware loads. Layout,
verbatim from the byuu specification:

    "BPS1"
    vli source_size
    vli target_size
    vli metadata_size
    metadata_size bytes of UTF-8 metadata

    while the target offset has not reached target_size:
        vli action                     # mode = action & 3, length = (action >> 2) + 1
        mode 0 SourceRead              # copy <length> bytes from source at the current
                                       #   target offset
        mode 1 TargetRead              # the next <length> bytes of the patch are the
                                       #   bytes to append
        mode 2 SourceCopy              # vli signed offset follows; move a source cursor
                                       #   and copy <length> bytes from there
        mode 3 TargetCopy              # vli signed offset follows; move a target cursor
                                       #   and copy <length> bytes from the target so far

    u32 le source_checksum             # CRC32 of the source
    u32 le target_checksum             # CRC32 of the target the caller must obtain
    u32 le patch_checksum              # CRC32 of everything above

A "vli" is a little-endian variable-length integer: 7 payload bits per byte, high bit
clear = "more follows"; each continuation adds an implicit 128**n offset so the encoding
is uniquely reversible. A signed vli uses bit 0 as the sign (1 = negative).

Public domain; the spec has no license.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass


BPS_MAGIC = b"BPS1"


class BpsError(ValueError):
    """The patch is malformed, or does not match the source given to :func:`apply`."""


# ── variable-length integer ───────────────────────────────────────────────
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
    """Only used by the tests to build synthetic patches; the patcher itself never
    writes a BPS. Encodes a non-negative integer as a byuu vli."""
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


# ── header ────────────────────────────────────────────────────────────────
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


# ── apply ─────────────────────────────────────────────────────────────────
def apply(patch: bytes, source: bytes, *, verify_source: bool = True) -> bytes:
    """Apply a .bps patch to *source* and return the target bytes.

    Raises :class:`BpsError` on a truncated patch, a mismatching source, or a mismatching
    output. *verify_source* off skips the source-CRC32 check for a source whose modest
    corruption the caller wants to see reflected in the output (BackPork's patches were
    built against pristine 10.01 libraries, so leave the check on for that use)."""
    hdr, pos = read_header(patch)
    if len(source) != hdr.source_size:
        raise BpsError(f"source is {len(source)} bytes, patch expects {hdr.source_size}")
    if len(patch) < pos + 12:                                # 3 × u32 footer
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
            # Byte-for-byte copy so an overlapping run (RLE) works: the end of the
            # window may advance into bytes we write in this same action.
            for i in range(length):
                out.append(out[tgt_cur + i])
            tgt_cur += length
        if len(out) > hdr.target_size:
            raise BpsError("actions produced more bytes than target_size declares")

    if len(out) != hdr.target_size:
        raise BpsError(f"actions produced {len(out)} bytes, target_size is {hdr.target_size}")
    if zlib.crc32(out) != tgt_crc:
        raise BpsError("patch applied cleanly but the target CRC does not match - the "
                       "source may be an intermediate variant the patch was not built for")
    return bytes(out)


# Convenience for the caller: apply from paths.
def apply_file(patch_path, source_path, target_path) -> None:
    from pathlib import Path
    Path(target_path).write_bytes(apply(Path(patch_path).read_bytes(), Path(source_path).read_bytes()))
