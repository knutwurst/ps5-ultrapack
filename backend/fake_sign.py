#!/usr/bin/env python3
"""Recursive fake-signing for decrypted PS5 game dumps.

Walks a folder, finds every ``.bin`` / ``.elf`` / ``.prx`` / ``.sprx`` file and
replaces each genuine ELF in place with its fake-signed SELF/SPRX, using the
vendored :mod:`make_fself` (flatz / ps5-payload-dev, redistributed by
alex-free/ps5-make-fself-recursive). This is a Python re-implementation of that
project's ``ps5mfr`` bash wrapper, with three deliberate safety improvements:

* **ELF-magic guard** - only files that actually start with ``\\x7fELF`` are
  signed. Data ``.bin`` files (not executables) and files that are *already*
  fake-signed (they start with the SELF magic ``\\x4F\\x15\\x3D\\x1D``, not the
  ELF magic) are skipped. This makes the whole operation **idempotent**: running
  it twice over the same dump is safe and a no-op on the second pass.
* **Atomic in-place replace** - each file is signed to a temp file in the same
  directory and then ``os.replace``-d over the original, so an interrupted run
  (crash, kill, full disk) can never leave a half-written, corrupt executable.
* **No subprocess per file** - the signer is imported and called directly.

The original file's permission bits are preserved.
"""
import contextlib
import io
import os
import struct
import sys
import tempfile

# Make the sibling vendored signer importable whether we're run as a script,
# imported as ``backend.fake_sign``, or loaded from the frozen bundle's backend
# data dir (cli.py puts this directory on sys.path, but be self-sufficient).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import make_fself  # noqa: E402  (vendored, see module docstring)

ELF_MAGIC = b"\x7fELF"
# Extensions worth probing, mirroring ps5mfr (eboot.bin is a .bin SELF).
SIGN_SUFFIXES = (".bin", ".elf", ".prx", ".sprx")
# Temp files we write next to each source during the atomic replace.
_TMP_PREFIX = ".fself-"


class NotSignable(Exception):
    """Raised when a candidate file is not something we can/should fake-sign:
    not an ELF at all, an already-signed SELF, or an ELF the signer can't parse
    (wrong class/arch/type, truncated). These are SKIPPED, not failures - a real
    PS5 dump's executables always parse, so this only fires on data blobs and
    foreign/stray files."""


def is_elf(path: str) -> bool:
    """True if *path* begins with the ELF magic (i.e. an unsigned executable)."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == ELF_MAGIC
    except OSError:
        return False


def fake_sign_file(path: str) -> None:
    """Fake-sign one file **in place**.

    Returns normally when the file was signed. Raises :class:`NotSignable` when
    the file should be skipped (not an ELF, already-signed SELF, or an ELF the
    signer can't parse - wrong arch/class/type, truncated). Raises any other
    exception only on a genuine I/O failure; the original is left untouched in
    that case (we only ``os.replace`` after a fully successful save).
    """
    if not is_elf(path):
        raise NotSignable("not an ELF")

    # load() reads the whole file into memory (ehdr + phdrs + segment bytes),
    # so once it returns we can safely overwrite the source path. The vendored
    # signer prints internal progress ("meta block …", "processing segment …")
    # to stdout - silence it so our own per-file log lines stay clean.
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            with open(path, "rb") as f:
                elf = make_fself.ElfFile(ignore_shdrs=True)
                elf.load(f)
        except make_fself.ElfError as exc:
            raise NotSignable(f"not a signable PS5 ELF ({exc})")
        except (struct.error, ValueError) as exc:
            raise NotSignable(f"unparseable ELF ({exc})")

        directory = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".fself-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as out:
                # All defaults => a "fake" program type with the standard fake
                # PAID, exactly what ps5mfr / make_fself.py produce with no flags.
                signed = make_fself.SignedElfFile(elf)
                signed.save(out)
            try:  # carry over the original mode bits (executables stay executable)
                os.chmod(tmp, os.stat(path).st_mode)
            except OSError:
                pass
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def find_targets(root: str):
    """Return (sorted list of candidate file paths, found_eboot_bin)."""
    targets = []
    found_eboot = False
    for dirpath, _dirnames, filenames in os.walk(root):   # followlinks=False: never escape the tree
        for name in filenames:
            if name.lower() == "eboot.bin":
                found_eboot = True
            if name.startswith(_TMP_PREFIX):
                continue   # our own leftover temp (see _sweep_stale_temps) - never a target
            if name.lower().endswith(SIGN_SUFFIXES):
                targets.append(os.path.join(dirpath, name))
    targets.sort()
    return targets, found_eboot


def _sweep_stale_temps(root: str, log) -> None:
    """Remove leftover ``.fself-*`` temp files from a previous run that was hard-
    killed (SIGKILL) mid-sign. The atomic os.replace means the originals are
    always intact; only the temp can linger. Self-heals on the next run."""
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
    """Recursively fake-sign every eligible file under *root*, in place.

    *log* is called with a single string per message (defaults to ``print``).
    Returns a counts dict: ``signed``, ``skipped``, ``failed``, ``by_ext``.
    """
    root = os.path.abspath(root)
    counts = {"signed": 0, "skipped": 0, "failed": 0, "by_ext": {}}

    if not os.path.isdir(root):
        log(f"[ERROR] Not a folder: {root}")
        counts["failed"] = 1
        return counts

    _sweep_stale_temps(root, log)
    targets, found_eboot = find_targets(root)
    if not found_eboot:
        log(f"[WARN] No eboot.bin found under {root} — this may not be a PS5 "
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
    # Non-zero only on hard failures, so callers can detect a broken run.
    sys.exit(1 if result["failed"] else 0)
