#!/usr/bin/env python3
"""Rename each built .ffpfsc to match the name of the folder it sits in.

The app derives the [v…] version in the .ffpfsc filename from the game's
param.json. For patched games whose param uses the full PS5 form ("01.200.000")
older builds fell back to masterVersion and wrote "[v01.00]". This one-off tool
renames the output files to YOUR folder names instead (which you keep correct).

It does NOT touch the image - only the filename. DRY-RUN by default.

Usage:
    python3 rename_ffpfsc_by_folder.py [ROOT]            # preview only
    python3 rename_ffpfsc_by_folder.py [ROOT] --apply    # actually rename

ROOT defaults to the output drive. Pass several roots if your games live in
more than one place, e.g.:
    python3 rename_ffpfsc_by_folder.py "/Volumes/Games/PS5" "/Volumes/Games2/PS5" --apply
"""
import sys
from pathlib import Path

DEFAULT_ROOTS = ["/Volumes/Games/PS5"]


def process(root: Path, apply: bool):
    renamed = skipped = ok = 0
    if not root.is_dir():
        print(f"!! root not found: {root}")
        return 0, 0, 0
    # Every .ffpfsc under the root; name it after the folder it lives in.
    for ff in sorted(root.rglob("*.ffpfsc")):
        folder = ff.parent
        if folder == root:
            print(f"SKIP (not in a game folder): {ff.name}")
            skipped += 1
            continue
        siblings = list(folder.glob("*.ffpfsc"))
        if len(siblings) != 1:
            print(f"SKIP ({len(siblings)} .ffpfsc in one folder): {folder.name}")
            skipped += 1
            continue
        target = folder / (folder.name + ".ffpfsc")
        if target.name == ff.name:
            ok += 1
            continue
        if target.exists():
            print(f"SKIP (target already exists): {target.name}")
            skipped += 1
            continue
        print(f"{'RENAME ' if apply else 'WOULD RENAME '}{folder.name}/")
        print(f"    {ff.name}")
        print(f" -> {target.name}")
        if apply:
            try:
                ff.rename(target)
                renamed += 1
            except OSError as e:
                print(f"    !! failed: {e}")
                skipped += 1
    return renamed, skipped, ok


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    roots = [a for a in args if a != "--apply"] or DEFAULT_ROOTS
    print(f"Mode: {'APPLY (renaming for real)' if apply else 'DRY-RUN (preview only)'}\n")
    tot_r = tot_s = tot_ok = 0
    for r in roots:
        print(f"### {r}")
        a, b, c = process(Path(r), apply)
        tot_r += a; tot_s += b; tot_ok += c
        print()
    print(f"{'Renamed' if apply else 'Would rename'}: {tot_r}   "
          f"Already correct: {tot_ok}   Skipped: {tot_s}")
    if not apply:
        print("\nDry-run only. Re-run with --apply once the preview looks right.")
        print("Review the list first - where a file's [v…] differs from your folder,\n"
              "the app may have read a MORE accurate version from param.json\n"
              "(e.g. a title whose param says v01.02 vs a folder labelled 1.00). Skip those\n"
              "manually if you'd rather keep the param version.")


if __name__ == "__main__":
    main()
