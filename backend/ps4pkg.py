"""Read PS4 packages (.pkg, title ids CUSA…): identity from the header and param.sfo."""
from __future__ import annotations

import hashlib
import hmac
import re
import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = 0x7F434E54
ENTRY_PARAM_SFO = 0x1000
ENTRY_ICON0_PNG = 0x1200
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
    return header_from_bytes(h)


def header_from_bytes(h: bytes) -> Ps4Header:
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


def identity_from_prefix(read_prefix, limit: int = 32 << 20, hints=()) -> Ps4Identity:
    """The identity of a package that can only be read from its start (a member of an
    archive): *read_prefix(n)* returns the first n bytes. Reads the header, then as far as
    the entry table and param.sfo reach (at most *limit* bytes). *hints*: the names around
    it (the member, the archive), see _identity."""
    head = read_prefix(0x1000)
    h = header_from_bytes(head)
    if not 0 < h.entry_count < 10000:
        raise Ps4PackageError("implausible entry count")
    need = h.entry_table_offset + 32 * h.entry_count
    if need > limit:
        raise Ps4PackageError("entry table beyond the readable start")
    data = read_prefix(need)
    for i in range(h.entry_count):
        eid, _fn, _f1, _f2, off, size = struct.unpack_from(">IIIIII", data, h.entry_table_offset + i * 32)
        if eid == ENTRY_PARAM_SFO:
            if off + size > limit:
                raise Ps4PackageError("param.sfo beyond the readable start")
            data = read_prefix(off + size)
            if len(data) < off + size:
                raise Ps4PackageError("param.sfo cut short")
            return _identity(h, parse_sfo(data[off:off + size]), hints)
    raise Ps4PackageError("no param.sfo in the package")


def read_icon(path) -> bytes | None:
    """The package's icon0.png (stored in the clear like param.sfo), or None."""
    try:
        data = _read_entry(path, read_header(path), ENTRY_ICON0_PNG)
    except Ps4PackageError:
        return None
    return data if data[:8] == b"\x89PNG\r\n\x1a\n" else None


def identity_from_sfo(sfo: dict, hints=()) -> Ps4Identity:
    """The identity of an unpacked PS4 game from its sce_sys/param.sfo (no package header)."""
    cid = str(sfo.get("CONTENT_ID") or "")
    return _identity(Ps4Header(cid, 0, 0, 0, 0, 0, 0), sfo, hints)


def read_identity(path) -> Ps4Identity:
    """The package's identity; the names of the file and the folders above it count as
    hints (see _identity)."""
    h = read_header(path)
    p = Path(path)
    return _identity(h, parse_sfo(_read_entry(path, h, ENTRY_PARAM_SFO)), [p.name] + [q.name for q in p.parents][:4])


_NAME_VERSION = re.compile(r"(?<![\d.])v\s?(\d{1,2})\.(\d{1,3})(?![\d.])", re.I)


def _norm_version(v: str) -> str:
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,3})\s*", v or "")
    return f"{int(m.group(1)):02d}.{m.group(2).ljust(2, '0')}" if m else ""


def _named_versions(hints) -> set:
    """Every 'v1.71' / 'v01.71' / '[v01.71]' in *hints*, as '01.71'."""
    return {f"{int(a):02d}.{b.ljust(2, '0')}" for h in hints or () for a, b in _NAME_VERSION.findall(str(h))}


def _identity(h: Ps4Header, sfo: dict, hints=()) -> Ps4Identity:
    """APP_VER is the application's version."""
    app_ver = str(sfo.get("APP_VER") or "")
    master = str(sfo.get("VERSION") or "")
    kind = _kind(str(sfo.get("CATEGORY") or ""), h.content_type)
    version = app_ver or master
    nm = _norm_version(master)
    if (kind == "game" and app_ver and nm and nm != _norm_version(app_ver)
            and tuple(map(int, nm.split("."))) > tuple(map(int, (_norm_version(app_ver) or "0.0").split(".")))
            and nm in _named_versions(hints)):
        version = master
    tid = str(sfo.get("TITLE_ID") or h.content_id[7:16])
    return Ps4Identity(
        title=str(sfo.get("TITLE") or ""),
        title_id=tid.upper(),
        content_id=str(sfo.get("CONTENT_ID") or h.content_id),
        kind=kind,
        version=version,
        app_ver=app_ver,
        content_type=h.content_type,
    )


def _entries(path, h: Ps4Header) -> list:
    """[(id, offset, size)] of the package's entry table."""
    with open(path, "rb") as f:
        f.seek(h.entry_table_offset)
        table = f.read(32 * h.entry_count)
    return [struct.unpack_from(">IIIIII", table, i * 32)[::1] for i in range(len(table) // 32)]


KEYSTONE_SIZE = 0x60
_KEYSTONE_HEAD = b"keystone" + bytes((2, 0, 1, 0)) + bytes(20)
# the key of the MAC over the first 0x40 bytes of a keystone (public, from LibOrbisPkg)
_KEYSTONE_MAC_KEY = bytes.fromhex("783D6F3AE91C0E0712FCAAB7950BDE06855CF7A22DCDBDE127E9BFCBAD0FF0FE")


def _keystone_bytes_problem(data: bytes) -> str | None:
    if len(data) != KEYSTONE_SIZE:
        return "not a keystone: a keystone file has exactly 96 bytes"
    if data[:0x20] != _KEYSTONE_HEAD:
        return "not a keystone: the file does not start like one"
    if not hmac.compare_digest(hmac.new(_KEYSTONE_MAC_KEY, data[:0x40], hashlib.sha256).digest(), data[0x40:]):
        return "the keystone is damaged: its checksum does not match"
    return None


def keystone_problem(path) -> str | None:
    """None when *path* holds a keystone a package can take (an export from a save: 96 bytes,
    version 2, a MAC that matches), else why not."""
    try:
        data = Path(path).read_bytes() if Path(path).stat().st_size <= 4096 else b""
    except OSError as e:
        return f"cannot read the keystone file: {e.strerror or e}"
    return _keystone_bytes_problem(data)


def make_keystone(fingerprint: bytes) -> bytes:
    """The 96-byte keystone for a 32-byte fingerprint (what Apollo shows as the save's
    keystone fingerprint): the v2 header, the fingerprint, the MAC over both."""
    head = _KEYSTONE_HEAD + fingerprint
    return head + hmac.new(_KEYSTONE_MAC_KEY, head, hashlib.sha256).digest()


# the keystone a builder makes from the all-zero passcode; retail saves do not open with it
DEFAULT_KEYSTONE_FINGERPRINT = bytes.fromhex("294A5ED06DB170618F2EED8C424B9D828879C080CC66FBC4864F69E974DEB856")


def keystone_from_text(text: str) -> tuple[bytes | None, str | None]:
    """(keystone, None) or (None, why not) for what the user typed or picked: a keystone file,
    its 64 hex digits fingerprint, or all 192 hex digits of the file."""
    raw = (text or "").strip().strip('"')
    if not raw:
        return None, "no keystone given"
    hexpart = "".join(ch for ch in raw if ch not in " :-\t\n")
    if all(ch in "0123456789abcdefABCDEF" for ch in hexpart) and len(hexpart) in (64, 192):
        data = bytes.fromhex(hexpart)
        if len(data) == 32:
            data = make_keystone(data)
    else:
        p = Path(raw).expanduser()
        if not p.is_file():
            return None, "not a keystone file and not 64 or 192 hex digits"
        try:
            data = p.read_bytes() if p.stat().st_size <= 4096 else b""
        except OSError as e:
            return None, f"cannot read the keystone file: {e.strerror or e}"
    why = _keystone_bytes_problem(data)
    if why:
        return None, why
    if data[0x20:0x40] == DEFAULT_KEYSTONE_FINGERPRINT:
        return None, "this is the default keystone itself; use the one from a save of the retail game"
    return data, None


def read_sfo(path) -> dict:
    """The param.sfo values of the package (from its header, a few KB)."""
    h = read_header(path)
    return parse_sfo(_read_entry(path, h, ENTRY_PARAM_SFO))


def _ver(v) -> tuple:
    try:
        return tuple(int(x) for x in str(v or "").split("."))
    except ValueError:
        return ()


def version_problem(path, sfo: dict | None = None) -> dict | None:
    """A full game whose master version (VERSION) is above its app version (APP_VER): an update
    merged into the game. The console then still downloads the official update. Returns
    {'app_ver', 'version', 'named'}: 'named' when the file or folder name gives the master version
    as the package version, which marks a merged release (a re-released master looks the same in
    the param.sfo, so that alone is no proof)."""
    sfo = sfo if sfo is not None else read_sfo(path)
    app, ver = str(sfo.get("APP_VER") or ""), str(sfo.get("VERSION") or "")
    if str(sfo.get("CATEGORY") or "") != "gd" or not app or not ver or not _ver(ver) > _ver(app):
        return None
    names = " ".join([Path(path).name] + [q.name for q in Path(path).parents][:2]).lower()
    short = ver.lstrip("0") or ver
    named = any(f"v{x}" in names for x in (ver, short)) and not any(f"v{x}" in names for x in (app, app.lstrip("0") or app))
    return {"app_ver": app, "version": ver, "named": named}


def save_folder(sfo: dict) -> str:
    """The title id a game's or update's saves live under."""
    return str(sfo.get("INSTALL_DIR_SAVEDATA") or sfo.get("TITLE_ID") or "")


ENTRY_GENERAL_DIGESTS = 0x80
BASE_CONTENT_SLOT, BASE_GAME_SLOT = 10, 12      # an update's record of its base package


def general_digests(path) -> dict[int, bytes]:
    """{slot: 32 bytes} of the set general digests (1 content, 2 game, ... 10 and 12 the base
    package an update belongs to)."""
    h = read_header(path)
    for eid, _fn, _f1, _f2, off, size in _entries(path, h):
        if eid == ENTRY_GENERAL_DIGESTS:
            with open(path, "rb") as f:
                f.seek(off)
                d = f.read(size)
            flags = struct.unpack_from(">I", d, 28)[0]
            return {i: d[32 * i:32 * i + 32] for i in range(1, size // 32) if flags >> i & 1}
    raise Ps4PackageError("no general digests entry")


if __name__ == "__main__":
    # python3 backend/ps4pkg.py PKG [PKG ...]: what the app reads from a PS4 package (the
    # header, the entry table and every param.sfo field). Reads a few MB, never the game.
    import sys
    for arg in sys.argv[1:]:
        print(f"== {arg}")
        try:
            hdr = read_header(arg)
            print(f"content_id   {hdr.content_id}\ncontent_type 0x{hdr.content_type:X}\n"
                  f"pfs_flags    0x{hdr.pfs_flags:X}\nentries      {hdr.entry_count}")
            for eid, _fn, _f1, _f2, off, size in _entries(arg, hdr):
                print(f"  entry 0x{eid:04X}  offset {off:>12}  size {size}")
            for k, v in sorted(parse_sfo(_read_entry(arg, hdr, ENTRY_PARAM_SFO)).items()):
                print(f"  sfo {k:<20} {v!r}")
            print("identity    ", read_identity(arg))
        except Exception as e:                      # noqa: BLE001 - a diagnostic prints and goes on
            print(f"  error: {e}")
