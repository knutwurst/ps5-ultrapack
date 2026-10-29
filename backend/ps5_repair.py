"""checks a PS5 package in place; a repair is a rebuild job, the source stays untouched"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import tempfile
from pathlib import Path

import fpkg

FIX_REBUILD, FIX_PLAYGO, FIX_FIRMWARE, FIX_KEYSTONE = "rebuild", "playgo", "firmware", "keystone"
FIX_STANDALONE, FIX_TROPHY = "standalone", "trophy"
# patch markers left over from an update; LibProsperoPkg drops them on every build anyway
PATCH_MARKERS = ("originContentVersion", "targetContentVersion")
LEVELS = ("fail", "warn", "info", "pass")
# LibProsperoPkg's all-zero passcode keystone, bytes 0x20..0x40; breaks retail saves
DEFAULT_KEYSTONE_FINGERPRINT = "9320F0D8DF5B1D9539508DA1CE060D9D37E0F4AFA9957689F65DA2C23575AC7C"

_FW = re.compile(r"^(?:0x)?([0-9a-fA-F]{2})([0-9a-fA-F]{2})[0-9a-fA-F]{12}$")


# UCP: SHA-1 at 0x1C over the file (field zeroed); console checks it, LibProsperoPkg leaves it stale
UCP_MAGIC = 0xB228C60A
UCP_DIRS = ("sce_sys/trophy2/", "sce_sys/uds/")


def ucp_problem(data: bytes) -> str:
    """why data is no UCP archive, empty when it is one; the digest is not checked"""
    if len(data) < 0x60 or struct.unpack_from(">I", data, 0)[0] != UCP_MAGIC:
        return "not a UCP archive"
    version, total = struct.unpack_from(">IQ", data, 4)
    count, rec = struct.unpack_from(">II", data, 0x10)
    if version != 1 or rec != 0x40:
        return "an unknown UCP layout"
    if total != len(data):
        return f"its size field says {total} bytes, the file has {len(data)}"
    table_end = 0x60 + count * 0x40
    if table_end > len(data):
        return "its entry table runs past the end"
    for i in range(count):
        off, size = struct.unpack_from(">QQ", data, 0x60 + i * 0x40 + 0x20)
        if off < table_end or off + size > total:
            return f"entry {i} lies outside the file"
    return ""


def ucp_digest(data: bytes) -> bytes:
    buf = bytearray(data)
    buf[0x1C:0x30] = bytes(20)
    return hashlib.sha1(bytes(buf)).digest()


def ucp_digest_ok(data: bytes) -> bool:
    return len(data) >= 0x60 and data[0x1C:0x30] == ucp_digest(data)


def ampr_index_rows(data: bytes) -> dict:
    """{'/app0/<path>' lowercased: size} of an AMPRIDX3 index; ValueError when it is none."""
    if len(data) < 48 or data[:8] != b"AMPRIDX3":
        raise ValueError("not an AMPRIDX3 index")
    _m, version, rec, count, blob_len, _ht, _ss, _sc = struct.unpack_from("<8sIIQQQII", data, 0)
    blob = 48 + count * rec
    if version != 3 or rec != 24 or blob + blob_len > len(data):
        raise ValueError("an AMPR index this check does not know")
    out = {}
    for i in range(count):
        off, plen, size, _mtime = struct.unpack_from("<IIQq", data, 48 + i * rec)
        out[data[blob + off:blob + off + plen].decode("utf-8", "replace").lower()] = size
    return out


# the index skips these besides sce_sys: itself and the emulator's own files
AMPR_UNINDEXED = ("ampr_emu.index", "ampr_commands.bin", "apr_emu.log")


def ampr_mismatch(index: dict, sizes: dict) -> list[str]:
    """game files the index sizes wrong or misses, plus indexed files that are gone"""
    try:
        from ultra_core import is_fs_junk_name
    except ImportError:                             # the backend on its own
        def is_fs_junk_name(name: str) -> bool:
            return name.startswith("._") or name.lower() in (".ds_store", "thumbs.db", "desktop.ini")
    bad = []
    data = {p: n for p, n in sizes.items() if not p.startswith("sce_sys/") and p not in AMPR_UNINDEXED
            and not is_fs_junk_name(p.rsplit("/", 1)[-1])}
    for p, n in data.items():
        if index.get("/app0/" + p) != n:
            bad.append(p)
    for k in index:
        p = k[len("/app0/"):]
        if not p.startswith("sce_sys/") and p not in data:
            bad.append(p)
    return sorted(set(bad))


def fw_text(v) -> str:
    """param.json "0x0450000000000000" -> "4.50"; "" when absent or malformed."""
    m = _FW.match(str(v or ""))
    return f"{int(m.group(1), 16):X}.{m.group(2)}" if m else ""


def fw_tuple(text: str) -> tuple[int, int] | None:
    """"4.50" -> (4, 50); "10.xx" -> (10, 0): a console of that line is at least x.00."""
    m = re.match(r"^(\d+)\.(\d\d|xx)$", str(text or "").strip())
    return (int(m.group(1)), 0 if m.group(2) == "xx" else int(m.group(2))) if m else None


def _group(check: str) -> str:
    for prefix, name in (("cnt.playgo", "PlayGo"), ("image.", "Images"), ("license.", "Licence"),
                         ("np.", "Trophies"), ("sound.", "Sound"), ("param.", "param.json"),
                         ("eboot.", "Executable"), ("inner.", "Image"), ("fih.", "Package"), ("cnt.", "Package")):
        if check.startswith(prefix):
            return name
    return "Package"


def fix_for(check: str, level: str, message: str) -> str | None:
    if level == "pass":
        return None
    if check.startswith("cnt.playgo"):
        return FIX_PLAYGO
    if check.startswith(("image.", "license.")) or check == "finalized":
        return FIX_REBUILD
    if check == "magic" and "CNT" in message:
        return FIX_REBUILD
    if check == "eboot.magic" and "raw ELF" in message:
        return FIX_REBUILD
    return None


def keystone_problem(data: bytes) -> str:
    """PS5 keystones are format 3; the checksum needs keys that aren't public"""
    if len(data) != 96:
        return "not a keystone: a keystone file has exactly 96 bytes"
    if data[:8] != b"keystone":
        return "not a keystone: the file does not start like one"
    if data[8] == 2:
        return "a PS4 keystone (format 2); a PS5 package needs a PS5 one (format 3)"
    if data[8] != 3:
        return f"an unknown keystone format ({data[8]})"
    if data[0x20:0x40].hex().upper() == DEFAULT_KEYSTONE_FINGERPRINT:
        return "this is the default keystone itself; take the one from the game's own dump"
    return ""


def _pkg_member(pkg: Path, member_lower: str, entries, td: Path) -> bytes | None:
    names = {str(e.get("path", "")).lower(): e["path"] for e in entries}
    if member_lower not in names:
        return None
    mf = td / ("m-" + member_lower.replace("/", "_"))
    mf.write_text(names[member_lower] + "\n", encoding="utf-8")
    if fpkg.extract_members(pkg, td / "x", mf, on_line=lambda _l: None) != 0:
        return None
    try:
        return (td / "x" / names[member_lower]).read_bytes()
    except OSError:
        return None


def keystone_from(source: str, scratch=None, image_member=None) -> tuple[bytes | None, str]:
    """takes a keystone file, its 192 hex digits, a game folder, a PS5 package or an image"""
    raw = str(source or "").strip()
    if re.fullmatch(r"[0-9A-Fa-f]{192}", raw):
        data = bytes.fromhex(raw)
    else:
        p = Path(raw)
        data = None
        if p.is_dir():
            ks = p / "sce_sys" / "keystone"
            if not ks.is_file():
                return None, "the folder has no sce_sys/keystone"
            data = ks.read_bytes()
        elif p.suffix.lower() == ".pkg" and p.is_file():
            if fpkg.is_ps4(p):
                return None, "a PS4 package; a PS5 package needs a PS5 keystone"
            with tempfile.TemporaryDirectory(prefix="ultrapack_ps5ks_", dir=scratch) as td:
                data = _pkg_member(p, "sce_sys/keystone", fpkg.list_inner(p).get("entries") or [], Path(td))
            if data is None:
                return None, "the package has no sce_sys/keystone"
        elif p.suffix.lower() in (".ffpfsc", ".ffpfs") and p.is_file():
            if image_member is None:
                return None, "cannot read inside this image here"
            with tempfile.TemporaryDirectory(prefix="ultrapack_ps5ks_", dir=scratch) as td:
                got = image_member(p, "sce_sys/keystone", Path(td))
                data = Path(got).read_bytes() if got and Path(got).is_file() else None
            if data is None:
                return None, "the image has no sce_sys/keystone"
        elif p.is_file():
            data = p.read_bytes()[:4096]
        else:
            return None, "not a keystone file, a game folder, a package or 192 hex digits"
    why = keystone_problem(data)
    return (None, why) if why else (data, "")


def standalone_changes(param: dict) -> list[str]:
    out = [k for k in PATCH_MARKERS if k in param]
    if str(param.get("versionFileUri") or "").strip():
        out.append("versionFileUri")
    add = param.get("addcont")
    if isinstance(add, dict) and add.get("serviceIdForSharing"):
        out.append("addcont.serviceIdForSharing")
    return out


def make_standalone(param: dict) -> list[str]:
    """clears update URL, patch markers and sharing id, in place"""
    done = standalone_changes(param)
    for k in PATCH_MARKERS:
        param.pop(k, None)
    if "versionFileUri" in done:
        param["versionFileUri"] = ""
    if "addcont.serviceIdForSharing" in done:
        v = param["addcont"]["serviceIdForSharing"]
        param["addcont"]["serviceIdForSharing"] = [] if isinstance(v, list) else ""
    return done


def repair_folder(root, log=print, standalone: bool = True, trophies: bool = False) -> list[str]:
    """only on the app's own unpacked copy: standalone param.json, trophy SHA-1s"""
    root = Path(root)
    done = []
    pj = root / "sce_sys" / "param.json"
    if standalone and pj.is_file():
        param = json.loads(pj.read_text(encoding="utf-8-sig"))
        changed = make_standalone(param)
        if changed:
            pj.write_text(json.dumps(param, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            log(f"[INFO] Standalone: {', '.join(changed)} cleared in param.json")
            done.append("standalone")
    if trophies:
        fixed = 0
        for d in UCP_DIRS:
            for f in sorted((root / d).glob("*.ucp")) if (root / d).is_dir() else ():
                data = f.read_bytes()
                if ucp_problem(data) or ucp_digest_ok(data):
                    continue
                buf = bytearray(data)
                buf[0x1C:0x30] = ucp_digest(data)
                f.write_bytes(bytes(buf))
                log(f"[INFO] Wrote the checksum of {d}{f.name}")
                fixed += 1
        if fixed:
            done.append("trophies")
    return done


def is_update(info: dict) -> bool:
    ident = info.get("ident") or {}
    if any(w in str(ident.get("package_type", "")).lower() for w in ("delta", "patch")):
        return True
    return any(str(e.get("name", "")).lower() in ("origin-deltainfo.dat", "target-deltainfo.dat")
               for e in ident.get("entries") or [])


def _version(v) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))) or (0,)


def peek(pkg, scratch=None, title_id: str = "") -> dict | None:
    """None when title_id is given and the header says another title"""
    pkg = Path(pkg)
    ident = fpkg.inspect_json(pkg)
    if title_id and str(ident.get("title_id") or "") != title_id:
        return None
    out = {"path": str(pkg), "name": pkg.name, "title_id": str(ident.get("title_id") or ""), "version": "",
           "origin": "", "update": is_update({"ident": ident})}
    with tempfile.TemporaryDirectory(prefix="ultrapack_ps5peek_", dir=scratch) as td:
        pj = _pkg_member(pkg, "sce_sys/param.json", fpkg.list_inner(pkg).get("entries") or [], Path(td))
    if pj:
        try:
            param = json.loads(pj.decode("utf-8", errors="replace"))
            out["version"] = str(param.get("contentVersion") or "")
            out["origin"] = str(param.get("originContentVersion") or "")
        except ValueError:
            pass
    return out


def related(pkg, title_id: str, scratch=None, limit: int = 30, max_read: int = 150) -> list[dict]:
    """same-title PS5 packages next to pkg, two levels deep; paths with the title id first"""
    import os
    pkg = Path(pkg)
    out = []
    if not title_id:
        return out
    near = []
    for d, dirs, files in os.walk(pkg.parent):
        if len(Path(d).relative_to(pkg.parent).parts) >= 2:
            dirs[:] = []
        near += [Path(d) / f for f in files if f.lower().endswith(".pkg") and not f.startswith("._")]
    tid = title_id.lower()
    near = sorted((p for p in near if p != pkg), key=lambda p: (tid not in str(p).lower(), str(p)))
    for n, x in enumerate(near):
        if len(out) >= limit or n >= max_read:
            break
        try:
            if fpkg.is_ps4(x):
                continue
            p = peek(x, scratch, title_id)
        except Exception:
            continue
        if p is not None:
            out.append(p)
    return out


def examine(pkg, scratch=None, neighbours: bool = False) -> dict:
    """reads the package in place; scratch only gets a few small files"""
    pkg = Path(pkg)
    out = {"ident": {}, "results": [], "param": None, "files": 0, "keystone": None, "keystone_fp": "",
           "sfo": False, "error": ""}
    try:
        out["ident"] = fpkg.inspect_json(pkg)
    except Exception as e:
        out["error"] = f"not a readable PS5 package: {e}"
        return out
    with tempfile.TemporaryDirectory(prefix="ultrapack_ps5check_", dir=scratch) as td:
        lines: list[str] = []
        fpkg.validate(pkg, json_out=True, temp_dir=td, on_line=lines.append)
        text = "\n".join(lines)
        try:
            out["results"] = json.loads(text[text.index("{"):])["results"]
        except (ValueError, KeyError):
            out["error"] = "the validate checklist did not run: " + (lines[-1] if lines else "no output")
        try:
            entries = fpkg.list_inner(pkg).get("entries") or []
        except Exception as e:
            out["error"] = out["error"] or f"the image cannot be listed: {e}"
            return out
        out["files"] = sum(1 for e in entries if e.get("type") == "file")
        lower = {str(e.get("path", "")).lower(): e for e in entries}
        out["ucp"] = []
        for path in sorted(p for p in lower if p.endswith(".ucp") and p.startswith(UCP_DIRS)):
            data = _pkg_member(pkg, path, entries, Path(td))
            if data is not None:
                out["ucp"].append({"path": lower[path]["path"], "problem": ucp_problem(data),
                                   "digest_ok": ucp_digest_ok(data)})
        out["ampr"] = {"emu": "fakelib/libsceampr.sprx" in lower, "index": "ampr_emu.index" in lower, "bad": []}
        if out["ampr"]["index"]:
            data = _pkg_member(pkg, "ampr_emu.index", entries, Path(td))
            try:
                sizes = {p: int(e.get("size") or 0) for p, e in lower.items() if e.get("type") == "file"}
                out["ampr"]["bad"] = ampr_mismatch(ampr_index_rows(data or b""), sizes)
            except ValueError as e:
                out["ampr"]["error"] = str(e)
        out["sfo"] = any(str(e.get("path", "")).lower() == "sce_sys/param.sfo" for e in entries)
        ks = _pkg_member(pkg, "sce_sys/keystone", entries, Path(td))
        out["keystone"] = ks is not None
        if ks is not None and len(ks) == 96:
            out["keystone_fp"] = ks[0x20:0x40].hex().upper()
        pj = _pkg_member(pkg, "sce_sys/param.json", entries, Path(td))
        if pj is not None:
            try:
                out["param"] = json.loads(pj.decode("utf-8", errors="replace"))
            except ValueError:
                pass
    if neighbours:
        out["related"] = related(pkg, str(out["ident"].get("title_id") or ""), scratch)
    return out


def findings(info: dict, console_fw: str | None = None) -> list[dict]:
    """rows for the view, problems first; console_fw like 7.61 or 10.xx"""
    rows = []
    for r in info.get("results") or []:
        check, level, msg = str(r.get("Check", "")), str(r.get("Level", "")), str(r.get("Message", ""))
        rows.append({"check": check, "group": _group(check), "level": level if level in LEVELS else "info",
                     "text": msg, "fix": fix_for(check, level, msg)})
    param = info.get("param") or {}

    def own(check, group, level, fix, tmpl, **args):
        # own rows keep their English template so the UI can translate them
        rows.append({"check": check, "group": group, "level": level, "fix": fix,
                     "tmpl": tmpl, "args": args, "text": tmpl.format(**args)})
    need = fw_text(param.get("requiredSystemSoftwareVersion"))
    need = "" if need == "0.00" else need          # no requirement
    have = fw_tuple(console_fw) if console_fw else None
    if need and have and (fw_tuple(need) or (0, 0)) > have:
        # a warning: reports differ on whether the console enforces it at install
        own("console.firmware", "Firmware", "warn", FIX_FIRMWARE,
            "needs system software {need}, the console runs {fw}", need=need, fw=console_fw)
    elif need and console_fw:
        own("console.firmware", "Firmware", "pass", None, "needs system software {need}, the console runs {fw}",
            need=need, fw=console_fw)
    elif need:
        own("console.firmware", "Firmware", "pass", None, "needs system software {need}", need=need)
    patch = is_update(info)
    for u in info.get("ucp") or []:
        name = u["path"].split("/")[-1]
        group = "Trophies" if "/trophy2/" in u["path"].lower() else "Activities"
        if u["problem"]:
            own("ucp." + name, group, "fail", None, "{name} is damaged: {why}", name=name, why=u["problem"])
        elif not u["digest_ok"]:
            own("ucp." + name, group, "fail", FIX_TROPHY,
                "{name}: its checksum does not match (the console checks it)", name=name)
        else:
            own("ucp." + name, group, "pass", None, "{name}: checksum matches", name=name)
    ampr = info.get("ampr") or {}
    if ampr.get("emu") and not ampr.get("index"):
        own("ampr.index", "AMPR", "fail", FIX_REBUILD,
            "the AMPR emulator is in fakelib but ampr_emu.index is missing: the game exits at start")
    elif ampr.get("index") and ampr.get("error"):
        own("ampr.index", "AMPR", "warn", FIX_REBUILD if ampr.get("emu") else None,
            "ampr_emu.index cannot be read: {why}", why=ampr["error"])
    elif ampr.get("index") and ampr.get("bad"):
        own("ampr.index", "AMPR", "fail" if ampr.get("emu") else "warn", FIX_REBUILD if ampr.get("emu") else None,
            "ampr_emu.index does not match {n} game files (first: {first})", n=len(ampr["bad"]), first=ampr["bad"][0])
    elif ampr.get("index"):
        own("ampr.index", "AMPR", "pass", None, "ampr_emu.index matches the game files")
    mine = _version(param.get("contentVersion"))
    for r in info.get("related") or []:
        if patch and not r["update"]:
            if mine <= _version(r["version"]):
                own("update.version", "Updates", "fail", None,
                    "not newer than the game next to it (v{base}): the console does not install it over the game",
                    base=r["version"])
            else:
                own("update.version", "Updates", "pass", None, "newer than the game next to it (v{base})",
                    base=r["version"])
            origin = str(param.get("originContentVersion") or "")
            if origin and _version(origin) != _version(r["version"]):
                own("update.origin", "Updates", "warn", None,
                    "made for game version {origin}, the game next to it is v{base}", origin=origin,
                    base=r["version"])
        elif not patch and r["update"]:
            if _version(r["version"]) <= mine:
                own("update." + r["name"], "Updates", "fail", None,
                    "the update {name} next to it is not newer than this game", name=r["name"])
            elif r["origin"] and _version(r["origin"]) != mine:
                own("update." + r["name"], "Updates", "warn", None,
                    "the update {name} next to it was made for game version {origin}", name=r["name"],
                    origin=r["origin"])
            else:
                own("update." + r["name"], "Updates", "pass", None, "the update {name} next to it fits",
                    name=r["name"])
    if patch and info.get("related") is not None and not any(not r["update"] for r in info["related"]):
        own("update.base", "Updates", "info", None,
            "no game package of this title next to it; the update needs the game installed first")
    markers = [k for k in PATCH_MARKERS if k in param]
    if markers and not patch:
        own("param.patch", "Updates", "warn", FIX_REBUILD,
            "marked as a patch applied over a base ({keys}) although it is a full game", keys=", ".join(markers))
    if str(param.get("versionFileUri") or "").strip() and not patch:
        own("param.versionFileUri", "Updates", "info", FIX_STANDALONE, "the game asks Sony's update server at start")
    if info.get("sfo"):
        own("image.param.sfo", "Package", "fail", FIX_REBUILD,
            "has a param.sfo: the console takes the title for a PS4 one")
    fp = info.get("keystone_fp") or ""
    if info.get("keystone") is False:
        own("image.keystone", "Saves", "warn", FIX_KEYSTONE,
            "no keystone: a build makes a new one, so saves of the retail game will not load")
    elif fp == DEFAULT_KEYSTONE_FINGERPRINT:
        own("image.keystone", "Saves", "warn", FIX_KEYSTONE,
            "the default keystone: saves of the retail game will not load")
    elif info.get("keystone"):
        own("image.keystone", "Saves", "pass", None, "keystone {fp}…, not the default one; a rebuild keeps it",
            fp=fp[:16])
    if patch:
        for r in rows:
            r["fix"] = None                 # the repair builds full games, never an update
        own("update.kind", "Updates", "info", None, "an update package: it is checked only, the rebuild is for full games")
    order = {lv: i for i, lv in enumerate(LEVELS)}
    return sorted(rows, key=lambda r: order.get(r["level"], 2))


def summary(rows: list[dict]) -> dict:
    """{'fail': n, 'warn': n, 'pass': n, 'fixes': {fix, ...}, 'unfixable': n}"""
    out = {lv: sum(1 for r in rows if r["level"] == lv) for lv in LEVELS}
    bad = [r for r in rows if r["level"] in ("fail", "warn")]
    out["fixes"] = {r["fix"] for r in bad if r["fix"]}
    out["unfixable"] = sum(1 for r in bad if not r["fix"])
    return out
