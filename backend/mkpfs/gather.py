"""Gather helpers for MkPFS.

Provide a scandir-based file gather implementation as a faster, low-level
alternative to Path.rglob("*"). Keep this module minimal and dependency-free
other than mkpfs.utils.is_ignored_name.
"""

from __future__ import annotations

import os
from pathlib import Path

from .utils import is_ignored_name


def gather_files_scandir(root: Path) -> list[Path]:
    return gather_tree_scandir(root)[0]


def gather_tree_scandir(root: Path) -> tuple[list[Path], list[Path]]:
    """every file and every folder below root, OS junk left out; a folder that cannot be
    read stops the scan: an image without its files would look complete"""
    root = Path(root)
    abs_files: list[Path] = []
    abs_dirs: list[Path] = []
    stack: list[Path] = [root]

    while stack:
        dir_path = stack.pop()
        try:
            with os.scandir(dir_path) as it:
                entries = list(it)
        except FileNotFoundError:
            if dir_path == root:
                raise
            continue                     # gone while we walked: nothing of it to pack
        # PermissionError and other read errors go up

        for entry in entries:
            name = entry.name
            if is_ignored_name(name):
                continue

            try:
                # Directories: descend but do NOT follow symlinks
                if entry.is_dir(follow_symlinks=False):
                    stack.append(dir_path / name)
                    abs_dirs.append(dir_path / name)
                    continue

                # Files: include regular files and file symlinks
                if entry.is_file():
                    abs_files.append(dir_path / name)
            except FileNotFoundError:
                continue

    return abs_files, abs_dirs
