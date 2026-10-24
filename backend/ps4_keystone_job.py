"""PS4 game fixes in place; a new keystone changes the digests, so every update gets rebound"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import copy_job
import fpkg
import ps4pkg

_BAR = re.compile(r"^\[#+\]\s*(\d+)%")


def _say(on_line, text):
    (on_line or (lambda t: print(t, flush=True)))(text)


def plan_update(update, base_sfo: dict) -> dict:
    """save folder fixes for an update; a field the sfo lacks can't be added in place"""
    out = {}
    sfo = ps4pkg.read_sfo(update)
    own, want = str(sfo.get("INSTALL_DIR_SAVEDATA") or ""), ps4pkg.save_folder(base_sfo)
    if own and want and own != want:
        out["save_folder"] = (own, want)
    elif not own and want:
        tid = str(sfo.get("TITLE_ID") or "")
        if tid and tid != want:
            out["save_folder_note"] = (tid, want)
    return out


def region_changed(update) -> bool:
    """converted from another region (sce_sys/region.changed): its language may differ"""
    try:
        return any(e.get("path") == "sce_sys/region.changed" for e in fpkg.list_inner(Path(update))["entries"])
    except Exception:
        return False


def check(base, updates) -> list[str]:
    """why these packages can't share one keystone job, empty when they can"""
    base = Path(base)
    errors = []
    try:
        ident = ps4pkg.read_identity(base)
    except (ps4pkg.Ps4PackageError, OSError) as e:
        return [f"{base.name}: {e}"]
    if ident.kind != "game":
        errors.append(f"{base.name} is no game package (a {ident.kind}); the keystone goes into the game")
    for u in updates:
        u = Path(u)
        try:
            ui = ps4pkg.read_identity(u)
            slots = ps4pkg.general_digests(u)
        except (ps4pkg.Ps4PackageError, OSError) as e:
            errors.append(f"{u.name}: {e}")
            continue
        if ui.content_id != ident.content_id:
            errors.append(f"{u.name} belongs to {ui.content_id}, not to {ident.content_id}")
        elif ps4pkg.BASE_CONTENT_SLOT not in slots or ps4pkg.BASE_GAME_SLOT not in slots:
            errors.append(f"{u.name} records no base package (not an update)")
    return errors


def run(base, keystone_text: str | None, updates, undo_root, on_line=None, fix_version: bool = False) -> int:
    """on an error the package being changed is left as it was"""
    base = Path(base)
    updates = [Path(u) for u in updates]
    _say(on_line, "[JOB] copy")
    _say(on_line, "[PHASE] Writing Final Image")
    keystone = None
    if (keystone_text or "").strip():
        keystone, why = ps4pkg.keystone_from_text(keystone_text)
        if keystone is None:
            _say(on_line, f"[ERROR] keystone: {why}")
            return 1
    if keystone is None and not fix_version and not updates:
        _say(on_line, "[ERROR] nothing to do: no keystone, no version fix, no update")
        return 1
    errors = check(base, updates)
    if errors:
        for e in errors:
            _say(on_line, f"[ERROR] {e}")
        return 1
    before = ps4pkg.general_digests(base)
    base_sfo = ps4pkg.read_sfo(base)
    vp = ps4pkg.version_problem(base, base_sfo) if fix_version else None
    if fix_version and vp is None:
        # already right, e.g. a retry after a later step failed: the other steps still run
        _say(on_line, f"[INFO] {base.name}: the version needs no fix")
        if keystone is None and not updates:
            _say(on_line, "[OK] PS4 PKG Tools: nothing left to do")
            return 0
    stamp = time.strftime("%Y-%m-%d %H%M%S")
    folder, n = Path(undo_root) / stamp, 2
    while folder.exists():                 # two runs in one second keep their own journals
        folder, n = Path(undo_root) / f"{stamp}-{n}", n + 1
    folder.mkdir(parents=True)
    journal = {"base": str(base), "steps": []}
    n_undo = [0]

    def undo_file():
        n_undo[0] += 1
        return folder / f"step-{n_undo[0]}.undo"

    def save_journal():
        (folder / "journal.json").write_text(json.dumps(journal, indent=1), encoding="utf-8")

    t0 = time.monotonic()
    size = max(1, base.stat().st_size)
    last = [-1]

    def relay(line: str) -> None:
        m = _BAR.match(line)
        if m:
            pct = min(99, int(m.group(1)))
            if pct > last[0]:
                last[0] = pct
                _say(on_line, copy_job.progress_line(pct, size * pct // 100, size, t0, "keystone"))
        elif line.startswith(("[keystone]", "[rebind]", "[sfo]", "[ok]", "[ERROR]", "[error]", "[usage]")):
            _say(on_line, line if not line.startswith("[error]") else "[ERROR]" + line[7:])

    changed: set[str] = set()          # packages an earlier step of this run changed

    def step(pkg: Path, rc_of, done: str, failed: str) -> bool:
        uf = undo_file()
        # in the journal before the tool starts: a run stopped half-way can still be undone
        entry = {"package": str(pkg), "undo": str(uf)}
        journal["steps"].append(entry)
        save_journal()
        if rc_of(uf) != 0:
            if not uf.is_file():                  # rolled back by the tool, or never opened
                journal["steps"].remove(entry)
                save_journal()
            _say(on_line, f"[ERROR] {failed}; " + ("the changes this run made to it stay, Undo last "
                                                   "change puts them back" if str(pkg) in changed or uf.is_file()
                                                   else "the package is unchanged"))
            return False
        if uf.is_file():
            changed.add(str(pkg))
        else:
            journal["steps"].remove(entry)
            save_journal()
        _say(on_line, f"[PS4] {done}")
        return True

    if vp is not None:
        new = {"APP_VER": vp["version"], "VERSION": vp["app_ver"]}
        if not step(base, lambda uf: fpkg.ps4_sfo(base, new, undo=uf, on_line=relay),
                    f"{base.name}: app version {vp['version']}, master version {vp['app_ver']}",
                    f"the version of {base.name} could not be set"):
            return 1
    if keystone is not None:
        ks_file = folder / "keystone"
        ks_file.write_bytes(keystone)
        if not step(base, lambda uf: fpkg.ps4_keystone(base, ks_file, undo=uf, on_line=relay),
                    f"keystone written into {base.name}", f"the keystone could not be written into {base.name}"):
            return 1
    rc_all = 0
    for u in updates:
        old = ps4pkg.general_digests(u)
        if (old.get(ps4pkg.BASE_CONTENT_SLOT) != before.get(1) and old.get(ps4pkg.BASE_CONTENT_SLOT) != ps4pkg.general_digests(base).get(1)):
            _say(on_line, f"[WARN] {u.name} was made for another build of this game; it is bound to this one anyway")
        plan = plan_update(u, base_sfo)
        note = plan.get("save_folder_note")
        if note:
            _say(on_line, f"[WARN] {u.name} saves under {note[0]}, the game under {note[1]}; its param.sfo has "
                          f"no save folder field, so this cannot be changed in place")
        sf = plan.get("save_folder")
        if sf and not step(u, lambda uf: fpkg.ps4_sfo(u, {"INSTALL_DIR_SAVEDATA": sf[1]}, undo=uf, on_line=relay),
                           f"{u.name}: saves go to {sf[1]} like the game's (was {sf[0]})",
                           f"the save folder of {u.name} could not be set"):
            rc_all = 1
            continue
        if not step(u, lambda uf: fpkg.ps4_rebind(u, base, undo=uf, on_line=relay),
                    f"{u.name} bound to {base.name}", f"{u.name} could not be bound to the changed package"):
            rc_all = 1
    _say(on_line, copy_job.progress_line(100, size, size, t0, "keystone"))
    if journal["steps"]:
        _say(on_line, f"[INFO] undo data: {folder}")
    _say(on_line, f"[OK] PS4 PKG Tools: {base}" if rc_all == 0 else f"[WARN] PS4 PKG Tools: {base}, finished with errors")
    return rc_all


def journals(undo_root) -> list[Path]:
    """runs not undone yet, newest first"""
    out = []
    def order(q):
        # by journal mtime, so a clock set back keeps the order; then the -2 suffix
        m = re.match(r"^(.*?)(?:-(\d+))?$", q.parent.name)
        try:
            t = q.stat().st_mtime_ns
        except OSError:
            t = 0
        return (t, m.group(1), int(m.group(2) or 1))
    for j in sorted(Path(undo_root).glob("*/journal.json"), key=order, reverse=True):
        try:
            steps = json.loads(j.read_text(encoding="utf-8")).get("steps") or []
        except (OSError, ValueError):
            continue
        if any(Path(st.get("undo", "")).is_file() for st in steps):
            out.append(j)
    return out


def last_journal(undo_root) -> Path | None:
    found = journals(undo_root)
    return found[0] if found else None


def describe(journal_path) -> dict:
    """for the question before an undo: game package, when, how many packages"""
    j = json.loads(Path(journal_path).read_text(encoding="utf-8"))
    pkgs = {st.get("package") for st in j.get("steps") or []}
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.strptime(Path(journal_path).parent.name[:17], "%Y-%m-%d %H%M%S"))
    except ValueError:
        when = ""
    return {"base": str(j.get("base") or ""), "when": when, "packages": len(pkgs)}


def undo(journal_path, on_line=None) -> int:
    """newest first; a package that can't be restored keeps its steps for the next Undo"""
    journal_path = Path(journal_path)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    left, blocked = [], set()
    for step in reversed(journal["steps"]):
        pkg, u = Path(step["package"]), Path(step["undo"])
        if not u.is_file():
            continue                     # the run stopped before this step changed anything
        if str(pkg) in blocked:
            left.append(step)            # a newer step of this package is still in place
            continue
        if not pkg.is_file():
            _say(on_line, f"[ERROR] {pkg.name} is not there (a drive unplugged, or moved?); "
                          f"its changes stay recorded for the next Undo")
            blocked.add(str(pkg)); left.append(step)
            continue
        rc = fpkg.ps4_keystone_restore(pkg, u, on_line=lambda line: _say(on_line, line) if line.startswith(("[ok]", "[ERROR]")) else None)
        if rc == 0:
            _say(on_line, f"[PS4] restored {pkg.name}")
        else:
            _say(on_line, f"[ERROR] could not restore {pkg.name}")
            blocked.add(str(pkg)); left.append(step)
    if not left:
        journal_path.rename(journal_path.with_name("journal.undone.json"))
        _say(on_line, "[OK] PS4 PKG Tools: undone")
        return 0
    journal["steps"] = list(reversed(left))
    journal_path.write_text(json.dumps(journal, indent=1), encoding="utf-8")
    return 1
