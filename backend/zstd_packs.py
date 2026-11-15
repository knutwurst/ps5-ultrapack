# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
"""zstd asset packs (ampr_zstd.index + ampr_zstd-NNN.pak), read at runtime by quer3q's ampr-emu-ng in fakelib/.

Format version 1 as ampr-emu-ng's docs/zstd-format.md and ps5-dump-forge's docs/zstd.md describe it:
an AMPRZPK1 index and AMPRZDT1 volumes, 256 KiB chunks, each one zstd frame (content size,
checksum, window <= 256 KiB) or raw when zstd doesn't shrink it.
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

try:
    from compression import zstd as _zstd          # Python 3.14+
except ImportError:                                 # pragma: no cover - depends on the Python
    try:
        from backports import zstd as _zstd
    except ImportError:
        _zstd = None

_HERE = Path(__file__).resolve().parent
_PACK_DIR = _HERE / "ampr_pack"
if str(_PACK_DIR) not in sys.path:
    sys.path.insert(0, str(_PACK_DIR))

INDEX_NAME = "ampr_zstd.index"
VOLUME_PREFIX, VOLUME_SUFFIX = "ampr_zstd-", ".pak"
INDEX_MAGIC, VOLUME_MAGIC = b"AMPRZPK1", b"AMPRZDT1"
FORMAT_VERSION = 1
INDEX_HEADER_SIZE, VOLUME_HEADER_SIZE = 128, 64
FILE_RECORD_SIZE = 32
CHUNK_SHIFT = 18
CHUNK_SIZE = 1 << CHUNK_SHIFT
VOLUME_LIMIT = (1 << 32) - (1 << 16)    # header included; chunk offsets fit u32, volumes fit FAT32
MAX_VOLUMES = 1 << 12
MAX_CHUNKS = (1 << 32) - 1
MAX_THREADS = 32
LEVEL_MIN, LEVEL_MAX, DEFAULT_LEVEL = 1, 22, 5
CODEC_RAW, CODEC_ZSTD = 0, 1
FRAME_MAGIC = b"\x28\xb5\x2f\xfd"

RUNTIME_NAME = "libSceAmpr.sprx"
RUNTIME_REL = "fakelib/" + RUNTIME_NAME
RUNTIME_VERSION = "0.4.0"
AMPR_INDEX = "ampr_emu.index"
LZ4_INDEX = "ampr_assets.index"
# ampr_emu / ampr-emu-ng files at the game root (ps5-dump-forge's artifact list)
_ARTIFACTS = {"ampr_assets.index", "ampr_assets.index.crc", "ampr_assets.index.runtime", AMPR_INDEX,
              RUNTIME_REL, "ampr_commands.bin", "ampr_emu.log", "apr_emu.log"}
# the PS5 kernel limits ampr-emu-ng checks on every ampr_emu.index path
PATH_MAX, NAME_MAX = 1024, 255
MIN_GAIN_PERCENT = 5


class ZstdPackError(RuntimeError):
    pass


def available() -> bool:
    return _zstd is not None


def fnv1a64(data: bytes) -> int:
    h = 14695981039346656037
    for b in data:
        h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def volume_name(n: int) -> str:
    return f"{VOLUME_PREFIX}{n:03d}{VOLUME_SUFFIX}"


def is_pack_name(rel: str) -> bool:
    low = rel.lower()
    if "/" in low:
        return False
    if low == INDEX_NAME:
        return True
    num = low[len(VOLUME_PREFIX):-len(VOLUME_SUFFIX)]
    return low.startswith(VOLUME_PREFIX) and low.endswith(VOLUME_SUFFIX) and len(num) >= 3 and num.isdigit()


def always_loose(rel: str) -> bool:
    """never packed: the console and the runtime read these before (or outside) the packs"""
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    first = low.split("/", 1)[0]
    if name == "eboot.bin" or low.endswith((".elf", ".self", ".prx", ".sprx", ".ini")):
        return True
    if "/" in low and (first in ("sce_sys", "sce_module") or first.startswith("fakelib")):
        return True
    return low in _ARTIFACTS or is_pack_name(rel)


def valid_path(rel: str) -> bool:
    """the format's path rules"""
    if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel:
        return False
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return False
    try:
        return len(rel.encode("utf-8")) <= 0xFFFF
    except UnicodeEncodeError:
        return False


def reachable(rel: str) -> bool:
    """ampr-emu-ng serves a packed file only through its ampr_emu.index record, and that index
    skips whitespace paths and the console refuses paths over its limits: such a file stays loose"""
    if any(c in rel for c in "\t\n\r"):
        return False
    try:
        full = ("/app0/" + rel).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(full) < PATH_MAX and all(len(part) <= NAME_MAX for part in full.split(b"/"))


def _crc(data) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def chunk_word(offset: int, volume: int, stored: int, codec: int) -> int:
    return offset | (volume << 32) | ((stored - 1) << 44) | (codec << 62)


def split_word(word: int) -> tuple[int, int, int, int]:
    """offset, volume, stored size, codec"""
    return word & 0xFFFFFFFF, (word >> 32) & 0xFFF, ((word >> 44) & 0x3FFFF) + 1, word >> 62


# zstd

class _Encoder:
    """one compressor per thread, reused frame to frame (same bytes as a fresh one)"""

    def __init__(self, level: int):
        P = _zstd.CompressionParameter
        self.c = _zstd.ZstdCompressor(options={P.compression_level: level, P.window_log: CHUNK_SHIFT,
                                               P.checksum_flag: 1, P.content_size_flag: 1})

    def encode(self, raw: bytes) -> tuple[int, bytes]:
        # pledged size, continue, then end: ps5-dump-forge's call order, so the frames match its bytes
        self.c.set_pledged_input_size(len(raw))
        frame = self.c.compress(raw, _zstd.ZstdCompressor.CONTINUE) + self.c.flush(_zstd.ZstdCompressor.FLUSH_FRAME)
        if len(frame) < len(raw):
            return CODEC_ZSTD, frame
        return CODEC_RAW, raw


def decode_chunk(codec: int, stored: bytes, size: int) -> bytes:
    if codec == CODEC_RAW:
        if len(stored) != size:
            raise ZstdPackError(f"raw chunk holds {len(stored)} bytes, {size} expected")
        return stored
    if codec != CODEC_ZSTD:
        raise ZstdPackError(f"chunk codec {codec} is invalid")
    if _zstd is None:
        raise ZstdPackError("zstd is not available in this Python")
    if len(stored) >= size:
        raise ZstdPackError("zstd chunk is not smaller than its data")
    check_frame(stored, size)
    try:
        out = _zstd.ZstdDecompressor(options={_zstd.DecompressionParameter.window_log_max: CHUNK_SHIFT}).decompress(
            stored, max_length=size)
    except _zstd.ZstdError as e:
        raise ZstdPackError(f"zstd chunk is damaged: {e}") from e
    if len(out) != size:
        raise ZstdPackError(f"zstd chunk decodes to {len(out)} bytes, {size} expected")
    return out


def check_frame(frame: bytes, size: int) -> None:
    """one frame, content size in the header, checksum on, no dictionary, window <= one chunk"""
    if len(frame) < 6 or frame[:4] != FRAME_MAGIC:
        raise ZstdPackError("zstd chunk does not start with a frame")
    fhd = frame[4]
    if not fhd & 0x04:
        raise ZstdPackError("zstd frame has no checksum")
    if fhd & 0x0B:
        raise ZstdPackError("zstd frame names a dictionary or sets a reserved bit")
    single_segment, fcs_flag = bool(fhd & 0x20), fhd >> 6
    if not single_segment and not fcs_flag:
        raise ZstdPackError("zstd frame has no content size")
    if not single_segment:
        wd = frame[5]
        exponent, mantissa = wd >> 3, wd & 7
        base = 1 << (10 + exponent)
        if base + (base >> 3) * mantissa > CHUNK_SIZE:
            raise ZstdPackError("zstd frame window is over 256 KiB")
    info = _zstd.get_frame_info(frame)
    if info.decompressed_size != size or info.dictionary_id:
        raise ZstdPackError(f"zstd frame says {info.decompressed_size} bytes, {size} expected")


# index

@dataclass
class FileRecord:
    path: str
    size: int
    first_chunk: int
    flags: int = 0

    @property
    def chunk_count(self) -> int:
        return -(-self.size // CHUNK_SIZE)


@dataclass
class Index:
    level: int
    archive_id: bytes
    files: list[FileRecord] = field(default_factory=list)
    chunks: list[int] = field(default_factory=list)        # chunk words
    volume_sizes: list[int] = field(default_factory=list)

    @property
    def logical_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def stored_bytes(self) -> int:
        return sum(s - VOLUME_HEADER_SIZE for s in self.volume_sizes)

    def to_bytes(self) -> bytes:
        paths = [f.path.encode("utf-8") for f in self.files]
        files_off = INDEX_HEADER_SIZE
        chunks_off = files_off + FILE_RECORD_SIZE * len(self.files)
        volumes_off = chunks_off + 8 * len(self.chunks)
        strings_off = volumes_off + 8 * len(self.volume_sizes)
        body = bytearray()
        path_off = 0
        for f, p in zip(self.files, paths):
            body += struct.pack("<QQIIHHI", fnv1a64(p), f.size, f.first_chunk, path_off, len(p), f.flags, 0)
            path_off += len(p)
        body += struct.pack(f"<{len(self.chunks)}Q", *self.chunks)
        body += struct.pack(f"<{len(self.volume_sizes)}Q", *self.volume_sizes)
        strings = b"".join(paths)
        body += strings
        head = bytearray(struct.pack(
            "<8sIIBBHI16sQQQQQQQQQ", INDEX_MAGIC, FORMAT_VERSION, INDEX_HEADER_SIZE, CHUNK_SHIFT, self.level, 0,
            len(self.volume_sizes), self.archive_id, len(self.files), len(self.chunks), files_off, chunks_off,
            volumes_off, strings_off, len(strings), self.logical_bytes, 0))
        head += struct.pack("<IIQ", _crc(body), 0, 0)
        struct.pack_into("<I", head, 116, _crc(bytes(head[:112]) + bytes(8) + bytes(head[120:])))
        return bytes(head) + bytes(body)


def parse_index(data: bytes) -> Index:
    """every check of the reader in ampr-emu-ng and ps5-dump-forge; ZstdPackError on the first failure"""
    if len(data) < INDEX_HEADER_SIZE:
        raise ZstdPackError(f"{INDEX_NAME} is shorter than its header")
    h = data[:INDEX_HEADER_SIZE]
    (magic, version, header_size, shift, level, res16, volumes, archive_id, n_files, n_chunks, files_off,
     chunks_off, volumes_off, strings_off, strings_len, total, res104) = struct.unpack_from(
        "<8sIIBBHI16sQQQQQQQQQ", h)
    crc_body, crc_head, res120 = struct.unpack_from("<IIQ", h, 112)
    if magic != INDEX_MAGIC:
        raise ZstdPackError(f"{INDEX_NAME} is not a zstd pack index")
    if version != FORMAT_VERSION or header_size != INDEX_HEADER_SIZE:
        raise ZstdPackError(f"{INDEX_NAME} has version {version}, header {header_size}; only version 1 is known")
    if shift != CHUNK_SHIFT:
        raise ZstdPackError(f"{INDEX_NAME} uses {1 << shift} byte chunks; only 256 KiB chunks are valid")
    if not LEVEL_MIN <= level <= LEVEL_MAX:
        raise ZstdPackError(f"{INDEX_NAME} names zstd level {level}")
    if res16 or res104 or res120:
        raise ZstdPackError(f"{INDEX_NAME} has reserved bytes set")
    if crc_head != _crc(h[:112] + bytes(8) + h[120:]):
        raise ZstdPackError(f"{INDEX_NAME} header CRC does not match")
    if volumes > MAX_VOLUMES or n_chunks > MAX_CHUNKS or (volumes == 0) != (n_chunks == 0):
        raise ZstdPackError(f"{INDEX_NAME} counts are out of range")
    if (files_off != INDEX_HEADER_SIZE or chunks_off != files_off + FILE_RECORD_SIZE * n_files
            or volumes_off != chunks_off + 8 * n_chunks or strings_off != volumes_off + 8 * volumes
            or strings_off + strings_len != len(data)):
        raise ZstdPackError(f"{INDEX_NAME} tables are not where the header says")
    if crc_body != _crc(data[INDEX_HEADER_SIZE:]):
        raise ZstdPackError(f"{INDEX_NAME} payload CRC does not match")
    idx = Index(level=level, archive_id=archive_id)
    idx.chunks = list(struct.unpack_from(f"<{n_chunks}Q", data, chunks_off))
    idx.volume_sizes = list(struct.unpack_from(f"<{volumes}Q", data, volumes_off))
    expect_chunk, expect_path, previous = 0, 0, None
    for i in range(n_files):
        ph, size, first, path_off, path_len, flags, res = struct.unpack_from(
            "<QQIIHHI", data, files_off + FILE_RECORD_SIZE * i)
        if path_off != expect_path or path_off + path_len > strings_len:
            raise ZstdPackError(f"{INDEX_NAME}: file record {i} path is out of place")
        raw_path = data[strings_off + path_off:strings_off + path_off + path_len]
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            raise ZstdPackError(f"{INDEX_NAME}: file record {i} path is not UTF-8") from None
        if not valid_path(path) or len(raw_path) >= PATH_MAX:
            raise ZstdPackError(f"{INDEX_NAME}: invalid path {path!r}")
        if ph != fnv1a64(raw_path):
            raise ZstdPackError(f"{INDEX_NAME}: path hash of {path!r} does not match")
        if flags & ~1 or res:
            raise ZstdPackError(f"{INDEX_NAME}: file record {i} has unknown flags")
        if first != expect_chunk:
            raise ZstdPackError(f"{INDEX_NAME}: chunks of {path!r} are not in order")
        key = (ph, raw_path)
        if previous is not None and key <= previous:
            raise ZstdPackError(f"{INDEX_NAME}: file records are not sorted or repeat a path")
        previous = key
        rec = FileRecord(path, size, first, flags)
        expect_chunk += rec.chunk_count
        expect_path += path_len
        idx.files.append(rec)
    if expect_chunk != n_chunks or expect_path != strings_len:
        raise ZstdPackError(f"{INDEX_NAME}: chunk or path totals do not add up")
    if total != idx.logical_bytes:
        raise ZstdPackError(f"{INDEX_NAME}: logical total does not match the files")
    _check_layout(idx)
    return idx


def _check_layout(idx: Index) -> None:
    """chunks back to back from offset 64, a volume ends where its last chunk ends"""
    volume, cursor = 0, VOLUME_HEADER_SIZE
    for f in idx.files:
        for k in range(f.chunk_count):
            off, vid, stored, codec = split_word(idx.chunks[f.first_chunk + k])
            want = min(CHUNK_SIZE, f.size - k * CHUNK_SIZE)
            if codec > CODEC_ZSTD:
                raise ZstdPackError(f"{f.path}: chunk {k} codec {codec} is invalid")
            if (codec == CODEC_RAW and stored != want) or (codec == CODEC_ZSTD and stored >= want):
                raise ZstdPackError(f"{f.path}: chunk {k} stored size {stored} does not fit its data")
            if f.flags & 1 and codec != CODEC_RAW:
                raise ZstdPackError(f"{f.path}: store-only file has a zstd chunk")
            if vid != volume:
                if vid != volume + 1 or idx.volume_sizes[volume] != cursor:
                    raise ZstdPackError(f"{f.path}: chunk {k} skips a volume or leaves a gap")
                volume, cursor = vid, VOLUME_HEADER_SIZE
            if off != cursor:
                raise ZstdPackError(f"{f.path}: chunk {k} is not where the previous one ends")
            cursor += stored
    if idx.volume_sizes:
        if volume + 1 != len(idx.volume_sizes) or idx.volume_sizes[volume] != cursor:
            raise ZstdPackError("the last volume does not end with the last chunk")
        if any(s > VOLUME_LIMIT or s <= VOLUME_HEADER_SIZE for s in idx.volume_sizes):
            raise ZstdPackError("a volume size is out of range")


def volume_header(vid: int, count: int, archive_id: bytes, size: int) -> bytes:
    head = bytearray(struct.pack("<8sIIII16sQ12sI", VOLUME_MAGIC, FORMAT_VERSION, VOLUME_HEADER_SIZE, vid, count,
                                 archive_id, size - VOLUME_HEADER_SIZE, bytes(12), 0))
    struct.pack_into("<I", head, 60, _crc(bytes(head[:60]) + bytes(4)))
    return bytes(head)


def check_volume_header(head: bytes, vid: int, idx: Index, actual_size: int) -> None:
    name = volume_name(vid)
    if len(head) < VOLUME_HEADER_SIZE:
        raise ZstdPackError(f"{name} is shorter than its header")
    magic, version, hsize, got_vid, count, aid, payload, res, crc = struct.unpack_from("<8sIIII16sQ12sI", head)
    if magic != VOLUME_MAGIC or version != FORMAT_VERSION or hsize != VOLUME_HEADER_SIZE:
        raise ZstdPackError(f"{name} is not a zstd pack volume")
    if crc != _crc(head[:60] + bytes(4)):
        raise ZstdPackError(f"{name} header CRC does not match")
    if got_vid != vid or count != len(idx.volume_sizes) or aid != idx.archive_id or res != bytes(12):
        raise ZstdPackError(f"{name} belongs to another archive or another position")
    if payload != idx.volume_sizes[vid] - VOLUME_HEADER_SIZE or actual_size != idx.volume_sizes[vid]:
        raise ZstdPackError(f"{name} is {actual_size} bytes, the index says {idx.volume_sizes[vid]}")


# writer

class _Volumes:
    def __init__(self, out_dir: Path, limit: int):
        self.dir, self.limit = out_dir, limit
        self.sizes: list[int] = []
        self.f = None
        self.cursor = 0

    def _open(self) -> None:
        if len(self.sizes) >= MAX_VOLUMES:
            raise ZstdPackError(f"the packs would need more than {MAX_VOLUMES} volumes")
        if self.f is not None:
            self.f.close()
        self.sizes.append(0)
        self.f = open(self.dir / volume_name(len(self.sizes) - 1), "wb")
        self.f.write(bytes(VOLUME_HEADER_SIZE))
        self.cursor = VOLUME_HEADER_SIZE

    def put(self, data: bytes) -> tuple[int, int]:
        if self.f is None or self.cursor + len(data) > self.limit:
            if self.f is not None:
                self.sizes[-1] = self.cursor
            self._open()
        off = self.cursor
        self.f.write(data)
        self.cursor += len(data)
        return off, len(self.sizes) - 1

    def finish(self, archive_id: bytes) -> list[int]:
        if self.f is None:
            return []
        self.sizes[-1] = self.cursor
        self.f.close()
        self.f = None
        for vid, size in enumerate(self.sizes):
            with open(self.dir / volume_name(vid), "r+b") as f:
                f.write(volume_header(vid, len(self.sizes), archive_id, size))
                f.flush()
                os.fsync(f.fileno())
        return self.sizes

    def close(self) -> None:
        if self.f is not None:
            self.f.close()
            self.f = None


def _read_exact(f, n: int, path: str) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise ZstdPackError(f"{path} got shorter while it was packed")
    return data


def pack_files(root: Path, files: list[tuple[str, int]], out_dir: Path, *, level: int = DEFAULT_LEVEL,
               threads: int = 0, progress=None, volume_limit: int = VOLUME_LIMIT, archive_id: bytes | None = None,
               cancel=None) -> Index:
    """writes ampr_zstd-NNN.pak and ampr_zstd.index for files (rel path, size) of root into out_dir"""
    if _zstd is None:
        raise ZstdPackError("zstd is not available in this Python (install backports.zstd)")
    if not LEVEL_MIN <= level <= LEVEL_MAX:
        raise ZstdPackError(f"the zstd level is {LEVEL_MIN} through {LEVEL_MAX}, not {level}")
    if not VOLUME_HEADER_SIZE + CHUNK_SIZE <= volume_limit <= VOLUME_LIMIT:
        raise ZstdPackError("volume limit out of range")
    root, out_dir = Path(root), Path(out_dir)
    entries = []
    for rel, size in files:
        if not valid_path(rel):
            raise ZstdPackError(f"{rel!r} is not a path the packs can hold")
        raw = rel.encode("utf-8")
        entries.append((fnv1a64(raw), raw, rel, size))
    entries.sort(key=lambda e: (e[0], e[1]))
    for a, b in zip(entries, entries[1:]):
        if (a[0], a[1]) == (b[0], b[1]):
            raise ZstdPackError(f"{a[2]} is listed twice")
    names = [e[2] for e in entries]
    dirs = {n.rsplit("/", 1)[0] for n in names if "/" in n}
    for n in names:
        if n in dirs:
            raise ZstdPackError(f"{n} is a file and a folder at once")
    threads = max(1, min(MAX_THREADS, threads or (os.cpu_count() or 4)))
    idx = Index(level=level, archive_id=archive_id or os.urandom(16))
    total = sum(e[3] for e in entries)
    done = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    vols = _Volumes(out_dir, volume_limit)
    import threading
    local = threading.local()

    def enc(raw: bytes) -> tuple[int, bytes]:
        e = getattr(local, "enc", None)
        if e is None:
            e = local.enc = _Encoder(level)
        return e.encode(raw)

    batch = threads * 4
    try:
        with ThreadPoolExecutor(threads) as pool:
            for ph, raw_path, rel, size in entries:
                rec = FileRecord(rel, size, len(idx.chunks))
                src = root / rel
                with open(src, "rb") as f:
                    left = size
                    while left:
                        if cancel is not None and cancel():
                            raise ZstdPackError("cancelled")
                        group = []
                        while left and len(group) < batch:
                            n = min(CHUNK_SIZE, left)
                            group.append(_read_exact(f, n, rel))
                            left -= n
                        for codec, data in pool.map(enc, group):
                            off, vid = vols.put(data)
                            idx.chunks.append(chunk_word(off, vid, len(data), codec))
                        done += sum(len(g) for g in group)
                        if progress:
                            progress(done, total)
                    if f.read(1):
                        raise ZstdPackError(f"{rel} grew while it was packed")
                if len(idx.chunks) > MAX_CHUNKS:
                    raise ZstdPackError("the packs would hold too many chunks")
                idx.files.append(rec)
        idx.volume_sizes = vols.finish(idx.archive_id)
    finally:
        vols.close()
    data = idx.to_bytes()
    tmp = out_dir / (INDEX_NAME + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(out_dir / INDEX_NAME)
    return idx


# reader

class Archive:
    """the packs of a game folder, checked as ampr-emu-ng checks them before it serves a file"""

    def __init__(self, root: Path, pack_dir: Path | None = None):
        self.root = Path(root)
        self.dir = Path(pack_dir) if pack_dir else self.root
        try:
            data = (self.dir / INDEX_NAME).read_bytes()
        except OSError as e:
            raise ZstdPackError(f"cannot read {INDEX_NAME}: {e}") from e
        self.index = parse_index(data)
        for vid in range(len(self.index.volume_sizes)):
            p = self.dir / volume_name(vid)
            try:
                with open(p, "rb") as f:
                    head = f.read(VOLUME_HEADER_SIZE)
                size = p.stat().st_size
            except OSError as e:
                raise ZstdPackError(f"cannot read {p.name}: {e}") from e
            check_volume_header(head, vid, self.index, size)
        self._fds: dict[int, object] = {}

    def close(self) -> None:
        for f in self._fds.values():
            f.close()
        self._fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _vol(self, vid: int):
        f = self._fds.get(vid)
        if f is None:
            f = self._fds[vid] = open(self.dir / volume_name(vid), "rb")
        return f

    def chunks(self, rec: FileRecord):
        """the decoded chunks of one file, in order"""
        for k in range(rec.chunk_count):
            off, vid, stored, codec = split_word(self.index.chunks[rec.first_chunk + k])
            f = self._vol(vid)
            f.seek(off)
            body = f.read(stored)
            if len(body) != stored:
                raise ZstdPackError(f"{volume_name(vid)} ends inside a chunk of {rec.path}")
            yield decode_chunk(codec, body, min(CHUNK_SIZE, rec.size - k * CHUNK_SIZE))

    def read(self, rel: str) -> bytes:
        rec = next((r for r in self.index.files if r.path == rel), None)
        if rec is None:
            raise ZstdPackError(f"{rel} is not in the packs")
        return b"".join(self.chunks(rec))

    def extract_to(self, dest: Path, *, progress=None, overwrite: bool = False) -> int:
        dest = Path(dest)
        root = dest.resolve()
        total = self.index.logical_bytes
        done = 0
        for rec in self.index.files:
            target = (dest / rec.path).resolve()
            if root not in target.parents:
                raise ZstdPackError(f"{rec.path} would land outside the game folder")
            if target.exists() and not overwrite:
                raise ZstdPackError(f"{rec.path} is packed and also exists as a loose file")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".zstd-part")
            with open(tmp, "wb") as f:
                for chunk in self.chunks(rec):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            tmp.replace(target)
        return done

    def compare_with(self, original: Path, *, progress=None) -> None:
        """every packed file decoded and compared byte for byte with original/<path>"""
        total, done = self.index.logical_bytes, 0
        for rec in self.index.files:
            src = Path(original) / rec.path
            try:
                st = src.stat()
            except OSError as e:
                raise ZstdPackError(f"{rec.path}: original is gone ({e})") from e
            if st.st_size != rec.size:
                raise ZstdPackError(f"{rec.path}: the original is {st.st_size} bytes, the packs hold {rec.size}")
            with open(src, "rb") as f:
                for k, chunk in enumerate(self.chunks(rec)):
                    if f.read(len(chunk)) != chunk:
                        raise ZstdPackError(f"{rec.path}: chunk {k} does not match the original")
                    done += len(chunk)
                    if progress:
                        progress(done, total)


# AMPRIDX3, the file table ampr-emu-ng joins the packed files to

def read_ampr_index(data: bytes) -> dict[str, int]:
    """'/app0/...' path -> size, from an AMPRIDX3 ampr_emu.index"""
    if len(data) < 0x30 or data[:8] != b"AMPRIDX3":
        raise ZstdPackError(f"{AMPR_INDEX} is not an AMPRIDX3 index")
    _m, ver, rec_size, n, blob_len, _slots_off, _slot_size, _slot_n = struct.unpack_from("<8sIIQQQII", data)
    if ver != 3 or rec_size != 24 or 0x30 + n * 24 + blob_len > len(data):
        raise ZstdPackError(f"{AMPR_INDEX} header is invalid")
    blob = 0x30 + n * 24
    out = {}
    for i in range(n):
        off, plen, size, _mt = struct.unpack_from("<IIQq", data, 0x30 + 24 * i)
        out[data[blob + off:blob + off + plen].decode("utf-8", "replace")] = size
    return out


# jobs on a game folder

def runtime_path() -> Path:
    """the bundled ampr-emu-ng libSceAmpr.sprx that reads zstd packs"""
    candidates = [_HERE / "native" / "ampr-ng" / RUNTIME_NAME]
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        candidates += [Path(frozen) / "backend" / "native" / "ampr-ng" / RUNTIME_NAME]
    for p in candidates:
        if p.is_file():
            return p
    raise ZstdPackError(f"the ampr-emu-ng runtime ({RUNTIME_NAME} {RUNTIME_VERSION}) is missing from the app; "
                        "in a source checkout run ./FETCH_AMPR_EMU.sh")


def is_packed(root: Path) -> bool:
    p = Path(root) / INDEX_NAME
    try:
        with open(p, "rb") as f:
            return f.read(8) == INDEX_MAGIC
    except OSError:
        return False


def select_files(root: Path) -> tuple[list[tuple[str, int]], list[str]]:
    """(packed files, loose files) of a game folder"""
    root = Path(root)
    packed, loose = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink() or not p.is_file():
                loose.append(rel)
                continue
            size = p.stat().st_size
            if not size or always_loose(rel) or not valid_path(rel) or not reachable(rel):
                loose.append(rel)       # empty files too, as ps5-dump-forge does
            else:
                packed.append((rel, size))
    return packed, loose


def _bar(pct: int, label: str, started: float, done: int = 0) -> str:
    width = 32
    pct = max(0, min(99, pct))
    fill = width * pct // 100
    line = f"[{'#' * fill}{'-' * (width - fill)}] {pct:3d}% {label}"
    elapsed = time.monotonic() - started
    if elapsed > 0.5 and done > 0 and pct > 0:
        line += f" @ {done / elapsed / 1024 ** 2:.2f} MB/s"
        left = elapsed * (100 - pct) / pct
        line += f" ETA {int(left // 60)}m{int(left % 60):02d}s"
    return line


class _Progress:
    """progress as the bar lines the GUI parses; a pass is one half of the job when halves=2"""

    def __init__(self, log, label: str, half: int = 0, halves: int = 1):
        self.log, self.label, self.half, self.halves = log, label, half, halves
        self.started, self.last = time.monotonic(), 0.0

    def __call__(self, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self.last < 0.5 and done < total:
            return
        self.last = now
        part = done / total if total else 1.0
        pct = int((self.half + part) * 100 / self.halves)
        self.log(_bar(pct, f"{self.label} {done / 1024 ** 3:.1f}/{total / 1024 ** 3:.1f} GB", self.started, done))


def _remove_emptied_dirs(root: Path, removed: list[Path]) -> None:
    root = root.resolve()
    parents = sorted({p.parent for p in removed}, key=lambda d: len(d.parts), reverse=True)
    for d in parents:
        while d != root and root in d.parents:
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


def pack_game(root: Path, *, level: int = DEFAULT_LEVEL, threads: int = 0, log=print, cancel=None) -> dict:
    """in place, deletes the packed originals: root must be a copy the app may change"""
    import build_ampr_index

    if _zstd is None:
        raise ZstdPackError("zstd is not available in this Python (install backports.zstd)")
    root = Path(root).resolve()
    if is_packed(root) or any(is_pack_name(p.name) for p in root.iterdir()):
        raise ZstdPackError("the game already holds zstd packs; unpack it first")
    if (root / LZ4_INDEX).exists():
        raise ZstdPackError(f"the game holds LZ4 asset packs ({LZ4_INDEX}); unpack them first")
    if not (root / "eboot.bin").is_file():
        raise ZstdPackError("no eboot.bin at the top of the folder, so this is not a game folder")

    # runtime goes in first, the index has to list it too
    fakelib = root / "fakelib"
    fakelib.mkdir(exist_ok=True)
    rt = fakelib / RUNTIME_NAME
    if rt.exists():
        log(f"[INFO] zstd: replacing the game's {RUNTIME_NAME} with ampr-emu-ng {RUNTIME_VERSION}, which reads "
            "zstd packs.")
    shutil.copyfile(runtime_path(), rt)

    index = root / AMPR_INDEX
    if build_ampr_index.build_index_local(root, index, False) != 0:
        raise ZstdPackError("could not build ampr_emu.index for the game (two paths may differ only in case)")
    listed = read_ampr_index(index.read_bytes())

    packed, loose = select_files(root)
    packed = [(rel, size) for rel, size in packed if listed.get("/app0/" + rel) == size]
    if not packed:
        raise ZstdPackError("nothing in this game can go into zstd packs")
    raw_total = sum(s for _r, s in packed)
    log(f"[INFO] zstd: packing {len(packed)} file(s), {raw_total / 1024 ** 3:.2f} GB, at level {level}; "
        f"{len(loose)} file(s) stay loose (eboot, modules, sce_sys, fakelib, .ini).")

    work = root.parent / f".{root.name}.zstd-work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        idx = pack_files(root, packed, work, level=level, threads=threads,
                         progress=_Progress(log, "zstd pack", 0, 2), cancel=cancel)
        gain = idx.logical_bytes - idx.stored_bytes
        if gain * 100 < idx.logical_bytes * MIN_GAIN_PERCENT:
            raise ZstdPackError(f"the packs would save only {gain / 1024 ** 2:.1f} MB of "
                                f"{idx.logical_bytes / 1024 ** 2:.1f} MB; not worth it, so the game stays as it is")
        log("[INFO] zstd: checking every chunk against the original…")
        with Archive(root, work) as arc:
            arc.compare_with(root, progress=_Progress(log, "zstd check", 1, 2))
        removed = []
        for rec in idx.files:
            p = root / rec.path
            p.unlink()
            removed.append(p)
        _remove_emptied_dirs(root, removed)
        names = [volume_name(v) for v in range(len(idx.volume_sizes))] + [INDEX_NAME]
        for name in names:
            dest = root / name
            if dest.exists():
                raise ZstdPackError(f"the game already has a file named {name}")
            shutil.move(str(work / name), str(dest))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log(f"[OK] zstd: {idx.logical_bytes / 1024 ** 3:.2f} GB of game data packed into "
        f"{idx.stored_bytes / 1024 ** 3:.2f} GB in {len(idx.volume_sizes)} volume(s) "
        f"({(idx.logical_bytes - idx.stored_bytes) / 1024 ** 3:.2f} GB smaller).")
    return {"logical_bytes": idx.logical_bytes, "stored_bytes": idx.stored_bytes, "files": len(idx.files),
            "volumes": len(idx.volume_sizes)}


def unpack_game(root: Path, *, log=print) -> dict:
    """in place, root must be a copy the app may change. runtime stays, it reads loose files too"""
    import build_ampr_index

    root = Path(root).resolve()
    if not is_packed(root):
        raise ZstdPackError(f"this game holds no zstd packs (no {INDEX_NAME})")
    with Archive(root) as arc:
        log(f"[INFO] zstd: unpacking {len(arc.index.files)} file(s) from {len(arc.index.volume_sizes)} volume(s)…")
        got = arc.extract_to(root, progress=_Progress(log, "zstd unpack"))
        volumes = [volume_name(v) for v in range(len(arc.index.volume_sizes))]
        files = len(arc.index.files)
    for name in volumes + [INDEX_NAME]:
        p = root / name
        if p.is_file():
            p.unlink()
    if build_ampr_index.build_index_local(root, root / AMPR_INDEX, False) != 0:
        raise ZstdPackError("could not rebuild ampr_emu.index for the unpacked game")
    log(f"[OK] zstd: unpacked {files} file(s), {got / 1024 ** 3:.2f} GB, back to plain files.")
    return {"files": files, "bytes": got}
