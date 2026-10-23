"""Put the original keystone into a PS4 game package and bind its updates to the changed package.

Saves hold the keystone of the game that wrote them; a package built with another one (the
default from the all-zero passcode) shows saves of the retail game as corrupted. The tool writes
the keystone in place (ps4-keystone). That changes the package's content and game digests, which
every update records as its base, so each update is rebound (ps4-rebind). Every overwritten range
is kept in an undo folder with a journal; undo() puts them back, updates first, then the game.
"""
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


def check(base, updates) -> list[str]:
    """Why these packages cannot go into one keystone job (empty when they can)."""
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


def run(base, keystone_text: str, updates, undo_root, on_line=None) -> int:
    """0 when the keystone is in *base* and every update points at it, 1 on an error (the
    package being changed is left as it was)."""
    base = Path(base)
    updates = [Path(u) for u in updates]
    _say(on_line, "[JOB] copy")
    _say(on_line, "[PHASE] Writing Final Image")
    keystone, why = ps4pkg.keystone_from_text(keystone_text)
    if keystone is None:
        _say(on_line, f"[ERROR] keystone: {why}")
        return 1
    errors = check(base, updates)
    if errors:
        for e in errors:
            _say(on_line, f"[ERROR] {e}")
        return 1
    before = ps4pkg.general_digests(base)
    folder = Path(undo_root) / time.strftime("%Y-%m-%d %H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    ks_file = folder / "keystone"
    ks_file.write_bytes(keystone)
    journal = {"base": str(base), "steps": []}

    def save_journal():
        (folder / "journal.json").write_text(json.dumps(journal, indent=1), encoding="utf-8")
        (Path(undo_root) / "last.json").write_text(json.dumps({"journal": str(folder / "journal.json")}),
                                                     encoding="utf-8")

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
        elif line.startswith(("[keystone]", "[rebind]", "[ok]", "[ERROR]", "[error]", "[usage]")):
            _say(on_line, line if not line.startswith("[error]") else "[ERROR]" + line[7:])

    undo = folder / "base.undo"
    rc = fpkg.ps4_keystone(base, ks_file, undo=undo, on_line=relay)
    if rc != 0:
        _say(on_line, f"[ERROR] the keystone could not be written into {base.name}; the package is unchanged")
        return 1
    if undo.is_file():
        journal["steps"].append({"package": str(base), "undo": str(undo)})
        save_journal()
    _say(on_line, f"[PS4] keystone written into {base.name}")
    rc_all = 0
    for i, u in enumerate(updates, 1):
        old = ps4pkg.general_digests(u)
        if (old.get(ps4pkg.BASE_CONTENT_SLOT) != before.get(1) and old.get(ps4pkg.BASE_CONTENT_SLOT) != ps4pkg.general_digests(base).get(1)):
            _say(on_line, f"[WARN] {u.name} was made for another build of this game; it is bound to this one anyway")
        uu = folder / f"update-{i}.undo"
        rc = fpkg.ps4_rebind(u, base, undo=uu, on_line=relay)
        if rc != 0:
            _say(on_line, f"[ERROR] {u.name} could not be bound to the new package; it is unchanged")
            rc_all = 1
            continue
        if uu.is_file():
            journal["steps"].append({"package": str(u), "undo": str(uu)})
            save_journal()
        _say(on_line, f"[PS4] {u.name} bound to {base.name}")
    _say(on_line, copy_job.progress_line(100, size, size, t0, "keystone"))
    if journal["steps"]:
        _say(on_line, f"[INFO] undo data: {folder}")
    _say(on_line, f"[OK] PS4 keystone: {base}")
    return rc_all


def last_journal(undo_root) -> Path | None:
    """The journal of the last run that changed something, if it is still there."""
    try:
        p = Path(json.loads((Path(undo_root) / "last.json").read_text(encoding="utf-8"))["journal"])
    except (OSError, ValueError, KeyError):
        return None
    return p if p.is_file() else None


def undo(journal_path, on_line=None) -> int:
    """Put back what the run of *journal_path* changed, newest first."""
    journal_path = Path(journal_path)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    rc_all = 0
    for step in reversed(journal["steps"]):
        pkg, u = Path(step["package"]), Path(step["undo"])
        rc = fpkg.ps4_keystone_restore(pkg, u, on_line=lambda line: _say(on_line, line) if line.startswith(("[ok]", "[ERROR]")) else None)
        if rc == 0:
            _say(on_line, f"[PS4] restored {pkg.name}")
        else:
            _say(on_line, f"[ERROR] could not restore {pkg.name}")
            rc_all = 1
    if rc_all == 0:
        journal_path.rename(journal_path.with_name("journal.undone.json"))
        _say(on_line, "[OK] PS4 keystone: undone")
    return rc_all
