"""python3 -m unittest backend.tests.test_backport"""
from __future__ import annotations

import struct
import tempfile
import os
import unittest
from pathlib import Path

from backend import backport as bp


def _elf_with_param(param_type: int, magic: int, sdk_ps4: int, sdk_ps5: int) -> bytes:
    """zeroed header around a Sony-style 40-byte param struct, so stray writes show up"""
    ehdr = bytearray(64)
    ehdr[0:4] = b"\x7fELF"
    ehdr[4] = 2                       # ELFCLASS64
    ehdr[5] = 1                       # ELFDATA2LSB
    e_phoff = 64
    e_phentsize = 56
    e_phnum = 1
    struct.pack_into("<Q", ehdr, 32, e_phoff)
    struct.pack_into("<H", ehdr, 54, e_phentsize)
    struct.pack_into("<H", ehdr, 56, e_phnum)

    param = bytearray(40)
    struct.pack_into("<Q", param, 0x00, 40)              # size
    struct.pack_into("<I", param, 0x08, magic)
    struct.pack_into("<I", param, 0x0C, 1)               # version
    struct.pack_into("<I", param, 0x10, sdk_ps4)
    struct.pack_into("<I", param, 0x14, sdk_ps5)
    struct.pack_into("<I", param, 0x18, 0xdeadbeef)      # anything past the SDK words stays
    struct.pack_into("<I", param, 0x1C, 0xbadf00d0)

    p_offset = 64 + e_phentsize
    p_filesz = len(param)
    phdr = struct.pack("<II6Q", param_type, 0, p_offset, 0, 0, p_filesz, 0, 0)
    return bytes(ehdr) + phdr + bytes(param)


class LowerSdkVersion(unittest.TestCase):
    def test_lowers_both_fields_when_source_is_newer(self):
        elf = _elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0x11090001, 0x08000041)
        out, changes = bp.lower_sdk_version(elf, "7.61")
        # PS4 11.09 to 7.59, PS5 8.00 to 7.00, per idlesauce's table
        self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x10)[0], 0x10590001)
        self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x14)[0], 0x07000038)
        self.assertEqual([(c.field, c.before, c.after) for c in changes],
                         [("ps4", 0x11090001, 0x10590001),
                          ("ps5", 0x08000041, 0x07000038)])
        self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x18)[0], 0xdeadbeef)

    def test_leaves_lower_source_alone(self):
        elf = _elf_with_param(bp.PT_SCE_MODULE_PARAM, 0x3C13F4BF, 0x10090001, 0x06000038)
        out, changes = bp.lower_sdk_version(elf, "7.61")
        self.assertEqual(out, elf)
        self.assertTrue(all(c.unchanged() for c in changes))

    def test_returns_no_change_for_elf_without_param(self):
        ehdr = bytearray(64)
        ehdr[0:4] = b"\x7fELF"
        ehdr[4] = 2; ehdr[5] = 1
        struct.pack_into("<Q", ehdr, 32, 64)
        struct.pack_into("<H", ehdr, 54, 56)
        struct.pack_into("<H", ehdr, 56, 1)
        phdr = struct.pack("<II6Q", 1, 0, 120, 0, 0, 16, 0, 0)   # PT_LOAD, 16 bytes
        payload = bytes(16)
        elf = bytes(ehdr) + phdr + payload
        out, changes = bp.lower_sdk_version(elf, "7.61")
        self.assertEqual(out, elf)
        self.assertEqual(changes, [])

    def test_ignores_param_with_wrong_magic(self):
        elf = _elf_with_param(bp.PT_SCE_PROCPARAM, 0xDEADBEEF, 0x11090001, 0x08000041)
        out, changes = bp.lower_sdk_version(elf, "7.61")
        self.assertEqual(out, elf)
        self.assertEqual(changes, [])

    def test_all_three_targets_write_the_expected_words(self):
        for label, (ps5, ps4) in bp.SDK_TARGETS.items():
            with self.subTest(target=label):
                elf = _elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0xFFFFFFFF, 0xFFFFFFFF)
                out, _ = bp.lower_sdk_version(elf, label)
                self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x10)[0], ps4)
                self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x14)[0], ps5)

    def test_rejects_unknown_target(self):
        with self.assertRaises(ValueError):
            bp.lower_sdk_version(b"\x7fELF" + b"\x00" * 60, "8.60")

    def test_ignores_a_non_elf(self):
        out, changes = bp.lower_sdk_version(b"not an ELF at all" + b"\x00" * 64, "7.61")
        self.assertEqual(out, b"not an ELF at all" + b"\x00" * 64)
        self.assertEqual(changes, [])


class WalkAndWrite(unittest.TestCase):
    def test_lower_sdk_in_folder_rewrites_only_the_changed_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eboot = _elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0x11090001, 0x08000041)
            low = _elf_with_param(bp.PT_SCE_MODULE_PARAM, 0x3C13F4BF, 0x10090001, 0x06000038)
            (root / "eboot.bin").write_bytes(eboot)
            (root / "libSceExample.prx").write_bytes(low)
            # the .txt never gets walked; the SELF-looking .prx is walked but fails the ELF check
            (root / "libSceAlreadySelf.prx").write_bytes(b"\x54\x14\xF5\xEE" + b"\x00" * 60)
            (root / "readme.txt").write_bytes(b"not an ELF")

            report = bp.lower_sdk_in_folder(root, "7.61")

            self.assertEqual({p.name for p, _ in report.written}, {"eboot.bin", "libSceExample.prx"})
            self.assertEqual({p.name for p in report.skipped_not_elf}, {"libSceAlreadySelf.prx"})

            after = (root / "eboot.bin").read_bytes()
            self.assertEqual(struct.unpack_from("<I", after, 64 + 56 + 0x14)[0], 0x07000038)
            self.assertEqual((root / "libSceExample.prx").read_bytes(), low)

    @unittest.skipIf(os.name == "nt", "chmod cannot make a file unreadable on Windows")
    def test_a_module_that_cannot_be_read_fails_the_backport(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eboot = _elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0x11090001, 0x08000041)
            (root / "eboot.bin").write_bytes(eboot)
            locked = root / "libSceLocked.prx"
            locked.write_bytes(eboot)
            os.chmod(locked, 0)
            try:
                with self.assertRaises(bp.BackportError) as cm:
                    bp.lower_sdk_in_folder(root, "7.61")
            finally:
                os.chmod(locked, 0o644)
            self.assertIn("libSceLocked.prx", str(cm.exception))
            self.assertEqual([p.name for p, _e in cm.exception.report.failed], ["libSceLocked.prx"])
            self.assertEqual((root / "eboot.bin").read_bytes(), eboot)

    def test_a_param_segment_past_the_end_is_a_clear_error(self):
        elf = bytearray(_elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0x11090001, 0x08000041))
        struct.pack_into("<Q", elf, 64 + 8, 1 << 40)          # p_offset far past the file
        with self.assertRaisesRegex(bp.MalformedElf, "past the end"):
            bp.lower_sdk_version(bytes(elf), "7.61")
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "eboot.bin").write_bytes(bytes(elf))
            with self.assertRaises(bp.BackportError) as cm:
                bp.lower_sdk_in_folder(Path(td), "7.61")
            self.assertIn("past the end", str(cm.exception))


class MalformedInputs(unittest.TestCase):
    def test_a_self_segment_far_past_the_file_is_refused_before_allocating(self):
        from backend import self_file
        signed = bytearray(_fself(_signable_elf()))
        img = self_file.parse_self(bytes(signed))
        load = next(i for i, ph in enumerate(img.phdrs) if ph[0] == bp.PT_LOAD)
        struct.pack_into("<Q", signed, img.elf_offset + 64 + load * 56 + 8, 1 << 62)
        with self.assertRaisesRegex(self_file.MalformedSelf, "past"):
            self_file.elf_image(bytes(signed))
        signed = bytearray(_fself(_signable_elf()))
        version = next(i for i, ph in enumerate(img.phdrs) if ph[0] == self_file.PT_SCE_VERSION)
        struct.pack_into("<Q", signed, img.elf_offset + 64 + version * 56 + 32, 1 << 62)
        with self.assertRaises(self_file.MalformedSelf):
            self_file.elf_image(bytes(signed))


class EncodeId(unittest.TestCase):
    def test_matches_shadps4_table(self):
        cases = {
            0x00: "A", 0x01: "B", 0x3F: "-",
            0x40: "BA", 0x41: "BB", 0xFFF: "--",
            0x1000: "BAA", 0x1001: "BAB",
            # 16 bits in 18: the top nibble 15 gives 'P', which checks the shift order
            0xFFFF: "P--",
        }
        for v, want in cases.items():
            self.assertEqual(bp.encode_id(v), want, f"id=0x{v:x}")

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError): bp.encode_id(-1)
        with self.assertRaises(ValueError): bp.encode_id(0x10000)


def _elf_with_imports(imports: list[tuple[str, int, str, int, str]]) -> bytes:
    ehdr = bytearray(64)
    ehdr[0:4] = b"\x7fELF"; ehdr[4] = 2; ehdr[5] = 1

    # strtab starts with the empty string, then each unique name, NUL-terminated
    strings: list[str] = []
    offsets: dict[str, int] = {}
    strings.append("")
    def add(s: str) -> int:
        if s not in offsets:
            offsets[s] = sum(len(x) + 1 for x in strings)
            strings.append(s)
        return offsets[s]
    add("")   # ensure offset 0 is empty
    lib_offs: dict[int, int] = {}
    mod_offs: dict[int, int] = {}
    sym_offs: list[int] = []
    for nid, lib_id, lib_name, mod_id, mod_name in imports:
        if lib_id not in lib_offs: lib_offs[lib_id] = add(lib_name)
        if mod_id not in mod_offs: mod_offs[mod_id] = add(mod_name)
        sym_name = f"{nid}#{bp.encode_id(lib_id)}#{bp.encode_id(mod_id)}"
        sym_offs.append(add(sym_name))
    strtab = b"\x00".join(s.encode() for s in strings) + b"\x00"

    # one 24-byte Elf64_Sym per import, only st_name matters
    symtab = b"".join(struct.pack("<IBBHQQ", off, 0, 0, 0, 0, 0) for off in sym_offs)

    strtab_off = 0
    symtab_off = len(strtab)
    dynlibdata = strtab + symtab

    # Elf64_Dyn: d_tag u64, d_val u64
    dyn_entries: list[tuple[int, int]] = [
        (bp.DT_SCE_STRTAB, strtab_off),
        (bp.DT_SCE_STRSZ, len(strtab)),
        (bp.DT_SCE_SYMTAB, symtab_off),
        (bp.DT_SCE_SYMTABSZ, len(symtab)),
        (bp.DT_SCE_SYMENT, 24),
    ]
    for lib_id, name_off in lib_offs.items():
        # low 32 = string offset; high halfword bits 48..63 = id.
        d_val = (lib_id << 48) | name_off
        dyn_entries.append((bp.DT_SCE_IMPORT_LIB, d_val))
    for mod_id, name_off in mod_offs.items():
        d_val = (mod_id << 48) | name_off
        dyn_entries.append((bp.DT_SCE_NEEDED_MODULE, d_val))
    dyn_entries.append((0, 0))            # DT_NULL
    dyn_bytes = b"".join(struct.pack("<QQ", t, v) for t, v in dyn_entries)

    phdrs = []
    ph_off = 64
    ph_entsize = _PHDR_SIZE = 56
    data_off = ph_off + 2 * ph_entsize
    phdrs.append(struct.pack("<II6Q", bp.PT_DYNAMIC, 0, data_off, 0, 0, len(dyn_bytes), 0, 0))
    dynlib_off = data_off + len(dyn_bytes)
    phdrs.append(struct.pack("<II6Q", bp.PT_SCE_DYNLIBDATA, 0, dynlib_off, 0, 0, len(dynlibdata), 0, 0))

    struct.pack_into("<Q", ehdr, 32, ph_off)
    struct.pack_into("<H", ehdr, 54, ph_entsize)
    struct.pack_into("<H", ehdr, 56, 2)

    return bytes(ehdr) + b"".join(phdrs) + dyn_bytes + dynlibdata


class ReadImportedNids(unittest.TestCase):
    def test_reads_one_import(self):
        elf = _elf_with_imports([("mc36MRb8k1w", 0, "libSceAgc", 0, "libSceAgc")])
        got = bp.read_imported_nids(elf)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].nid, "mc36MRb8k1w")
        self.assertEqual(got[0].library, "libSceAgc")
        self.assertEqual(got[0].module, "libSceAgc")

    def test_reads_across_libraries_and_modules(self):
        elf = _elf_with_imports([
            ("AAAAAAAAAAA", 0x00, "libSceAgc",      0x00, "libSceAgc"),
            ("BBBBBBBBBBB", 0x01, "libSceGnmDriver",0x01, "libSceGnmDriver"),
            # id 0x40 and up takes 2 chars, so the reader's round trip gets checked too
            ("CCCCCCCCCCC", 0x40, "libSceRareLib",  0x02, "libSceGnmDriver"),
        ])
        got = bp.read_imported_nids(elf)
        libs = {i.library for i in got}
        self.assertEqual(libs, {"libSceAgc", "libSceGnmDriver", "libSceRareLib"})
        self.assertEqual(sorted(i.nid for i in got), ["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"])

    def test_ignores_non_import_symbols(self):
        base = _elf_with_imports([("mc36MRb8k1w", 0, "libSceAgc", 0, "libSceAgc")])
        got = bp.read_imported_nids(base)
        self.assertTrue(all(len(i.nid) == 11 for i in got))
        self.assertTrue(all(i.library for i in got))

    def test_returns_empty_for_non_elf(self):
        self.assertEqual(bp.read_imported_nids(b"this is not an ELF"), [])

    def test_returns_empty_for_elf_without_dynamic(self):
        ehdr = bytearray(64)
        ehdr[0:4] = b"\x7fELF"; ehdr[4] = 2; ehdr[5] = 1
        struct.pack_into("<Q", ehdr, 32, 64)
        struct.pack_into("<H", ehdr, 54, 56)
        struct.pack_into("<H", ehdr, 56, 1)
        phdr = struct.pack("<II6Q", 1, 0, 120, 0, 0, 0, 0, 0)
        self.assertEqual(bp.read_imported_nids(bytes(ehdr) + phdr), [])


def _elf_with_exports(exports: list[tuple[str, int, str, int, str]]) -> bytes:
    """every symbol defined (st_shndx != 0), so they count as exports"""
    elf = _elf_with_imports(exports)
    buf = bytearray(elf)
    # symtab is the file's tail, 24 bytes per symbol
    n = len(exports)
    sym_start = len(buf) - n * 24
    for i in range(n):
        # st_shndx is offset 6 (u32 st_name, u8 info, u8 other, u16 shndx).
        struct.pack_into("<H", buf, sym_start + i * 24 + 6, 1)
    return bytes(buf)


class ReadExportsAndDb(unittest.TestCase):
    def test_read_exported_nids_only_returns_defined_symbols(self):
        imp_elf = _elf_with_imports([("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX")])
        exp_elf = _elf_with_exports([("BBBBBBBBBBB", 0, "libSceY", 0, "libSceY")])
        self.assertEqual([i.nid for i in bp.read_imported_nids(imp_elf)], ["AAAAAAAAAAA"])
        self.assertEqual([i.nid for i in bp.read_exported_nids(imp_elf)], [])
        self.assertEqual([i.nid for i in bp.read_exported_nids(exp_elf)], ["BBBBBBBBBBB"])
        self.assertEqual([i.nid for i in bp.read_imported_nids(exp_elf)], [])

    def test_build_firmware_nid_db_groups_by_library_field(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "libSceX.sprx").write_bytes(_elf_with_exports([
                ("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX"),
                ("BBBBBBBBBBB", 0, "libSceX", 0, "libSceX"),
            ]))
            (root / "libSceY.sprx").write_bytes(_elf_with_exports([
                ("CCCCCCCCCCC", 0, "libSceY", 0, "libSceY"),
            ]))
            (root / "readme.txt").write_bytes(b"ignored")
            db = bp.build_firmware_nid_db(root)
            self.assertEqual(db["libSceX"], {"AAAAAAAAAAA", "BBBBBBBBBBB"})
            self.assertEqual(db["libSceY"], {"CCCCCCCCCCC"})
            self.assertNotIn("readme", db)


class AnalyseBackport(unittest.TestCase):
    def test_ok_partial_missing_categorisation(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"; src.mkdir()
            fw = Path(td) / "fw"; fw.mkdir()

            (src / "eboot.bin").write_bytes(_elf_with_imports([
                ("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX"),
                ("BBBBBBBBBBB", 0, "libSceX", 0, "libSceX"),
                ("CCCCCCCCCCC", 1, "libSceY", 0, "libSceX"),
                ("DDDDDDDDDDD", 2, "libSceZ", 0, "libSceX"),
            ]))
            (fw / "libSceX.sprx").write_bytes(_elf_with_exports([
                ("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX"),
                ("BBBBBBBBBBB", 0, "libSceX", 0, "libSceX"),
            ]))
            (fw / "libSceY.sprx").write_bytes(_elf_with_exports([
                ("XXXXXXXXXXX", 0, "libSceY", 0, "libSceY"),  # doesn't cover CCC
            ]))
            report = bp.analyse_backport(src, "7.61", fw_libs_root=fw)

            by = {r.library: r for r in report.per_library}
            self.assertEqual(by["libSceX"].status, "ok")
            self.assertEqual(by["libSceY"].status, "partial")
            self.assertEqual(by["libSceY"].unresolved, {"CCCCCCCCCCC"})
            self.assertEqual(by["libSceZ"].status, "missing")
            self.assertFalse(by["libSceZ"].in_firmware)
            self.assertEqual(report.blocking_libraries(), ["libSceZ"])

    def test_fakelib_can_cover_a_missing_firmware_lib(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"; src.mkdir()
            fw = Path(td) / "fw"; fw.mkdir()
            fk = Path(td) / "fakelib"; fk.mkdir()
            (src / "eboot.bin").write_bytes(_elf_with_imports([
                ("AAAAAAAAAAA", 0, "libSceCustom", 0, "libSceCustom"),
            ]))
            (fk / "libSceCustom.sprx").write_bytes(_elf_with_exports([
                ("AAAAAAAAAAA", 0, "libSceCustom", 0, "libSceCustom"),
            ]))
            report = bp.analyse_backport(src, "7.61", fw_libs_root=fw, backport_libs_root=fk)
            by = {r.library: r for r in report.per_library}
            self.assertEqual(by["libSceCustom"].status, "ok")
            self.assertEqual(by["libSceCustom"].fakelib_covers, {"AAAAAAAAAAA"})


MODULE_MAGIC = 0x3C13F4BF


def _signable_elf(sdk_ps4: int = 0x12090001, sdk_ps5: int = 0x10000040,
                  symbols_elf: bytes | None = None) -> bytes:
    """make_fself-friendly ELF; param and PT_DYNAMIC sit in a PT_LOAD so the SELF keeps them"""
    param = bytearray(40)
    struct.pack_into("<Q", param, 0x00, 40)
    struct.pack_into("<I", param, 0x08, MODULE_MAGIC)
    struct.pack_into("<I", param, 0x0C, 1)
    struct.pack_into("<I", param, 0x10, sdk_ps4)
    struct.pack_into("<I", param, 0x14, sdk_ps5)
    struct.pack_into("<I", param, 0x18, 0xdeadbeef)
    dyn = dynlib = b""
    if symbols_elf:
        for p_type, off, size in bp._iter_phdrs(symbols_elf):
            if p_type == bp.PT_DYNAMIC:
                dyn = symbols_elf[off:off + size]
            elif p_type == bp.PT_SCE_DYNLIBDATA:
                dynlib = symbols_elf[off:off + size]
    load_off = 0x200
    load = bytes(param) + b"\xAA" * 8 + dyn + b"\x55" * 24
    dyn_off = load_off + len(param) + 8
    dynlib_off = load_off + len(load)
    version = b"1.00\x00\x00\x00\x00"
    version_off = dynlib_off + len(dynlib)
    phdrs = [(bp.PT_LOAD, 6, load_off, 0, 0, len(load), len(load), 0x4000),
             (bp.PT_SCE_MODULE_PARAM, 4, load_off, 0, 0, len(param), len(param), 8)]
    if dyn:
        phdrs += [(bp.PT_DYNAMIC, 6, dyn_off, 48, 48, len(dyn), len(dyn), 8),
                  (bp.PT_SCE_DYNLIBDATA, 4, dynlib_off, 0, 0, len(dynlib), 0, 16)]
    phdrs.append((0x6FFFFF01, 4, version_off, 0, 0, len(version), 0, 16))     # PT_SCE_VERSION
    ehdr = bytearray(64)
    ehdr[0:4] = b"\x7fELF"; ehdr[4] = 2; ehdr[5] = 1; ehdr[6] = 1; ehdr[7] = 9
    struct.pack_into("<2HI3QI6H", ehdr, 16, 0xFE18, 0x3E, 1, 0, 64, 0, 0, 64, 56, len(phdrs), 0, 0, 0)
    head = bytes(ehdr) + b"".join(struct.pack("<2I6Q", *ph) for ph in phdrs)
    return head.ljust(load_off, b"\0") + load + dynlib + version


def _fself(elf: bytes, ps5: bool = False) -> bytes:
    import contextlib, io
    from backend import make_fself
    f = make_fself.ElfFile(ignore_shdrs=True)
    out = io.BytesIO()
    with contextlib.redirect_stdout(io.StringIO()):
        f.load(io.BytesIO(elf))
        make_fself.SignedElfFile(f).save(out)
    data = out.getvalue()
    return (b"\x54\x14\xF5\xEE" + data[4:]) if ps5 else data


def _encrypt_flag(signed: bytes) -> bytes:
    """Mark every data entry of a SELF as encrypted, like a retail file."""
    buf = bytearray(signed)
    count = struct.unpack_from("<H", buf, 0x18)[0]
    for i in range(count):
        props = struct.unpack_from("<Q", buf, 0x20 + i * 32)[0]
        if props & (1 << 11):
            struct.pack_into("<Q", buf, 0x20 + i * 32, props | (1 << 1))
    return bytes(buf)


def _param_words(elf: bytes) -> tuple[int, int]:
    """(ps4, ps5) SDK words of an ELF image."""
    _kind, off = bp._find_param_segment(elf)
    return struct.unpack_from("<2I", elf, off + 0x10)


class SignedSources(unittest.TestCase):
    def test_the_elf_image_of_a_fake_signed_file_is_the_original(self):
        from backend import self_file
        elf = _signable_elf()
        for ps5 in (False, True):
            signed = _fself(elf, ps5=ps5)
            img = self_file.parse_self(signed)
            self.assertIsNotNone(img)
            self.assertTrue(img.readable)
            self.assertEqual(img.flavour, "ps5" if ps5 else "ps4")
            self.assertEqual(self_file.elf_image(signed, img), elf)

    def test_lowering_a_signed_file_changes_only_the_sdk_words(self):
        from backend import self_file
        elf = _signable_elf(0x12090001, 0x10000040)
        signed = _fself(elf, ps5=True)
        out, changes = bp.lower_sdk_version(signed, "7.61")
        self.assertEqual([(c.field, c.after) for c in changes if not c.unchanged()],
                         [("ps4", 0x10590001), ("ps5", 0x07000038)])
        self.assertEqual(len(out), len(signed))
        diff = [i for i in range(len(out)) if out[i] != signed[i]]
        self.assertTrue(diff and len(diff) <= 8, diff)
        self.assertTrue(max(diff) - min(diff) < 8, "only the two adjacent words change")
        image = self_file.elf_image(out)
        self.assertEqual(_param_words(image), (0x10590001, 0x07000038))
        self.assertEqual(struct.unpack_from("<I", image, 0x200 + 0x18)[0], 0xdeadbeef)

    def test_symbols_are_read_through_the_signature(self):
        raw = _elf_with_imports([("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX"),
                                 ("BBBBBBBBBBB", 1, "libSceY", 0, "libSceX")])
        signed = _fself(_signable_elf(symbols_elf=raw))
        self.assertEqual([(i.nid, i.library) for i in bp.read_imported_nids(signed)],
                         [(i.nid, i.library) for i in bp.read_imported_nids(raw)])

    def test_an_encrypted_file_is_refused_not_skipped_silently(self):
        locked = _encrypt_flag(_fself(_signable_elf(), ps5=True))
        with self.assertRaises(ValueError):
            bp.lower_sdk_version(locked, "7.61")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "eboot.bin").write_bytes(locked)
            (root / "libSceOk.sprx").write_bytes(_fself(_signable_elf()))
            self.assertEqual([p.name for p in bp.encrypted_selfs(root)], ["eboot.bin"])
            report = bp.lower_sdk_in_folder(root, "7.61")
            self.assertEqual([p.name for p in report.encrypted], ["eboot.bin"])
            self.assertEqual([p.name for p in report.self_patched], ["libSceOk.sprx"])
            self.assertEqual((root / "eboot.bin").read_bytes(), locked)
            self.assertEqual(bp.analyse_backport(root, "7.61").unreadable, ["eboot.bin"])

    def test_a_signed_file_in_a_folder_is_lowered_in_place(self):
        from backend import self_file
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "eboot.bin").write_bytes(_fself(_signable_elf(0x12090001, 0x10000040), ps5=True))
            report = bp.lower_sdk_in_folder(root, "6.02")
            self.assertEqual([p.name for p in report.self_patched], ["eboot.bin"])
            self.assertIn("fake-signed", report.summary())
            image = self_file.elf_image((root / "eboot.bin").read_bytes())
            self.assertEqual(_param_words(image), (0x10090001, 0x06000038))


class SdkWordsOfFile(unittest.TestCase):
    def test_raw_signed_and_encrypted_executables(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "raw.bin").write_bytes(_elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0x12090001, 0x10000040))
            (root / "signed.bin").write_bytes(_fself(_signable_elf(0x11590001, 0x09600004), ps5=True))
            (root / "locked.bin").write_bytes(_encrypt_flag(_fself(_signable_elf(), ps5=True)))
            self.assertEqual(bp.sdk_words_of_file(root / "raw.bin"), (0x10000040, 0x12090001))
            self.assertEqual(bp.sdk_words_of_file(root / "signed.bin", head_size=0x200), (0x09600004, 0x11590001))
            self.assertIsNone(bp.sdk_words_of_file(root / "locked.bin"))
            self.assertIsNone(bp.sdk_words_of_file(root / "missing.bin"))


def _fw_lib(folder: Path, ps4: int, ps5: int, name: str = "libSceKernel.sprx", sub: str = "system/common/lib"):
    d = folder / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(_elf_with_param(bp.PT_SCE_MODULE_PARAM, MODULE_MAGIC, ps4, ps5))


class FirmwareFolders(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "firmware"
        _fw_lib(self.root / "6.02", 0x10090001, 0x06020004)
        _fw_lib(self.root / "9.60", 0x11590001, 0x09600004)
        _fw_lib(self.root / "10.01", 0x12090001, 0x10010000)
        _fw_lib(self.root / "10.20", 0x12090001, 0x10200006)
        _fw_lib(self.root / "5.02", 0x09690001, 0x05100023)                # holds 5.10 files
        (self.root / "notes").mkdir()

    def tearDown(self):
        self._td.cleanup()

    def test_targets_are_the_public_ones_plus_every_firmware_folder(self):
        self.assertEqual(bp.available_targets(self.root),
                         ["5.02", "6.02", "7.61", "9.60", "10.xx", "10.01", "10.20"])
        self.assertEqual(bp.available_targets(None), ["6.02", "7.61", "10.xx"])

    def test_each_target_reads_its_own_folder(self):
        self.assertEqual(bp.firmware_folder(self.root, "9.60"), self.root / "9.60")
        self.assertEqual(bp.firmware_folder(self.root, "10.xx"), self.root / "10.01")
        self.assertIsNone(bp.firmware_folder(self.root, "8.60"))

    def test_sdk_words_come_from_the_firmware_libraries(self):
        self.assertEqual(bp.target_words("9.60", self.root), (0x09600004, 0x11590001))
        self.assertEqual(bp.target_words("7.61", self.root), bp.SDK_TARGETS["7.61"])
        with self.assertRaises(ValueError):
            bp.target_words("8.60", self.root)
        with self.assertRaises(ValueError):
            bp.target_words("9.60")                     # no firmware folder at all

    def test_a_folder_with_newer_libraries_is_refused(self):
        self.assertIn("from firmware 5.10", bp.firmware_problem(self.root / "5.02", "5.02"))
        with self.assertRaises(ValueError):
            bp.target_words("5.02", self.root)
        self.assertEqual(bp.firmware_problem(self.root / "9.60", "9.60"), "")

    def test_encrypted_firmware_libraries_are_named(self):
        d = self.root / "7.00" / "system" / "common" / "lib"
        d.mkdir(parents=True)
        (d / "libSceKernel.sprx").write_bytes(_encrypt_flag(_fself(_signable_elf(), ps5=True)))
        self.assertIn("encrypted", bp.firmware_problem(self.root / "7.00", "7.00"))
        with self.assertRaises(ValueError):
            bp.target_words("7.00", self.root)

    def test_the_setting_may_point_at_one_firmware(self):
        one = self.root / "9.60"
        self.assertEqual(bp.firmware_folders(one), {"9.60": one})
        self.assertEqual(bp.firmware_folder(one, "9.60"), one)
        self.assertIsNone(bp.firmware_folder(one, "6.02"), "never check 6.02 against 9.60 files")
        plain = Path(self._td.name) / "fw"
        _fw_lib(plain, 0x10790001, 0x07610000, sub=".")
        self.assertEqual(bp.firmware_folder(plain, "7.61"), plain)

    def test_sdk_word_names_its_firmware(self):
        self.assertEqual(bp.sdk_firmware(0x07610000), "7.61")
        self.assertEqual(bp.sdk_firmware(0x10010000), "10.01")
        self.assertEqual(bp.sdk_firmware(0x05100023), "5.10")

    def test_patched_libraries_are_picked_per_target(self):
        libs = Path(self._td.name) / "patched"
        (libs / "7.61").mkdir(parents=True); (libs / "6.02").mkdir()
        self.assertEqual(bp.patched_libs_folder(libs, "7.61"), libs / "7.61")
        self.assertIsNone(bp.patched_libs_folder(libs, "9.60"))
        self.assertEqual(bp.patched_libs_folder(libs / "7.61", "7.61"), libs / "7.61")
        self.assertIsNone(bp.patched_libs_folder(libs / "7.61", "6.02"))
        mine = Path(self._td.name) / "mine"; mine.mkdir()
        self.assertEqual(bp.patched_libs_folder(mine, "9.60"), mine)

    def test_the_firmware_folder_is_never_the_patched_libraries(self):
        self.assertIn("original libraries", bp.patched_libs_problem(self.root, self.root))
        self.assertIn("original libraries", bp.patched_libs_problem(self.root / "9.60", self.root))
        self.assertEqual(bp.patched_libs_problem(Path(self._td.name) / "patched", self.root), "")
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)
            (src / "eboot.bin").write_bytes(_elf_with_imports([("AAAAAAAAAAA", 0, "libSceKernel", 0, "libSceKernel")]))
            (self.root / "9.60" / "system" / "common" / "lib" / "libSceKernel.sprx").write_bytes(
                _elf_with_exports([("AAAAAAAAAAA", 0, "libSceKernel", 0, "libSceKernel")]))
            r = bp.analyse_backport(src, "6.02", fw_libs_root=self.root, backport_libs_root=self.root)
            self.assertFalse(any(l.fakelib_covers for l in r.per_library), "originals never count as patched")

    def test_lowering_with_words_from_a_firmware_folder(self):
        with tempfile.TemporaryDirectory() as td:
            game = Path(td)
            (game / "eboot.bin").write_bytes(
                _elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0x12090001, 0x10000040))
            words = bp.target_words("9.60", self.root)
            report = bp.lower_sdk_in_folder(game, "9.60", words)
            self.assertEqual(len(report.written), 1)
            self.assertEqual(_param_words((game / "eboot.bin").read_bytes()), (0x11590001, 0x09600004))


class AnalyseWithFirmwareFolders(unittest.TestCase):
    def test_the_target_folder_decides_and_subfolders_are_read(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"; src.mkdir()
            fw = Path(td) / "firmware"
            (src / "eboot.bin").write_bytes(_elf_with_imports([
                ("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX"),
                ("BBBBBBBBBBB", 0, "libSceX", 0, "libSceX"),
            ]))
            for name, nids in (("7.61", ["AAAAAAAAAAA"]), ("10.01", ["AAAAAAAAAAA", "BBBBBBBBBBB"])):
                d = fw / name / "system" / "common" / "lib"; d.mkdir(parents=True)
                (d / "libSceX.sprx").write_bytes(_elf_with_exports([(n, 0, "libSceX", 0, "libSceX") for n in nids]))
            old = bp.analyse_backport(src, "7.61", fw_libs_root=fw)
            self.assertTrue(old.firmware_checked)
            self.assertEqual(old.unresolved_count(), 1)
            self.assertEqual(old.unresolved_libraries(), ["libSceX"])
            new = bp.analyse_backport(src, "10.01", fw_libs_root=fw)
            self.assertEqual(new.unresolved_count(), 0)
            none = bp.analyse_backport(src, "9.60", fw_libs_root=fw)
            self.assertFalse(none.firmware_checked)
            self.assertIn("no 9.60 folder", none.firmware_note)

    def test_functions_the_game_exports_itself_count_as_covered(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"; (src / "sce_module").mkdir(parents=True)
            fw = Path(td) / "fw"; fw.mkdir()
            (src / "eboot.bin").write_bytes(_elf_with_imports([
                ("CCCCCCCCCCC", 0, "libGameHelper", 0, "libGameHelper"),
            ]))
            (src / "sce_module" / "libGameHelper.prx").write_bytes(_elf_with_exports([
                ("CCCCCCCCCCC", 0, "libGameHelper", 0, "libGameHelper"),
            ]))
            report = bp.analyse_backport(src, "7.61", fw_libs_root=fw)
            lib = {r.library: r for r in report.per_library}["libGameHelper"]
            self.assertEqual(lib.status, "ok")
            self.assertEqual(lib.game_covers, {"CCCCCCCCCCC"})
            self.assertEqual(report.unresolved_count(), 0)


if __name__ == "__main__":
    unittest.main()
