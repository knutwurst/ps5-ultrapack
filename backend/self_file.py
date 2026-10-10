"""Read and patch the SELF container that wraps a signed PS4/PS5 executable."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

SELF_MAGICS = {b"\x4f\x15\x3d\x1d": "ps4", b"\x54\x14\xf5\xee": "ps5"}

_EXT = struct.Struct("<I2HQ2H4x")
_ENTRY = struct.Struct("<4Q")
_PHDR = struct.Struct("<2I6Q")
_HEADERS_END = 0x20

_PROPS_ENCRYPTED = 1 << 1
_PROPS_COMPRESSED = 1 << 3
_PROPS_HAS_BLOCKS = 1 << 11
_PROPS_INDEX_SHIFT = 20
_PROPS_INDEX_MASK = 0xFFFF

PT_SCE_VERSION = 0x6FFFFF01


def is_self(data: bytes) -> bool:
    return bytes(data[:4]) in SELF_MAGICS


@dataclass
class SelfSegment:
    """Where one program header's bytes sit inside the SELF file."""
    phdr_index: int
    self_offset: int
    filesz: int
    encrypted: bool
    compressed: bool


@dataclass
class SelfImage:
    flavour: str                       # "ps4" or "ps5", from the magic
    elf_offset: int                    # where the embedded ELF header starts
    elf_header_size: int               # ELF header + program headers, as embedded
    file_size: int                     # the header's file size (the version data follows)
    phdrs: list[tuple] = field(default_factory=list)   # (type, flags, offset, vaddr, paddr, filesz, memsz, align)
    segments: dict[int, SelfSegment] = field(default_factory=dict)

    @property
    def readable(self) -> bool:
        """True when every stored segment is plain: a fake-signed SELF."""
        return all(not s.encrypted and not s.compressed for s in self.segments.values())


def parse_self(data: bytes, size: int | None = None) -> SelfImage | None:
    """The layout of a SELF, or None when *data* is not one this reader understands.
    *size* is the whole file's size when *data* holds only its first bytes (the
    headers): segments are then checked against the file, not against *data*."""
    limit = len(data) if size is None else size
    if len(data) < _HEADERS_END or not is_self(data):
        return None
    _key_type, _header_size, _meta_size, file_size, count, _flags = _EXT.unpack_from(data, 8)
    elf_offset = _HEADERS_END + count * _ENTRY.size
    if elf_offset + 0x40 > len(data) or data[elf_offset:elf_offset + 4] != b"\x7fELF":
        return None
    phoff = struct.unpack_from("<Q", data, elf_offset + 0x20)[0]
    ehsize, phentsize, phnum = struct.unpack_from("<3H", data, elf_offset + 0x34)
    if phentsize < _PHDR.size or elf_offset + phoff + phentsize * phnum > len(data):
        return None
    img = SelfImage(flavour=SELF_MAGICS[bytes(data[:4])], elf_offset=elf_offset,
                    elf_header_size=max(ehsize, phoff + phentsize * phnum), file_size=file_size)
    img.phdrs = [_PHDR.unpack_from(data, elf_offset + phoff + i * phentsize) for i in range(phnum)]
    for i in range(count):
        props, offset, filesz, _memsz = _ENTRY.unpack_from(data, _HEADERS_END + i * _ENTRY.size)
        if not props & _PROPS_HAS_BLOCKS:
            continue                   # a meta entry: block digests, not program bytes
        index = (props >> _PROPS_INDEX_SHIFT) & _PROPS_INDEX_MASK
        if index >= phnum or offset + filesz > limit:
            return None
        img.segments[index] = SelfSegment(index, offset, filesz,
                                          bool(props & _PROPS_ENCRYPTED), bool(props & _PROPS_COMPRESSED))
    return img


def elf_image(data: bytes, img: SelfImage | None = None) -> bytes | None:
    """An ELF image rebuilt from a fake-signed SELF: its headers, every stored segment at
    its file offset, and the version segment. Section headers are not kept in a SELF,
    so the image names none. None for a SELF with encrypted or compressed segments."""
    img = img or parse_self(data)
    if img is None or not img.readable:
        return None
    end = img.elf_header_size
    for index, seg in img.segments.items():
        end = max(end, img.phdrs[index][2] + seg.filesz)
    version = [(i, ph) for i, ph in enumerate(img.phdrs) if ph[0] == PT_SCE_VERSION and ph[5]]
    for _i, ph in version:
        end = max(end, ph[2] + ph[5])
    buf = bytearray(end)
    buf[:img.elf_header_size] = data[img.elf_offset:img.elf_offset + img.elf_header_size]
    for index, seg in img.segments.items():
        at = img.phdrs[index][2]
        buf[at:at + seg.filesz] = data[seg.self_offset:seg.self_offset + seg.filesz]
    for _i, ph in version:
        tail = data[img.file_size:img.file_size + ph[5]]
        buf[ph[2]:ph[2] + len(tail)] = tail
    struct.pack_into("<Q", buf, 0x28, 0)          # e_shoff: no section headers in the image
    struct.pack_into("<3H", buf, 0x3A, 0, 0, 0)   # e_shentsize, e_shnum, e_shstrndx
    return bytes(buf)


def self_offset(img: SelfImage, elf_offset: int, length: int = 1) -> int | None:
    """The SELF file offset of *length* bytes at *elf_offset* in the ELF, when they lie
    inside one stored segment; None otherwise."""
    for index, seg in img.segments.items():
        start = img.phdrs[index][2]
        if start <= elf_offset and elf_offset + length <= start + seg.filesz:
            return seg.self_offset + (elf_offset - start)
    return None
