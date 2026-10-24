from __future__ import annotations

import filecmp
import json
import os
import re
import time
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
# half-written files of a running or killed copy/sort: never touched
PARTIAL_SUFFIXES = (".copy-tmp", ".ps4sort-part")
_TAG = re.compile(r"\[((?:PPSA|CUSA)\d{5})\]", re.I)
_NAME_ROLE = ((re.compile(r"\bback-?port(?:ed)?\b", re.I), "backport"),
              (re.compile(r"\b(?:update|patch)\b", re.I), "update"))


@dataclass
class Entry:
    """folder, image, package or archive set; parts lists the volumes"""
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
    """(entries, companions), unread; links not followed; an entry-less folder is one companion"""
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


def role_folder(path, root) -> str:
    """nearest role-naming folder below root; backports keep it so firmwares never collide"""
    path, root = Path(path), Path(root)
    for d in _below(path, root):
        if _name_role(d.name):
            return d.relative_to(root).as_posix()
    return ""


def name_role(path, root=None) -> str:
    """role from the file name, else from the nearest folder that says one"""
    path = Path(path)
    role = _name_role(path.stem)
    if not role and root is not None:
        for d in _below(path, Path(root)):
            role = _name_role(d.name)
            if role:
                break
    return role


def scan(root, identify: Callable, on_progress=None) -> tuple[list[Entry], list[Path]]:
    """identify(path, kind) gives a dict or None; if it raises, the message becomes the note"""
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
                role = name_role(e.path, root) or role     # the user's own naming, until the package says more
            e.role = role
        elif not e.note:
            e.note = ("the game inside cannot be read before unpacking" if e.kind == "archive"
                      else "no game metadata found")
        if on_progress:
            on_progress(n, len(entries), e.path.name)
    return entries, companions


def _below(p: Path, root: Path) -> list[Path]:
    """folders strictly between root and p, nearest first"""
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
    """a title folder already there, if d's name carries tid's tag"""
    if not d or not _TAG.search(d.name) or _TAG.search(d.name).group(1).upper() != tid:
        return None
    return uc.Ps4Library(d.name, uc._lib_version_tag(d.name), (d / uc.PS4_DLC_PACK).is_dir())


def _game_name(tid: str):
    """from the title list on disk, never downloads"""
    try:
        import title_db
        return title_db.lookup(tid, Path(uc.APP_DIR) / "titles", online=False)
    except Exception:
        return None


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
                                                                   {tid: known} if known else None,
                                                                   title_of=_game_name)}
        title_dir = title_dirs[tid] = root / next(iter(placed.values()))[0]
        label = labels[tid] = next((e.title for e in group if e.role == "game" and e.title), group[0].title) or tid
        # str(): Windows compares paths without case, and a new spelling is a rename
        if home is not None and str(home) != str(title_dir):
            if title_dir.exists() and not _same_entry(home, title_dir):
                home = None                                   # the name is taken: no rename, single moves
            else:
                moves.append(Move(home, title_dir, tid, label, is_dir=True))

        def now(p: Path, home=home, title_dir=title_dir) -> Path:
            """where p is once the home folder has its new name"""
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
            if str(now(e.path)) == str(dst):
                in_place += 1
                continue
            moves.append(Move(e.path, dst, tid, label, is_dir=e.kind == "folder"))
        # companions in a one-game folder go along; the home itself moves as a whole
        for top in tops:
            if top == home:
                continue
            for c in companions:
                if top == c.parent or top in c.parents:
                    moves.append(Move(c, title_dir / c.relative_to(top), tid, label, is_dir=c.is_dir()))
    # in a shared folder a companion goes along only if its name ties it to one game
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
    """colliding names both stay, as does a move onto something that stays; case-only is fine"""
    homes = {m.src: m.dst for m in p.moves if m.is_dir}

    def now(target: Path) -> Path:
        """what sits at target today, mapped back through renamed folders"""
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


def _drop_sidecar(p: Path) -> None:
    try:
        sc = p.parent / ("._" + p.name)
        if sc.is_file():
            sc.unlink()
    except OSError:
        pass


def _drop_sidecars_of(ops: list) -> None:
    """on exFAT macOS sprinkles ._ sidecars on new folders and moved files; sweep them up"""
    seen: set = set()
    for op in ops:
        for key in ("src", "dst", "path"):
            if key in op:
                p = Path(op[key])
                for q in (p, p.parent):
                    if q not in seen:
                        seen.add(q)
                        _drop_sidecar(q)


def _rename(src: Path, dst: Path, step: Optional[Callable[[Path, Path], None]] = None) -> None:
    """case-only renames on case-blind drives go via a temp name; step() feeds the journal"""
    step = step or (lambda _a, _b: None)
    if str(src) != str(dst) and str(src).lower() == str(dst).lower():
        tmp = src.with_name(src.name + ".organize-case")
        os.rename(src, tmp)
        step(src, tmp)
        try:
            os.rename(tmp, dst)
        except OSError:
            try:
                os.rename(tmp, src)
                step(tmp, src)
            except OSError:
                pass                    # it stays at tmp; the journal says so
            raise
        step(tmp, dst)
    else:
        os.rename(src, dst)
        step(src, dst)


def _only_clutter(d: Path) -> bool:
    try:
        return all(is_fs_junk_name(c.name) for c in d.iterdir())
    except OSError:
        return False


def _remove_empty(d: Path) -> bool:
    """OS clutter alone counts as empty"""
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
    """renames only: a cross-drive move fails, nothing is ever copied"""
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

    def record(op: dict) -> None:
        """journal every step right away, so a killed run leaves an accurate one"""
        ops.append(op)
        _write_json(Path(journal_path), {"root": str(root), "ops": ops})

    def moved_step(a: Path, b: Path) -> None:
        record({"op": "move", "src": str(a), "dst": str(b)})

    def mkdirs(d: Path) -> None:
        missing = []
        q = d
        while not q.exists() and q != q.parent:
            missing.append(q)
            q = q.parent
        for q in reversed(missing):
            q.mkdir()
            record({"op": "mkdir", "path": str(q)})

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
                    # src would land inside a new folder of its own name: step aside first
                    aside = src.with_name(src.name + ".organize-move")
                    os.rename(src, aside)
                    moved_step(src, aside)
                    src = aside
                mkdirs(dst.parent)
                _rename(src, dst, moved_step)
            except OSError as e:
                failed.append(f"{m.src.name}: {e.strerror or e}")
                continue
            if m.is_dir:
                moved_dirs.append((m.src, dst))
            _drop_sidecar(src)
            _drop_sidecar(dst)
            touched.add(src.parent)
            moved += 1
            _say(on_line, f"[ORGANIZE] {src.relative_to(root) if root in src.parents else src} -> "
                          f"{dst.relative_to(root) if root in dst.parents else dst}")
        for d in sorted(touched, key=lambda q: len(q.parts), reverse=True):
            q = now(d)
            while q != root and root in q.parents and _remove_empty(q):
                record({"op": "rmdir", "path": str(q)})
                q = q.parent
    finally:
        _drop_sidecars_of(ops)
        if ops:
            _write_json(Path(journal_path), {"root": str(root), "ops": ops})
    return {"moved": moved, "failed": failed}


def _journal_order(j: Path):
    """mtime first, then <time>.json before <time>-2.json (two runs in one second)"""
    m = re.match(r"^(.*?)(?:-(\d+))?$", j.stem)
    try:
        t = j.stat().st_mtime_ns
    except OSError:
        t = 0
    return (t, m.group(1), int(m.group(2) or 1))


def journals(folder, legacy=None) -> list[Path]:
    """Apply journals not undone yet, newest first; legacy is the old single file, counted oldest"""
    found = sorted((j for j in Path(folder).glob("*.json") if not j.name.endswith(".undone.json")),
                   key=_journal_order, reverse=True) if Path(folder).is_dir() else []
    if legacy and Path(legacy).is_file():
        found.append(Path(legacy))
    return found


def new_journal_path(folder) -> Path:
    Path(folder).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H%M%S")
    j, n = Path(folder) / f"{stamp}.json", 2
    while j.exists() or j.with_name(j.stem + ".undone.json").exists():
        j, n = Path(folder) / f"{stamp}-{n}.json", n + 1
    return j


def describe(journal_path) -> dict:
    """for the undo prompt: library folder, item count, when"""
    data = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    moves = sum(1 for op in data.get("ops", []) if op.get("op") == "move")
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(Path(journal_path).stat().st_mtime))
    except OSError:
        when = ""
    return {"root": str(data.get("root") or ""), "moves": moves, "when": when}


def undo(journal_path, on_line=None) -> dict:
    """only moves back what is still where Organize put it"""
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


def make_identify(read_image_member: Callable, firmware_of: Callable, passwords=None) -> Callable:
    """reads only param.json/param.sfo and eboot.bin, never a whole game"""
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
            i = ps4pkg.identity_from_sfo(sfo, [path.name, path.parent.name])
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


def library_folders(out) -> dict:
    """{title id: Ps4Library} for title folders right under out; first by name wins"""
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


def _free_name(dst_dir: Path, name: str, taken=(), key=str, is_dir: bool = False) -> str:
    """'<stem> (n)<ext>' free on disk and not in taken"""
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    n = 2
    while os.path.lexists(dst_dir / f"{stem} ({n}){ext}") or key(dst_dir / f"{stem} ({n}){ext}") in taken:
        n += 1
    return f"{stem} ({n}){ext}"


def case_blind(d: Path) -> bool:
    """probe-file test; unknown counts as case-blind, the careful answer"""
    try:
        fd, probe = tempfile.mkstemp(prefix=".CaseProbe-", dir=str(d))
        os.close(fd)
    except OSError:
        return True
    probe = Path(probe)
    try:
        return os.path.lexists(probe.with_name(probe.name.swapcase()))
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
        _drop_sidecar(probe)


_BAR = re.compile(r"^\[#+\]\s*(\d+)%")


_SET_ASIDE = re.compile(r"^(?P<base>.+)\.copy-old(?: \(\d+\))?$")


def restore_set_aside(folder: Path, on_line=None) -> int:
    """<name>.copy-old from a cut-short Overwrite gets its name back: may be the only intact copy"""
    back = 0
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return 0
    for p in entries:
        m = _SET_ASIDE.match(p.name)
        if not m:
            continue
        base = p.with_name(m.group("base"))
        if os.path.lexists(base):
            continue
        try:
            os.rename(p, base)
            back += 1
            _say(on_line, f"[INFO] {base.name}: took its name back from an Overwrite that was cut short")
        except OSError:
            pass
    return back


def organize_into(src, out, identify: Callable, *, mode: str = "keep", if_exists: str = "skip", on_line=None) -> int:
    import copy_job
    src, out = Path(src), Path(out)
    if src.is_dir() and (out == src or src in out.parents):
        _say(on_line, f"[ERROR] the library {out} lies inside the source {src}; choose another output folder")
        return 1
    _say(on_line, "[JOB] copy")
    _say(on_line, "[PHASE] Writing Final Image")
    companions: list[Path] = []
    if src.is_dir() and _game_folder_kind(src):
        entries = [Entry(src, "folder")]
    elif src.is_file():
        kind = FILE_KINDS.get(src.suffix.lower())
        entries = [Entry(src, kind)] if kind else []
    else:
        entries, companions = walk(src)
    # an archive inside the source is no game of its own here: it comes along like a companion
    companions += [q for e in entries if e.kind == "archive" for q in (e.parts or [e.path])]
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
            if e.kind == "pkg" and e.role == "game" and not e.title_id.startswith("CUSA"):
                e.role = name_role(e.path, src if src.is_dir() else None) or e.role   # as scan() does
    ok = [e for e in entries if e.known]
    unread = [e for e in entries if not e.known]
    single = len({e.title_id for e in ok}) == 1 and src.is_dir()
    kept: list[str] = []                     # what stays in the source, for the [KEPT] line
    for e in unread:
        if single:
            _say(on_line, f"[WARN] {e.path.name}: {e.note or 'no game metadata found'}; "
                          "it goes into the title folder as it is")
            companions.append(e.path)
        else:
            _say(on_line, f"[WARN] {e.path.name}: {e.note or 'no game metadata found'}; left where it is")
            kept.append(e.path.name)
    if not ok:
        _say(on_line, "[ERROR] nothing with readable game metadata found")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    # names that differ only in case are one name on such a drive
    key = (lambda p: str(p).casefold()) if case_blind(out) else str
    known = library_folders(out)
    sizes = {e.path: max(1, _size_of(e.path)) for e in ok}
    # game and biggest first: on a name clash the main one keeps the name
    ok.sort(key=lambda e: (e.role != "game", -sizes[e.path]))
    # library_layout groups by title, so match by source path, not order
    _by_key = {Path(k): (k, f, sb, n) for k, f, sb, n in
               uc.library_layout([_lib_item(e) for e in ok], {t: k for t, k in known.items() if k},
                                 title_of=_game_name)}
    placed = [_by_key[e.path] for e in ok]
    for _folder in {(f, sb) for _k, f, sb, _n in placed}:
        for _d in {out / _folder[0], out / _folder[0] / _folder[1]} if _folder[1] else {out / _folder[0]}:
            if _d.is_dir():
                restore_set_aside(_d, on_line)
    # an existing title folder gets its new name (newer version) by rename
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
    total = sum(sizes.values())
    done = 0
    last = [-1]
    t0 = time.monotonic()

    def bar(pct_of_item: int, size: int, name: str) -> None:
        moved = int(done + size * pct_of_item / 100)
        pct = int(moved * 100 / total)
        if pct > last[0]:
            last[0] = pct
            _say(on_line, copy_job.progress_line(pct, moved, total, t0, f"copy {name}"))

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
    written: set[str] = set()                # key() of every target this run wrote
    cmode = copy_job.MOVE if mode == "move" else copy_job.KEEP
    for e, (_k, folder, sub, name) in zip(ok, placed):
        folder = renamed.get(folder, folder)
        if e.role == "backport" and not sub and src.is_dir():
            sub = Path(role_folder(e.path, src)).name
        dst_dir = out / folder / sub if sub else out / folder
        if out / folder not in folders:
            folders.append(out / folder)
        size = sizes[e.path]
        target = dst_dir / name
        if key(target) in written:
            # another source of this run already took the name: never skip one of them
            name = _free_name(dst_dir, name, written, key, is_dir=e.kind == "folder")
            _say(on_line, f"[WARN] {e.path.name} gets the same name as another item of this source; "
                          f"it goes in as {name}")
            target = dst_dir / name
        run = (lambda nm: copy_job.run_copy_tree(e.path, dst_dir / nm, mode=cmode, on_line=relay(size, name))) \
            if e.kind == "folder" else \
            (lambda nm: copy_job.run_copy(e.path, dst_dir, dst_name=nm, mode=cmode, on_line=relay(size, name)))
        if target.exists() and not _same_entry(target, e.path):
            if if_exists == "keep":
                name = _free_name(dst_dir, name, written, key, is_dir=e.kind == "folder")
                target = dst_dir / name
            elif if_exists == "overwrite":
                # a free name: a leftover of an interrupted run may be the only intact copy
                part = name + ".copy-old"
                if os.path.lexists(dst_dir / part):
                    part = _free_name(dst_dir, part)
                try:
                    os.rename(target, dst_dir / part)
                except OSError as ex:
                    _say(on_line, f"[ERROR] could not set {name} aside: {ex}")
                    rc_all = 1
                    done += size
                    continue
                written.add(key(target))
                rc = run(name)
                if rc == 0:
                    old = dst_dir / part
                    copy_job.rmtree_writable(old) if old.is_dir() else old.unlink()
                    _say(on_line, f"[OK] Organized: {target} (replaced)")
                else:
                    os.rename(dst_dir / part, target)
                    rc_all = 1
                done += size
                continue
            else:
                note = " (rule Ask: a running job does not stop for each item)" if if_exists == "ask" else ""
                _say(on_line, f"[INFO] already there, skipped{note}: {Path(folder) / name}")
                kept.append(e.path.name)
                done += size
                continue
        written.add(key(target))
        rc = run(name)
        if rc in (0, 2):
            _say(on_line, f"[OK] Organized: {dst_dir / name}")
        else:
            rc_all = 1
        done += size
    # extras follow a single game into its folder; with several titles they stay put
    if companions and single and folders:
        home = folders[0]
        for c in companions:
            try:
                dst = home / c.relative_to(src)
            except ValueError:
                continue
            if os.path.lexists(dst):
                if c.is_file() and dst.is_file() and filecmp.cmp(c, dst, shallow=False):
                    _say(on_line, f"[INFO] already there, skipped: {dst.relative_to(out)}")
                    continue
                dst = dst.parent / _free_name(dst.parent, dst.name)     # both stay
                _say(on_line, f"[WARN] {c.name}: another item of that name is there; it comes along as {dst.name}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            rc = (copy_job.run_copy_tree(c, dst, mode=cmode, on_line=lambda _l: None) if c.is_dir() else
                  _bring_file(c, dst, move=cmode == copy_job.MOVE))
            if rc in (0, 2):
                _say(on_line, f"[OK] Came along: {dst.relative_to(out)}")
            else:
                # the job fails, so the app keeps the unpack and nothing of the release is lost
                _say(on_line, f"[ERROR] could not bring {c.name} along; it stays in the source")
                kept.append(c.name)
                rc_all = 1
    elif companions:
        _say(on_line, f"[INFO] {len(companions)} other item(s) stay in the source: they belong to no one game")
        kept.extend(c.name for c in companions)
    bar(100, 0, "")
    for f in folders:
        uc.strip_fs_junk(f)
        _drop_sidecar(f)
    if kept:
        _say(on_line, copy_job.kept_line(kept))
    return rc_all or (copy_job.KEPT if kept else 0)


def _bring_file(src: Path, dst: Path, *, move: bool) -> int:
    """rename on one drive, else copy to a temp name and swap it in; move drops the source after"""
    if move:
        try:
            os.rename(src, dst)
            return 0
        except OSError:
            pass
    part = None
    try:
        fd, part = tempfile.mkstemp(prefix=".copy-", suffix=".tmp", dir=dst.parent)
        os.close(fd)
        shutil.copy2(src, part)
        os.replace(part, dst)
        part = None
        if move:
            src.unlink()
        return 0
    except OSError:
        if part:
            try:
                os.unlink(part)
            except OSError:
                pass
        return 1


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
