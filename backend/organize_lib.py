"""Organize a library in place, and write one source into a library (the "Organize" output).

scan()    finds what a folder holds: game folders, images, packages and archive sets (each read
          for its game), and the other files beside them (companions). OS clutter is never
          listed. Reading goes through an *identify(path, kind)* callable, so the rules can be
          tested on synthetic folders; make_identify() builds the real one.
plan()    decides where everything belongs: one title folder per game directly under the
          root (ultra_core.library_layout names it and everything in it). A folder that holds
          only one game is renamed to the title folder, so what sits beside the game travels
          with it; a folder holding several games is split; archives whose game is unclear
          and anything that would collide stay where they are, with the reason.
apply()   carries a plan out with renames on the same drive only, removes folders it left
          empty and the '._' sidecars of what it moved, and writes a journal.
undo()    moves everything in a journal back.
organize_into() copies or moves one source (file or folder tree) into a library under its
          library name, joining a title folder that is already there."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # ultra_core next to the app
import ultra_core as uc
from ultra_core import is_fs_junk_name

FILE_KINDS = {".ffpfs": "ffpfs", ".ffpfsc": "ffpfsc", ".exfat": "exfat", ".ffpkg": "ffpkg", ".pkg": "pkg"}
# Half-written files of a copy or sort that is still running (or was cut short): never touched.
PARTIAL_SUFFIXES = (".copy-tmp", ".ps4sort-part")
_TAG = re.compile(r"\[((?:PPSA|CUSA)\d{5})\]", re.I)
_NAME_ROLE = ((re.compile(r"\bback-?port(?:ed)?\b", re.I), "backport"),
              (re.compile(r"\b(?:update|patch)\b", re.I), "update"))


@dataclass
class Entry:
    """A game folder, an image, a package or an archive set (*parts* lists its volumes)."""
    path: Path
    kind: str                       # folder | ffpfs | ffpfsc | exfat | ffpkg | pkg | archive
    platform: str = ""              # ps5 | ps4
    role: str = ""                  # game | update | dlc | backport | other
    title: str = ""
    title_id: str = ""
    version: str = ""
    fw: str = ""
    parts: list = field(default_factory=list)
    note: str = ""                  # why the game could not be read
    ps4: object = None              # ps4pkg.Ps4Identity

    @property
    def known(self) -> bool:
        return bool(self.title_id)

    @property
    def paths(self) -> list:
        return self.parts or [self.path]


@dataclass
class Move:
    src: Path
    dst: Path
    group: str                      # the title id the move belongs to
    label: str                      # the game's title, for the preview
    is_dir: bool = False


@dataclass
class Stay:
    path: Path
    reason: str


@dataclass
class Plan:
    root: Path
    moves: list
    stays: list
    in_place: int = 0               # entries already where they belong


def _say(on_line, text):
    (on_line or (lambda t: print(t, flush=True)))(text)


# ── scan ─────────────────────────────────────────────────────────────────────
def _game_folder_kind(d: Path) -> str:
    """'ps5' / 'ps4' for an unpacked game (sce_sys + eboot.bin), else ''."""
    try:
        if not (d / "eboot.bin").is_file():
            return ""
        if (d / "sce_sys" / "param.json").is_file():
            return "ps5"
        if (d / "sce_sys" / "param.sfo").is_file():
            return "ps4"
    except OSError:
        pass
    return ""


def _skipped(name: str) -> bool:
    return (name.startswith(".") or is_fs_junk_name(name) or name in uc._JOB_SCAN_SKIP_DIRS
            or name.lower().endswith(PARTIAL_SUFFIXES))


def walk(root) -> tuple[list[Entry], list[Path]]:
    """(entries, companions) under *root*, unread. Links are not followed. A folder that
    holds no entry at any depth is one companion."""
    root = Path(root)
    entries: list[Entry] = []
    companions: list[Path] = []

    def visit(d: Path) -> tuple[bool, list]:
        """(holds an entry, the companions of *d* and below)."""
        try:
            children = sorted(d.iterdir(), key=lambda q: q.name.lower())
        except OSError:
            return False, []
        found = False
        mine: list[Path] = []
        archives: dict[str, list] = {}
        for c in children:
            if _skipped(c.name) or c.is_symlink():
                continue
            if c.is_dir():
                if _game_folder_kind(c):
                    entries.append(Entry(c, "folder"))
                    found = True
                else:
                    sub_found, sub_mine = visit(c)
                    if sub_found:
                        found = True
                        mine.extend(sub_mine)
                    else:
                        mine.append(c)
            elif c.is_file():
                kind = FILE_KINDS.get(c.suffix.lower())
                if kind:
                    entries.append(Entry(c, kind))
                    found = True
                elif uc.is_archive_file(c) or re.search(r"\.(7z|zip)\.\d{3}$", c.name, re.I):
                    first = uc.ArchiveExtractor._first_volume(c)
                    archives.setdefault(str(first), []).append(c)
                else:
                    mine.append(c)
        for first, vols in archives.items():
            parts = sorted({*vols, *(p for p in uc.archive_set_parts(Path(first)) if p.parent == d)},
                           key=lambda q: q.name.lower())
            entries.append(Entry(Path(first), "archive", parts=parts))
            found = True
        return found, mine

    companions.extend(visit(root)[1])
    entries.sort(key=lambda e: str(e.path).lower())
    return entries, companions


def _name_role(name: str) -> str:
    for rx, role in _NAME_ROLE:
        if rx.search(name):
            return role
    return ""


def scan(root, identify: Callable, on_progress=None) -> tuple[list[Entry], list[Path]]:
    """walk() plus the game of every entry, read through *identify(path, kind)* →
    {'title','title_id','version','fw','role','platform','ps4'} or None (it may raise; the
    message becomes the entry's note). *on_progress(done, total, name)* after each read."""
    entries, companions = walk(root)
    for n, e in enumerate(entries, 1):
        try:
            got = identify(e.path, e.kind)
        except Exception as ex:                       # noqa: BLE001 - a reader's failure is a note
            got, e.note = None, str(ex) or ex.__class__.__name__
        if got and got.get("title_id"):
            e.title = str(got.get("title") or "")
            e.title_id = str(got["title_id"]).upper()
            e.version = str(got.get("version") or "")
            e.fw = str(got.get("fw") or "")
            e.ps4 = got.get("ps4")
            e.platform = got.get("platform") or ("ps4" if e.title_id.startswith("CUSA") else "ps5")
            role = got.get("role") or "game"
            if e.platform == "ps5" and e.kind == "pkg" and role == "game":
                role = _name_role(e.path.stem) or role     # the user's own naming, until the package says more
            e.role = role
        elif not e.note:
            e.note = ("the game inside cannot be read before unpacking" if e.kind == "archive"
                      else "no game metadata found")
        if on_progress:
            on_progress(n, len(entries), e.path.name)
    return entries, companions


# ── plan ─────────────────────────────────────────────────────────────────────
def _below(p: Path, root: Path) -> list[Path]:
    """The folders between *root* (excluded) and *p* (excluded), nearest first."""
    out = []
    q = p.parent
    while q != root and root in q.parents:
        out.append(q)
        q = q.parent
    return out


def _norm(s: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() + " "


def _set_stem(name: str) -> str:
    return re.sub(r"(\.part\d+)?\.(rar|zip|7z|r\d{2,}|\d{3}|ffpfsc|ffpfs|pkg|exfat|ffpkg)$", "", name, flags=re.I)


def _known_folder(d: Path, tid: str):
    """A Ps4Library for *d* when its name carries *tid*'s tag (a title folder already there)."""
    if not d or not _TAG.search(d.name) or _TAG.search(d.name).group(1).upper() != tid:
        return None
    return uc.Ps4Library(d.name, uc._lib_version_tag(d.name), (d / uc.PS4_DLC_PACK).is_dir())


def _lib_item(e: Entry) -> uc.LibItem:
    return uc.LibItem(e.path, e.kind, e.role or "game", e.title, e.title_id, e.version, e.fw, e.ps4)


def plan(root, entries: list[Entry], companions: list[Path]) -> Plan:
    root = Path(root)
    moves: list[Move] = []
    stays: list[Stay] = [Stay(e.path, e.note or "no game metadata found") for e in entries if not e.known]
    unclear = {e.path.parent for e in entries if e.kind == "archive" and not e.known}
    titles_below: dict[Path, set] = defaultdict(set)
    for e in entries:
        for d in _below(e.path, root):
            titles_below[d].add(e.title_id if e.known else "?")
    by_title: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.known:
            by_title[e.title_id].append(e)
    in_place = 0
    title_dirs: dict[str, Path] = {}
    labels: dict[str, str] = {}
    for tid, group in sorted(by_title.items()):
        # The highest folder below the root that holds this game and nothing else.
        tops: dict[Path, int] = defaultdict(int)
        for e in group:
            top = None
            for d in _below(e.path, root):
                if titles_below[d] == {tid}:
                    top = d
            if top is not None:
                tops[top] += 1
        home = None
        if tops:
            home = sorted(tops, key=lambda d: (d.parent != root, _TAG.search(d.name) is None, -tops[d],
                                               d.name.lower()))[0]
        known = _known_folder(home, tid) if home is not None and home.parent == root else None
        placed = {p: (f, s, n) for p, f, s, n in uc.library_layout([_lib_item(e) for e in group],
                                                                   {tid: known} if known else None)}
        title_dir = title_dirs[tid] = root / next(iter(placed.values()))[0]
        label = labels[tid] = next((e.title for e in group if e.role == "game" and e.title), group[0].title) or tid
        if home is not None and home != title_dir:
            if title_dir.exists() and not _same_entry(home, title_dir):
                home = None                                   # the name is taken: no rename, single moves
            else:
                moves.append(Move(home, title_dir, tid, label, is_dir=True))

        def now(p: Path, home=home, title_dir=title_dir) -> Path:
            """Where *p* is once the home folder has its new name."""
            if home is not None and (p == home or home in p.parents):
                return title_dir / p.relative_to(home)
            return p

        for e in group:
            _f, sub, name = placed[e.path]
            dst_dir = title_dir / sub if sub else title_dir
            if e.kind == "archive":
                if e.path.parent in unclear:
                    stays.extend(Stay(p, "it is not clear which game the archives here belong to") for p in e.paths)
                    continue
                if home is not None and home in e.path.parents:
                    in_place += 1                             # travels with its folder, name kept
                    continue
                for p in e.paths:
                    moves.append(Move(p, title_dir / p.name, tid, label))
                continue
            dst = dst_dir / name
            if now(e.path) == dst:
                in_place += 1
                continue
            moves.append(Move(e.path, dst, tid, label, is_dir=e.kind == "folder"))
        # What sits beside the game: in a folder of this game alone it goes along (the home
        # moves as a whole; any other such folder is emptied into the title folder).
        for top in tops:
            if top == home:
                continue
            for c in companions:
                if top == c.parent or top in c.parents:
                    moves.append(Move(c, title_dir / c.relative_to(top), tid, label, is_dir=c.is_dir()))
    # In a folder shared by several games (or the root), a companion goes along only when its
    # name ties it to exactly one game there: the title id, the title, or the name of an
    # archive set or container of that game.
    owned = {m.src for m in moves}
    titles_in: dict[Path, dict] = defaultdict(dict)
    for e in entries:
        if e.known:
            keys = titles_in[e.path.parent].setdefault(e.title_id, set())
            keys.add(_norm(e.title_id))
            t = uc.canonical_game_title(e.title)
            if len(t) >= 4:
                keys.add(_norm(t))
            keys.add(_norm(_set_stem(e.path.name)))
    for c in companions:
        if c in owned or c.parent not in titles_in:
            continue
        if any(len(titles_below.get(d, ())) == 1 and "?" not in titles_below[d]
               for d in [c.parent] + _below(c.parent, root)):
            continue                                          # inside a one-game folder: handled above
        cn = _norm(_set_stem(c.name))
        hits = [tid for tid, keys in titles_in[c.parent].items() if any(k in cn for k in keys if k.strip())]
        if len(hits) != 1:
            stays.append(Stay(c, "not tied to one game by its name"))
        elif title_dirs[hits[0]] != c.parent:
            tid = hits[0]
            moves.append(Move(c, title_dirs[tid] / c.name, tid, labels[tid], is_dir=c.is_dir()))
    return _drop_conflicts(Plan(root, moves, stays, in_place))


def _same_entry(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _drop_conflicts(p: Plan) -> Plan:
    """Two things that would get the same name both stay; so does one whose target is taken
    by something that does not move away. A case-only rename is no conflict."""
    homes = {m.src: m.dst for m in p.moves if m.is_dir}

    def now(target: Path) -> Path:
        """What sits at *target* today: inside a folder that is renamed, its old place."""
        for src, dst in homes.items():
            if target == dst or dst in target.parents:
                return src / target.relative_to(dst)
        return target

    srcs = {m.src for m in p.moves}
    seen: dict[str, list] = defaultdict(list)
    for m in p.moves:
        seen[str(m.dst).lower()].append(m)
    keep, stays = [], list(p.stays)
    for m in p.moves:
        twins = seen[str(m.dst).lower()]
        if len(twins) > 1:
            others = ", ".join(t.src.name for t in twins if t is not m)
            stays.append(Stay(m.src, f"would get the same name as {others}"))
            continue
        occupant = now(m.dst)
        if os.path.lexists(occupant) and occupant not in srcs and not _same_entry(occupant, m.src):
            stays.append(Stay(m.src, f"{m.dst.name} already exists"))
            continue
        keep.append(m)
    return Plan(p.root, keep, stays, p.in_place)


def plan_to_json(p: Plan) -> dict:
    return {"root": str(p.root), "in_place": p.in_place,
            "moves": [{"src": str(m.src), "dst": str(m.dst), "group": m.group, "label": m.label, "is_dir": m.is_dir}
                      for m in p.moves],
            "stays": [{"path": str(s.path), "reason": s.reason} for s in p.stays]}


def plan_from_json(d: dict) -> Plan:
    return Plan(Path(d["root"]),
                [Move(Path(m["src"]), Path(m["dst"]), m.get("group", ""), m.get("label", ""), bool(m.get("is_dir")))
                 for m in d.get("moves", [])],
                [Stay(Path(s["path"]), s.get("reason", "")) for s in d.get("stays", [])],
                int(d.get("in_place", 0)))


# ── apply / undo ─────────────────────────────────────────────────────────────
def _drop_sidecar(p: Path) -> None:
    try:
        sc = p.parent / ("._" + p.name)
        if sc.is_file():
            sc.unlink()
    except OSError:
        pass


def _drop_sidecars_of(ops: list) -> None:
    """On exFAT macOS gives a folder it creates, and a file it moves, a '._' sidecar (the
    provenance attribute); remove those beside everything the operations touched, and
    beside the folders above them."""
    seen: set = set()
    for op in ops:
        for key in ("src", "dst", "path"):
            if key in op:
                p = Path(op[key])
                for q in (p, p.parent):
                    if q not in seen:
                        seen.add(q)
                        _drop_sidecar(q)


def _rename(src: Path, dst: Path) -> None:
    """os.rename, also for a change of case alone on a drive that ignores case."""
    if src != dst and str(src).lower() == str(dst).lower():
        tmp = src.with_name(src.name + ".organize-case")
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        os.rename(src, dst)


def _only_clutter(d: Path) -> bool:
    try:
        return all(is_fs_junk_name(c.name) for c in d.iterdir())
    except OSError:
        return False


def _remove_empty(d: Path) -> bool:
    """Remove *d* when it is empty or holds only OS clutter. True when it went."""
    if not d.is_dir() or d.is_symlink() or not _only_clutter(d):
        return False
    uc.strip_fs_junk(d)
    try:
        d.rmdir()
    except OSError:
        return False
    _drop_sidecar(d)
    return True


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def apply(p: Plan, journal_path, selected=None, on_line=None) -> dict:
    """Carry out *p* (only the moves whose index is in *selected*, when given). Renames only:
    a move that would cross drives fails and nothing is copied. Returns {'moved', 'failed'}."""
    root = Path(p.root)
    ops: list[dict] = []
    moved_dirs: list[tuple[Path, Path]] = []
    touched: set[Path] = set()
    failed: list[str] = []
    moved = 0

    def now(src: Path) -> Path:
        for old, new in moved_dirs:
            if src == old or old in src.parents:
                return new / src.relative_to(old)
        return src

    def mkdirs(d: Path) -> None:
        missing = []
        q = d
        while not q.exists() and q != q.parent:
            missing.append(q)
            q = q.parent
        for q in reversed(missing):
            q.mkdir()
            ops.append({"op": "mkdir", "path": str(q)})

    try:
        for i, m in enumerate(p.moves):
            if selected is not None and i not in selected:
                continue
            src, dst = now(m.src), m.dst
            if not os.path.lexists(src):
                failed.append(f"{m.src.name}: no longer there")
                continue
            if os.path.lexists(dst) and not _same_entry(src, dst):
                failed.append(f"{dst.name}: already exists")
                continue
            try:
                if src in dst.parents:
                    # A game folder that already has its title folder's name goes inside a new
                    # folder of that name: step aside first.
                    aside = src.with_name(src.name + ".organize-move")
                    os.rename(src, aside)
                    ops.append({"op": "move", "src": str(src), "dst": str(aside)})
                    src = aside
                mkdirs(dst.parent)
                _rename(src, dst)
            except OSError as e:
                failed.append(f"{m.src.name}: {e.strerror or e}")
                continue
            ops.append({"op": "move", "src": str(src), "dst": str(dst)})
            if m.is_dir:
                moved_dirs.append((m.src, dst))
            _drop_sidecar(src)
            _drop_sidecar(dst)
            touched.add(src.parent)
            moved += 1
            _say(on_line, f"[ORGANIZE] {src.relative_to(root) if root in src.parents else src} -> "
                          f"{dst.relative_to(root) if root in dst.parents else dst}")
        # Folders the moves left empty (or holding only clutter) go, up to the root.
        for d in sorted(touched, key=lambda q: len(q.parts), reverse=True):
            q = now(d)
            while q != root and root in q.parents and _remove_empty(q):
                ops.append({"op": "rmdir", "path": str(q)})
                q = q.parent
    finally:
        _drop_sidecars_of(ops)
        if ops:
            _write_json(Path(journal_path), {"root": str(root), "ops": ops})
    return {"moved": moved, "failed": failed}


def undo(journal_path, on_line=None) -> dict:
    """Move back everything the journal moved that is still where Organize put it."""
    journal_path = Path(journal_path)
    data = json.loads(journal_path.read_text(encoding="utf-8"))
    back, failed = 0, []
    for op in reversed(data.get("ops", [])):
        kind = op.get("op")
        if kind == "rmdir":
            Path(op["path"]).mkdir(parents=True, exist_ok=True)
        elif kind == "mkdir":
            _remove_empty(Path(op["path"]))
        elif kind == "move":
            src, dst = Path(op["src"]), Path(op["dst"])
            if not os.path.lexists(dst):
                failed.append(f"{dst.name}: no longer there")
                continue
            if os.path.lexists(src) and not _same_entry(src, dst):
                failed.append(f"{src.name}: something else is there now")
                continue
            try:
                src.parent.mkdir(parents=True, exist_ok=True)
                _rename(dst, src)
            except OSError as e:
                failed.append(f"{dst.name}: {e.strerror or e}")
                continue
            _drop_sidecar(dst)
            _drop_sidecar(src)
            back += 1
            _say(on_line, f"[ORGANIZE] back: {dst.name} -> {src}")
    _drop_sidecars_of(data.get("ops", []))
    os.replace(journal_path, journal_path.with_name(journal_path.stem + ".undone.json"))
    return {"moved": back, "failed": failed}


# ── the real readers ─────────────────────────────────────────────────────────
def make_identify(read_image_member: Callable, firmware_of: Callable, passwords=None) -> Callable:
    """identify(path, kind) for scan(), over the app's readers: *read_image_member(image,
    member)* returns a member's bytes out of a .ffpfs/.ffpfsc, *firmware_of(path)* the
    firmware a game's eboot.bin needs ('' when unknown). Only param.json / param.sfo and
    eboot.bin are ever read, never a whole game."""
    import ps4pkg

    def ps4_dict(i) -> dict:
        return {"title": i.title, "title_id": i.title_id, "version": i.version, "role": i.kind,
                "platform": "ps4", "ps4": i}

    def ps5_dict(data, path=None, role="game") -> Optional[dict]:
        ident = uc.ident_from_param_bytes(data) if data else None
        if not ident:
            return None
        fw = ""
        if path is not None and role in ("game", "backport"):
            try:
                fw = firmware_of(path) or ""
            except Exception:
                fw = ""
        return dict(ident, fw=fw, role=role, platform="ps5")

    def identify(path: Path, kind: str) -> Optional[dict]:
        path = Path(path)
        if kind == "folder":
            pj = path / "sce_sys" / "param.json"
            if pj.is_file():
                return ps5_dict(pj.read_bytes(), path)
            sfo = ps4pkg.parse_sfo((path / "sce_sys" / "param.sfo").read_bytes())
            app_ver = str(sfo.get("APP_VER") or "")
            i = ps4pkg.Ps4Identity(title=str(sfo.get("TITLE") or ""), title_id=str(sfo.get("TITLE_ID") or "").upper(),
                                   content_id=str(sfo.get("CONTENT_ID") or ""),
                                   kind=ps4pkg._kind(str(sfo.get("CATEGORY") or ""), 0),
                                   version=app_ver or str(sfo.get("VERSION") or ""), app_ver=app_ver, content_type=0)
            return ps4_dict(i) if i.title_id else None
        if kind in ("ffpfs", "ffpfsc"):
            return ps5_dict(read_image_member(path, "sce_sys/param.json"), path)
        if kind in ("exfat", "ffpkg"):
            from mkpfs.game_metadata import read_game_metadata
            m = read_game_metadata(path)
            tid = (getattr(m, "title_id", "") or "").strip().upper()
            if not re.fullmatch(r"(PPSA|CUSA)\d{5}", tid):
                return None
            return {"title": (getattr(m, "game_title", "") or "").strip(), "title_id": tid,
                    "version": (getattr(m, "version", "") or "").strip(), "role": "game", "platform": "ps5"}
        if kind == "pkg":
            if ps4pkg.is_ps4_package(path):
                return ps4_dict(ps4pkg.read_identity(path))
            import fpkg
            ct = fpkg.inspect_json(path).get("content_type")
            role = "dlc" if ct in (33, 34) else "game"
            with tempfile.TemporaryDirectory(prefix="organize-") as td:
                members = Path(td) / "members.txt"
                members.write_text("sce_sys/param.json\n", encoding="utf-8")
                fpkg.extract_members(path, Path(td) / "out", members, on_line=lambda _l: None)
                pj = Path(td) / "out" / "sce_sys" / "param.json"
                data = pj.read_bytes() if pj.is_file() else None
            if role == "game":
                role = _name_role(path.stem) or role
            return ps5_dict(data, path, role)
        if kind == "archive":
            data = uc.ArchiveExtractor.read_game_param(path, passwords)
            if data:
                return ps5_dict(data)
            info = uc.ArchiveExtractor.ps4_archive_info(path, passwords)
            i = info.get("ident") if info else None
            return ps4_dict(i) if i is not None else None
        return None

    return identify


# ── one source into a library (the "Organize" job output) ───────────────────
def library_folders(out) -> dict:
    """{title id: Ps4Library} for the title folders directly under *out* (first by name wins)."""
    found: dict = {}
    try:
        children = sorted(Path(out).iterdir(), key=lambda q: q.name.lower())
    except OSError:
        return found
    for d in children:
        if d.is_dir() and not is_fs_junk_name(d.name):
            m = _TAG.search(d.name)
            if m and m.group(1).upper() not in found:
                found[m.group(1).upper()] = _known_folder(d, m.group(1).upper())
    return found


def _free_name(dst_dir: Path, name: str) -> str:
    stem, ext = os.path.splitext(name)
    n = 2
    while (dst_dir / f"{stem} ({n}){ext}").exists():
        n += 1
    return f"{stem} ({n}){ext}"


_BAR = re.compile(r"^\[#+\]\s*(\d+)%")


def organize_into(src, out, identify: Callable, *, mode: str = "keep", if_exists: str = "skip", on_line=None) -> int:
    """Copy (mode keep) or move (mode move) what *src* holds into the library *out*, in its own
    format and under its library name. A title folder already in *out* is joined and its
    version tag raised when the source is newer. The conflict rule per item: skip (default;
    'ask' acts as skip), overwrite (the old one goes once the new one is complete), keep (a
    ' (2)' suffix). 0 when everything readable is in the library."""
    import copy_job
    src, out = Path(src), Path(out)
    if src.is_dir() and (out == src or src in out.parents):
        _say(on_line, f"[ERROR] the library {out} lies inside the source {src}; choose another output folder")
        return 1
    _say(on_line, "[JOB] copy")
    _say(on_line, "[PHASE] Writing Final Image")
    if src.is_dir() and _game_folder_kind(src):
        entries = [Entry(src, "folder")]
    elif src.is_file():
        kind = FILE_KINDS.get(src.suffix.lower())
        entries = [Entry(src, kind)] if kind else []
    else:
        entries, _ = walk(src)
    entries = [e for e in entries if e.kind != "archive"]
    for e in entries:
        try:
            got = identify(e.path, e.kind)
        except Exception as ex:
            got, e.note = None, str(ex)
        if got and got.get("title_id"):
            e.title, e.title_id = str(got.get("title") or ""), str(got["title_id"]).upper()
            e.version, e.fw, e.ps4 = str(got.get("version") or ""), str(got.get("fw") or ""), got.get("ps4")
            e.role = got.get("role") or "game"
    ok = [e for e in entries if e.known]
    for e in entries:
        if not e.known:
            _say(on_line, f"[WARN] {e.path.name}: {e.note or 'no game metadata found'}; left where it is")
    if not ok:
        _say(on_line, "[ERROR] nothing with readable game metadata found")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    known = library_folders(out)
    placed = uc.library_layout([_lib_item(e) for e in ok], {t: k for t, k in known.items() if k})
    # A title folder already there takes its new name (a newer version) by a rename.
    renamed: dict[str, str] = {}
    for e, (_k, folder, _s, _n) in zip(ok, placed):
        lib = known.get(e.title_id)
        if not lib or lib.folder == folder or folder in renamed:
            continue
        if (out / folder).exists():
            _say(on_line, f"[WARN] {folder} already exists; the files go into {lib.folder}, its name stays")
            renamed[folder] = lib.folder
            continue
        try:
            os.rename(out / lib.folder, out / folder)
            _drop_sidecar(out / lib.folder)
            renamed[folder] = folder
            _say(on_line, f"[ORGANIZE] renamed {lib.folder} -> {folder}")
        except OSError as ex:
            _say(on_line, f"[WARN] could not rename {lib.folder} ({ex}); the files go into it as it is")
            renamed[folder] = lib.folder
    sizes = {e.path: max(1, _size_of(e.path)) for e in ok}
    total = sum(sizes.values())
    done = 0
    last = [-1]

    def bar(pct_of_item: int, size: int, name: str) -> None:
        pct = int((done + size * pct_of_item / 100) * 100 / total)
        if pct > last[0]:
            last[0] = pct
            _say(on_line, f"[{'#' * max(1, pct // 5)}] {pct}% copy {name}")

    def relay(size: int, name: str):
        def sink(line: str) -> None:
            m = _BAR.match(line)
            if m:
                bar(int(m.group(1)), size, name)
            elif not line.startswith(("[JOB]", "[PHASE]", "[SUCCESS]")):
                _say(on_line, line)
        return sink

    rc_all = 0
    folders: list[Path] = []
    cmode = copy_job.MOVE if mode == "move" else copy_job.KEEP
    for e, (_k, folder, sub, name) in zip(ok, placed):
        folder = renamed.get(folder, folder)
        dst_dir = out / folder / sub if sub else out / folder
        if out / folder not in folders:
            folders.append(out / folder)
        size = sizes[e.path]
        target = dst_dir / name
        run = (lambda nm: copy_job.run_copy_tree(e.path, dst_dir / nm, mode=cmode, on_line=relay(size, name))) \
            if e.kind == "folder" else \
            (lambda nm: copy_job.run_copy(e.path, dst_dir, dst_name=nm, mode=cmode, on_line=relay(size, name)))
        if target.exists() and not _same_entry(target, e.path):
            if if_exists == "keep":
                name = _free_name(dst_dir, name)
            elif if_exists == "overwrite":
                part = name + ".copy-old"
                try:
                    os.rename(target, dst_dir / part)
                except OSError as ex:
                    _say(on_line, f"[ERROR] could not set {name} aside: {ex}")
                    rc_all = 1
                    done += size
                    continue
                rc = run(name)
                if rc == 0:
                    old = dst_dir / part
                    shutil.rmtree(old) if old.is_dir() else old.unlink()
                    _say(on_line, f"[OK] Organized: {target} (replaced)")
                else:
                    os.rename(dst_dir / part, target)
                    rc_all = 1
                done += size
                continue
            else:
                note = " (rule Ask: a running job does not stop for each item)" if if_exists == "ask" else ""
                _say(on_line, f"[INFO] already there, skipped{note}: {folder}/{name}")
                done += size
                continue
        rc = run(name)
        if rc in (0, 2):
            _say(on_line, f"[OK] Organized: {dst_dir / name}")
        else:
            rc_all = 1
        done += size
    bar(100, 0, "")
    for f in folders:
        uc.strip_fs_junk(f)
        _drop_sidecar(f)
    return rc_all


def _size_of(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for dirpath, _dirs, files in os.walk(p):
        for n in files:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                pass
    return total
