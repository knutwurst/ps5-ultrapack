"""High-level Python API for UnRAR extraction.

Mirrors the subset of the `rarfile` library API used by ps5-ffpfs-cli.
"""

import os
from pathlib import Path

from unrar import _unrar


class BadRarFile(Exception):
    """Invalid or corrupted RAR archive."""


class RarWrongPassword(Exception):
    """Incorrect or missing password for encrypted archive."""


class NeedFirstVolume(Exception):
    """Multi-volume archive must start from the first volume."""


class RarExtractionCancelled(Exception):
    """Extraction was aborted via the cancel callback."""


class SolidArchive(BadRarFile):
    """A solid archive: one member cannot be read without decompressing what precedes it."""


class RarInfo:
    """Metadata about a single member of a RAR archive."""

    __slots__ = ("filename", "file_size", "compress_size", "is_directory")

    def __init__(self, data: dict):
        self.filename = data["filename"]
        self.file_size = data["file_size"]
        self.compress_size = data["compress_size"]
        self.is_directory = data["is_directory"]

    def isdir(self) -> bool:
        return self.is_directory


class RarFile:
    """A RAR archive file."""

    def __init__(self, filename: str | Path, mode: str = "r", pwd: str | None = None):
        self.filename = str(filename)
        self.pwd = pwd
        self._filelist: list[RarInfo] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def infolist(self) -> list[RarInfo]:
        """Return a list of RarInfo instances for all members."""
        if self._filelist is None:
            try:
                raw_list = _unrar.list_files(self.filename, self.pwd)
            except _unrar.UnrarError as exc:
                msg = str(exc)
                low = msg.lower()
                # Header-encrypted (-hp) archives fail here when the password is
                # missing (UnRAR error 22) or wrong (error 24). Surface that as a
                # password error rather than a generic "bad archive".
                if "error 22" in low or "error 24" in low or "password" in low:
                    raise RarWrongPassword(msg) from exc
                raise BadRarFile(msg) from exc
            self._filelist = [RarInfo(item) for item in raw_list]
        return self._filelist

    def read_prefix(self, name: str, nbytes: int) -> bytes:
        """The first *nbytes* of one member (a package's header), without extracting it.
        Raises SolidArchive for a later member of a solid archive."""
        try:
            return _unrar.read_prefix(self.filename, name, int(nbytes), self.pwd)
        except PermissionError as exc:
            raise RarWrongPassword(str(exc)) from exc
        except _unrar.UnrarError as exc:
            msg = str(exc)
            if "solid archive" in msg.lower():
                raise SolidArchive(msg) from exc
            raise BadRarFile(msg) from exc

    def read(self, name: str) -> bytes:
        """The bytes of one member, without extracting the others (they are skipped, which
        is cheap unless the archive is solid: then SolidArchive is raised)."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="rar-member-") as td:
            dest = os.path.join(td, "member")
            try:
                got = _unrar.extract_names(self.filename, [name], [dest], self.pwd)
            except PermissionError as exc:
                raise RarWrongPassword(str(exc)) from exc
            except _unrar.UnrarError as exc:
                msg = str(exc)
                low = msg.lower()
                if "solid archive" in low:
                    raise SolidArchive(msg) from exc
                if "error 22" in low or "error 24" in low or "password" in low:
                    raise RarWrongPassword(msg) from exc
                raise BadRarFile(msg) from exc
            if not got or not os.path.isfile(dest):
                raise KeyError(name)
            with open(dest, "rb") as f:
                return f.read()

    def namelist(self) -> list[str]:
        """Return a list of member filenames."""
        return [info.filename for info in self.infolist()]

    def extractall(self, path: str | Path | None = None, progress=None, cancel=None):
        """Extract all members to the given path (default: current directory).

        progress, if given, is called as progress(percent: int) periodically
        during extraction (0–100). cancel, if given, is a callable polled
        periodically; when it returns truthy, extraction aborts and
        RarExtractionCancelled is raised."""
        if path is None:
            path = "."
        dest = str(path)

        # Validate member paths before extraction (defense-in-depth)
        dest_resolved = Path(dest).resolve()
        infos = self.infolist()
        for info in infos:
            member_path = (dest_resolved / info.filename).resolve()
            try:
                member_path.relative_to(dest_resolved)
            except ValueError:
                raise BadRarFile(f"Unsafe path in archive: {info.filename}")

        # Wrap the caller's percent callback into a cumulative-bytes callback,
        # using the total uncompressed size we already have from infolist().
        byte_cb = None
        if progress is not None:
            total = sum(i.file_size for i in infos if not i.is_directory)
            if total > 0:
                def byte_cb(done_bytes, _total=total):
                    try:
                        progress(min(100, int(done_bytes * 100 / _total)))
                    except Exception:
                        pass

        try:
            _unrar.extract_all(self.filename, dest, self.pwd, byte_cb, cancel)
        except PermissionError as exc:
            raise RarWrongPassword(str(exc)) from exc
        except _unrar.UnrarError as exc:
            msg = str(exc).lower()
            if "cancel" in msg:
                raise RarExtractionCancelled(str(exc)) from exc
            if ("password" in msg or "missing password" in msg
                    or "error 22" in msg or "error 24" in msg):
                raise RarWrongPassword(str(exc)) from exc
            raise BadRarFile(str(exc)) from exc
