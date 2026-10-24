"""patch backups: originals in app0/, restore.jsonl written before every change"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

MANIFEST = "restore.jsonl"


class Journal:
    def __init__(self, backup_dir: Path, game_root: Path):
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.backup_dir / MANIFEST
        self._write({"game_root": str(Path(game_root).resolve())})

    def _write(self, entry: dict) -> None:
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def record(self, op: str, rel: str) -> None:
        """op: replaced/removed (original in app0/), added, or mkdir"""
        self._write({"op": op, "path": rel})

    def keep_original(self, game_root: Path, rel: str) -> None:
        """Copy the game's file at *rel* into app0/, complete or not at all."""
        keep = self.backup_dir / "app0" / rel
        keep.parent.mkdir(parents=True, exist_ok=True)
        part = keep.with_name(keep.name + ".part")
        try:
            shutil.copy2(Path(game_root) / rel, part)
            os.replace(part, keep)
        except BaseException:
            part.unlink(missing_ok=True)
            raise


def _entries(backup_dir: Path) -> tuple[str | None, list[dict]]:
    root, ops = None, []
    with open(Path(backup_dir) / MANIFEST, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue          # a line cut off by a crash
            if "game_root" in entry:
                root = root or entry["game_root"]
            elif entry.get("op") and entry.get("path"):
                ops.append(entry)
    return root, ops


def _inside(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"a recorded path leaves the game folder: {rel}")
    return p


def restore_originals(backup_dir: Path, game_root: Path) -> int:
    """safe to rerun; raises if game_root isn't the patched folder, backup kept on failure"""
    backup_dir, game_root = Path(backup_dir), Path(game_root).resolve()
    if not (backup_dir / MANIFEST).is_file():
        return 0
    recorded, ops = _entries(backup_dir)
    if recorded and Path(recorded) != game_root:
        raise ValueError(f"this backup belongs to {recorded}, not to {game_root}")
    undone, failed = 0, []
    for entry in reversed(ops):
        op, rel = entry["op"], entry["path"]
        try:
            target = _inside(game_root, rel)
            if op in ("replaced", "removed"):
                keep = backup_dir / "app0" / rel
                if not keep.is_file():
                    continue          # recorded, but the game file was never touched
                target.parent.mkdir(parents=True, exist_ok=True)
                # a temp name of its own, never one a user file may carry
                fd, part = tempfile.mkstemp(prefix=f".{target.name[:40]}.", suffix=".restore", dir=target.parent)
                os.close(fd)
                part = Path(part)
                try:
                    shutil.copy2(keep, part)
                    os.replace(part, target)
                except BaseException:
                    part.unlink(missing_ok=True)
                    raise
                undone += 1
            elif op == "added":
                if target.is_file() or target.is_symlink():
                    target.unlink()
                undone += 1
            elif op == "mkdir":
                try:
                    target.rmdir()    # only when empty: nothing of the user's goes
                except OSError:
                    pass
        except Exception as e:
            failed.append(f"{rel}: {e}")
    if failed:
        raise RuntimeError(f"{len(failed)} file(s) could not be put back, first {failed[0]}")
    return undone
