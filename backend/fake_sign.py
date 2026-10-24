#!/usr/bin/env python3
"""Recursive fake-signing for decrypted PS5 game dumps."""
import contextlib
import io
import os
import struct
import sys
import tempfile

# vendored signer must import as script, as package and from the frozen bundle
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import make_fself  # noqa: E402  (vendored, see module docstring)

ELF_MAGIC = b"\x7fELF"
# same list as ps5mfr; eboot.bin is a .bin SELF
SIGN_SUFFIXES = (".bin", ".elf", ".prx", ".sprx")
_TMP_PREFIX = ".fself-"


class NotSignable(Exception):
    """not an ELF, already a SELF, or unparseable; a truncated ELF is a failure, not a skip"""


def is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == ELF_MAGIC
    except OSError:
        return False


def fake_sign_file(path: str) -> None:
    """in place; the original is only replaced after a complete save"""
    if not is_elf(path):
        raise NotSignable("not an ELF")

    # load() pulls the whole file into RAM, so we may overwrite it; mute the chatty signer
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            with open(path, "rb") as f:
                elf = make_fself.ElfFile(ignore_shdrs=True)
                elf.load(f)
        except make_fself.ElfTruncatedError as exc:
            raise OSError(f"truncated file, left unsigned ({exc})")
        except make_fself.ElfError as exc:
            raise NotSignable(f"not a signable PS5 ELF ({exc})")
        except (struct.error, ValueError) as exc:
            raise NotSignable(f"unparseable ELF ({exc})")

        directory = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".fself-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as out:
                # defaults give the standard fake PAID, same as ps5mfr / make_fself.py without flags
                signed = make_fself.SignedElfFile(elf)
                signed.save(out)
            try:  # executables stay executable
                os.chmod(tmp, os.stat(path).st_mode)
            except OSError:
                pass
            if os.name == "nt":
                os.chmod(path, 0o666)       # Windows will not replace a read-only file
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def find_targets(root: str):
    """(sorted candidate paths, found_eboot_bin)"""
    targets = []
    found_eboot = False
    for dirpath, _dirnames, filenames in os.walk(root):   # followlinks=False: never escape the tree
        for name in filenames:
            if name.lower() == "eboot.bin":
                found_eboot = True
            if name.startswith(_TMP_PREFIX):
                continue  # our own leftover temp, never a target
            if name.lower().endswith(SIGN_SUFFIXES):
                targets.append(os.path.join(dirpath, name))
    targets.sort()
    return targets, found_eboot


def _sweep_stale_temps(root: str, log) -> None:
    """leftover .fself-* temps of a SIGKILLed run; os.replace kept the originals intact"""
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith(_TMP_PREFIX):
                try:
                    os.unlink(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    pass
    if removed:
        log(f"[INFO] Cleaned {removed} leftover temp file(s) from an interrupted run.")


def fake_sign_tree(root: str, log=print) -> dict:
    root = os.path.abspath(root)
    counts = {"signed": 0, "skipped": 0, "failed": 0, "by_ext": {}}

    if not os.path.isdir(root):
        log(f"[ERROR] Not a folder: {root}")
        counts["failed"] = 1
        return counts

    _sweep_stale_temps(root, log)
    targets, found_eboot = find_targets(root)
    if not found_eboot:
        log(f"[WARN] No eboot.bin found under {root} - this may not be a PS5 "
            f"game dump. Fake-signing every ELF found anyway; cancel now if this "
            f"is the wrong folder.")
    if not targets:
        log(f"[INFO] No .bin/.elf/.prx/.sprx files found under {root}.")
        return counts

    log(f"[INFO] Fake-signing up to {len(targets)} candidate file(s) in {root} …")
    for path in targets:
        rel = os.path.relpath(path, root)
        ext = os.path.splitext(path)[1].lower()
        try:
            fake_sign_file(path)
            counts["signed"] += 1
            counts["by_ext"][ext] = counts["by_ext"].get(ext, 0) + 1
            log(f"[OK] signed  {rel}")
        except NotSignable as exc:
            counts["skipped"] += 1
            log(f"[SKIP] {rel} ({exc})")
        except Exception as exc:  # noqa: BLE001 - a real I/O failure; report and keep going
            counts["failed"] += 1
            log(f"[FAIL] {rel} ({exc})")

    by_ext = ", ".join(f"{n}× {e}" for e, n in sorted(counts["by_ext"].items())) or "none"
    log(f"[DONE] Fake-signed {counts['signed']} file(s) ({by_ext}); "
        f"skipped {counts['skipped']} non-ELF; {counts['failed']} failed.")
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: fake_sign.py <decrypted-ps5-dump-folder>", file=sys.stderr)
        sys.exit(2)
    result = fake_sign_tree(sys.argv[1])
    # non-zero only on hard failures
    sys.exit(1 if result["failed"] else 0)
