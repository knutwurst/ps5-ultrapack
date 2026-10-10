"""Backport engine - SDK version lowering, fakelib bundle assembly, NID compatibility analysis."""
from __future__ import annotations

import os
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:                                    # imported as backend.backport (tests) or backport (cli.py)
    from . import self_file
except ImportError:                     # pragma: no cover - the cli.py path
    import self_file

# ── SDK constants (verbatim from idlesauce's gist) ────────────────────────
# Keyed by human label. Value: (ps5_sdk_word, ps4_sdk_word). Both are 4-byte
# little-endian integers written into the SCE param struct.
SDK_TARGETS: dict[str, tuple[int, int]] = {
    "7.61":  (0x07000038, 0x10590001),
    "6.02":  (0x06000038, 0x10090001),
    "10.xx": (0x10000040, 0x12090001),
}

# ── ELF constants ─────────────────────────────────────────────────────────
_ELF_MAGIC = b"\x7fELF"
_ELFCLASS64 = 2
_ELFDATA2LSB = 1

# Elf64_Ehdr offsets used here.
_EH_PHOFF = 32       # Elf64_Off  e_phoff
_EH_PHENTSIZE = 54   # Elf64_Half e_phentsize
_EH_PHNUM = 56       # Elf64_Half e_phnum

# Elf64_Phdr: p_type (u32), p_flags (u32), p_offset (u64), p_vaddr (u64),
# p_paddr (u64), p_filesz (u64), p_memsz (u64), p_align (u64) = 56 bytes.
_PHDR_FMT = "<2I6Q"
_PHDR_SIZE = struct.calcsize(_PHDR_FMT)

PT_SCE_PROCPARAM = 0x61000001    # /app0/eboot.bin
PT_SCE_MODULE_PARAM = 0x61000002  # every .prx / .sprx

# Magic values at +0x08 inside the param struct. The layout is otherwise
# identical between the two, and the writer treats them the same way.
_MAGIC_PROC = 0x4942524F           # b"ORBI"
_MAGIC_MODULE = 0x3C13F4BF
_ACCEPTED_MAGICS = (_MAGIC_PROC, _MAGIC_MODULE)

# Byte offsets of the two 4-byte SDK fields inside the param struct.
_SDK_PS4_OFFSET = 0x10
_SDK_PS5_OFFSET = 0x14


# ── result types ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SdkChange:
    """One SDK field the downgrade would write, and what is there today."""
    kind: str        # "proc" or "module"
    field: str       # "ps5" or "ps4"
    before: int
    after: int

    def unchanged(self) -> bool:
        return self.before == self.after


# ── ELF walking ───────────────────────────────────────────────────────────
def _is_ps5_elf64(data: bytes) -> bool:
    """True if the blob starts with a 64-bit little-endian ELF header. Everything
    the tool cares about is 64-bit LE; a stray 32-bit ELF is left alone."""
    return (
        len(data) >= 64
        and data[:4] == _ELF_MAGIC
        and data[4] == _ELFCLASS64
        and data[5] == _ELFDATA2LSB
    )


def _as_elf(data: bytes) -> bytes | None:
    """The ELF bytes behind *data*: itself for a raw ELF, the rebuilt image for a
    fake-signed SELF, None for anything else (an encrypted SELF included)."""
    if self_file.is_self(data):
        return self_file.elf_image(data)
    return data if _is_ps5_elf64(data) else None


def _iter_phdrs(data: bytes):
    """Yield (ptype, p_offset, p_filesz) for each program header. Silently
    stops at the end of the file - a truncated ELF returns what it can."""
    if not _is_ps5_elf64(data):
        return
    e_phoff = struct.unpack_from("<Q", data, _EH_PHOFF)[0]
    e_phentsize = struct.unpack_from("<H", data, _EH_PHENTSIZE)[0]
    e_phnum = struct.unpack_from("<H", data, _EH_PHNUM)[0]
    if e_phentsize < _PHDR_SIZE:
        return
    for i in range(e_phnum):
        base = e_phoff + i * e_phentsize
        if base + _PHDR_SIZE > len(data):
            return
        p_type, _flags, p_offset, _vaddr, _paddr, p_filesz, _memsz, _align = \
            struct.unpack_from(_PHDR_FMT, data, base)
        yield p_type, p_offset, p_filesz


def _find_param_segment(data: bytes) -> tuple[str, int] | None:
    """Return ("proc"|"module", file offset of the param struct) or None.
    Prefers the first PT_SCE_PROCPARAM (eboot); a module has PT_SCE_MODULE_PARAM
    instead. A binary with both is unusual - the caller sees the first hit."""
    for p_type, p_offset, p_filesz in _iter_phdrs(data):
        if p_type not in (PT_SCE_PROCPARAM, PT_SCE_MODULE_PARAM):
            continue
        if p_filesz < _SDK_PS5_OFFSET + 4:
            continue
        magic = struct.unpack_from("<I", data, p_offset + 0x08)[0]
        if magic not in _ACCEPTED_MAGICS:
            continue
        return ("proc" if p_type == PT_SCE_PROCPARAM else "module", p_offset)
    return None


def sdk_words_from_reader(read, size: int, head_size: int = 4 << 20) -> tuple[int, int] | None:
    """(ps5, ps4) SDK words of an executable, raw or fake-signed, given *read(offset, n)*
    over its bytes and its *size*: only the headers and the param struct are read (an
    eboot can be hundreds of MB, maybe inside an image). None when there is none or
    the file is encrypted."""
    head = read(0, min(head_size, size))
    if self_file.is_self(head):
        img = self_file.parse_self(head, size)
        if img is None or not img.readable:
            return None
        spots = [(ph[0], ph[2], ph[5]) for ph in img.phdrs]
        where = lambda off: self_file.self_offset(img, off, 0x18)
    else:
        spots = list(_iter_phdrs(head))
        where = lambda off: off
    for p_type, p_offset, p_filesz in spots:
        if p_type not in (PT_SCE_PROCPARAM, PT_SCE_MODULE_PARAM) or p_filesz < 0x18:
            continue
        at = where(p_offset)
        if at is None or at + 0x18 > size:
            continue
        blob = read(at, 0x18)
        if len(blob) == 0x18 and struct.unpack_from("<I", blob, 0x08)[0] in _ACCEPTED_MAGICS:
            ps4, ps5 = struct.unpack_from("<2I", blob, _SDK_PS4_OFFSET)
            return ps5, ps4
    return None


def sdk_words_of_file(path: Path, head_size: int = 4 << 20) -> tuple[int, int] | None:
    """sdk_words_from_reader for a file on disk."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size

            def read(offset: int, n: int) -> bytes:
                f.seek(offset)
                return f.read(n)
            return sdk_words_from_reader(read, size, head_size)
    except OSError:
        return None


# ── targets: the public table, or the user's own firmware files ───────────
_FW_NAME = re.compile(r"^\d{1,2}\.\d{2}$")


def _fw_key(name: str) -> tuple[int, int]:
    major, minor = name.split(".")
    return int(major), int(minor)


def firmware_folders(fw_root: Path | None) -> dict[str, Path]:
    """{firmware: folder} for every subfolder of *fw_root* named like a firmware
    (7.61, 10.01, ...), sorted by version. A root without such subfolders that is
    itself named like one (the setting points at a single firmware) is that one."""
    if not fw_root:
        return {}
    root = Path(fw_root)
    if not root.is_dir():
        return {}
    found = [d for d in root.iterdir() if d.is_dir() and _FW_NAME.match(d.name)]
    if not found and _FW_NAME.match(root.name):
        return {root.name: root}
    return {d.name: d for d in sorted(found, key=lambda d: _fw_key(d.name))}


def firmware_folder(fw_root: Path | None, target: str) -> Path | None:
    """The folder holding *target*'s original libraries under *fw_root*.

    With one subfolder per firmware: <root>/<target>; for the SDK-only target
    "10.xx" the lowest 10.* folder. A root with libraries but no firmware name
    anywhere (an older single-folder setting) is used for every target."""
    if not fw_root:
        return None
    root = Path(fw_root)
    folders = firmware_folders(root)
    if folders:
        if target in folders:
            return folders[target]
        if target == "10.xx":
            tens = [n for n in folders if n.startswith("10.")]
            return folders[tens[0]] if tens else None
        return None
    if root.is_dir() and any(root.rglob("*.sprx")):
        return root
    return None


def _target_key(name: str) -> tuple[int, int]:
    return (10, -1) if name == "10.xx" else _fw_key(name)


def available_targets(fw_root: Path | None) -> list[str]:
    """The public targets and every firmware found under *fw_root*, by version."""
    names = set(SDK_TARGETS) | set(firmware_folders(fw_root))
    return sorted(names, key=_target_key)


def patched_libs_folder(libs_root: Path | None, target: str) -> Path | None:
    """The patched libraries for *target*: <root>/<target> when Prepare made one, the
    root itself when the user filled it by hand. None when the root only holds sets
    for other targets."""
    if not libs_root:
        return None
    root = Path(libs_root)
    if not root.is_dir():
        return None
    sets = firmware_folders(root)
    if not sets:
        return root
    return sets.get(target)


def patched_libs_problem(libs_root: Path | None, fw_root: Path | None) -> str:
    """Why *libs_root* cannot serve as the patched libraries; '' when it can. The firmware
    libraries folder, or a folder inside it, holds ORIGINAL libraries: copied into
    fakelib/ they would load in place of the console's own."""
    if not libs_root or not fw_root:
        return ""
    try:
        libs, fw = Path(libs_root).resolve(), Path(fw_root).resolve()
    except OSError:
        return ""
    if libs == fw or fw in libs.parents:
        return ("the patched libraries folder is your firmware libraries folder, which holds the "
                "original libraries; choose the folder with the patched ones, or leave it empty")
    return ""


def sdk_firmware(word: int) -> str:
    """The firmware a PS5 SDK word names: its top half is BCD, 0x07610000 = 7.61."""
    def bcd(b: int) -> int:
        return (b >> 4) * 10 + (b & 0xF)
    return f"{bcd((word >> 24) & 0xFF)}.{bcd((word >> 16) & 0xFF):02d}"


def derive_sdk_words(fw_dir: Path, sample: int = 80) -> tuple[int, int] | None:
    """(ps5, ps4) SDK words of a firmware, read from its own system libraries: the
    most common pair in their module params. None when no library carries one."""
    counts: Counter = Counter()
    for path in sorted(Path(fw_dir).rglob("*.sprx"))[:sample]:
        try:
            elf = _as_elf(path.read_bytes())
        except OSError:
            continue
        hit = _find_param_segment(elf) if elf else None
        if hit:
            ps4, ps5 = struct.unpack_from("<2I", elf, hit[1] + _SDK_PS4_OFFSET)
            counts[(ps5, ps4)] += 1
    return counts.most_common(1)[0][0] if counts else None


def firmware_problem(fw_dir: Path | None, name: str | None) -> str:
    """Why the libraries in *fw_dir* cannot stand for firmware *name*; '' when they can."""
    label = name or "firmware"
    if fw_dir is None:
        return f"no {label} folder in the firmware libraries folder"
    if not any(Path(fw_dir).rglob("*.sprx")):
        return f"the {label} folder holds no .sprx libraries"
    words = derive_sdk_words(fw_dir)
    if words is None:
        return f"the {label} libraries are encrypted; the check and the SDK values need decrypted ones"
    real = sdk_firmware(words[0])
    if name and _FW_NAME.match(name) and _FW_NAME.match(real) and _fw_key(real) > _fw_key(name):
        return f"the libraries in the {name} folder are from firmware {real}"
    return ""


def target_words(target: str, fw_root: Path | None = None) -> tuple[int, int]:
    """(ps5, ps4) SDK words for *target*: the public table for 7.61, 6.02 and 10.xx,
    otherwise read from that firmware's libraries under *fw_root*. ValueError when
    neither knows the target, or when its folder cannot stand for that firmware."""
    if target in SDK_TARGETS:
        return SDK_TARGETS[target]
    if not (fw_root and _FW_NAME.match(target)):
        raise ValueError(f"unknown backport target {target!r}: not in the public table "
                         f"({', '.join(SDK_TARGETS)}) and no firmware libraries folder to read it from")
    fw_dir = firmware_folder(fw_root, target)
    problem = firmware_problem(fw_dir, target)
    if problem:
        raise ValueError(f"backport target {target}: {problem}")
    return derive_sdk_words(fw_dir)


def is_encrypted_self(path: Path, head_size: int = 1 << 16) -> bool:
    """A SELF whose segments are encrypted or compressed (a retail executable). Reads
    the headers only."""
    try:
        with open(path, "rb") as f:
            head = f.read(head_size)
            size = os.fstat(f.fileno()).st_size
    except OSError:
        return False
    if not self_file.is_self(head):
        return False
    img = self_file.parse_self(head, size)
    return img is not None and not img.readable


def encrypted_selfs(root: Path) -> list[Path]:
    """Executables under *root* that are SELFs with encrypted or compressed
    segments: no backport can read or change them."""
    return [path for path in iter_source_elfs(root) if is_encrypted_self(path)]


# ── the writes ────────────────────────────────────────────────────────────
def lower_sdk_version(elf: bytes, target: str,
                      words: tuple[int, int] | None = None) -> tuple[bytes, list[SdkChange]]:
    """Return a copy of *elf* with its SDK words lowered to *target*, plus one SdkChange per field considered (kept even when equal, so the caller can log a no-op)."""
    if words is None:
        if target not in SDK_TARGETS:
            raise ValueError(f"unknown backport target {target!r}; expected one of {sorted(SDK_TARGETS)}")
        words = SDK_TARGETS[target]
    ps5_target, ps4_target = words
    if self_file.is_self(elf):
        img = self_file.parse_self(elf)
        image = self_file.elf_image(elf, img) if img else None
        if image is None:
            raise ValueError("an encrypted or unreadable SELF: a backport needs decrypted or "
                             "fake-signed executables")
        hit = _find_param_segment(image)
        if hit is None:
            return elf, []
        kind, param = hit
        base = self_file.self_offset(img, param + _SDK_PS4_OFFSET, 8)
        if base is None:                      # the param struct is not in a stored segment
            return elf, []
        base -= _SDK_PS4_OFFSET
    else:
        hit = _find_param_segment(elf)
        if hit is None:
            return elf, []
        kind, base = hit
    changes: list[SdkChange] = []
    buf = bytearray(elf)
    for field, offset, new_val in (("ps4", _SDK_PS4_OFFSET, ps4_target),
                                   ("ps5", _SDK_PS5_OFFSET, ps5_target)):
        cur = struct.unpack_from("<I", buf, base + offset)[0]
        after = min(cur, new_val)
        changes.append(SdkChange(kind=kind, field=field, before=cur, after=after))
        if after != cur:
            struct.pack_into("<I", buf, base + offset, after)
    return bytes(buf), changes


# ── file walking (for a game folder / staged /app0 root) ──────────────────
_ELF_SUFFIXES = (".prx", ".sprx", ".elf", ".self")


def iter_source_elfs(root: Path):
    """Yield every path under *root* that could carry SDK metadata: eboot.bin,
    plus every .prx / .sprx / .elf / .self. Symlinks are skipped (safety, and
    the staging step handles their contents already)."""
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        name = p.name.lower()
        if name == "eboot.bin" or name.endswith(_ELF_SUFFIXES):
            yield p


@dataclass
class LowerReport:
    """What the downgrade pass did to a folder. `written` lists the files that
    actually changed (source path, effective changes). `skipped` lists files
    that carry no param segment or are not raw ELFs - the caller reports those
    but never treats them as an error."""
    written: list[tuple[Path, list[SdkChange]]]
    skipped_no_param: list[Path]
    skipped_not_elf: list[Path]          # neither a raw ELF nor a SELF this reader understands
    self_patched: list[Path] = field(default_factory=list)   # fake-signed, lowered in place
    encrypted: list[Path] = field(default_factory=list)      # SELFs with encrypted segments, untouched

    def summary(self) -> str:
        changed = sum(1 for _p, ch in self.written if any(not c.unchanged() for c in ch))
        parts = [f"lowered the SDK in {changed} file(s)"
                 + (f" ({len(self.self_patched)} fake-signed, changed in place)" if self.self_patched else "")]
        if len(self.written) > changed:
            parts.append(f"{len(self.written) - changed} already at or below the target")
        if self.skipped_no_param:
            parts.append(f"{len(self.skipped_no_param)} without a param segment")
        if self.skipped_not_elf:
            parts.append(f"{len(self.skipped_not_elf)} not executables")
        if self.encrypted:
            parts.append(f"{len(self.encrypted)} encrypted")
        return "; ".join(parts)


# ── SCE dynamic tags (from pedrocluis/sce-elf + SocraticBliss PS4-SELF-Tools) ─
# Every value is an Elf64_Xword d_tag as it appears in PT_DYNAMIC. The reader
# only needs a handful of these - the rest are documented so a future user of
# this module knows which tag is which without another round of research.
DT_NEEDED         = 0x00000001    # standard ELF, PS5 may still use it
DT_STRTAB         = 0x00000005
DT_SYMTAB         = 0x00000006
DT_STRSZ          = 0x0000000A
DT_SYMENT         = 0x0000000B
DT_SCE_MODULE_INFO   = 0x6100000D
DT_SCE_NEEDED_MODULE = 0x6100000F
DT_SCE_MODULE_ATTR   = 0x61000011
DT_SCE_EXPORT_LIB    = 0x61000013
DT_SCE_IMPORT_LIB    = 0x61000015
DT_SCE_EXPORT_LIB_ATTR = 0x61000017
DT_SCE_IMPORT_LIB_ATTR = 0x61000019
DT_SCE_HASH          = 0x61000025
DT_SCE_JMPREL        = 0x61000029
DT_SCE_PLTRELSZ      = 0x6100002D
DT_SCE_STRTAB        = 0x61000035
DT_SCE_STRSZ         = 0x61000037
DT_SCE_SYMTAB        = 0x61000039
DT_SCE_SYMENT        = 0x6100003B
DT_SCE_SYMTABSZ      = 0x6100003F
# PS5 respellings that appear in some retail modules (pedrocluis notes 2026).
DT_SCE_NEEDED_MODULE_PS5 = 0x61000045
DT_SCE_EXPORT_LIB_PS5    = 0x61000047
DT_SCE_IMPORT_LIB_PS5    = 0x61000049

PT_DYNAMIC = 0x2
PT_LOAD = 0x1
PT_SCE_DYNLIBDATA = 0x61000000

# NID base64 alphabet: standard base64 with '/' → '-' (no padding).
_NID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"
NID_LENGTH = 11


def encode_id(v: int) -> str:
    """shadPS4's EncodeId: pack an unsigned 16-bit id into 1..3 base64 characters.
    <0x40 → 1 char, <0x1000 → 2 chars, else 3 chars. Matches the id chars that
    follow the '#' in an imported symbol name."""
    if v < 0 or v >= 0x10000:
        raise ValueError(f"library/module id out of range: {v}")
    out = []
    if v >= 0x1000: out.append(_NID_ALPHABET[(v >> 12) & 0x3f])
    if v >= 0x40:   out.append(_NID_ALPHABET[(v >> 6)  & 0x3f])
    out.append(_NID_ALPHABET[v & 0x3f])
    return "".join(out)


@dataclass(frozen=True)
class NidImport:
    """One imported function: its 11-character NID, the library name it comes
    from (resolved via DT_SCE_IMPORT_LIB), and the imported-module name it lives
    in (resolved via DT_SCE_NEEDED_MODULE). `library` / `module` may be empty
    when the id was not in the dynamic table (defensive)."""
    nid: str
    library: str
    module: str


def _iter_dyn_entries(data: bytes):
    """Yield (d_tag, d_val) for each Elf64_Dyn in the file's PT_DYNAMIC segment.
    An ELF without a PT_DYNAMIC yields nothing."""
    if not _is_ps5_elf64(data):
        return
    for p_type, p_offset, p_filesz in _iter_phdrs(data):
        if p_type != PT_DYNAMIC:
            continue
        end = min(p_offset + p_filesz, len(data))
        pos = p_offset
        while pos + 16 <= end:
            d_tag, d_val = struct.unpack_from("<QQ", data, pos)
            pos += 16
            if d_tag == 0:                       # DT_NULL terminates
                return
            yield d_tag, d_val
        return


def _dynlibdata_slice(data: bytes) -> bytes:
    """Bytes of the PT_SCE_DYNLIBDATA segment, or b'' when absent (some PS5
    modules keep the tables in PT_LOAD instead)."""
    for p_type, p_offset, p_filesz in _iter_phdrs(data):
        if p_type == PT_SCE_DYNLIBDATA:
            return data[p_offset:p_offset + p_filesz]
    return b""


def _load_segments(data: bytes):
    """List of (vaddr, size, file_offset, filesz) for each PT_LOAD segment,
    used to resolve a virtual address in the PS5 dual-layout case."""
    out = []
    for p_type, p_offset, p_filesz in _iter_phdrs(data):
        if p_type != PT_LOAD:
            continue
        # We need vaddr + memsz for a proper vaddr → file_offset walk; re-read.
        # _iter_phdrs only exposes offset/filesz for the SDK-lowering job - a
        # second parse here keeps that helper focused.
        # (Not worth generalising; PT_LOAD count is tiny.)
    # Full re-parse: pull vaddr/memsz too.
    if not _is_ps5_elf64(data):
        return out
    e_phoff = struct.unpack_from("<Q", data, _EH_PHOFF)[0]
    e_phentsize = struct.unpack_from("<H", data, _EH_PHENTSIZE)[0]
    e_phnum = struct.unpack_from("<H", data, _EH_PHNUM)[0]
    for i in range(e_phnum):
        base = e_phoff + i * e_phentsize
        if base + _PHDR_SIZE > len(data): break
        p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _align = \
            struct.unpack_from(_PHDR_FMT, data, base)
        if p_type == PT_LOAD:
            out.append((p_vaddr, p_filesz, p_offset, p_filesz))
    return out


def _read_at_vaddr(data: bytes, loads, vaddr: int, size: int) -> bytes:
    """Read *size* bytes at virtual address *vaddr* by walking PT_LOAD entries.
    Returns b'' when the range is not covered (unusual; a game whose DT_STRTAB
    points outside every PT_LOAD is malformed)."""
    for v, vsz, off, fsz in loads:
        if v <= vaddr < v + vsz:
            local = vaddr - v
            end = min(local + size, fsz)
            return data[off + local:off + end]
    return b""


def _read_table(data: bytes, dynlibdata: bytes, loads, addr_or_off: int, size: int) -> bytes:
    """The SCE dual layout: PS4-shaped SELFs put tables at *offsets into
    PT_SCE_DYNLIBDATA*, while a PS5 module can put them at *virtual addresses
    into PT_LOAD*. Try the dynlibdata first (it is what BestPig/Auto-Backpork
    walk), fall back to vaddr resolution."""
    if 0 <= addr_or_off < len(dynlibdata) and addr_or_off + size <= len(dynlibdata):
        # Only trust this branch when dynlibdata actually holds the requested
        # bytes; otherwise the offset happened to fit but the payload is not
        # ours.
        candidate = dynlibdata[addr_or_off:addr_or_off + size]
        if len(candidate) == size:
            return candidate
    return _read_at_vaddr(data, loads, addr_or_off, size)


def _cstr(buf: bytes, offset: int) -> str:
    """C-string starting at *offset* in *buf*, decoded as UTF-8 (Sony names are
    ASCII in practice). Empty string for an out-of-range offset."""
    if offset < 0 or offset >= len(buf):
        return ""
    end = buf.find(b"\x00", offset)
    return buf[offset:end if end >= 0 else len(buf)].decode("utf-8", errors="replace")


def read_symbols(data: bytes) -> tuple[list[NidImport], list[NidImport]]:
    """Return (imports, exports) as NidImport lists."""
    data = _as_elf(data)
    if data is None or not _is_ps5_elf64(data):
        return [], []
    dyn = list(_iter_dyn_entries(data))
    if not dyn:
        return [], []
    dynlibdata = _dynlibdata_slice(data)
    loads = _load_segments(data)

    def pick(*tags: int):
        for t, v in dyn:
            if t in tags:
                return v
        return None

    strtab_addr = pick(DT_SCE_STRTAB, DT_STRTAB)
    strtab_size = pick(DT_SCE_STRSZ, DT_STRSZ)
    symtab_addr = pick(DT_SCE_SYMTAB, DT_SYMTAB)
    symtab_size = pick(DT_SCE_SYMTABSZ)
    syment = pick(DT_SCE_SYMENT, DT_SYMENT) or 24
    if None in (strtab_addr, strtab_size, symtab_addr, symtab_size):
        return [], []

    strtab = _read_table(data, dynlibdata, loads, strtab_addr, strtab_size)
    symtab = _read_table(data, dynlibdata, loads, symtab_addr, symtab_size)
    if not strtab or not symtab:
        return [], []

    # Library / module id → name maps. A dynamic value packs the id in its
    # high halfword and the string-table offset in its low 32 bits (Sony's
    # convention; see sce-elf/dynamic.rs). PS5 respellings share the layout.
    libs: dict[str, str] = {}
    mods: dict[str, str] = {}
    for t, v in dyn:
        name_off = v & 0xFFFFFFFF
        the_id = (v >> 48) & 0xFFFF
        if t in (DT_SCE_IMPORT_LIB, DT_SCE_EXPORT_LIB, DT_SCE_IMPORT_LIB_PS5, DT_SCE_EXPORT_LIB_PS5):
            libs[encode_id(the_id)] = _cstr(strtab, name_off)
        elif t in (DT_SCE_NEEDED_MODULE, DT_SCE_MODULE_INFO, DT_SCE_NEEDED_MODULE_PS5):
            mods[encode_id(the_id)] = _cstr(strtab, name_off)

    imports: list[NidImport] = []
    exports: list[NidImport] = []
    for i in range(0, len(symtab), syment):
        if i + 24 > len(symtab):
            break
        st_name, _st_info, _st_other, st_shndx, _st_value, _st_size = \
            struct.unpack_from("<IBBHQQ", symtab, i)
        name = _cstr(strtab, st_name)
        parts = name.split("#")
        if len(parts) != 3:
            continue
        nid, lib_enc, mod_enc = parts
        if len(nid) != NID_LENGTH or any(c not in _NID_ALPHABET for c in nid):
            continue
        entry = NidImport(nid=nid, library=libs.get(lib_enc, ""), module=mods.get(mod_enc, ""))
        (imports if st_shndx == 0 else exports).append(entry)
    return imports, exports


def read_imported_nids(data: bytes) -> list[NidImport]:
    """Convenience: just the imports from :func:`read_symbols`. Kept as a
    named entry point because the compatibility check reads imports far more
    often than exports."""
    return read_symbols(data)[0]


def read_exported_nids(data: bytes) -> list[NidImport]:
    """Convenience: just the exports from :func:`read_symbols`."""
    return read_symbols(data)[1]


# ── firmware NID database + game compatibility check (phase 2) ────────────
@dataclass
class LibraryReport:
    """One imported library's compatibility with the target firmware."""
    library: str                    # e.g. "libSceAgc"
    used_nids: set[str]             # NIDs the game imports from it
    in_firmware: bool               # firmware module exports at least one NID?
    firmware_covers: set[str]       # subset of used_nids the firmware exports
    fakelib_covers: set[str]        # subset covered by user-supplied fakelib
    unresolved: set[str]            # in none of them
    game_covers: set[str] = field(default_factory=set)   # exported by the game's own modules

    @property
    def status(self) -> str:
        if not self.used_nids: return "empty"
        covered = self.firmware_covers | self.fakelib_covers | self.game_covers
        if covered >= self.used_nids: return "ok"
        if self.in_firmware or self.fakelib_covers or self.game_covers: return "partial"
        return "missing"


@dataclass
class BackportReport:
    """Whole-game compatibility summary."""
    target: str
    per_library: list[LibraryReport]
    unnamed_imports: int            # symbols we could not map to a library name
    firmware_checked: bool = False  # the target firmware's own libraries were read
    firmware_note: str = ""         # why the firmware folder could not stand for the target
    unreadable: list[str] = field(default_factory=list)   # encrypted executables of the game

    def unresolved_count(self) -> int:
        return sum(len(l.unresolved) for l in self.per_library)

    def unresolved_libraries(self) -> list[str]:
        return [l.library for l in self.per_library if l.unresolved]

    def summary(self) -> str:
        n = len(self.per_library)
        ok = sum(1 for l in self.per_library if l.status == "ok")
        partial = sum(1 for l in self.per_library if l.status == "partial")
        missing = sum(1 for l in self.per_library if l.status == "missing")
        return (f"target {self.target}: {ok}/{n} libraries fully covered, "
                f"{partial} partial, {missing} missing"
                + (f", {self.unnamed_imports} unresolved import(s)" if self.unnamed_imports else ""))

    def blocking_libraries(self) -> list[str]:
        return [l.library for l in self.per_library if l.status == "missing"]

    def needs_patched_libraries(self) -> bool:
        """Some function is only there thanks to the user's patched libraries."""
        return any(l.fakelib_covers - l.firmware_covers - l.game_covers for l in self.per_library)

    def verdict(self, patched: bool = False) -> str:
        """One plain sentence: what the report means for the build. *patched* says
        whether patched libraries for the target were part of the check."""
        if self.unreadable:
            return (f"{len(self.unreadable)} executable(s) are encrypted, so nothing can be checked "
                    f"or changed. Decrypt the game first.")
        if self.firmware_note:
            return f"Not checked: {self.firmware_note}."
        if not self.firmware_checked:
            return f"Not checked: no functions could be read from the {self.target} libraries."
        missing = self.unresolved_count()
        libs = self.unresolved_libraries()
        where = ", ".join(libs[:4]) + (", …" if len(libs) > 4 else "")
        if not missing:
            if self.needs_patched_libraries():
                return f"Every function the game uses is available on {self.target} with your patched libraries."
            return f"Firmware {self.target} has every function the game uses. Lowering the SDK is enough."
        if patched:
            return (f"{missing} function(s) are in neither {self.target} nor your patched libraries "
                    f"({where}). The game may stop when it calls them.")
        lacks = f"The game uses {missing} function(s) that {self.target} lacks ({where})."
        if self.target in ("7.61", "6.02"):
            return f"{lacks} It needs the patched libraries for {self.target}; Prepare {self.target} in Settings makes them."
        return f"{lacks} It needs patched libraries for {self.target}, and public ones exist only for 7.61 and 6.02."


def build_firmware_nid_db(fw_libs_root: Path) -> dict[str, set[str]]:
    """Read every *.prx/*.sprx under *fw_libs_root* and return {library_name: {NID, ...}}."""
    fw_libs_root = Path(fw_libs_root)
    db: dict[str, set[str]] = {}
    for path in sorted(fw_libs_root.rglob("*")):     # system/common/lib, system_ex/..., priv/lib
        if not path.is_file() or path.suffix.lower() not in (".prx", ".sprx"):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        exports = read_exported_nids(data)
        if not exports:
            continue
        # Every export declares its own library via the '#lib' field; a module
        # may publish more than one (e.g. libc + libc_internal). Group by that.
        for e in exports:
            db.setdefault(e.library or path.stem, set()).add(e.nid)
    return db


def analyse_backport(source_root: Path, target: str,
                     fw_libs_root: Path | None = None,
                     backport_libs_root: Path | None = None) -> BackportReport:
    """Cross-check the ELFs under *source_root* against the target firmware."""
    fw_dir = firmware_folder(fw_libs_root, target) if fw_libs_root else None
    fw_db = build_firmware_nid_db(fw_dir) if fw_dir else {}
    note = "no firmware libraries folder given"
    if fw_libs_root:
        if fw_dir is None:
            note = firmware_problem(None, target)
        else:
            note = firmware_problem(fw_dir, fw_dir.name if _FW_NAME.match(fw_dir.name) else None)
    fk_dir = (patched_libs_folder(backport_libs_root, target)
              if backport_libs_root and not patched_libs_problem(backport_libs_root, fw_libs_root) else None)
    fakelib_db = build_firmware_nid_db(fk_dir) if fk_dir else {}

    per_lib: dict[str, set[str]] = {}
    own: dict[str, set[str]] = {}
    unnamed = 0
    unreadable: list[str] = []
    source_root = Path(source_root)
    for path in iter_source_elfs(source_root):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        elf = _as_elf(data)
        if elf is None:
            img = self_file.parse_self(data) if self_file.is_self(data) else None
            if img is not None and not img.readable:
                unreadable.append(str(path.relative_to(source_root)))
            continue
        imports, exports = read_symbols(elf)
        for imp in imports:
            if not imp.library:
                unnamed += 1
                continue
            per_lib.setdefault(imp.library, set()).add(imp.nid)
        for exp in exports:
            own.setdefault(exp.library or path.stem, set()).add(exp.nid)

    reports: list[LibraryReport] = []
    for lib in sorted(per_lib):
        used = per_lib[lib]
        fw = fw_db.get(lib, set())
        fk = fakelib_db.get(lib, set())
        mine = own.get(lib, set())
        reports.append(LibraryReport(
            library=lib,
            used_nids=used,
            in_firmware=lib in fw_db,
            firmware_covers=used & fw,
            fakelib_covers=used & fk,
            unresolved=used - fw - fk - mine,
            game_covers=used & mine,
        ))
    return BackportReport(target=target, per_library=reports, unnamed_imports=unnamed,
                          firmware_checked=bool(fw_db), firmware_note=note, unreadable=unreadable)


def lower_sdk_in_folder(root: Path, target: str, words: tuple[int, int] | None = None) -> LowerReport:
    """Walk *root* and apply lower_sdk_version to every eboot/prx/sprx it holds, in place."""
    report = LowerReport(written=[], skipped_no_param=[], skipped_not_elf=[])
    for path in iter_source_elfs(root):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        signed = self_file.is_self(data)
        if signed:
            img = self_file.parse_self(data)
            if img is None:
                report.skipped_not_elf.append(path)
                continue
            if not img.readable:
                report.encrypted.append(path)
                continue
        elif not _is_ps5_elf64(data):
            report.skipped_not_elf.append(path)
            continue
        new_data, changes = lower_sdk_version(data, target, words)
        effective = [c for c in changes if not c.unchanged()]
        if not changes:
            report.skipped_no_param.append(path)
            continue
        if not effective:
            report.written.append((path, changes))          # already at/below target
            continue
        tmp = path.with_suffix(path.suffix + ".backport-tmp")
        try:
            tmp.write_bytes(new_data)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
        report.written.append((path, changes))
        if signed:
            report.self_patched.append(path)
    return report
