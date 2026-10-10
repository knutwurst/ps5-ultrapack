"""Tk-free core of PS5 UltraPack."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import NamedTuple


def _bundled_backend_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend"
    return Path(__file__).resolve().parent / "backend"


if sys.platform == "darwin":
    APP_DIR = Path.home() / "Library" / "Application Support" / "PS5_UltraPack"
else:
    APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "PS5_UltraPack"
# Tests and headless drivers point this at a scratch folder so they never touch the
# real profile (settings, queue, passwords, reports).
_ENV_APP_DIR = os.environ.get("PS5_FFPFSC_APP_DIR", "").strip()
if _ENV_APP_DIR:
    APP_DIR = Path(_ENV_APP_DIR)

# One-time migration after the renames (PS5 FFPFSC PRO → PS5 FFPFSC ULTRA → PS5 UltraPack):
# if the settings dir doesn't exist yet but an older one does, move the newest one over so
# saved settings, queue, history, passwords, drive config and the backport patch cache
# carry across without the user noticing.
_LEGACY_APP_DIRS = (
    APP_DIR.parent / "PS5_FFPFSC_ULTRA_BIZKUT",
    APP_DIR.parent / "PS5_FFPFSC_PRO_BIZKUT",
)
try:
    if not _ENV_APP_DIR and not APP_DIR.exists():
        for _legacy in _LEGACY_APP_DIRS:
            if _legacy.is_dir():
                APP_DIR.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(_legacy), str(APP_DIR))
                break
except Exception:
    pass

RAW_LOG_FILE = APP_DIR / "raw_tool_output.log"
FINAL_REPORT_FILE = APP_DIR / "last_result_report.txt"
HISTORY_FILE = APP_DIR / "history.json"
SETTINGS_FILE = APP_DIR / "settings.json"

TITLE_RE = re.compile(r"\b(PPSA\d{5}|CUSA\d{5})\b", re.I)
PROGRESS_RE = re.compile(r"\[(?P<bar>[#\-]{4,})\]\s*(?P<pct>\d{1,3})%\s*(?P<label>.*)", re.I)
PFS_IMAGE_SUFFIXES = {".ffpfs", ".ffpfsc"}
DISK_IMAGE_SUFFIXES = {".exfat", ".ffpkg"}


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def open_path(path) -> None:
    """Open a file or folder with the default OS handler (cross-platform)."""
    p = str(path)
    try:
        if os.name == "nt":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:
        pass


def now_time() -> str:
    return time.strftime("%H:%M:%S")


def now_datetime() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def format_size(num) -> str:
    try:
        num = float(num)
    except Exception:
        return " - "
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024 or unit == "TB":
            return f"{num:.2f} {unit}" if unit != "B" else f"{num:.0f} {unit}"
        num /= 1024


def format_duration(seconds) -> str:
    seconds = int(max(0, seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def humanize_eta(raw) -> str:
    """Turn a backend ETA token into hours/minutes/seconds. mkpfs emits raw seconds
    ('ETA 1695s'), which is unreadable past a minute - convert to '1h 05m' / '28m 15s'
    / '45s'. Accepts an optional unit (s/m/h); a bare number is treated as seconds.
    Returns the input unchanged if it can't be parsed, and ' - ' for the no-ETA marker."""
    if raw is None:
        return " - "
    s = str(raw).strip().lower()
    if s in ("", " - ", "-"):
        return " - "
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?|s|sec|secs|seconds?)?$", s)
    if not m:
        return str(raw)
    val = float(m.group(1))
    unit = m.group(2) or "s"
    if unit.startswith("h"):
        total = val * 3600
    elif unit.startswith("m"):   # m / min / minutes
        total = val * 60
    else:
        total = val
    total = int(round(total))
    if total <= 0:
        return "0s"
    h, r = divmod(total, 3600)
    mm, ss = divmod(r, 60)
    if h:
        return f"{h}h {mm:02d}m"
    if mm:
        return f"{mm}m {ss:02d}s"
    return f"{ss}s"


def eta_seconds(text) -> float | None:
    """Seconds in a time-left text the app shows or the backend prints ('38m 08s', '1h 05m',
    '45s', 'ETA 2m 00s'); None when there is none."""
    m = re.search(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s)?\s*$", str(text or "").strip())
    if not m or not any(m.groups()):
        return None
    h, mi, se = (int(g) if g else 0 for g in m.groups())
    return float(h * 3600 + mi * 60 + se)


# The long steps of a job, in the order they run; the time-left estimate works over these.
LONG_STEPS = ("Extracting", "Creating Temp PFS", "Compressing", "Writing Final Image")


def job_time_left(phases, stage, stage_left, stage_elapsed, size, rates, widths) -> float | None:
    """Seconds a job still needs: *stage_left* for the running *stage*, then every later step of *phases* at the speed this Mac showed for it before (*rates*: bytes of the game per second), else in the proportion of the progress bands (*widths*) to the running step's whole time (elapsed + left)."""
    if stage not in phases or stage_left is None:
        return None
    total = float(stage_left)
    whole = max(1.0, float(stage_elapsed) + float(stage_left))
    for later in phases[phases.index(stage) + 1:]:
        rate = (rates or {}).get(later)
        if rate and size:
            total += size / float(rate)
        elif widths.get(later) and widths.get(stage):
            total += whole * widths[later] / widths[stage]
        else:
            return None
    return total


def get_free_space(path: Path) -> int:
    try:
        target = path if path.exists() else path.parent
        usage = shutil.disk_usage(str(target))
        return usage.free
    except Exception:
        return 0


def get_total_space(path: Path) -> int:
    try:
        target = path if path.exists() else path.parent
        usage = shutil.disk_usage(str(target))
        return usage.total
    except Exception:
        return 0


def same_drive(path_a: Path, path_b: Path) -> bool:
    """True if both paths are on the same filesystem/volume."""
    try:
        a = path_a if path_a.exists() else path_a.parent
        b = path_b if path_b.exists() else path_b.parent
        return os.stat(a).st_dev == os.stat(b).st_dev
    except Exception:
        try:
            return path_a.resolve().drive.lower() == path_b.resolve().drive.lower()
        except Exception:
            return str(path_a)[:2].lower() == str(path_b)[:2].lower()


def space_safety_factor() -> float:
    """User-tunable multiplier on the *temp* free-space requirement: 1.0 keeps the
    recommended worst-case headroom; below 1.0 allows tighter fits (higher risk of
    an out-of-space failure mid-pack). Clamped to [0.5, 2.0]. Output-drive needs are
    never scaled down - the final container can be as large as the source."""
    try:
        return min(2.0, max(0.5, float(load_settings().get("space_safety_factor", 1.0))))
    except Exception:
        return 1.0


# Peak SCRATCH-drive multiples of the EXTRACTED game size for one packing run.
# CRUCIAL: for our config (PS5 / 64 KiB blocks / 32-bit inodes / unsigned) MkPFS pass-2
# uses the direct-to-image STREAMING builder - it writes the final container straight to
# the OUTPUT drive with NO spool on the temp drive (verified: a 109.6 GB game left only
# ~113 GB on the temp drive - the inner image alone, no spool). So the temp/build drive
# only ever holds {extracted source (archives) + inner .ffpfs image}, never a third spool
# copy. The inner image is barely larger than the source - 64 KiB block-alignment waste is
# only ~1-2% for real games (large asset/audio/video files): MEASURED 109.6 GB → 113 GB
# image = 1.03x. (Heavy padding only happens for tiny-file games, which fit the SSD anyway.)
# So image ≈ 1.05x; we budget 1.2x for headroom. Hence:
#   ARCHIVE: source(1.0) + image(1.2) = 2.2x  (a 150 GB game = 330 GB ≤ a 353 GB SSD → its
#            source extracts to the SSD too, so the many-file read is fast). Was 2.5/3.7 - 
#            those over-budgeted the image (and a non-existent spool) and needlessly pushed
#            ~140-160 GB games' sources onto the slow HDD.
#   INPLACE: image only (1.2x); folder/disk-image sources are read in place, no 2nd copy.
#   PATCH:   a full game copy + image (~3.0x), no spool.
# The "+1.05 same_temp_output_drive" term in estimate_peak_space_needed() adds the final
# container when the build drive IS the output drive. The backend pre-pass-2 assert is the
# final backstop and space_safety_factor() tunes headroom (raise it for more margin).
ARCHIVE_PEAK_FACTOR = 2.2
INPLACE_PEAK_FACTOR = 1.2
PATCH_PEAK_FACTOR   = 3.0
# A chain that unpacks a .pkg: the package tool writes the decoded inner image and then the
# files beside it, so the drive holds twice the game at the end of the unpack (measured: an
# 80.9 GB game, 80.9 GB decoded image next to it); pass 1 then holds the files plus the
# uncompressed image (~1.03x). Valid only on the game's real size (pkg_content_size), which
# is larger than the package: its data is compressed.
PKG_UNPACK_PEAK_FACTOR = 2.2

# Free-space multiple for JUST the inner uncompressed PFS image (pass 1) on the temp/SSD
# drive - used by the split path (image on the SSD, source on the output drive). 1.2x covers
# 64 KiB block padding (~1-2% measured) plus a safety margin; matches INPLACE above.
IMAGE_PEAK_FACTOR = 1.2


def archive_set_parts(archive: Path) -> list:
    """Every file of *archive*'s multi-part volume set (the archive itself when it is a
    single file). Anchored to the archive's own volume naming, so "Game Update.part1.rar"
    or "Game.nfo" next to "Game.part1.rar" are not part of the set."""
    archive = Path(archive)
    base = re.sub(
        r'(\.part\d+\.rar|\.r\d{2,}|\.7z\.\d+|\.zip\.\d+|\.z\d+|\.\d{3}|\.rar|\.zip|\.7z)$',
        '', archive.name, flags=re.I)
    vol_re = re.compile(re.escape(base)
                        + r"\.(part\d+\.rar|r\d{2,}|7z\.\d{3}|zip\.\d{3}|z\d{2}|\d{3}|rar|zip|7z)$",
                        re.I)
    try:
        parts = [p for p in archive.parent.iterdir() if p.is_file() and vol_re.match(p.name)]
    except OSError:
        parts = []
    return parts or ([archive] if archive.exists() else [])


def archive_set_ondisk_size(archive: Path) -> int:
    """Sum the on-disk bytes of an archive's whole multi-part volume set. A fallback
    extracted-size proxy when headers can't be read - still COMPRESSED, so a rough floor."""
    total = 0
    for p in archive_set_parts(archive):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    if total:
        return total
    try:
        return archive.stat().st_size
    except Exception:
        return 0


def _item_is_single_pass(item) -> bool:
    """True for directly-supplied disk images (.exfat, .ffpkg, .ffpfs etc.) that are
    compressed in a SINGLE pass by the backend - no inner image on the temp drive.
    Derived purely from persistent attributes so it works after queue save/restore."""
    try:
        # A copy job never runs mkpfs: same-drive is an os.rename, cross-drive is a
        # straight file copy. Only the OUTPUT drive needs space (for the copy target
        # on cross-drive; nothing new on same-drive). No temp, no inner image.
        if getattr(item, "operation", "pack") == "copy":
            return True
        # Only a PACK of a single image file is single-pass. A patch job also has a
        # .ffpfsc file path + inplace source_kind, but it extracts AND repacks (needs
        # temp) - so it must NOT be treated as single-pass. unpack/fake-sign likewise.
        # A resume (OOM retry compressing the already-built inner .ffpfs) is a single-pass
        # compress of an existing image: only the output drive needs space - no new inner
        # build, no extracted source. _resume_inner is transient (never persisted), so a
        # restored queue just falls through to the disk-image test below.
        ri = getattr(item, "_resume_inner", None)
        if ri and Path(str(ri)).is_file():
            return True
        # A chain that passes a disk image / .ffpfs straight to the pack path (nothing to
        # change, a format the pack path takes natively) is the same single pass.
        if getattr(item, "operation", "pack") == "chain":
            return (getattr(item, "chain_to", None) in ("ffpfs", "ffpfsc")
                    and not chain_needs_unpack(item)
                    and not getattr(item, "archive_path", None)
                    and Path(getattr(item, "path", "") or "").is_file())
        return (getattr(item, "operation", "pack") == "pack"
                and getattr(item, "source_kind", "") == "inplace"
                and not getattr(item, "archive_path", None)
                and Path(getattr(item, "path", "") or "").is_file())
    except Exception:
        return False


def _build_size_of(item) -> int:
    """The honest EXTRACTED size to base space decisions on: the header-read extracted size when known, else item.size (already extracted for folders/disk images)."""
    es = getattr(item, "extracted_size", 0)
    if es:
        return int(es)
    if getattr(item, "source_kind", None) == "archive":
        return 0   # unknown extracted size - do NOT fall back to compressed size
    return int(getattr(item, "size", 0) or 0)


def shows_extracted_size(item) -> bool:
    """True when the size we display is the EXTRACTED (header-read) size, not the on-disk
    size - i.e. a not-yet-extracted archive with a known uncompressed size. (After
    extraction the item flips to source_kind='inplace' and .size is the real folder size.)"""
    return getattr(item, "source_kind", "") == "archive" and getattr(item, "extracted_size", 0) > 0


def display_size(item) -> int:
    """The size to SHOW the user. For an archive, .size is the COMPRESSED volume set; the
    EXTRACTED size (already read from headers, and what space/placement use) is the
    meaningful number, so show that. Falls back to the on-disk .size when the header was
    unreadable (extracted_size == 0). No extra disk I/O - the value is computed at add time."""
    if shows_extracted_size(item):
        return int(item.extracted_size)
    return int(getattr(item, "size", 0) or 0)


# A tempfile.mkdtemp()-created scratch dir is "tmp" + exactly 8 chars from [a-z0-9_]
# (the prefix mkpfs/the backend use). Match that EXACTLY so cleanup never rmtrees an
# unrelated user folder that merely starts with "tmp" (e.g. "tmp_notes", "Tmp Renders").
_APP_TMP_RE = re.compile(r"tmp[a-z0-9_]{8}$")


def _is_app_tmp_dir(name: str) -> bool:
    return bool(_APP_TMP_RE.fullmatch(name))


def _peak_factor_for(item) -> float:
    """Scratch peak multiple for this item's source kind (and auto-patch mode)."""
    try:
        if getattr(item, "patch_source", None) and load_settings().get("auto_integrate_patch", False):
            return PATCH_PEAK_FACTOR
    except Exception:
        pass
    # A chain that must unpack its container into scratch before repacking holds the
    # unpacked game AND the inner image at once - the same shape as a patch job.
    if getattr(item, "operation", "pack") == "chain" and chain_needs_unpack(item):
        if chain_source_kind(item) == "pkg" and getattr(item, "pkg_content_size", 0):
            return PKG_UNPACK_PEAK_FACTOR
        return PATCH_PEAK_FACTOR
    return INPLACE_PEAK_FACTOR if getattr(item, "source_kind", "archive") == "inplace" else ARCHIVE_PEAK_FACTOR


# ── chain job helpers (Tk-free; the job dialog and the queue row both use them) ──
CHAIN_TARGETS = ("folder", "ffpfs", "ffpfsc", "pkg")
CHAIN_TARGET_LABEL = {"folder": "folder", "ffpfs": ".ffpfs", "ffpfsc": ".ffpfsc", "pkg": ".pkg"}
# "Organize": the source goes into the library in its own format (a copy job, see
# GameItem.content_kind); not a chain target, the job dialog offers it beside them.
ORGANIZE_TARGET = "organize"
ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z", ".r00")


def chain_source_kind(item) -> str:
    """folder | archive | exfat | ffpkg | ffpfs | ffpfsc | pkg | file - what the backend's
    --to chain will see. An archive placeholder (not yet extracted) is 'archive'."""
    if getattr(item, "archive_path", None):
        return "archive"
    p = Path(str(getattr(item, "path", "") or ""))
    if not str(p):
        return "file"
    if p.is_dir():
        return "folder"
    suf = p.suffix.lower()
    return {".exfat": "exfat", ".ffpkg": "ffpkg", ".ffpfs": "ffpfs",
            ".ffpfsc": "ffpfsc", ".pkg": "pkg"}.get(suf, suf.lstrip(".") or "file")


_CONTAINER_LABELS = {".exfat": ".exfat", ".ffpkg": ".ffpkg", ".ffpfs": ".ffpfs", ".ffpfsc": ".ffpfsc", ".pkg": ".pkg"}
_TITLE_ID_PLACEHOLDERS = {"Unknown", "📦", "💾", "📤", ""}


_ARCHIVE_KINDS = ((re.compile(r"\.(part\d+\.rar|rar|r\d{2,})$", re.I), ".rar"),
                  (re.compile(r"\.7z(\.\d{3})?$", re.I), ".7z"),
                  (re.compile(r"\.(zip|z\d{2}|zip\.\d{3})$", re.I), ".zip"))


def archive_label(path) -> str:
    """'.rar', '.7z' or '.zip' for an archive or any volume of a set; 'Archive' otherwise."""
    name = Path(str(path or "")).name
    return next((label for rx, label in _ARCHIVE_KINDS if rx.search(name)), "Archive")


def source_label(item) -> str:
    """What the job's source was, for the queue row: the archive's kind ('.rar', '.7z', '.zip'), 'Folder' or a container's suffix."""
    arc = getattr(item, "archive_path", None) or getattr(item, "origin_archive", None)
    if arc or getattr(item, "source_kind", "") == "archive":
        return archive_label(arc)
    raw = str(getattr(item, "path", "") or "").strip()
    if not raw:                          # Path("") would be ".", the working folder
        return "Folder"
    p = Path(raw)
    if p.is_dir():
        return "Folder"
    label = _CONTAINER_LABELS.get(p.suffix.lower())
    if label:
        return label
    if p.is_file() and p.suffix and " " not in p.suffix and len(p.suffix) <= 8:
        return p.suffix.lower()          # some other container the backend may still read
    return "Folder"


def shown_title_id(item) -> str:
    """The title id a row may show: the one read from the archive when the job has it, else
    the item's own, never a placeholder glyph."""
    for tid in (getattr(item, "archive_title_id", ""), getattr(item, "title_id", "")):
        tid = str(tid or "").strip()
        if tid and tid not in _TITLE_ID_PLACEHOLDERS:
            return tid
    return ""


_BACKPORT_TARGET = re.compile(r"^(?:\d{1,2}\.\d{2}|10\.xx)$")


def is_backport_target(name) -> bool:
    """A backport target the backend accepts: 7.61, 6.02, 10.xx, or a firmware whose
    original libraries the user keeps in the firmware libraries folder (9.60, ...)."""
    return isinstance(name, str) and bool(_BACKPORT_TARGET.match(name))


def chain_changes(item) -> list[str]:
    """The content changes a chain job applies, in backend order: patch → backport → sign."""
    out: list[str] = []
    if getattr(item, "patch_source", None):
        out.append("patch")
    if getattr(item, "backport_target", None):
        out.append(f"backport {item.backport_target}")
    if getattr(item, "chain_sign", False):
        out.append("sign")
    return out


def chain_needs_unpack(item) -> bool:
    """True when the backend must unpack the source into scratch: a container that the
    target path does not take natively, or any container when something is to change."""
    kind = chain_source_kind(item)
    to = getattr(item, "chain_to", None) or "ffpfsc"
    if kind in ("folder", "archive"):
        return False
    if chain_changes(item):
        return True
    native = {"ffpfs": {"exfat", "ffpkg", "ffpfs"}, "ffpfsc": {"exfat", "ffpkg", "ffpfs"},
              "pkg": {"ffpfs", "ffpfsc", "exfat", "ffpkg"}}
    return kind not in native.get(to, set())


def chain_summary(item) -> str:
    """The sentence the dialog shows above 'Add to queue' and the queue row carries:
    'Sign, backport to 7.61, then build .ffpfsc' · 'Unpack to folder' · 'Build .pkg' ·
    'Copy or move (same format)' · 'Sign in place' · 'Nothing to do'."""
    if getattr(item, "content_kind", "") == "ps4":
        return "Sort into the PS4 library"
    if getattr(item, "content_kind", "") == ORGANIZE_TARGET or getattr(item, "chain_to", None) == ORGANIZE_TARGET:
        return "Organize into the library (same format)"
    to = getattr(item, "chain_to", None) or "ffpfsc"
    kind = chain_source_kind(item)
    label = CHAIN_TARGET_LABEL.get(to, to)
    parts: list[str] = []
    for c in chain_changes(item):
        if c.startswith("backport "):
            parts.append("backport to " + c.split(" ", 1)[1])
        else:
            parts.append({"patch": "integrate patch", "sign": "sign"}.get(c, c))
    if not parts:
        if kind == to == "folder":
            return "Nothing to do"
        if kind == to:
            return "Copy or move (same format)"
        return "Unpack to folder" if to == "folder" else f"Build {label}"
    head = ", ".join(parts)
    head = head[0].upper() + head[1:]
    if to == "folder":
        return f"{head} in place" if kind == "folder" else f"{head}, then unpack to folder"
    return f"{head}, then build {label}"


def estimate_peak_space_needed(extracted_size: int, factor: float = ARCHIVE_PEAK_FACTOR,
                               same_temp_output_drive: bool = True) -> int:
    """Peak free space the SCRATCH (build) drive needs. *factor* is the co-resident
    multiple of the EXTRACTED size for {source + inner image + pass-2 spool}. When the
    final .ffpfsc also lands on this drive (scratch == output), add ~1x for it."""
    mult = factor + (1.05 if same_temp_output_drive else 0.0)
    return int(extracted_size * mult * space_safety_factor())


def estimate_image_space_needed(extracted_size: int) -> int:
    """Free space the temp/SSD drive needs for JUST the inner uncompressed PFS image
    (pass 1), in the split path where the pass-2 spool is placed adaptively elsewhere."""
    return int(extracted_size * IMAGE_PEAK_FACTOR * space_safety_factor())


# Realistic final-.ffpfsc size as a fraction of the UNPACKED game. Measured over 70+
# runs: mean ~0.59, worst ~0.87. 0.75 leaves headroom above the mean without reserving
# the full incompressible worst case (which needlessly skipped easily-fitting games like
# a small retail title: 148 GB unpacked → 37 GB .rar, but the old 1.05x reserved ~156 GB on the
# output drive and skipped it on a 131 GB-free disk).
COMPRESSED_OUTPUT_RATIO = 0.75


def estimate_output_space_needed(game_size: int, compressed: bool = True,
                                 known_packed: int = 0) -> int:
    """Free space the OUTPUT drive must have for the final container."""
    worst = int(game_size * 1.05)
    if not compressed:
        return worst
    est = int(game_size * COMPRESSED_OUTPUT_RATIO)
    if known_packed:
        est = max(est, int(known_packed * 1.25))
    return min(est, worst)


def _space_requirements(item, temp_dir: Path, out_dir: Path) -> list[tuple[str, Path, int]]:
    """What the chosen placement needs, drive by drive: [(label, folder, bytes needed)]."""
    temp_dir, out_dir = Path(temp_dir), Path(out_dir)
    size = _build_size_of(item)
    if size <= 0:
        return []
    comp  = bool(getattr(item, "_output_compressed", True))
    known = int(getattr(item, "size", 0) or 0) if getattr(item, "source_kind", "") == "archive" else 0
    out_final = estimate_output_space_needed(size, comp, known)
    if getattr(item, "operation", "pack") == "copy":
        try:
            src = Path(str(getattr(item, "path", "") or ""))
            src_dir = src.parent if src.exists() else None
        except Exception:
            src_dir = None
        if getattr(item, "archive_path", None) and not getattr(item, "path", None):
            # an archive (PS4 packages, Organize): unpacked on the output drive itself (see
            # _item_is_single_pass and the router), then renamed into place
            return [("Output drive (archive unpacked)", out_dir, int(size * 1.02))]
        if src_dir is not None and same_drive(src_dir, out_dir):
            return []
        # Streamed through a .copy-tmp then os.replace (no doubling); 1.02x slack.
        return [("Output drive", out_dir, int(size * 1.02))]
    if _item_is_single_pass(item):
        return [("Output drive", out_dir, out_final)]
    if getattr(item, "_extract_on_pool", False):
        image_dir   = Path(getattr(item, "_build_temp", temp_dir))
        extract_dir = Path(getattr(item, "_build_root", image_dir))
        return [("Inner image drive", image_dir, estimate_image_space_needed(size)),
                ("Extracted source drive", extract_dir, int(size)),
                ("Output drive", out_dir, out_final)]
    if getattr(item, "_image_only_on_temp", False):
        # The source copy is reserved only before extraction (archive); afterwards it is
        # already on the output drive and counted in its free space.
        src_on_out = size if getattr(item, "source_kind", "") == "archive" else 0
        return [("Temp drive (inner image)", temp_dir, estimate_image_space_needed(size)),
                ("Output drive", out_dir, src_on_out + out_final)]
    same = same_drive(temp_dir, out_dir)
    # On a same-drive-OK drive (SSD) skip the extra final-image padding - matches the
    # router's leaner one-drive estimate so a single fast SSD isn't false-skipped.
    same_pad = same and not getattr(item, "_same_drive_ok", False)
    needs = [("Temp drive (whole scratch)", temp_dir,
              estimate_peak_space_needed(size, _peak_factor_for(item), same_pad))]
    if not same:
        needs.append(("Output drive", out_dir, out_final))
    return needs


def _space_preflight_ok(item, temp_dir: Path, out_dir: Path) -> bool:
    """True only if every drive the chosen placement uses has the room it needs."""
    return all(get_free_space(d) >= need for _label, d, need in _space_requirements(item, temp_dir, out_dir))


def _fs_status(fs):
    """Filesystem hint for the diagnostics rows: FAT/exFAT warn, NTFS ok, else neutral."""
    if fs in ("exFAT", "FAT32", "FAT"):
        return "warn"
    if fs == "NTFS":
        return "ok"
    return None


def _space_report(item, temp_dir, out_dir, temp_fs="", out_fs=""):
    """(rows, space_ok, banner) for the Drive Space Diagnostics dialog, built from
    _space_requirements - the numbers the pre-flight gate uses. Tk-free, so it is tested
    without opening a window. rows: [(label, value, status)] with status ok/warn/None."""
    rows = [("Game Size", format_size(display_size(item)), None)]
    short = None
    reqs = _space_requirements(item, Path(temp_dir), Path(out_dir))
    for label, d, need in reqs:
        free = get_free_space(d)
        ok = free >= need
        if not ok and short is None:
            short = label
        rows.append((f"{label} needs", format_size(need), None))
        rows.append((f"{label} free", format_size(free), "ok" if ok else "warn"))
    if not reqs:
        rows.append(("Space needed", "size unknown - checked during the run", None))
    bsize = _build_size_of(item)
    if bsize > 0:
        rows.append(("Est. Final Output", f"~{format_size(int(bsize * 0.55))} - {format_size(bsize)}", None))
    rows.append(("Temp Filesystem", temp_fs or " - ", _fs_status(temp_fs)))
    rows.append(("Output Filesystem", out_fs or " - ", _fs_status(out_fs)))
    if short is None:
        return rows, True, "✓  Enough space to proceed."
    return rows, False, f"⚠  {short}: not enough free space - the run would fail."


def get_folder_size(path: Path) -> int:
    return folder_size(path) if path.exists() else 0


def find_newest_ffpfsc_after(folder: Path, started_at: float):
    try:
        if not folder.exists():
            return None
        candidates = []
        # Both the compressed (.ffpfsc) and uncompressed (.ffpfs) deliverables.
        for pat in ("*.ffpfsc", "*.ffpfs"):
            for p in folder.glob(pat):
                try:
                    if p.is_file() and p.stat().st_size > 0 and p.stat().st_mtime >= started_at - 2:
                        candidates.append(p)
                except OSError:
                    pass
        if not candidates:
            return None
        return max(candidates, key=lambda x: x.stat().st_mtime)
    except Exception:
        return None


def compression_rating(saved_pct: float) -> tuple[str, str]:
    if saved_pct >= 25:
        return "EXCELLENT", "Great compression candidate. This title is worth keeping compressed."
    if saved_pct >= 10:
        return "GOOD", "Good result. Compression is likely worth it."
    if saved_pct >= 5:
        return "OKAY", "Small but usable savings. Keep only if storage is tight."
    return "POOR", "Not worth compressing. This title is already highly compressed or not a good candidate."


_DRIVE_TYPE_CACHE: dict = {}


def _drive_cache_key(path: Path):
    try:
        t = path if path.exists() else path.parent
        return os.stat(str(t)).st_dev
    except Exception:
        return str(path)


def get_drive_type(path: Path) -> str:
    """Detect SSD/NVMe vs HDD for the volume holding *path*: 'SSD', 'HDD' or 'Unknown'.
    Cached per device. This MAY SHELL OUT (PowerShell on Windows, diskutil on macOS), so
    call it OFF the UI thread; use drive_type_cached() on the main thread."""
    key = _drive_cache_key(path)
    if key in _DRIVE_TYPE_CACHE:
        return _DRIVE_TYPE_CACHE[key]
    dt = _probe_drive_type(path)
    # Only cache a DEFINITIVE result. A transient "Unknown" (diskutil/df slow or busy under
    # heavy I/O, a just-mounted external drive) must not stick for the whole session and
    # mislabel an SSD - leave it uncached so the next call re-probes when the drive is idle.
    if dt != "Unknown":
        _DRIVE_TYPE_CACHE[key] = dt
    return dt


def drive_type_cached(path: Path) -> str:
    """Non-blocking: the already-probed drive type for *path*, or 'Unknown' if it hasn't
    been probed yet. Safe on the UI thread - never shells out."""
    return _DRIVE_TYPE_CACHE.get(_drive_cache_key(path), "Unknown")


def temp_drive_label(path: Path) -> str:
    """Honest label for the temp/scratch drive: 'SSD temp' ONLY when we have actually
    confirmed solid-state, otherwise the neutral 'temp drive'. We never call a drive an SSD
    on assumption - an external HDD (or an un-probed drive) must not be mislabelled."""
    return "SSD temp" if drive_type_cached(path) == "SSD" else "temp drive"


KEEP_AWAKE_FILENAME = ".ffpfsc_keepalive"


def poke_drive_keepalive(d: Path) -> bool:
    """Force a tiny physical write to the drive holding *d* and flush it to the device, so an idle external HDD doesn't park its heads / spin down."""
    try:
        f = d / KEEP_AWAKE_FILENAME
        with open(f, "wb") as fh:
            fh.write(b"ffpfsc keep-alive\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception:
        return False


def _name_looks_ssd(name: str) -> bool:
    """True if a device/volume name explicitly signals solid-state storage."""
    s = (name or "").lower()
    return any(t in s for t in ("ssd", "nvme", "solid state", "solid-state", "flash disk"))


def _probe_drive_speed(path: Path, name_hint: str = "") -> str:
    """Classify a drive whose OS flash flag is unavailable (USB-attached SSDs through a bridge that masks it - diskutil prints 'Info not available')."""
    import time, tempfile, statistics
    hinted = _name_looks_ssd(name_hint)
    try:
        target = path if path.exists() else path.parent
        if not target.is_dir():
            target = target.parent
        # Require ≥200 MB free so a write-probe never edges a tight volume toward ENOSPC.
        try:
            if shutil.disk_usage(str(target)).free < 200 * 1024 * 1024:
                return "SSD" if hinted else "Unknown"   # can't probe safely - trust the name
        except Exception:
            return "SSD" if hinted else "Unknown"
        # 1) fsync write-latency - the reliable discriminator. Median of small flushed writes
        #    at scattered offsets: SSD ~sub-2 ms, HDD ~5-15 ms (seek+rotation), and a slow bus
        #    does NOT add seek latency, so a hub-throttled USB SSD is still recognised.
        lat_ms = []
        try:
            with tempfile.NamedTemporaryFile(dir=str(target), prefix=".ffpfsc_probe_", delete=True) as tf:
                tf.write(os.urandom(8 * 1024 * 1024)); tf.flush(); os.fsync(tf.fileno())
                blk = os.urandom(4096)   # reused per write - content is irrelevant to latency
                for i in range(16):
                    tf.seek((i * 700_001) % (8 * 1024 * 1024 - 4096))
                    t = time.perf_counter()
                    tf.write(blk); tf.flush(); os.fsync(tf.fileno())
                    lat_ms.append((time.perf_counter() - t) * 1000.0)
        except Exception:
            pass
        med = statistics.median(lat_ms) if lat_ms else None
        # 2) sequential throughput - best of 2 (a real SSD clears the bar on its fastest try).
        best_mbps = 0.0
        buf = os.urandom(32 * 1024 * 1024)   # one incompressible payload, reused per try
        for _ in range(2):
            try:
                with tempfile.NamedTemporaryFile(dir=str(target), prefix=".ffpfsc_probe_", delete=True) as tf:
                    t = time.perf_counter()
                    tf.write(buf); tf.flush(); os.fsync(tf.fileno())
                    dt = time.perf_counter() - t
                best_mbps = max(best_mbps, 32.0 / max(dt, 1e-6))
            except Exception:
                pass
        # Decide by LATENCY, not throughput: low fsync latency = solid-state (no seek),
        # even for a SLOW link - a USB SSD that only sustains ~40 MB/s still flushes in
        # ~ms, whereas a spinning disk pays ~5-15 ms of seek+rotation per flush. So a low
        # throughput must NEVER demote a genuine SSD; only a clearly seek-bound latency
        # signature is called HDD. (A high throughput is just a bonus positive signal.)
        if (med is not None and med < 4.0) or best_mbps >= 150 or hinted:
            return "SSD"
        if med is not None and med > 12.0:
            return "HDD"        # unmistakably rotational (seek+rotation bound)
        return "Unknown"        # caller leans SSD for a USB/external no-flash-flag device
    except Exception:
        return "SSD" if hinted else "Unknown"


def _probe_drive_type(path: Path) -> str:
    """Actually probe the drive type (may block ~1-2 s). See get_drive_type."""
    try:
        target = path if path.exists() else path.parent
    except Exception:
        target = path
    # ── Windows: PowerShell MediaType / BusType ──────────────────────────────
    if os.name == "nt":
        try:
            drive_letter = path.resolve().drive.rstrip(":\\")
            if not drive_letter:
                return "Unknown"
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-Partition -DriveLetter '{drive_letter}' | Get-Disk | Select-Object -ExpandProperty MediaType"],
                capture_output=True, text=True, timeout=6,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            media = result.stdout.strip().upper()
            if "SSD" in media or "NVM" in media:
                return "SSD"
            if "HDD" in media or "UNSPECIFIED" in media:
                if "UNSPECIFIED" in media:
                    result2 = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f"Get-Partition -DriveLetter '{drive_letter}' | Get-Disk | Select-Object -ExpandProperty BusType"],
                        capture_output=True, text=True, timeout=6,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    bus = result2.stdout.strip().upper()
                    if "NVME" in bus or "SATA" in bus:
                        return "SSD"
                    if "ATA" in bus:
                        return "HDD"
                return "HDD"
        except Exception:
            pass
        return "Unknown"
    # ── macOS: diskutil 'Solid State: Yes/No' on the backing device ──────────
    if sys.platform == "darwin":
        try:
            df = subprocess.run(["df", str(target)], capture_output=True, text=True, timeout=6)
            dev = df.stdout.strip().splitlines()[-1].split()[0]   # /dev/diskXsY
            whole = re.sub(r"(s\d+)+$", "", dev)                  # /dev/diskX physical disk (strip APFS synth too)
            saw_info_unavailable = False
            name_hint = ""
            is_usb_external = False
            for d in ([dev, whole] if whole != dev else [dev]):
                info = subprocess.run(["diskutil", "info", d], capture_output=True, text=True, timeout=8)
                for line in info.stdout.splitlines():
                    parts = line.split(":", 1)
                    if len(parts) != 2:
                        continue
                    key = parts[0].strip()
                    val = parts[1].strip()
                    if key == "Solid State":
                        # Compare the value EXACTLY - a substring check false-matches "No"
                        # inside "Info not available".
                        if val == "Yes":
                            return "SSD"
                        if val == "No":
                            return "HDD"
                        # "Info not available" - common on USB-attached SSDs (the bridge
                        # masks the flash bit). Note it and fall through to the probe.
                        saw_info_unavailable = True
                    elif key in ("Device / Media Name", "Volume Name") and val \
                            and not val.lower().startswith("not applicable"):
                        name_hint = (name_hint + " " + val).strip()
                    elif key == "Protocol" and val.upper() == "USB":
                        is_usb_external = True
                    elif key == "Device Location" and val == "External":
                        is_usb_external = True
            if saw_info_unavailable:
                speed_dt = _probe_drive_speed(target, name_hint=name_hint)
                if speed_dt != "Unknown":
                    return speed_dt
                # Ambiguous probe on a USB/external device whose bridge hides the flash flag:
                # portable SSDs report "Info not available" here (a real external HDD reports
                # "No"), so treat it as SSD - a slow-but-solid-state drive (e.g. ~40 MB/s over
                # an old bridge) must stay usable as scratch, not get misrouted as a spinning
                # disk (which is what triggered the false "not enough space" abort).
                if is_usb_external:
                    return "SSD"
                # Non-USB device that hides its flash flag and probes ambiguous: return a
                # DEFINITIVE result so get_drive_type CACHES it - "Unknown" is never cached,
                # so returning it here would re-run the heavy probe on every call. Conservative
                # HDD (a mislabeled internal SSD just routes a bit cautiously; it never
                # re-creates the false abort, which was about a USB drive).
                return "HDD"
        except Exception:
            pass
        return "Unknown"
    # ── Linux: /sys/dev/block rotational flag (0 = SSD, 1 = HDD) ──────────────
    try:
        st = os.stat(str(target))
        node = Path(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
        disk = node.resolve()
        if (disk / "partition").exists():
            disk = disk.parent
        rot = disk / "queue" / "rotational"
        if rot.exists():
            return "HDD" if rot.read_text().strip() == "1" else "SSD"
    except Exception:
        pass
    return "Unknown"


def get_filesystem_type(path: Path) -> str:
    """Return filesystem label (NTFS, exFAT, FAT32 …) using GetVolumeInformationW."""
    if os.name != "nt":
        return "Unknown"
    try:
        import ctypes
        target = path if path.exists() else path.parent
        drive = str(target.resolve()).split("\\")[0] + "\\"  # e.g. "C:\\"
        buf = ctypes.create_unicode_buffer(64)
        ctypes.windll.kernel32.GetVolumeInformationW(
            drive, None, 0, None, None, None, buf, ctypes.sizeof(buf)
        )
        return buf.value.strip() or "Unknown"
    except Exception:
        return "Unknown"


def is_game_folder(path: Path) -> bool:
    """Return True if *path* looks like a PS5 game folder."""
    return path.is_dir() and (path / "sce_sys").is_dir() and (path / "eboot.bin").is_file()


def find_game_folders(root: Path, max_depth: int = 3) -> list[Path]:
    """Recursively find all PS5 game subfolders under *root* (up to max_depth levels)."""
    found: list[Path] = []

    def _scan(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        if is_game_folder(path):
            found.append(path)
            return  # don't recurse inside a game folder
        try:
            for child in sorted(path.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    _scan(child, depth + 1)
        except (PermissionError, OSError):
            pass

    _scan(root, 0)
    return found


JOB_CONTAINER_SUFFIXES = {".ffpfs", ".ffpfsc", ".pkg", ".exfat", ".ffpkg"}
# The app's own scratch folders: never sources, even when a temp folder sits in the tree.
_JOB_SCAN_SKIP_DIRS = {"_ffpfsc_temp", "_extracted", "_ffpfsc_extract", "_ffpfsc_inner", "__MACOSX"}


def is_archive_file(path: Path) -> bool:
    """A .zip / .rar / .7z, or any volume of a multi-part RAR set (.partN.rar, .rNN)."""
    suf = path.suffix.lower()
    return suf in (".zip", ".rar", ".7z") or bool(re.match(r"^\.r\d{2,}$", suf))


def find_job_sources(root: Path, max_depth: int = 6) -> list[Path]:
    """Every source a job can start from under *root*, one entry each: game folders (not
    entered), containers (.ffpfs/.ffpfsc/.pkg/.exfat/.ffpkg) and archives (.zip/.rar/.7z;
    a multi-part RAR set once, as its first volume). Other files (notes, checksums),
    filesystem junk and the app's own scratch folders are skipped. Sorted by path."""
    found: list[Path] = []
    seen: set[str] = set()

    def _scan(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(path.iterdir(), key=lambda q: q.name.lower())
        except OSError:
            return
        for child in children:
            name = child.name
            if name.startswith(".") or is_fs_junk_name(name):
                continue
            try:
                if child.is_dir():
                    if name in _JOB_SCAN_SKIP_DIRS:
                        continue
                    if is_game_folder(child):
                        found.append(child)
                    else:
                        _scan(child, depth + 1)
                elif child.is_file():
                    if child.suffix.lower() in JOB_CONTAINER_SUFFIXES:
                        found.append(child)
                    elif is_archive_file(child):
                        first = ArchiveExtractor._first_volume(child)
                        key = str(first).lower()
                        if key not in seen:
                            seen.add(key)
                            found.append(first)
            except OSError:
                continue

    _scan(Path(root), 0)
    return found


def find_files_by_suffix(root: Path, suffixes: set[str], max_depth: int = 6) -> list[Path]:
    """Recursively find files with selected suffixes without walking unbounded trees."""
    found: list[Path] = []

    def _scan(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                # Skip filesystem junk - a '._image.ffpfs' AppleDouble sidecar carries
                # the real suffix and would otherwise be picked up as a real image.
                if child.name.startswith("._") or child.name == ".DS_Store":
                    continue
                if child.is_file() and child.suffix.lower() in suffixes:
                    found.append(child)
                elif child.is_dir() and not child.name.startswith("."):
                    _scan(child, depth + 1)
        except (PermissionError, OSError):
            pass

    if root.is_file() and root.suffix.lower() in suffixes:
        return [root]
    if root.is_dir():
        _scan(root, 0)
    return found


def has_any_files(root: Path) -> bool:
    try:
        if root.is_file():
            return True
        return any(p.is_file() for p in root.rglob("*"))
    except Exception:
        return False


def validate_game_structure(path: Path) -> list[str]:
    """Return a list of human-readable warnings for incomplete PS5 game folders."""
    warnings: list[str] = []
    sce_sys   = path / "sce_sys"
    param_json = sce_sys / "param.json"
    eboot     = path / "eboot.bin"
    if not sce_sys.is_dir():
        warnings.append("sce_sys folder not found - this may not be a PS5 game dump.")
    elif not param_json.is_file():
        warnings.append("sce_sys/param.json missing - ShadowMount compatibility not guaranteed.")
    if not eboot.is_file():
        warnings.append("eboot.bin not found - the dump may be incomplete.")
    return warnings


# Maps log keywords → user-friendly cause + fix.
# Order matters: smart_error_from_log() returns the FIRST keyword found in the
# log, so the most specific causes (post-pack verify mismatches) come first - 
# otherwise an incidental keyword like "memoryerror" elsewhere in the output
# would mask the real diagnosis.
_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("missing in image",
     "Verify failed: a file in the source folder is missing from the packed image.\n"
     "Open the Logs tab and search for 'missing in image:' to see the exact file."),
    ("extra in image",
     "Verify failed: the image contains a file that is not in the source folder.\n"
     "Open the Logs tab and search for 'extra in image:' to see the exact file."),
    ("flat_path_table",
     "Verify failed: the image's path table does not match the source tree.\n"
     "See the Logs tab for the mismatching entry."),
    ("unable to stage source file",
     "Temp drive does not support hardlinks or symlinks.\n"
     "Fix: use a temp folder on an NTFS-formatted SSD/NVMe."),
    ("hard link and symlink both failed",
     "Temp drive does not support hardlinks or symlinks.\n"
     "Fix: use a temp folder on an NTFS-formatted SSD/NVMe."),
    ("memoryerror",
     "Not enough RAM during compression.\n"
     "Fix: lower CPU cores to 2 or 1, or set compression Level to 5."),
    ("no space left on device",
     "Drive ran out of space mid-compression.\n"
     "Fix: free up space on the temp or output drive."),
    ("no such file or directory",
     "A required file was not found - the game folder may be incomplete."),
    ("could not find any valid game",
     "No valid PS5 game folders detected.\n"
     "Fix: select the folder that contains sce_sys and eboot.bin."),
    ("missing/invalid param.json",
     "param.json is missing or corrupt - not a valid PS5 game dump."),
    ("permission denied",
     "Access denied.\n"
     "Fix: run as administrator, or move files off a read-only drive."),
    ("winerror 5",
     "Access denied (WinError 5).\n"
     "Fix: run as administrator."),
    ("winerror 1",
     "Windows system error (WinError 1).\n"
     "Fix: run as administrator."),
    ("calledprocesserror",
     "A backend subprocess failed - check the raw log for details."),
]


def smart_error_from_log() -> str:
    """Scan the raw log file and return a user-friendly error string, or ''."""
    if not RAW_LOG_FILE.exists():
        return ""
    try:
        text = RAW_LOG_FILE.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        return ""
    for keyword, message in _ERROR_PATTERNS:
        if keyword in text:
            return message
    return ""


def get_backend_python_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--backend-internal"]
    return [sys.executable]


def backend_base_dir() -> Path:
    return _bundled_backend_dir()


def folder_size(path: Path) -> int:
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    except Exception:
        pass
    return total


def file_count(path: Path) -> int:
    try:
        if path.is_file():
            return 1
        return sum(1 for p in path.rglob("*") if p.is_file())
    except Exception:
        return 0


class FolderStats:
    """Everything GameItem needs about a folder, gathered in ONE directory walk instead of
    six recursive globs (size, file count, param.json candidates, artwork) - on a
    hub-throttled USB drive each walk of a 100k-file game costs seconds."""
    ARTWORK_NAMES = ("icon0.png", "pic0.png", "pic1.png")

    def __init__(self, path: Path):
        self.size = 0
        self.count = 0
        self.param_jsons: list[Path] = []
        first_art: dict[str, Path] = {}
        try:
            if path.is_file():
                self.size, self.count = path.stat().st_size, 1
            else:
                for dirpath, _dirnames, filenames in os.walk(path):
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        try:
                            st = os.stat(fp)
                        except OSError:
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        self.size += st.st_size
                        self.count += 1
                        if fn == "param.json":
                            self.param_jsons.append(Path(fp))
                        elif fn in self.ARTWORK_NAMES and fn not in first_art:
                            first_art[fn] = Path(fp)
        except Exception:
            pass
        self.artwork = next((first_art[n] for n in self.ARTWORK_NAMES if n in first_art), None)


def parse_title_id(path: Path, param_jsons: list | None = None) -> str:
    # Prefer the game's OWN folder name, then its param.json; only fall back to the
    # full path last - a title id in a PARENT directory (e.g. a "[CUSA12345]" dump
    # folder) must not win over the game's own id.
    m = TITLE_RE.search(path.name)
    if m:
        return m.group(1).upper()
    try:
        for p in (param_jsons if param_jsons is not None else path.rglob("param.json")):
            text = p.read_text(encoding="utf-8", errors="ignore")
            m = TITLE_RE.search(text)
            if m:
                return m.group(1).upper()
    except Exception:
        pass
    m = TITLE_RE.search(str(path))   # last resort: anywhere in the path
    if m:
        return m.group(1).upper()
    return "Unknown"


def guess_game_name(path: Path) -> str:
    # 1. Try param.json for the real localised title first
    for candidate in (path / "sce_sys" / "param.json",
                      path / "sce_sys" / "param.sfo"):   # sfo handled below
        pass  # only param.json is plaintext
    param = path / "sce_sys" / "param.json"
    if param.exists():
        try:
            import json as _json
            data = _json.loads(param.read_text(encoding="utf-8", errors="replace"))
            # param.json structure: {"titleId":..., "localizedParameters":{"defaultLanguage":"en-US", "en-US":{"titleName":"..."}}}
            loc = data.get("localizedParameters", {})
            default_lang = loc.get("defaultLanguage", "")
            title = (loc.get(default_lang, {}).get("titleName", "")
                     or loc.get("en-US", {}).get("titleName", "")
                     or next((v.get("titleName", "") for v in loc.values()
                               if isinstance(v, dict) and v.get("titleName")), ""))
            if title:
                return title.strip()
        except Exception:
            pass

    # 2. Fall back to folder name, cleaning up common PS5 dump suffixes
    name = path.name
    # Strip "-app" / "_app" suffix (e.g. PPSA00001-app → PPSA00001)
    # Do NOT use parent folder - it is often a generic dump dir like "PS5 DUMPS"
    name = re.sub(r"[-_]app$", "", name, flags=re.I)
    name = re.sub(r"\s*\[.*?\]\s*", " ", name)
    name = re.sub(r"-\[.*?\]", "", name)
    return name.replace("_", " ").strip(" -") or path.name


def guess_game_version(path: Path) -> str:
    """Best-effort game/content version, or '' if unknown."""
    # X.YY / X.YYY with an optional third group (the full PS5 XX.YYY.ZZZ version).
    _VER = r"\d{1,2}\.\d{2,3}(?:\.\d{2,3})?"
    try:
        param = path / "sce_sys" / "param.json"
        if param.exists():
            data = json.loads(param.read_text(encoding="utf-8", errors="replace"))
            for key in ("contentVersion", "masterVersion", "appVer", "app_ver", "version"):
                v = str(data.get(key, "")).strip()
                if re.fullmatch(_VER, v):
                    return v
    except Exception:
        pass
    # Fall back to a version pattern in the source folder name (e.g. "…01.004…")
    m = re.search(rf"\bv?({_VER})\b", path.name)
    return m.group(1) if m else ""


# ── AMPR / APR (PlayGo) support ───────────────────────────────────────────────
# APR = a PlayGo game (streamed/chunked delivery, marked by sce_sys/playgo-chunk.dat).
# AMPR = the emu shim it needs to boot from a compressed container: two user-supplied
# .sprx files injected into a fakelib/ folder, plus an ampr_emu.index. No file format - 
# a game category + injected runtime files. See _build_ampr_index for the index layout.
AMPR_SPRX_FILES = ["libSceAmpr.sprx", "libScePlayGo.sprx"]


def is_apr_game(path) -> bool:
    """True if *path* is a game folder using PlayGo (an APR title)."""
    try:
        sce = Path(path) / "sce_sys"
        return (sce / "playgo-chunk.dat").exists() or (sce / "playgo_chunk.dat").exists()
    except Exception:
        return False


# Two filename-length ceilings, BOTH in UTF-8 BYTES (the filesystem and ShadowMountPlus
# checks count bytes, not characters - a "™" is 3 bytes, not 1):
#  • MAX_FILENAME_BYTES - the filesystem hard cap (exFAT 255 UTF-16 units / APFS 255 bytes).
#    Used by the general sanitiser so no path component is ever filesystem-illegal.
#  • SHADOWMOUNT_NAME_LIMIT - the stricter limit ShadowMountPlus enforces on the .ffpfsc
#    FILENAME: it rejects longer names with ENAMETOOLONG ("Dateiname zu lang"). EMPIRICAL
#    (2026-06-21): a 59-byte name mounts, a 69-byte name fails → the real cap is ~64. Set
#    conservatively to 63 (one under the likely char[64] buffer) so generated names always
#    fit. The output namer budgets against THIS value - change it in one place if the exact
#    constant turns out different.
MAX_FILENAME_BYTES = 255
SHADOWMOUNT_NAME_LIMIT = 63


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    """Trim *s* so its UTF-8 encoding is <= *max_bytes*, never splitting a character."""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", "ignore")


def short_version(ver: str) -> str:
    """Collapse a PS5 version to two groups and drop any leading 'v' for filenames:
    'v01.007.000' -> '01.007', '02.001.010' -> '02.001', '01.030' -> '01.030'."""
    if not ver:
        return ver
    m = re.match(r"v*(\d{1,2}\.\d{2,3})", ver)
    return m.group(1) if m else ver.lstrip("v")


def sanitize_filename(s: str) -> str:
    """Make *s* safe as a cross-platform filename component (keeps spaces,
    brackets, &, etc.; strips path separators and reserved characters), and cap it
    to the filesystem's per-name limit."""
    s = s.replace("/", "-").replace("\\", "-").replace(":", "-")
    s = re.sub(r"[™®©℠℗]", "", s)   # ™ ® © ℠ ℗ - waste bytes, no value on a console drive
    s = re.sub(r'[*?"<>|\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return _truncate_to_bytes(s, MAX_FILENAME_BYTES).strip()


# Redundant "edition" qualifiers dropped from an output filename ONLY when the full name
# would otherwise exceed SHADOWMOUNT_NAME_LIMIT (so games that fit keep their full title).
# Longest phrases first; a separator in front (hyphen, en or em dash, colon) goes too.
_EDITION_FLUFF_RE = re.compile(
    r"\s*[-\u2013\u2014:]?\s*\b("
    r"\d{1,3}(?:st|nd|rd|th)\s+anniversary\s+edition"
    r"|game\s+of\s+the\s+year\s+edition|goty\s+edition"
    r"|complete\s+edition|definitive\s+edition|enhanced\s+edition"
    r"|deluxe\s+edition|ultimate\s+edition|standard\s+edition"
    r"|special\s+edition|gold\s+edition|premium\s+edition"
    r"|anniversary\s+edition|remastered|remaster"
    r")\b",
    re.IGNORECASE)


def _strip_edition_fluff(name: str) -> str:
    """Remove redundant 'edition'/'remastered' qualifiers and tidy leftover separators.
    Used only as a fallback when a name is over the ShadowMount length budget."""
    out = _EDITION_FLUFF_RE.sub("", name)
    out = re.sub(r"\s{2,}", " ", out).strip(" -\u2013\u2014:")
    return out


def descriptive_ffpfsc_name(item, ext: str = ".ffpfsc", *,
                            name_override: str | None = None,
                            tid_override: str | None = None,
                            ver_override: str | None = None,
                            v_prefix: bool = False,
                            fw_override: str | None = None) -> str:
    """Build a findable output filename for *item*: '<Game Name> [<TITLEID>] [v<version>]<ext>' (version omitted if unknown)."""
    if not ext.startswith("."):
        ext = "." + ext
    PLACEHOLDERS = {"Unknown", "📦", "💾", "📤", ""}
    tid = (tid_override if tid_override is not None else (getattr(item, "title_id", "") or "")).strip()
    if tid in PLACEHOLDERS:
        tid = ""
    # Prefer the stable friendly name (the bundle/folder name the user saw, or a folder
    # pack's param.json title) over item.name, which collapses to the extracted stem
    # (e.g. "PPSA00001") after an archive/bundle is unpacked. This makes the .ffpfsc
    # named after the GAME, the same as packing a folder directly.
    name = (name_override if name_override is not None
            else (getattr(item, "display_name", "") or getattr(item, "name", "") or "")).strip()
    # The friendly name is often a release/bundle FOLDER that already carries bracketed
    # metadata, e.g. "a retail reference title [PPSA00001] [v01.200.007]". Strip any title-id bracket
    # and any version bracket here so they are re-added once, canonically, below - 
    # otherwise the version (and id) would show up twice in the filename.
    name = re.sub(r"\s*\[\s*(?:PPSA|CUSA)\d{5}\s*\]", "", name, flags=re.I)
    name = re.sub(r"\s*\[\s*[vV]?\d{1,2}(?:\.\d{1,3}){1,3}\s*\]", "", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -_")
    if name in PLACEHOLDERS or name == tid:
        name = tid or "output"
    elif tid:
        # Drop a bare title-id embedded at the end of the name (a dump folder like
        # "Example Title Remastered PPSA00001") so it is re-added cleanly as
        # [TITLEID] below - matching the "Name [TITLEID] [ver]" library convention.
        stripped = re.sub(r"[\s_\-]*\b" + re.escape(tid) + r"\b[\s_\-]*$", "",
                          name, flags=re.I).strip(" -_")
        if stripped:
            name = stripped
    suffix_parts = []
    if tid and tid.lower() not in name.lower():
        suffix_parts.append(f"[{tid}]")
    # Cache the version on the item the first time we can read it (source intact). An OOM
    # resume DELETES the source folder, so a live re-read would drop the [v…] tag and the
    # resume would then write a DIFFERENTLY-named .ffpfsc (orphaning the original instead of
    # overwriting it). Prefer the cached tag; only (re)compute while the source still exists.
    if ver_override is not None:
        ver = ver_override
    else:
        ver = (getattr(item, "_ver_tag", "") or "") if item is not None else ""
        if not ver:
            src = getattr(item, "path", None)
            ver = guess_game_version(src) if isinstance(src, Path) else ""
            if ver and item is not None:
                try:
                    item._ver_tag = ver
                except Exception:
                    pass
    if ver:
        # Shortened version tag (e.g. [01.007]) - matches shorten_ffpfsc_versions.sh;
        # the auto-organize layout writes it as [v01.007].
        suffix_parts.append(f"[{'v' if v_prefix else ''}{short_version(ver)}]")
    if fw_override:
        suffix_parts.append(f"[fw{fw_override}]")
    suffix = (" " + " ".join(suffix_parts)) if suffix_parts else ""
    # Reserve room (by UTF-8 bytes) for the [version][TITLEID] suffix + extension so those
    # collision-resistant tags survive the filename-length cap instead of being truncated.
    if item is not None:
        try:
            item._name_was_truncated = False
            item._name_fluff_stripped = False
        except Exception:
            pass
    name_budget = max(20, SHADOWMOUNT_NAME_LIMIT - len(suffix.encode("utf-8")) - len(ext.encode("utf-8")))
    clean = sanitize_filename(name)
    fluff_stripped = False
    if len(clean.encode("utf-8")) > name_budget:
        # Over budget - first drop redundant edition qualifiers (much nicer than a blunt
        # cut). Only adopt the result if it actually shortened to something non-empty.
        reduced = sanitize_filename(_strip_edition_fluff(clean))
        if reduced and len(reduced.encode("utf-8")) < len(clean.encode("utf-8")):
            clean = reduced
            fluff_stripped = True
    trunc = _truncate_to_bytes(clean, name_budget)
    truncated = len(trunc.encode("utf-8")) < len(clean.encode("utf-8"))
    if item is not None:
        try:
            item._name_was_truncated = truncated
            item._name_fluff_stripped = fluff_stripped and not truncated
        except Exception:
            pass
    name = trunc.strip() or "output"
    return sanitize_filename(name + suffix) + ext


# ── Auto-organize: library names from the game's own metadata ─────────────────
def ident_from_param_bytes(data) -> dict | None:
    """{'title', 'title_id', 'version'} from the bytes of a sce_sys/param.json (Sony's
    layout: the title of the default language, else any language, else titleName). None
    when the data is no param.json or names neither a title nor a title id."""
    try:
        d = json.loads(data.decode("utf-8-sig", errors="replace") if isinstance(data, (bytes, bytearray)) else data)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    title = ""
    lp = d.get("localizedParameters") or {}
    if isinstance(lp, dict):
        lang = lp.get("defaultLanguage")
        block = lp.get(lang) if isinstance(lang, str) else None
        if isinstance(block, dict):
            title = str(block.get("titleName") or "")
        if not title:
            for v in lp.values():
                if isinstance(v, dict) and v.get("titleName"):
                    title = str(v["titleName"]); break
    if not title:
        title = str(d.get("titleName") or "")
    tid = str(d.get("titleId") or "").strip().upper()
    ver = str(d.get("contentVersion") or d.get("masterVersion") or "").strip()
    if not (title or tid):
        return None
    return {"title": title.strip(), "title_id": tid, "version": ver}


def canonical_game_title(title: str) -> str:
    """A game title as it appears in a library name: no ™®©℠℗, 'A: B' → 'A - B'
    (a bare colon would become 'A- B' through the filename sanitiser), tidy spaces."""
    t = re.sub(r"[™®©℠℗]", "", title or "")
    t = re.sub(r"\s*:\s+", " - ", t)
    t = t.replace(":", "-")
    t = re.sub(r"\s{2,}", " ", t).strip(" -_.")
    return t


def organized_names(ident: dict, ext: str, item=None) -> tuple[str, str]:
    """The auto-organize layout for one game - (folder, file): '<Title> [<TITLEID>] [vXX.YYY.ZZZ]' the per-game folder (full version) '<Title> [<TITLEID>] [vXX.YYY] [fwN.NN]<ext>' the file inside it (short version, the firmware the game needs) *ident* carries 'title', 'title_id', 'version' as read from param.json and 'fw' from the executable's SDK version (any may be empty; missing tags are simply left out)."""
    title = canonical_game_title(ident.get("title") or "")
    tid = (ident.get("title_id") or "").strip().upper()
    ver = (ident.get("version") or "").strip().lstrip("vV")
    base = title or tid or "output"
    # Without a title the id already names the folder; do not repeat it as a tag.
    tid_tag = f"[{tid}]" if tid and tid.lower() not in base.lower() else ""
    folder = sanitize_filename(" ".join(p for p in (base, tid_tag, f"[v{ver}]" if ver else "") if p))
    fname = descriptive_ffpfsc_name(item, ext, name_override=base, tid_override=tid, ver_override=ver, v_prefix=True,
                                    fw_override=(ident.get("fw") or "").strip() or None)
    return folder, fname


PS4_DLC_PACK_FROM = 4          # from this many DLCs of one title on they go into "DLC Pack/"
PS4_DLC_PACK = "DLC Pack"


class Ps4Library(NamedTuple):
    """A title folder that is already in the PS4 library: its name, the version in its
    '[vXX.YY]' tag ('' when it has none) and whether it holds a 'DLC Pack' folder."""
    folder: str
    version: str
    has_dlc_pack: bool


_PS4_TID_TAG = re.compile(r"\[(CUSA\d{5})\]", re.I)
_PS4_VER_TAG = re.compile(r"\[v(\d+(?:\.\d+)*)\]", re.I)


def ps4_folder_with_version(folder: str, version: str) -> str:
    """*folder* with its '[vXX.YY]' tag set to *version* (added after the id tag when absent)."""
    if _PS4_VER_TAG.search(folder):
        return _PS4_VER_TAG.sub(f"[v{version}]", folder, count=1)
    m = _PS4_TID_TAG.search(folder)
    if m:
        return folder[:m.end()] + f" [v{version}]" + folder[m.end():]
    return f"{folder} [v{version}]"


def scan_ps4_library(root) -> dict:
    """{CUSA id: Ps4Library} for the title folders directly under *root*. A folder counts
    when its name carries a '[CUSA12345]' tag; OS clutter is skipped; the first folder per
    id wins (sorted by name)."""
    found: dict = {}
    try:
        entries = sorted(Path(root).iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return found
    for d in entries:
        if not d.is_dir() or is_fs_junk_name(d.name):
            continue
        m = _PS4_TID_TAG.search(d.name)
        if not m or m.group(1).upper() in found:
            continue
        v = _PS4_VER_TAG.search(d.name)
        found[m.group(1).upper()] = Ps4Library(d.name, v.group(1) if v else "", (d / PS4_DLC_PACK).is_dir())
    return found


def _ver_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v or "0"))


def _dlc_name(dlc_title: str, *game_titles: str) -> str:
    """The DLC's own part of its title: '<Game> - Extra Pack' -> 'Extra Pack' (the longest
    of *game_titles* that starts it is cut)."""
    t = canonical_game_title(dlc_title)
    for g in sorted((g for g in game_titles if g), key=len, reverse=True):
        if t.lower().startswith(g.lower()):
            t = t[len(g):]
            break
    return t.strip(" -\u2013\u2014:_.") or canonical_game_title(dlc_title)


def ps4_layout(items, known: dict | None = None):
    """Library placement of PS4 packages, grouped by title id: '<Title> [CUSA…] [vX]/<Title> [CUSA…] [v01.00].pkg' game '…/<Title> [CUSA…] UPDATE [v01.07].pkg' update '…/<Title> DLC <name> [CUSA…] [v01.07].pkg' DLC (game/update version) '…/DLC Pack/…' from PS4_DLC_PACK_FROM DLCs on The folder carries the highest version of the set."""
    known = known or {}
    groups: dict[str, list] = {}
    for src, ident in items:
        groups.setdefault((ident.title_id or "").upper(), []).append((Path(src), ident))
    out = []
    for tid, members in groups.items():
        base_ids = [i for _, i in members if i.kind in ("game", "update")]
        game = next((i for _, i in members if i.kind == "game"), None) or (base_ids[0] if base_ids else None)
        if game:
            title = canonical_game_title(game.title) or tid
        else:                                            # no game in the set: the title before " - "
            first = canonical_game_title(members[0][1].title or "")
            title = re.split(r"\s+[-\u2013\u2014]\s+", first, maxsplit=1)[0].strip() or tid
        set_ver = max((i.version for i in base_ids if i.version), key=_ver_key, default="")
        folder_ver = set_ver or max((i.version for _, i in members if i.version), key=_ver_key, default="")
        dlc_count = sum(1 for _, i in members if i.kind == "dlc")
        dlc_sub = PS4_DLC_PACK if dlc_count >= PS4_DLC_PACK_FROM else ""
        pkg_title = title                                # as the packages spell it (for the DLC cut)
        lib = known.get(tid)
        if lib:
            title = lib.folder.split(f"[{tid}]", 1)[0].strip() or title
            top = max((v for v in (lib.version, set_ver) if v), key=_ver_key, default="")
            raise_tag = bool(set_ver) and _ver_key(set_ver) > _ver_key(lib.version or "0")
            folder = ps4_folder_with_version(lib.folder, top) if raise_tag else lib.folder
            set_ver = top
            if lib.has_dlc_pack:
                dlc_sub = PS4_DLC_PACK
        else:
            folder = sanitize_filename(" ".join(p for p in (title, f"[{tid}]", f"[v{folder_ver}]" if folder_ver else "") if p))
        for src, i in members:
            sub = ""
            if i.kind == "update":
                name = f"{title} [{tid}] UPDATE" + (f" [v{i.version}]" if i.version else "")
            elif i.kind == "dlc":
                ver = set_ver or i.version or ""
                name = f"{title} DLC {_dlc_name(i.title, title, pkg_title)} [{tid}]" + (f" [v{ver}]" if ver else "")
                sub = dlc_sub
            elif i.kind == "game":
                name = f"{title} [{tid}]" + (f" [v{i.version}]" if i.version else "")
            else:
                name = canonical_game_title(i.title) or src.stem
            out.append((src, folder, sub, sanitize_filename(name) + ".pkg"))
    return out


# ── Library layout for every format (Organize) ────────────────────────────────
LIB_IMAGE_KINDS = ("ffpfs", "ffpfsc", "exfat", "ffpkg")


class LibItem(NamedTuple):
    """One thing Organize recognised: a game folder, an image, a package or an archive set.
    *key* is its path (the first volume for an archive set); *kind* folder | ffpfs | ffpfsc |
    exfat | ffpkg | pkg | archive; *role* game | update | dlc | backport | other; *ps4* the
    Ps4Identity of a PS4 package, folder or archive (None for PS5)."""
    key: Path
    kind: str
    role: str
    title: str
    title_id: str
    version: str
    fw: str = ""
    ps4: object = None


def _lib_version_tag(folder: str) -> str:
    v = _PS4_VER_TAG.search(folder or "")
    return v.group(1) if v else ""


def library_layout(items, known: dict | None = None):
    """Where each recognised item belongs in a library: [(key, title folder, subdir, name)]."""
    known = known or {}
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault((it.title_id or "").upper(), []).append(it)
    out = []
    for tid, members in groups.items():
        lib = known.get(tid)
        if tid.startswith("CUSA"):
            with_ident = [it for it in members if it.ps4 is not None]
            placed = {p: (f, s, n) for p, f, s, n in
                      ps4_layout([(it.key, it.ps4) for it in with_ident], known={tid: lib} if lib else None)}
            folder = next(iter(placed.values()))[0] if placed else (lib.folder if lib else sanitize_filename(
                " ".join(p for p in (canonical_game_title(members[0].title) or tid, f"[{tid}]") if p)))
            for it in members:
                f, s, n = placed.get(Path(it.key), (folder, "", it.key.name))
                if it.kind == "folder":
                    n, s = n[:-4] if n.lower().endswith(".pkg") else n, ""
                elif it.kind == "archive":
                    n, s = Path(it.key).name, ""
                out.append((it.key, f, s, n))
            continue
        base = [it for it in members if it.role in ("game", "update")]
        main = max((it for it in members if it.role == "game"), key=lambda it: _ver_key(it.version), default=None) \
            or (base[0] if base else members[0])
        title = canonical_game_title(main.title) or tid
        set_ver = max((it.version for it in base if it.version), key=_ver_key, default="")
        top = set_ver or max((it.version for it in members if it.version), key=_ver_key, default="")
        if lib and lib.version and _ver_key(lib.version) > _ver_key(top or "0"):
            top = lib.version
        folder = organized_names({"title": title, "title_id": tid, "version": top}, "")[0]
        dlcs = sum(1 for it in members if it.role == "dlc" and it.kind == "pkg")
        dlc_sub = PS4_DLC_PACK if dlcs >= PS4_DLC_PACK_FROM or (lib and lib.has_dlc_pack) else ""
        for it in members:
            ident = {"title": title, "title_id": tid, "version": it.version, "fw": it.fw}
            sub = ""
            if it.kind == "archive" or it.role == "other":
                name = Path(it.key).name
            elif it.kind == "folder":
                name = organized_names(ident, "")[0]
            elif it.kind == "pkg" and it.role == "update":
                name = sanitize_filename(f"{title} [{tid}] UPDATE" + (f" [v{short_version(it.version)}]" if it.version else "")) + ".pkg"
            elif it.kind == "pkg" and it.role == "dlc":
                ver = set_ver or it.version
                name = sanitize_filename(f"{title} DLC {_dlc_name(it.title, title)} [{tid}]"
                                         + (f" [v{short_version(ver)}]" if ver else "")) + ".pkg"
                sub = dlc_sub
            elif it.kind == "pkg" and it.role == "backport":
                name = sanitize_filename(f"{title} BACKPORT" + (f" [fw{it.fw}]" if it.fw else "") + f" [{tid}]") + ".pkg"
            else:
                name = organized_names(ident, "." + it.kind)[1]
            out.append((it.key, folder, sub, name))
    return out


# ── Cover art cache ───────────────────────────────────────────────────────────
# A job's cover (sce_sys/icon0.png) is read once, as early as its source allows (a folder,
# an archive read alone, an image, a package), and kept as a small PNG in the app folder.
# The job carries only the key, so the cover outlives the job's extraction and a restart.
ART_DIR = APP_DIR / "art"
ART_STORE_PX = 256              # stored size: twice the largest size shown
_ART_KEEP_DAYS = 30


def art_source_key(item) -> str:
    """The cache key of *item*'s cover: its original source (the archive it came from, else
    its own path). '' when the job has no source."""
    src = (getattr(item, "origin_archive", None) or getattr(item, "archive_path", None)
           or getattr(item, "path", None))
    src = str(src or "").strip()
    return hashlib.sha1(src.encode("utf-8", "replace")).hexdigest()[:20] if src else ""


def art_cache_file(key: str) -> Path | None:
    """The cached cover for *key*, or None when there is none."""
    if not key:
        return None
    f = ART_DIR / f"{key}.png"
    return f if f.is_file() else None


def store_art(key: str, data) -> Path | None:
    """Keep *data* (PNG/JPEG bytes or a path to one) as the cover for *key*, scaled down to
    ART_STORE_PX. None when it is no readable picture. Safe off the main thread (no Tk)."""
    if not key or not data:
        return None
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else str(data))
        img = img.convert("RGBA")
        img.thumbnail((ART_STORE_PX, ART_STORE_PX))
        ART_DIR.mkdir(parents=True, exist_ok=True)
        dest = ART_DIR / f"{key}.png"
        tmp = dest.with_name(f"{key}.{os.getpid()}.{threading.get_ident()}.tmp")
        img.save(tmp, "PNG")
        os.replace(tmp, dest)
        return dest
    except Exception:
        return None


def prune_art_cache(keep_keys) -> int:
    """Remove cached covers that no job uses and nobody looked at for _ART_KEEP_DAYS days."""
    keep = set(keep_keys or ())
    cutoff = time.time() - _ART_KEEP_DAYS * 86400
    n = 0
    try:
        files = list(ART_DIR.iterdir())
    except OSError:
        return 0
    for f in files:
        try:
            if f.stem not in keep and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def find_artwork(path: Path):
    if path.is_file():
        return None
    for name in ["icon0.png", "pic0.png", "pic1.png"]:
        try:
            hits = list(path.rglob(name))
            if hits:
                return hits[0]
        except Exception:
            pass
    return None


def load_history():
    ensure_app_dir()
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(items):
    ensure_app_dir()
    HISTORY_FILE.write_text(json.dumps(items[-100:], indent=2), encoding="utf-8")


_SETTINGS_UNREADABLE = False      # settings.json exists but does not parse
_SETTINGS_CORRUPT_COPY: Path | None = None   # where the unreadable file was moved


def load_settings() -> dict:
    """Parsed settings.json, or {} when it does not exist. A file that exists but does
    not parse is flagged so save_settings() keeps it aside instead of overwriting the
    whole profile (queue, passwords, drives) with the single key being saved."""
    global _SETTINGS_UNREADABLE
    ensure_app_dir()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            _SETTINGS_UNREADABLE = False
            return data if isinstance(data, dict) else {}
        except Exception:
            _SETTINGS_UNREADABLE = True
    return {}


def save_settings(data: dict) -> None:
    """Merge *data* into settings.json atomically: write settings.json.tmp, then
    os.replace, so a crash or power loss mid-write never leaves a torn file."""
    global _SETTINGS_CORRUPT_COPY
    ensure_app_dir()
    existing = load_settings()
    if _SETTINGS_UNREADABLE:
        try:
            keep = SETTINGS_FILE.with_name(f"settings.json.corrupt-{int(time.time())}")
            SETTINGS_FILE.replace(keep)
            _SETTINGS_CORRUPT_COPY = keep
        except Exception:
            pass
    existing.update(data)
    tmp = SETTINGS_FILE.with_name("settings.json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


def is_first_run() -> bool:
    return not SETTINGS_FILE.exists()


def get_last_log_lines(n: int = 50) -> str:
    try:
        if RAW_LOG_FILE.exists():
            lines = RAW_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
    except Exception:
        pass
    return ""




# ─── Archive Extractor ─────────────────────────────────────────────────────────

class ArchiveExtractionCancelled(RuntimeError):
    """Raised when the user cancels an archive extraction."""


class ArchivePasswordError(RuntimeError):
    """An extractor reported a wrong or missing archive password. Its own type so
    extract_with_passwords() moves on to the next candidate instead of surfacing
    it as a generic failure."""


class ArchiveToolError(RuntimeError):
    """A CLI extractor exited non-zero for a reason other than the password. The exit
    status is kept so callers can tell 'could not run at all' (7-Zip exit 7 = command
    line error) from a data error inside the archive."""

    def __init__(self, message: str, returncode: int = 1):
        super().__init__(message)
        self.returncode = returncode


class ArchiveExtractor:
    """Extract ZIP / RAR / 7z to a temp subfolder and return the game root Path."""

    SUPPORTED = {".zip", ".rar", ".7z"}

    # How the CLI tools word a wrong or missing password. 7-Zip: "Cannot open encrypted
    # archive. Wrong password?", "Data Error in encrypted file. Wrong password?",
    # "ERROR: Wrong password : <file>"; a bare "Enter password" prompt means it asked
    # for one we did not pass. UnRAR: "Incorrect password for <archive>", "The specified
    # password is incorrect.", "(password incorrect ?)", "Corrupt file or wrong password."
    _CLI_PASSWORD_RE = re.compile(
        r"wrong password|enter password|incorrect password|password (?:is )?incorrect", re.I)

    @staticmethod
    def extract(archive: Path, dest_root: Path, log_fn=None, progress_fn=None,
                password: str = "", cancel_event: threading.Event | None = None) -> Path:
        """Extract *archive* under *dest_root/<stem>* and return the extracted root.
        progress_fn(pct, filename) is called periodically.
        password is used for encrypted archives (ZIP/RAR/7z)."""
        # Unique per-archive subfolder. Two queued archives that share a stem
        # (Game.zip + Game.rar, or same-named releases from different folders)
        # must not extract into - and rmtree - each other's tree. The digest of
        # the absolute path keeps it stable, so re-extracting the same archive
        # reuses (and refreshes) its own folder.
        digest = hashlib.sha1(str(archive.resolve()).encode("utf-8", "replace")).hexdigest()[:8]
        dest = dest_root / f"{archive.stem}__{digest}"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        suffix = archive.suffix.lower()
        if log_fn:
            log_fn("INFO", f"Extracting {archive.name} → {dest}"
                   + (" [password-protected]" if password else ""))
        ArchiveExtractor._check_cancel(cancel_event)
        try:
            if suffix == ".zip":
                ArchiveExtractor._zip(archive, dest, log_fn, progress_fn, password, cancel_event)
            elif suffix == ".rar":
                ArchiveExtractor._rar(archive, dest, log_fn, progress_fn, password, cancel_event)
            elif suffix == ".7z":
                ArchiveExtractor._sevenz(archive, dest, log_fn, progress_fn, password, cancel_event)
            else:
                raise ValueError(f"Unsupported archive format: {archive.suffix}")
        except BaseException:
            # Never leave a half-written tree behind (failed/cancelled/wrong-password
            # attempt) - the next password candidate / run starts from a clean dest.
            shutil.rmtree(dest, ignore_errors=True)
            raise
        ArchiveExtractor._check_cancel(cancel_event)
        # Whatever clutter the extractor wrote goes now, before the game root is looked for:
        # the RAR and 7z readers extract whole, and an exFAT temp drive adds AppleDouble
        # sidecars of its own.
        junk = strip_fs_junk(dest)
        if junk and log_fn:
            log_fn("INFO", f"Removed {junk} OS clutter file(s)/folder(s) from the extracted archive.")
        if not has_any_files(dest):
            raise RuntimeError(
                f"Archive extraction produced no files: {archive.name}\n\n"
                "The archive may be empty, encrypted with a wrong password, or the required macOS extractor failed."
            )
        game_root = ArchiveExtractor._find_root(dest, log_fn=log_fn)
        if log_fn:
            log_fn("OK", f"Extracted to: {game_root}")
        return game_root

    @staticmethod
    def _is_password_error(e: Exception) -> bool:
        """True if *e* signals a wrong/missing archive password (any format).

        Type checks first; the string match is phrase-anchored so an unrelated
        error mentioning a file like ``passwords.txt`` or an offset like
        ``error 224`` does not get misread as a password failure."""
        # A bare PermissionError is a real filesystem permission problem, NOT a wrong
        # password - no archive lib here reports a bad password that way (ZIP raises
        # RuntimeError("Bad password"), py7zr/RAR raise their own types or error 22/24).
        # Letting it fall through to the message regex below means a password-mentioning
        # error still counts, but a genuine FS error surfaces correctly instead of being
        # mislabelled "wrong password" (which would also burn every saved password).
        if isinstance(e, ArchivePasswordError):
            return True     # a CLI / py7zr verdict the extractor already classified
        if type(e).__name__ in ("RarWrongPassword", "PasswordRequired",
                                 "WrongPassword", "BadPassword", "PasswordError"):
            return True
        low = str(e).lower()
        return bool(re.search(
            r"(?:wrong|bad|missing|incorrect|no)[ -]?password"
            r"|password[ -]?(?:required|incorrect|protected|needed|wrong)"
            r"|enter the correct password"
            r"|\berror 2[24]\b",        # UnRAR 22 = missing pwd, 24 = bad pwd
            low,
        ))

    @staticmethod
    def extract_with_passwords(archive: Path, dest_root: Path, passwords: list[str],
                               log_fn=None, progress_fn=None,
                               cancel_event: threading.Event | None = None) -> Path:
        """Extract *archive*, trying each candidate password in order until one works."""
        named = []
        for p in passwords:
            p = (p or "").strip()
            if p and p not in named:
                named.append(p)
        candidates = named + [""]   # always end with a clean no-password attempt
        for idx, pwd in enumerate(candidates):
            ArchiveExtractor._check_cancel(cancel_event)
            try:
                return ArchiveExtractor.extract(
                    archive, dest_root, log_fn=log_fn, progress_fn=progress_fn,
                    password=pwd, cancel_event=cancel_event,
                )
            except ArchiveExtractionCancelled:
                raise
            except Exception as e:
                if ArchiveExtractor._is_password_error(e):
                    if log_fn and pwd and len(candidates) > 1:
                        log_fn("INFO", f"  password {idx + 1}/{len(named)} did not match - trying next…")
                    continue
                raise   # not a password problem - surface it
        if named:
            raise RuntimeError(
                f"Could not open {archive.name}: wrong or missing password - none of the "
                f"{len(named)} saved password(s) worked.\n\n"
                "Add the correct password in Settings → Saved Archive Passwords "
                "(or in the 'Archive Password' field) and try again."
            )
        raise RuntimeError(
            f"Could not open {archive.name}: it is password-protected and no password "
            "is saved.\n\nAdd the password in Settings → Saved Archive Passwords "
            "(or in the 'Archive Password' field) and try again."
        )

    @staticmethod
    def read_game_param(archive: Path, passwords=None) -> bytes | None:
        """The game's sce_sys/param.json read out of *archive* alone, the other members skipped: so the job knows the game's name and its output before anything is unpacked."""
        return ArchiveExtractor.read_shallowest(archive, "sce_sys/param.json", passwords)

    @staticmethod
    def read_shallowest(archive: Path, tail: str, passwords=None) -> bytes | None:
        """The shallowest member of *archive* whose path ends in *tail* ("eboot.bin",
        "sce_sys/param.json"), read alone; the same rules as read_game_param."""
        archive = Path(archive)
        if re.match(r"^\.r\d{2,}$", archive.suffix.lower()) or archive.suffix.lower() == ".rar":
            archive = ArchiveExtractor._first_volume(archive)
        suffix = archive.suffix.lower()
        if suffix not in (".zip", ".rar", ".7z"):
            return None
        pwds = [p.strip() for p in (passwords or []) if p and p.strip()]
        names = ArchiveExtractor.list_members(archive, pwds)
        want = tail.lower()
        hits = sorted((n for n in names if n.lower().rstrip("/") == want or n.lower().rstrip("/").endswith("/" + want)),
                      key=lambda n: (n.count("/"), len(n)))
        if not hits:
            return None
        name = hits[0]
        cands = pwds + [""]
        try:
            if suffix == ".zip":
                with zipfile.ZipFile(archive, "r") as zf:
                    for pwd in [""] + pwds:
                        try:
                            return zf.read(name, pwd=pwd.encode() if pwd else None)
                        except RuntimeError:             # ZipCrypto: wrong / missing password
                            continue
                        except NotImplementedError:      # AES: the stdlib cannot decrypt it
                            return None
                return None
            if suffix == ".rar":
                backend_dir = backend_base_dir()
                if str(backend_dir) not in sys.path:
                    sys.path.insert(0, str(backend_dir))
                from unrar import rarfile as _br  # type: ignore
                for pwd in cands:
                    try:
                        return _br.RarFile(str(archive), pwd=pwd or None).read(name)
                    except _br.RarWrongPassword:
                        continue
                    except Exception:
                        return None                      # solid, damaged, a part missing
                return None
            import py7zr  # type: ignore
            for pwd in cands:
                try:
                    kwargs = {"password": pwd} if pwd else {}
                    with py7zr.SevenZipFile(str(archive), mode="r", **kwargs) as sz:
                        if getattr(sz.archiveinfo(), "solid", True):
                            return None
                        with tempfile.TemporaryDirectory(prefix="7z-member-") as td:
                            sz.extract(path=td, targets=[name])
                            f = Path(td) / name
                            return f.read_bytes() if f.is_file() else None
                except Exception:
                    continue
        except Exception:
            return None
        return None

    @staticmethod
    def read_member_prefix(archive: Path, name: str, nbytes: int, passwords=None) -> bytes | None:
        """The first *nbytes* of one member, read without extracting it (a package's
        header). ZIP and RAR (the first member of a solid RAR, any member otherwise); None
        when the format or the archive does not allow it (7z, a later member of a solid RAR,
        a wrong password)."""
        archive = Path(archive)
        if re.match(r"^\.r\d{2,}$", archive.suffix.lower()) or archive.suffix.lower() == ".rar":
            archive = ArchiveExtractor._first_volume(archive)
        suffix = archive.suffix.lower()
        pwds = [p.strip() for p in (passwords or []) if p and p.strip()]
        try:
            if suffix == ".zip":
                with zipfile.ZipFile(archive, "r") as zf:
                    for pwd in [""] + pwds:
                        try:
                            with zf.open(name, pwd=pwd.encode() if pwd else None) as f:
                                return f.read(nbytes)
                        except RuntimeError:
                            continue
                        except NotImplementedError:
                            return None
                return None
            if suffix == ".rar":
                backend_dir = backend_base_dir()
                if str(backend_dir) not in sys.path:
                    sys.path.insert(0, str(backend_dir))
                from unrar import rarfile as _br  # type: ignore
                for pwd in pwds + [""]:
                    try:
                        return _br.RarFile(str(archive), pwd=pwd or None).read_prefix(name, nbytes)
                    except _br.RarWrongPassword:
                        continue
                    except Exception:
                        return None
        except Exception:
            return None
        return None

    @staticmethod
    def ps4_archive_info(archive: Path, passwords=None) -> dict | None:
        """{'packages': [member names], 'ident': Ps4Identity or None} for an archive whose
        payload is PS4 packages; None for anything else. The first package's header and
        param.sfo are read from the archive alone (ZIP, RAR); when that is not possible, a
        CUSA title id in the package or archive names decides, and 'ident' is None."""
        archive = Path(archive)
        names = ArchiveExtractor.list_members(
            ArchiveExtractor._first_volume(archive) if archive.suffix.lower() == ".rar" else archive, passwords)
        pkgs = [n for n in names if n.lower().endswith(".pkg")
                and not any(is_fs_junk_name(part) for part in n.split("/"))]
        if not pkgs or any(n.lower().rstrip("/").endswith(("eboot.bin", "sce_sys/param.json")) for n in names):
            return None
        backend_dir = backend_base_dir()
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        try:
            import ps4pkg  # type: ignore
        except Exception:
            return None
        cache: dict[int, bytes] = {}

        def prefix(n: int) -> bytes:
            if n not in cache:
                cache[n] = ArchiveExtractor.read_member_prefix(archive, pkgs[0], n, passwords) or b""
            return cache[n]
        ident = None
        try:
            ident = ps4pkg.identity_from_prefix(prefix, hints=[pkgs[0], archive.name, archive.parent.name])
        except Exception:
            ident = None
        if ident is not None:
            return {"packages": pkgs, "ident": ident} if ident.title_id.startswith("CUSA") else None
        if any(re.search(r"CUSA\d{5}", s, re.I) for s in pkgs + [archive.name, archive.parent.name]):
            return {"packages": pkgs, "ident": None}
        return None

    @staticmethod
    def list_members(archive: Path, passwords=None) -> list[str]:
        """Return member names ('/'-separated) WITHOUT extracting - a cheap peek
        used to tell a game archive from a DLC/extra. Tries candidate passwords
        for header-encrypted archives. Returns [] if it can't be opened."""
        suffix = archive.suffix.lower()
        cands = [p for p in (passwords or []) if p] + [""]
        if suffix == ".zip":
            try:
                with zipfile.ZipFile(archive, "r") as zf:
                    return [n.replace("\\", "/") for n in zf.namelist()]
            except Exception:
                return []
        if suffix == ".rar":
            resolved = ArchiveExtractor._first_volume(archive)
            try:
                backend_dir = backend_base_dir()
                if str(backend_dir) not in sys.path:
                    sys.path.insert(0, str(backend_dir))
                from unrar import rarfile as _br  # type: ignore
            except Exception:
                return []
            for pwd in cands:
                try:
                    with _br.RarFile(str(resolved), pwd=pwd or None) as rf:
                        return [n.replace("\\", "/") for n in rf.namelist()]
                except Exception:
                    continue
            return []
        if suffix == ".7z":
            try:
                import py7zr  # type: ignore
            except ImportError:
                return []
            for pwd in cands:
                try:
                    kwargs = {"password": pwd} if pwd else {}
                    with py7zr.SevenZipFile(str(archive), mode="r", **kwargs) as sz:
                        return [n.replace("\\", "/") for n in sz.getnames()]
                except Exception:
                    continue
            return []
        return []

    @staticmethod
    def uncompressed_size(archive: Path, passwords=None) -> int:
        """Total UNCOMPRESSED size of the archive's members (probe_header's size); 0
        when it cannot be read, so callers fall back to an estimate."""
        return ArchiveExtractor.probe_header(archive, passwords)[1]

    @staticmethod
    def probe_header(archive: Path, passwords=None) -> tuple[bool, int]:
        """(opened, size): whether the archive's header could be read with one of *passwords* (or none), and the total UNCOMPRESSED size of its members, read from the headers WITHOUT extracting (milliseconds)."""
        state, size, _reason = ArchiveExtractor.probe_header_state(archive, passwords)
        return state == "open", size

    # UnRAR's DLL error codes that mean the archive itself is broken, in plain words.
    _RAR_DAMAGE = {
        12: "its data is damaged (a checksum does not match): a part is corrupt or incomplete",
        13: "the archive is damaged",
        14: "the file is not a RAR archive, or it is damaged at the start",
        15: "a part of the set is missing or cannot be opened",
        18: "a part of the set cannot be read",
    }

    @staticmethod
    def volume_gaps(archive: Path) -> list[str]:
        """Names of the parts missing from a multi-part RAR set between its first part and
        the highest one present (….part1.rar … .partN.rar, or .rar + .r00 …). A missing
        LAST part leaves no gap; UnRAR reports that one when it reads the set."""
        name = archive.name
        m = re.match(r"^(?P<base>.*\.part)(?P<num>\d+)\.rar$", name, re.I)
        try:
            if m:
                base, width = m.group("base"), len(m.group("num"))
                pat = re.compile(re.escape(base) + r"(\d+)\.rar$", re.I)
                nums = {int(mm.group(1)) for f in archive.parent.iterdir() if (mm := pat.match(f.name))}
                if not nums:
                    return []
                return [f"{base}{str(n).zfill(width)}.rar" for n in range(1, max(nums) + 1) if n not in nums]
            m = re.match(r"^(?P<base>.+)\.(?:rar|r\d{2,})$", name, re.I)
            if m:
                base = m.group("base")
                pat = re.compile(re.escape(base) + r"\.r(\d{2,})$", re.I)
                found = [(int(mm.group(1)), len(mm.group(1))) for f in archive.parent.iterdir()
                         if (mm := pat.match(f.name))]
                if not found:
                    return []
                width = found[0][1]
                nums = {n for n, _w in found}
                return [f"{base}.r{str(n).zfill(width)}" for n in range(0, max(nums) + 1) if n not in nums]
        except OSError:
            return []
        return []

    @staticmethod
    def rar_damage_reason(msg: str, archive: Path | None = None) -> str:
        """What a non-password UnRAR failure means, in plain words, naming a missing part
        when the set has a gap. Empty when *msg* is no known damage code."""
        gaps = ArchiveExtractor.volume_gaps(archive) if archive is not None else []
        if gaps:
            return (f"{len(gaps)} part{'s' if len(gaps) != 1 else ''} of the set "
                    f"{'are' if len(gaps) != 1 else 'is'} missing ({', '.join(gaps[:3])}"
                    f"{', …' if len(gaps) > 3 else ''})")
        m = re.search(r"\berror (\d+)\b", msg or "", re.I)
        code = int(m.group(1)) if m else None
        return ArchiveExtractor._RAR_DAMAGE.get(code, "")

    @staticmethod
    def _rar4_encrypted_headers(archive: Path) -> bool:
        """A RAR 4 archive with encrypted headers (rar -hp, old format). It has no password
        check value, so a wrong password reads as a damaged archive: for these a failure
        may still mean "wrong password"."""
        try:
            with open(archive, "rb") as f:
                head = f.read(7 + 13)
        except OSError:
            return False
        # marker block, then the main archive header: CRC(2) type(1)=0x73 flags(2)
        return (head[:7] == b"Rar!\x1a\x07\x00" and len(head) >= 12 and head[9] == 0x73
                and bool(int.from_bytes(head[10:12], "little") & 0x0080))

    @staticmethod
    def probe_header_state(archive: Path, passwords=None) -> tuple[str, int, str]:
        """("open" | "locked" | "damaged", size, reason)."""
        suffix = archive.suffix.lower()
        if re.match(r"^\.r\d{2,}$", suffix):
            archive = ArchiveExtractor._first_volume(archive)
            suffix = archive.suffix.lower()
        cands = [p for p in (passwords or []) if p] + [""]
        try:
            if suffix == ".zip":
                try:
                    with zipfile.ZipFile(archive, "r") as zf:
                        return "open", sum(int(getattr(zi, "file_size", 0) or 0) for zi in zf.infolist()), ""
                except zipfile.BadZipFile as e:
                    return "damaged", 0, f"it is not a readable ZIP ({e}): damaged or incomplete"
            if suffix == ".rar":
                resolved = ArchiveExtractor._first_volume(archive)
                backend_dir = backend_base_dir()
                if str(backend_dir) not in sys.path:
                    sys.path.insert(0, str(backend_dir))
                from unrar import rarfile as _br  # type: ignore
                damage = ""
                for pwd in cands:
                    try:
                        with _br.RarFile(str(resolved), pwd=pwd or None) as rf:
                            return "open", sum(int(getattr(ri, "file_size", 0) or 0) for ri in rf.infolist()), ""
                    except _br.RarWrongPassword:
                        continue
                    except Exception as e:
                        damage = damage or (ArchiveExtractor.rar_damage_reason(str(e), resolved) or str(e))
                        continue
                if damage and not ArchiveExtractor._rar4_encrypted_headers(resolved):
                    return "damaged", 0, damage
                gaps = ArchiveExtractor.volume_gaps(resolved)
                if gaps:   # a hole in the set: no password reads past it
                    return "damaged", 0, ArchiveExtractor.rar_damage_reason("", resolved)
                return "locked", 0, ""
            if suffix == ".7z":
                import py7zr  # type: ignore
                for pwd in cands:
                    try:
                        kwargs = {"password": pwd} if pwd else {}
                        with py7zr.SevenZipFile(str(archive), mode="r", **kwargs) as sz:
                            total = sum(int(getattr(f, "uncompressed", 0) or 0) for f in sz.list())
                            if total <= 0:
                                total = int(getattr(sz.archiveinfo(), "uncompressed", 0) or 0)
                            return "open", total, ""
                    except Exception:
                        continue
                return "locked", 0, ""
        except Exception:
            return "locked", 0, ""
        return "open", 0, ""

    @staticmethod
    def plausible_extracted_size(size: int, ondisk: int) -> int:
        """*size* when it is a sane reading of the whole set, else 0 (the gate then estimates)."""
        return size if (size and size >= ondisk * 0.5) else 0

    @staticmethod
    def names_look_like_game(names) -> bool:
        """True if a member-name listing contains the PS5 game signature
        (an eboot.bin plus a sce_sys/param.json), at any depth."""
        low = [n.lower().rstrip("/") for n in names]
        has_eboot = any(n == "eboot.bin" or n.endswith("/eboot.bin") for n in low)
        has_param = any(n.endswith("sce_sys/param.json") for n in low)
        return has_eboot and has_param

    # ── format handlers ────────────────────────────────────────────────────────

    @staticmethod
    def _check_cancel(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ArchiveExtractionCancelled("Archive extraction cancelled by user.")

    @staticmethod
    def _zip(archive: Path, dest: Path, log_fn, progress_fn=None, password: str = "",
             cancel_event: threading.Event | None = None):
        # WinZip/7-Zip AES encryption (method 99 - what 7-Zip and WinZip produce for a
        # password-protected zip) is beyond the stdlib, which only knows ZipCrypto:
        # zipfile raises NotImplementedError even with the right password. Such archives
        # go through the native 7-Zip CLI, with the same progress/cancel/password
        # plumbing as .7z.
        with zipfile.ZipFile(archive, "r") as zf:
            aes = any(getattr(zi, "compress_type", 0) == 99 for zi in zf.infolist())
        if aes:
            ArchiveExtractor._zip_via_native_7z(archive, dest, log_fn, progress_fn, password,
                                                cancel_event, reason="AES-encrypted ZIP")
            return
        pwd_bytes = password.encode() if password else None
        with zipfile.ZipFile(archive, "r") as zf:
            # Clutter inside the archive (__MACOSX/, ._*, .DS_Store, …) is not written at all.
            names = [n for n in zf.namelist()
                     if not any(is_fs_junk_name(part) for part in n.replace("\\", "/").split("/") if part)]
            total = len(names)
            for i, name in enumerate(names):
                ArchiveExtractor._check_cancel(cancel_event)
                try:
                    zf.extract(name, dest, pwd=pwd_bytes)
                except NotImplementedError as e:
                    # A method the header scan did not flag (Deflate64, PPMd, an AES
                    # member without the method-99 marker, …): same native route.
                    ArchiveExtractor._zip_via_native_7z(
                        archive, dest, log_fn, progress_fn, password, cancel_event,
                        reason=f"ZIP with a method the built-in extractor cannot read ({e})")
                    return
                ArchiveExtractor._check_cancel(cancel_event)
                pct = int((i + 1) / total * 100) if total else 0
                if progress_fn:
                    progress_fn(pct, name)
                elif log_fn and total > 0 and i % max(1, total // 20) == 0:
                    log_fn("INFO", f"  {pct}%  {name}")

    @staticmethod
    def _zip_via_native_7z(archive: Path, dest: Path, log_fn, progress_fn, password: str,
                           cancel_event, reason: str) -> None:
        """Hand a zip the stdlib cannot read to the native 7-Zip CLI (the .7z route)."""
        exe = ArchiveExtractor._find_native_7z()
        if not exe:
            raise RuntimeError(
                f"{reason} - install 7-Zip (7zz) to extract it: {archive.name}\n"
                "  macOS:    brew install sevenzip\n"
                "  Windows:  https://www.7-zip.org/  (put 7z.exe on PATH)")
        if log_fn:
            log_fn("INFO", f"  zip: {reason} - using native {os.path.basename(exe)}.")
        ArchiveExtractor._run_native_7z(exe, archive, dest, log_fn=log_fn, progress_fn=progress_fn,
                                        password=password, cancel_event=cancel_event)

    @staticmethod
    def _find_rar_tool(log_fn=None) -> str | None:
        """Return the first usable RAR-extraction executable found, or None."""
        import shutil as _shutil

        _script_dir = Path(getattr(sys, "frozen", None) and sys.executable
                           or __file__).parent

        # Absolute-path candidates (check existence directly - no subprocess needed)
        absolute_candidates = [
            # Next to the app / in app-data (user can drop UnRAR.exe here)
            _script_dir / "unrar.exe",
            _script_dir / "tools" / "unrar.exe",
            APP_DIR / "unrar.exe",
            # 7-Zip standard install locations
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
            # WinRAR standard install locations
            Path(r"C:\Program Files\WinRAR\UnRAR.exe"),
            Path(r"C:\Program Files\WinRAR\Rar.exe"),
            Path(r"C:\Program Files (x86)\WinRAR\UnRAR.exe"),
            Path(r"C:\Program Files (x86)\WinRAR\Rar.exe"),
            # macOS / Linux: Homebrew (Apple Silicon + Intel), MacPorts, system.
            # GUI apps don't inherit the shell PATH, so check these directly.
            Path("/opt/homebrew/bin/unrar"), Path("/opt/homebrew/bin/7z"),
            Path("/opt/homebrew/bin/7za"),   Path("/opt/homebrew/bin/rar"),
            Path("/usr/local/bin/unrar"),    Path("/usr/local/bin/7z"),
            Path("/usr/local/bin/7za"),      Path("/usr/local/bin/rar"),
            Path("/opt/local/bin/unrar"),    Path("/opt/local/bin/7z"),
            Path("/usr/bin/unrar"),          Path("/usr/bin/7z"),
            Path("/usr/bin/7za"),
        ]
        for p in absolute_candidates:
            if p.exists():
                if log_fn:
                    log_fn("INFO", f"RAR tool found: {p}")
                return str(p)

        # Short names resolved via PATH
        for name in ("unrar", "rar", "7z", "7za"):
            if _shutil.which(name):
                if log_fn:
                    log_fn("INFO", f"RAR tool found on PATH: {name}")
                return name

        return None

    @staticmethod
    def _cli_tail_lines(tail) -> list[str]:
        """The retained CLI output as readable lines: \\r / backspace progress redraws
        are flattened, blank and repeated segments dropped."""
        out: list[str] = []
        for raw in tail:
            for seg in str(raw).split("\r"):
                seg = re.sub(r"\s*\x08+\s*", " ", seg).strip()
                if seg and (not out or out[-1] != seg):
                    out.append(seg)
        return out

    @staticmethod
    def _cli_excerpt(lines: list[str], limit: int = 8) -> str:
        """Short excerpt for an error message: the lines that name a problem, else the
        last few. Empty when there is nothing to show."""
        hits = [l for l in lines
                if re.search(r"error|warn|wrong|password|cannot|can't|unsupported|missing", l, re.I)]
        shown = (hits or lines)[-limit:]
        return ("\n  " + "\n  ".join(shown)) if shown else ""

    @staticmethod
    def _run_extract_process(cmd: list[str], tool_name: str, log_fn=None, progress_fn=None,
                             cancel_event: threading.Event | None = None) -> None:
        """Run a CLI extractor, streaming its progress; raise on a non-zero exit.
        The last lines of its output are kept so the failure names the reason: a
        wrong/missing password raises ArchivePasswordError, anything else
        ArchiveToolError (carrying the exit code) - callers key their fallback on it."""
        from collections import deque
        proc = subprocess.Popen(
            cmd,
            # Closed stdin: an encrypted archive we passed no password for makes the tool
            # prompt ("Enter password:") - it must fail, not wait for a keyboard.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        lines: queue.Queue[str] = queue.Queue()
        tail: deque[str] = deque(maxlen=20)   # what the tool said last - for the error

        def _reader():
            try:
                for raw in proc.stdout or []:
                    lines.put(raw.rstrip())
            finally:
                try:
                    if proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass

        threading.Thread(target=_reader, daemon=True).start()
        last_log_t = time.time()

        def _handle_line(line: str) -> None:
            nonlocal last_log_t
            if not line:
                return
            tail.append(line)
            if log_fn and (time.time() - last_log_t >= 5 or "error" in line.lower()):
                log_fn("INFO", f"  extract: {line}")
                last_log_t = time.time()
            if progress_fn:
                m = re.search(r"(\d+)%", line)
                if m:
                    progress_fn(int(m.group(1)), line)

        while True:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise ArchiveExtractionCancelled("Archive extraction cancelled by user.")
            try:
                _handle_line(lines.get(timeout=0.1))
            except queue.Empty:
                if proc.poll() is not None:
                    break

        while True:
            try:
                _handle_line(lines.get_nowait())
            except queue.Empty:
                break

        code = proc.wait()
        if code != 0:
            readable = ArchiveExtractor._cli_tail_lines(tail)
            excerpt = ArchiveExtractor._cli_excerpt(readable)
            if ArchiveExtractor._CLI_PASSWORD_RE.search("\n".join(readable)):
                raise ArchivePasswordError(
                    f"{tool_name}: wrong or missing archive password (exit code {code}).{excerpt}")
            raise ArchiveToolError(
                f"{tool_name} exited with code {code} - extraction failed.{excerpt}",
                returncode=code)

    @staticmethod
    def _first_volume(archive: Path) -> Path:
        """Multi-volume RAR sets must be opened on the FIRST volume. If *archive*
        is a later part, return the first volume in the same folder (else *archive*).

        Handles both the new scheme (name.partN.rar → name.part1.rar, width kept)
        and the old scheme (name.rNN → name.rar)."""
        name = archive.name
        m = re.match(r"^(?P<base>.*\.part)(?P<num>\d+)(?P<ext>\.rar)$", name, re.I)
        if m:
            first = archive.with_name(f"{m.group('base')}{'1'.zfill(len(m.group('num')))}{m.group('ext')}")
            return first if first.exists() else archive
        m = re.match(r"^(?P<base>.+)\.r\d{2,}$", name, re.I)   # .r00 … .r100 … (old scheme)
        if m:
            first = archive.with_name(f"{m.group('base')}.rar")
            return first if first.exists() else archive
        return archive

    @staticmethod
    def _rar_error_hint(e: Exception, archive: Path | None = None) -> str:
        """Turn a raw UnRAR error into a short, user-actionable reason."""
        msg = str(e).strip()
        low = msg.lower()
        # Password problems FIRST - header-encrypted (-hp) archives fail while
        # *reading headers* with UnRAR error 22 (no password) or 24 (wrong
        # password). These also contain "read header failed", so they must be
        # matched before the multi-volume branch below.
        if ("password" in low or "error 22" in low or "error 24" in low
                or type(e).__name__ == "RarWrongPassword"):
            return ("this RAR is password-protected - enter the correct password in the "
                    "'Archive Password' field and try again "
                    "(error 22 = no password given, 24 = wrong password)")
        damage = ArchiveExtractor.rar_damage_reason(msg, archive)
        if damage:
            return f"{damage} - download the set again, or check that every part is complete"
        if ("read header failed" in low or "failed to open" in low or "missing" in low or "volume" in low):
            return ("a volume of this multi-part RAR is missing, incomplete, or it was "
                    "opened on the wrong part - make sure every .partN.rar (or .rNN) file "
                    "is present in the same folder")
        return msg or e.__class__.__name__

    @staticmethod
    def _rar(archive: Path, dest: Path, log_fn, progress_fn=None, password: str = "",
             cancel_event: threading.Event | None = None):
        # Multi-volume sets must be opened on the first volume. If the user added
        # a later part (….part3.rar / ….r02), switch to the first one.
        resolved = ArchiveExtractor._first_volume(archive)
        if resolved != archive:
            if log_fn:
                log_fn("INFO", f"Multi-part RAR detected - using first volume: {resolved.name}")
            archive = resolved

        bundled_err: str | None = None   # the real reason the native module failed
        bundled_imported = False          # did the native module import at all?
        multipart_hint = (
            "If this is a multi-part RAR, make sure ALL parts are present in the "
            "same folder (….part1.rar … .partN.rar, or .rar + .r00 + .r01 …) and "
            "that the download is complete and not corrupted."
        )

        # ── Try bundled native UnRAR bindings first (macOS/Windows/Linux) ─────
        try:
            ArchiveExtractor._check_cancel(cancel_event)
            backend_dir = backend_base_dir()
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))
            from unrar import rarfile as bundled_rarfile  # type: ignore
            bundled_imported = True
            _cancel_poll = (lambda: bool(cancel_event is not None and cancel_event.is_set()))
            try:
                with bundled_rarfile.RarFile(str(archive), pwd=password or None) as rf:
                    rf.extractall(
                        str(dest),
                        progress=(lambda p: progress_fn(p, archive.name)) if progress_fn else None,
                        cancel=_cancel_poll,
                    )
            except getattr(bundled_rarfile, "RarExtractionCancelled", ()):
                raise ArchiveExtractionCancelled("Archive extraction cancelled by user.")
            ArchiveExtractor._check_cancel(cancel_event)
            if log_fn:
                log_fn("OK", "RAR extracted via bundled native UnRAR")
            return
        except ArchiveExtractionCancelled:
            raise
        except ImportError as e:
            bundled_err = f"native UnRAR module unavailable ({e})"
            if log_fn:
                log_fn("WARN", f"Bundled UnRAR unavailable ({e}) - trying fallback extractors…")
        except Exception as e:
            bundled_err = ArchiveExtractor._rar_error_hint(e, archive)
            if log_fn:
                log_fn("WARN", f"Bundled UnRAR failed: {e}")

        # macOS: if the native module loaded but extraction failed, the archive
        # itself is the problem (incomplete / wrong part / corrupt). External CLI
        # tools can't fix that and are frequently unsigned Homebrew binaries that
        # macOS Gatekeeper blocks with a scary dialog - so report the real reason
        # instead of spawning them. (Windows/Linux keep the full fallback chain.)
        if sys.platform == "darwin" and bundled_imported:
            # Only add the multi-part hint when it isn't a password problem.
            extra = "" if "password" in (bundled_err or "").lower() else f"\n\n{multipart_hint}"
            raise RuntimeError(
                f"RAR extraction failed - {bundled_err}.{extra}\n\n"
                "ZIP and .7z archives extract without external RAR tools."
            )

        # ── Try rarfile Python library next ───────────────────────────────────
        try:
            ArchiveExtractor._check_cancel(cancel_event)
            import rarfile  # type: ignore
            with rarfile.RarFile(str(archive)) as rf:
                if password:
                    rf.setpassword(password.encode())
                rf.extractall(str(dest))
            ArchiveExtractor._check_cancel(cancel_event)
            if log_fn:
                log_fn("OK", "RAR extracted via rarfile library")
            return
        except ImportError:
            pass
        except Exception as e:
            if log_fn:
                log_fn("WARN", f"rarfile failed ({e}) - trying CLI tools…")

        # ── Find any suitable CLI tool ─────────────────────────────────────────
        tool = ArchiveExtractor._find_rar_tool(log_fn=log_fn)
        if tool:
            tool_name = Path(tool).name.lower()
            is_7z = "7z" in tool_name
            if is_7z:
                # -bsp1 streams progress to stdout; -y auto-confirms
                cmd = [tool, "x", str(archive), f"-o{dest}", "-y", "-bsp1", "-bso0"]
                if password:
                    cmd.append(f"-p{password}")
            else:
                cmd = [tool, "x", "-y"]
                if password:
                    cmd.append(f"-p{password}")
                cmd += [str(archive), str(dest) + os.sep]

            if log_fn:
                log_fn("INFO", f"Running: {Path(tool).name}  (this may take a while for large archives…)")

            try:
                ArchiveExtractor._run_extract_process(
                    cmd, Path(tool).name, log_fn=log_fn,
                    progress_fn=progress_fn, cancel_event=cancel_event
                )
                if log_fn:
                    log_fn("OK", f"RAR extracted via {Path(tool).name}")
                return
            except (ArchiveExtractionCancelled, RuntimeError):
                raise
            except Exception as e:
                raise RuntimeError(f"Extraction error ({Path(tool).name}): {e}")

        # ── Nothing worked - informative error (real reason, not a blanket msg) ──
        # On macOS this is only reached when the native module failed to *import*
        # (a broken build); an import-less Mac genuinely has no RAR extractor.
        reason = bundled_err or "no RAR extractor was available"
        if sys.platform == "darwin":
            raise RuntimeError(
                f"RAR extraction failed - {reason}.\n\n"
                f"{multipart_hint}\n\n"
                "No 7z/unrar CLI was found either. Install one with:\n"
                "  brew install sevenzip      (provides 7z)\n"
                "  brew install carlocab/personal/unrar   (or: brew install rar)\n\n"
                "ZIP and .7z archives extract without external RAR tools."
            )
        raise RuntimeError(
            f"RAR extraction failed - {reason}.\n\n"
            f"{multipart_hint}\n\n"
            "EASIEST FIX - install any ONE of these:\n"
            "  1. 7-Zip:  https://www.7-zip.org/\n"
            "  2. WinRAR: https://www.rarlab.com/download.htm\n\n"
            "ZIP and .7z archives extract without any extra tools."
        )

    @staticmethod
    def _find_native_7z() -> str | None:
        """Locate a native 7-Zip binary: PATH first, then well-known Homebrew install paths (a .app bundled with PyInstaller doesn't inherit the user's shell PATH, so /opt/homebrew/bin is invisible without explicit probing)."""
        names = ("7zz", "7z", "7za")
        for name in names:
            p = shutil.which(name)
            if p:
                return p
        # macOS: Homebrew binaries when PATH was sanitised (e.g. /usr/bin only).
        if sys.platform == "darwin":
            for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
                for name in names:
                    cand = f"{prefix}/{name}"
                    if os.path.isfile(cand) and os.access(cand, os.X_OK):
                        return cand
        return None

    @staticmethod
    def _run_native_7z(exe: str, archive: Path, dest: Path, log_fn=None, progress_fn=None,
                       password: str = "", cancel_event: threading.Event | None = None) -> None:
        """Extract *archive* (any format the binary reads: 7z, zip, …) with the native 7-Zip CLI."""
        cmd = [exe, "x", str(archive), f"-o{dest}", "-y", "-bsp1", f"-p{password}",
               # OS and archiver clutter is not extracted at all (strip_fs_junk catches the rest).
               "-xr!._*", "-xr!.DS_Store", "-xr!__MACOSX", "-xr!Thumbs.db", "-xr!desktop.ini"]
        ArchiveExtractor._run_extract_process(
            cmd, os.path.basename(exe), log_fn=log_fn,
            progress_fn=progress_fn, cancel_event=cancel_event)

    @staticmethod
    def _py7zr_password_failure(e: Exception) -> bool:
        """py7zr has no wrong-password error. A bad key surfaces as corrupt data
        (LZMAError 'Corrupt input data', CrcError), as an unreadable header (Bad7zFile - or, in py7zr 1.1, TypeError 'Unknown field' on a header-encrypted archive) or
        as PasswordRequired. Meaningful only when a password was actually supplied."""
        if type(e).__name__ in ("LZMAError", "CrcError", "Bad7zFile", "PasswordRequired"):
            return True
        return isinstance(e, TypeError) and "unknown field" in str(e).lower()

    @staticmethod
    def _sevenz(archive: Path, dest: Path, log_fn, progress_fn=None, password: str = "",
                cancel_event: threading.Event | None = None):
        ArchiveExtractor._check_cancel(cancel_event)
        # Universal progress: py7zr's extractall has no callback and p7zip 17.05's
        # -bsp1 output is sparse, so run a background thread that polls the dest
        # folder size every 5 s and reports %% against the header-read uncompressed
        # total. Works regardless of which 7z code path runs.
        expected_total = 0
        if progress_fn:
            try:
                expected_total = ArchiveExtractor.uncompressed_size(
                    archive, [password] if password else [])
            except Exception:
                expected_total = 0
        stop_evt = threading.Event()
        poll_thread = None
        if progress_fn and expected_total > 0:
            def _poll():
                while not stop_evt.is_set():
                    try:
                        total = 0
                        for root, _, files in os.walk(str(dest)):
                            for f in files:
                                try:
                                    total += os.path.getsize(os.path.join(root, f))
                                except Exception:
                                    pass
                        pct = min(99, int(100 * total / expected_total))
                        gb = total / (1024 ** 3)
                        progress_fn(pct, f"  ({gb:.1f} GB extracted)")
                    except Exception:
                        pass
                    if stop_evt.wait(5):
                        return
            poll_thread = threading.Thread(target=_poll, daemon=True)
            poll_thread.start()

        try:
            # Native CLI first - py7zr is pure-Python LZMA (slow); native uses C with
            # SIMD/threaded LZMA and saturates the I/O instead.
            exe = ArchiveExtractor._find_native_7z()
            if exe:
                try:
                    if log_fn:
                        log_fn("INFO", f"  7z: using native {os.path.basename(exe)} (fast).")
                    ArchiveExtractor._run_native_7z(exe, archive, dest, log_fn=log_fn,
                                                    progress_fn=progress_fn, password=password,
                                                    cancel_event=cancel_event)
                    return
                except FileNotFoundError:
                    # Disappeared between probe and exec → the pure-Python route below.
                    if log_fn:
                        log_fn("WARN", "  7z: native CLI not runnable - falling back to py7zr.")
                except ArchiveToolError as e:
                    # py7zr only stands in for a CLI that could not run at all (7-Zip exit
                    # 7 = command-line error, e.g. an old build rejecting a switch). After
                    # a data or password error it would re-read the same bytes with the
                    # same password and fail as LZMAError('Corrupt input data') - which
                    # buried the wrong-password verdict under a generic failure, so the
                    # next saved password was never tried.
                    if e.returncode != 7:
                        raise
                    if log_fn:
                        log_fn("WARN", f"  7z: native CLI rejected the command line ({e}) - "
                                       "falling back to py7zr.")
            # Fallback: pure-Python py7zr (always available with our bundled deps).
            try:
                import py7zr  # type: ignore
            except ImportError:
                py7zr = None
            if py7zr is None:
                raise RuntimeError(
                    "Cannot extract 7z - install py7zr:  pip install py7zr\n"
                    "or put 7z / 7zz / 7za on your PATH (Homebrew: brew install sevenzip)."
                )
            if log_fn:
                log_fn("INFO", "  7z: using py7zr (pure-Python - install 7-Zip (7zz) for ~5x speedup).")
            kwargs = {"password": password} if password else {}
            try:
                with py7zr.SevenZipFile(str(archive), mode="r", **kwargs) as sz:
                    sz.extractall(str(dest))
            except Exception as e:
                # Only a failure WITH a password in play is a password verdict (see
                # _py7zr_password_failure); without one the archive really is damaged.
                if password and ArchiveExtractor._py7zr_password_failure(e):
                    raise ArchivePasswordError(
                        f"7z: wrong password? (py7zr: {type(e).__name__}: {str(e)[:200]})") from e
                raise
            ArchiveExtractor._check_cancel(cancel_event)
        finally:
            stop_evt.set()
            if poll_thread is not None:
                poll_thread.join(timeout=2)
            if progress_fn:
                progress_fn(100, archive.name)

    # ── helper ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_root(dest: Path, log_fn=None) -> Path:
        """Walk the extraction tree and return the PS5 game root."""
        from collections import deque
        # Level-by-level BFS (bounded): every folder of one depth is checked before
        # descending, so side-by-side game roots are seen together.
        level: list[Path] = [dest]
        visited = 0
        while level and visited < 200:
            visited += len(level)
            hits = [d for d in level if (d / "sce_sys" / "param.json").exists()]
            if len(hits) == 1:
                return hits[0]
            if hits:
                common = Path(os.path.commonpath([str(h) for h in hits]))
                if log_fn:
                    log_fn("WARN", f"{len(hits)} game roots side by side in the archive "
                                   f"({', '.join(h.name for h in hits)}) - using their parent "
                                   f"'{common.name}' so every one gets classified.")
                return common
            nxt: list[Path] = []
            for d in level:
                try:
                    nxt.extend(p for p in d.iterdir() if p.is_dir())
                except PermissionError:
                    continue
            level = nxt

        # Second pass - title-ID folder name (e.g. PPSA00001-app, CUSA12345)
        queue_dirs: deque[Path] = deque([dest])
        visited = 0
        while queue_dirs and visited < 200:
            current = queue_dirs.popleft()
            visited += 1
            if re.search(r'\b(?:PPSA|CUSA)\d{5}\b', current.name, re.I):
                return current
            try:
                queue_dirs.extend(p for p in current.iterdir() if p.is_dir())
            except PermissionError:
                continue

        # Legacy: single top-level folder unwrap
        try:
            items = [p for p in dest.iterdir() if p.is_dir()]
            if len(items) == 1:
                return items[0]
        except Exception:
            pass

        return dest


# ─── Game Item ─────────────────────────────────────────────────────────────────

# Files copied to the destination alongside a packed game, minus tooling junk.
EXTRA_JUNK_EXTS = {".nfo", ".sfv", ".txt", ".diz", ".url", ".md5", ".sha1",
                   ".srr", ".jpg", ".jpeg", ".png", ".gif", ".db"}

# OS/Finder/archiver metadata that must never enter the image OR be copied to the
# destination. The mkpfs backend filters the same set inside the image
# (pfs.is_fs_junk); this GUI copy path is a separate process, so it carries its own.
# Lower-case, compared case-insensitively. The same list lives in backend/cli.py,
# backend/after_job.py, backend/mkpfs/utils.py and the fPKG tool (FsJunk.cs); a test keeps
# them equal.
FS_JUNK_NAMES = {".ds_store", ".localized", ".lsoverride", ".apdisk", ".volumeicon.icns", "icon\r",
                 "thumbs.db", "ehthumbs.db", "desktop.ini",
                 # junk DIRECTORIES (so they're never carried as a DLC/extra sibling)
                 "__macosx", ".spotlight-v100", ".fseventsd", ".trashes", ".temporaryitems",
                 ".documentrevisions-v100", ".appledouble", "$recycle.bin", "system volume information"}


def is_fs_junk_name(name: str) -> bool:
    """True for macOS/Windows filesystem junk (by basename). '._*' = AppleDouble."""
    return name.startswith("._") or name.lower() in FS_JUNK_NAMES


def ident_from_folder_name(archive) -> dict | None:
    """A stand-in name for an archive whose game cannot be read before it is unpacked (an
    image or a solid set inside): the folder around it, when that folder carries the title id
    and words besides ('PPSA99097 Example Game' -> 'Example Game'). Release tags in brackets
    and words such as 'Compressed' are dropped. None when no readable title is left."""
    a = Path(archive)
    for name in (a.parent.name, re.sub(r"(\.part\d+)?\.(rar|zip|7z|r\d{2,})$", "", a.name, flags=re.I)):
        m = TITLE_RE.search(name)
        if not m:
            continue
        tid = m.group(1).upper()
        t = TITLE_RE.sub(" ", name)
        t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t)
        t = re.sub(r"\b(compressed|ffpfsc|ffpfs|pkg|backport|fw\s*\d[\d.]*|v\d[\d.]*|usa|eur|jpn|asia|app\d*)\b", " ", t, flags=re.I)
        t = re.sub(r"[_.\-\u2013\u2014]+", " ", t)
        t = re.sub(r"\s{2,}", " ", t).strip()
        if re.search(r"[A-Za-z]{3,}", t):
            return {"title": t, "title_id": tid, "version": ""}
    return None


def strip_written_clutter(path) -> int:
    """Remove the macOS clutter a job's own output carries: the '._' sidecar beside *path*
    and beside the folder holding it, and, when *path* is a folder the job wrote, every
    clutter entry inside it. On exFAT macOS stores the provenance attribute it adds to each
    written file as such a sidecar; the system drops it after a few seconds, but an eject
    before that keeps it for good. Never touches anything else in the folder around *path*.
    Returns how many entries went."""
    if not path:
        return 0
    p = Path(path)
    removed = strip_fs_junk(p) if p.is_dir() else 0
    for q in (p, p.parent):
        sidecar = q.parent / ("._" + q.name)
        try:
            if q.name and sidecar.is_file():
                sidecar.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def strip_fs_junk(root) -> int:
    """Delete every junk file and folder under *root* (see is_fs_junk_name). Used right
    after an archive is unpacked, so clutter from inside the archive, or AppleDouble
    sidecars an exFAT drive added, never reach a job. Returns how many entries went."""
    root = Path(root)
    removed = 0
    if not root.is_dir():
        return 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            if is_fs_junk_name(name):
                try:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    pass
        for name in list(dirnames):
            if is_fs_junk_name(name):
                p = os.path.join(dirpath, name)
                try:
                    if os.path.islink(p):
                        os.remove(p)
                    else:
                        shutil.rmtree(p)
                    removed += 1
                except OSError:
                    pass
    return removed


# The work folders this app creates beside a temp folder, on an extra temp drive and in an
# output folder. Nothing else is ever pruned.
SCRATCH_DIR_NAMES = ("_ffpfsc_temp", "_ffpfsc_extract", "_extracted", "_ffpfsc_inner")


def _only_clutter_below(d: Path) -> bool:
    """True when nothing but OS clutter (files) and empty folders lie below *d*."""
    try:
        for e in d.iterdir():
            if e.is_symlink():
                return False
            if e.is_dir():
                if not _only_clutter_below(e):
                    return False
            elif not is_fs_junk_name(e.name):
                return False
    except OSError:
        return False
    return True


def _remove_with_sidecar(d: Path) -> bool:
    try:
        shutil.rmtree(d)
    except OSError:
        return False
    try:
        (d.parent / ("._" + d.name)).unlink()
    except OSError:
        pass
    return True


def prune_empty_scratch(bases, min_age: float = 0.0, stop=None) -> list:
    """Remove the app's work folders (SCRATCH_DIR_NAMES) directly inside each of *bases*
    once nothing but OS clutter is left in them, with their '._' sidecars. Inside a work
    folder, a job's folder goes only as a whole and only when it is empty in the same way;
    one with a real file anywhere below it (a kept unpack) stays exactly as it is, empty
    subfolders included. Nothing outside the work folders is touched. Call only while no
    job runs: a job's empty folder may be about to fill; as a second guard a folder changed
    within *min_age* seconds stays, and *stop()* (when given) is asked before each removal.
    Returns the work folders removed."""
    gone = []

    def settled(d: Path) -> bool:
        if stop is not None and stop():
            return False
        try:
            return time.time() - d.stat().st_mtime >= min_age
        except OSError:
            return False

    def prune(c: Path) -> None:
        was_settled = settled(c)              # before this pass changes it by removing below
        try:
            children = list(c.iterdir())
        except OSError:
            return
        for e in children:
            if e.is_symlink() or not e.is_dir():
                continue
            if e.name in SCRATCH_DIR_NAMES:
                prune(e)
            elif _only_clutter_below(e) and settled(e):
                _remove_with_sidecar(e)
        if was_settled and _only_clutter_below(c) and (stop is None or not stop()) and _remove_with_sidecar(c):
            gone.append(c)

    seen = set()
    for base in bases or ():
        try:
            base = Path(base)
            key = base.resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if key in seen or not base.is_dir():
            continue
        seen.add(key)
        for name in SCRATCH_DIR_NAMES:
            top = base / name
            if top.is_dir() and not top.is_symlink():
                prune(top)
    return gone


# Glob patterns for the dir-copy path (shutil.ignore_patterns) so a copied DLC/extra
# folder never carries OS/archiver metadata to the destination.
_COPYTREE_JUNK_GLOBS = ("._*", ".DS_Store", ".localized", ".LSOverride", ".apdisk", ".VolumeIcon.icns",
                        "Icon\r", "Thumbs.db", "ehthumbs.db", "desktop.ini", "__MACOSX", ".Spotlight-V100",
                        ".fseventsd", ".Trashes", ".TemporaryItems", ".DocumentRevisions-V100",
                        ".AppleDouble", "$RECYCLE.BIN", "System Volume Information")


def detect_game_bundle(folder: Path, candidate_passwords=None, log_fn=None, detect_patch=False):
    """Inspect *folder* for the 'game + extras' layout.

    Returns (game_source, siblings, all_games, patch_source):
      • game_source - the single game (a game subfolder, a disk image, or an
        archive whose listing shows a PS5 game), or None.
      • siblings - the other files to copy next to the output (tooling junk and
        the game's own multi-part volumes removed).
      • all_games - every game candidate found (so the caller can warn on >1).
      • patch_source - when *detect_patch* is set and the folder holds a base game
        plus one clearly-smaller game-like sibling (a patch carries eboot.bin, so it
        reads as a 'game' too), that sibling - to be overlaid via --patch. Else None.
    Archives are only *listed* here (peeked), never extracted.
    """
    try:
        entries = list(folder.iterdir())
    except Exception:
        return None, [], [], None   # 4-tuple - callers unpack (game, siblings, all_games, patch)
    # Drop filesystem junk up front (._* AppleDouble carry real suffixes, .DS_Store,
    # Thumbs.db, junk dirs) so it is never mistaken for a game file or kept as a sibling.
    entries = [p for p in entries if not is_fs_junk_name(p.name)]
    files = [p for p in entries if p.is_file()]
    subdirs = [p for p in entries if p.is_dir()]

    archive_files = [f for f in files if f.suffix.lower() in (".zip", ".rar", ".7z")]
    image_files = [f for f in files if f.suffix.lower() in DISK_IMAGE_SUFFIXES]

    games: list[Path] = []

    # Game subfolders directly inside (a loose dump sitting in the folder).
    for d in subdirs:
        if is_game_folder(d):
            games.append(d)
        else:
            inner = find_game_folders(d, max_depth=2)
            if len(inner) == 1:
                games.append(inner[0])

    # Disk images are games as-is.
    games.extend(image_files)

    # Archives: only the first volume of each multi-part set is a candidate
    # (.part02+/.r01+ resolve back to the first volume and are skipped).
    first_volumes = [a for a in archive_files
                     if ArchiveExtractor._first_volume(a) == a]
    if first_volumes:
        if len(first_volumes) == 1 and not games:
            # One archive set and nothing else competing → it IS the game. Don't
            # peek: listing a multi-volume RAR reads through every part (slow over
            # USB). Extraction confirms it later, and a non-game archive just
            # fails with a clear "no game found".
            games.append(first_volumes[0])
            if log_fn:
                log_fn("INFO", f"  Game archive (by structure): {first_volumes[0].name}")
        else:
            # Ambiguous (several archive sets, or one alongside a folder/image) →
            # peek each to find which actually holds the game.
            for a in first_volumes:
                names = ArchiveExtractor.list_members(a, candidate_passwords)
                if names and ArchiveExtractor.names_look_like_game(names):
                    games.append(a)
                    if log_fn:
                        log_fn("INFO", f"  Game archive detected: {a.name}")
                elif log_fn:
                    log_fn("INFO", f"  Not a game (kept as extra): {a.name}")

    def _vol_set(cand: Path) -> set:
        """Every on-disk file belonging to *cand* - an archive's whole volume set,
        or the single file. Empty for a folder candidate."""
        s: set = set()
        if cand.is_file() and cand.suffix.lower() in (".zip", ".rar", ".7z"):
            for a in archive_files:
                if a == cand or ArchiveExtractor._first_volume(a) == cand:
                    s.add(a.resolve())
        elif cand.is_file():
            s.add(cand.resolve())
        return s

    def _cand_size(cand: Path) -> int:
        try:
            if cand.is_dir():
                return folder_size(cand)
            return sum((p.stat().st_size for p in _vol_set(cand)), 0) or cand.stat().st_size
        except Exception:
            return 0

    def _siblings_excluding(*cands: Path) -> list:
        exclude: set = set()
        for c in cands:
            exclude |= _vol_set(c)
        files_out = [f for f in files
                     if f.resolve() not in exclude
                     and f.suffix.lower() not in EXTRA_JUNK_EXTS
                     and not is_fs_junk_name(f.name)]
        # Also carry whole EXTRA subfolders (e.g. an '[ ALL DLC ]' wrapper) - anything
        # that isn't the chosen game/patch, doesn't CONTAIN it, and isn't OS junk.
        game_paths = set()
        for g in cands:
            try:
                game_paths.add(g.resolve())
            except Exception:
                pass
        dirs_out = []
        for d in subdirs:
            try:
                rp = d.resolve()
            except Exception:
                continue
            if rp in game_paths:
                continue                                   # the game/patch folder itself
            if any(str(g) == str(rp) or str(g).startswith(str(rp) + os.sep) for g in game_paths):
                continue                                   # a wrapper that holds the game
            if is_fs_junk_name(d.name):
                continue
            dirs_out.append(d)
        return files_out + dirs_out

    # Normal case: exactly one game in the folder.
    if len(games) == 1:
        game = games[0]
        return game, _siblings_excluding(game), games, None

    # Auto-patch: a base game plus a single, clearly-smaller game-like sibling. A
    # patch carries eboot.bin, so it also reads as a 'game'; pick the larger as the
    # base and the smaller (a folder, or a zip/rar the backend can unpack) as the
    # patch - only when it is distinctly smaller, so two real games are not mistaken
    # for a base+patch pair.
    if detect_patch and len(games) == 2:
        base, other = sorted(games, key=_cand_size, reverse=True)
        bs, ps = _cand_size(base), _cand_size(other)
        # The patch must be a folder or a zip/rar the backend can unpack; the base
        # must resolve to a game folder (a disk image can't be overlaid this way).
        patchable = other.is_dir() or other.suffix.lower() in (".zip", ".rar")
        base_ok = base.is_dir() or base.suffix.lower() in (".zip", ".rar", ".7z")
        if patchable and base_ok and bs > 0 and ps <= 0.7 * bs:
            if log_fn:
                log_fn("INFO", f"  Auto-patch: base '{base.name}', patch '{other.name}'")
            return base, _siblings_excluding(base, other), games, other

    return None, [], games, None


def scan_parent_for_bundles(parent: Path, candidate_passwords=None, log_fn=None, detect_patch=False):
    """Treat *parent* as a library of games: every immediate subfolder that holds
    a game becomes its own bundle, so its folder is recreated at the destination
    with just the .ffpfsc (plus any extras) inside. Returns a list of bundle
    GameItems (one per game subfolder)."""
    items = []
    try:
        children = sorted((d for d in parent.iterdir() if d.is_dir()),
                          key=lambda p: p.name.lower())
    except Exception:
        return items
    for child in children:
        game, siblings, patch = None, [], None
        if is_game_folder(child):
            game = child                       # the subfolder itself is the dump
        else:
            g, sib, _all, p = detect_game_bundle(child, candidate_passwords, log_fn, detect_patch)
            if g is not None:
                game, siblings, patch = g, sib, p
            else:
                inner = find_game_folders(child, max_depth=2)
                if len(inner) == 1:
                    game = inner[0]
        if game is not None:
            try:
                items.append(GameItem.from_bundle(child, game, siblings, patch))
                if log_fn:
                    extra = f" + {len(siblings)} extra(s)" if siblings else ""
                    extra += " + patch" if patch else ""
                    log_fn("INFO", f"  Library game: {child.name}{extra}")
            except Exception as e:
                if log_fn:
                    log_fn("WARN", f"  Skipped {child.name}: {e}")
    return items


class GameItem:
    # Class-level defaults so items built via __new__ (from_*, history, restored queue)
    # always have these attributes even when an older saved queue predates them.
    ampr_emu = False        # PlayGo/APR title? (auto-detected)
    art_key = ""            # the job's cover in the art cache (see store_art); survives a restart
    content_kind = ""       # "ps4": a PS4 package set, sorted into the library by a copy job;
                            # "organize": any source written into the library in its own format
    display_name = None     # STABLE queue label captured at add time; survives extraction
                            # (item.name gets rewritten to the extracted stem, which still
                            # drives the OUTPUT filename, but the queue keeps showing this)
    output_path = None      # per-job output folder/file snapshot; None → use the global Output
    output_compressed = None # per-job format: True=.ffpfsc, False=.ffpfs; None → not yet set
    patch_source = None     # patch dir/archive (operation == "patch")
    patch_overwrite = False # patch: overwrite the source .ffpfsc in place vs a "[patched]" copy
    patch_inplace = False   # patch: overlay onto a throwaway temp extract (archive game source)
    unwrap = True           # convert/unpack: True = unwrap to a folder, False = stop at inner .ffpfs
    copy_mode = None        # copy job: "keep" | "organize" | "move" (backend copy_job); None = keep
    backport_target = None  # None|"7.61"|"6.02"|"10.xx"|another firmware: lower SDK before the build
    backport_libs_root = None  # str: folder of user-supplied patched sprx dropped into fakelib/
    chain_to = None         # chain job: "folder" | "ffpfs" | "ffpfsc" | "pkg"
    chain_sign = False      # chain job: fake-sign executables (after patch and backport)
    header_locked = False   # archive: no saved password opened its header when it was added
    archive_problem = ""    # archive: why it cannot be read (damaged, a part missing); no password helps
    archive_title = ""      # archive: the game read from the param.json inside, before extraction
    archive_title_id = ""
    archive_version = ""
    pkg_content_size = 0    # .pkg source: bytes of its files, from the package's directory (0 = not read)
    origin_archive = None   # str: the archive this job's source was extracted from (retry restarts there)
    origin_extracted_size = 0  # that archive's extracted size, read from its headers
    kept_extract = False    # cancelled after its archive was extracted: that copy stays until the job is removed
    compression_level = None  # .ffpfsc job: zlib level 1-9 chosen in the job editor; None → the default
    status_note = ""        # why the job failed or was skipped (shown in the details pane)
    after_source = None     # once Done: "keep" | "trash" | "move" | "delete"; None (older queues) = keep
    after_move_to = None    # str: the folder "move" sends the source to
    source_root = None      # str: the folder the job was added from; never moved or removed whole

    def __init__(self, path: Path):
        self.path       = path
        self.archive_path: Path | None = None   # set for archive placeholders
        self.operation  = "pack"
        self.name       = guess_game_name(path)
        _st             = FolderStats(path)      # one walk for size/count/param.json/artwork
        self.title_id   = parse_title_id(path, _st.param_jsons)
        self.size       = _st.size
        self.files      = _st.count
        self.artwork    = _st.artwork if path.is_dir() else None
        self.status     = "Queued"
        self.source_kind    = "inplace"   # a folder is packed in place - no second copy
        self.extracted_size = self.size   # already extracted; honest size for space math
        self.ampr_emu       = is_apr_game(path)   # PlayGo/APR title? (auto-detected)

    @classmethod
    def from_archive(cls, archive: Path) -> "GameItem":
        """Placeholder item for an archive that has not been extracted yet."""
        obj          = cls.__new__(cls)
        # Normalize a directly-dropped multi-part volume back to the FIRST volume so the
        # size and the whole-set logic always cover the entire game, not one part.
        first = ArchiveExtractor._first_volume(archive)
        obj.path         = None
        obj.archive_path = first
        obj.operation    = "pack"
        obj.name         = first.stem
        obj.title_id     = "📦"
        obj.size         = archive_set_ondisk_size(first)   # whole compressed volume set
        obj.files        = 0
        obj.artwork      = None
        obj.status       = "Pending Extract"
        obj.password     = None          # optional per-archive password override
        obj.source_kind  = "archive"     # unpacks a SECOND copy onto the build drive
        # Honest extracted size read from the archive headers (no extraction), with the
        # saved passwords for header-encrypted sets. header_locked says whether any of
        # them opened the header: only then is a password prompt worth showing. A header
        # that opens but holds an odd size keeps extracted_size 0 (the gate estimates).
        try:
            pw = [p.strip() for p in (load_settings().get("archive_passwords") or []) if str(p).strip()]
        except Exception:
            pw = []
        state, hdr, problem = ArchiveExtractor.probe_header_state(first, pw)
        obj.header_locked   = state == "locked"
        obj.archive_problem = problem if state == "damaged" else ""
        obj.extracted_size  = ArchiveExtractor.plausible_extracted_size(hdr, obj.size)
        return obj

    @classmethod
    def from_bundle(cls, bundle_dir: Path, game_source: Path, siblings, patch_source=None) -> "GameItem":
        """A source folder holding one game (archive / game folder / disk image) plus extra files (DLCs etc.)."""
        suffix = game_source.suffix.lower()
        if game_source.is_dir():
            obj = cls(game_source)
        elif suffix in DISK_IMAGE_SUFFIXES:
            obj = cls.from_exfat(game_source)
        else:
            obj = cls.from_archive(game_source)
        obj.bundle_dir       = bundle_dir
        obj.bundle_subfolder = bundle_dir.name
        obj.bundle_siblings  = list(siblings)
        obj.patch_source     = patch_source
        obj.name             = bundle_dir.name   # nicer queue label until extraction
        return obj

    @classmethod
    def from_exfat(cls, exfat_file: Path) -> "GameItem":
        """Item for a direct .exfat / .ffpkg disk image - passed straight to cli.py, no extraction needed."""
        obj              = cls.__new__(cls)
        obj.path         = exfat_file          # handed directly to the backend
        obj.archive_path = None                # not an archive - no extraction step
        obj.operation    = "pack"
        obj.name         = exfat_file.stem
        obj.title_id     = parse_title_id(exfat_file) or "💾"
        obj.size         = exfat_file.stat().st_size if exfat_file.exists() else 0
        obj.files        = 1
        obj.artwork      = None
        obj.status       = "Queued"
        obj.source_kind    = "inplace"   # disk image is read in place - no second copy
        obj._is_disk_image = True        # single-pass: mkpfs compresses directly, no temp inner image
        obj.extracted_size = obj.size
        return obj

    @classmethod
    def from_pfs_image(cls, image_file: Path) -> "GameItem":
        """Item for an existing .ffpfs / .ffpfsc image that should be unpacked."""
        obj              = cls.__new__(cls)
        obj.path         = image_file
        obj.archive_path = None
        obj.operation    = "unpack"
        obj.name         = image_file.stem
        obj.title_id     = parse_title_id(image_file) or "📤"
        obj.size         = image_file.stat().st_size if image_file.exists() else 0
        obj.files        = 1
        obj.artwork      = None
        obj.status       = "Queued"
        obj.source_kind    = "inplace"   # unpack op - no second copy on the build drive
        obj.extracted_size = obj.size
        return obj

    @classmethod
    def from_fake_sign(cls, folder: Path) -> "GameItem":
        """Job that recursively fake-signs the executables in *folder* in place."""
        obj              = cls.__new__(cls)
        obj.path         = folder
        obj.archive_path = None
        obj.operation    = "fake-sign"
        obj.name         = folder.name
        obj.title_id     = parse_title_id(folder) or "🖊"
        obj.size         = 0
        obj.files        = 0
        obj.artwork      = None
        obj.status       = "Queued"
        obj.source_kind    = "inplace"   # signs in place - no second copy
        obj.extracted_size = 0
        return obj

    @classmethod
    def from_patch(cls, game: Path, patch: Path, *, output_path=None,
                   overwrite: bool = False, inplace: bool = False) -> "GameItem":
        """Job that overlays *patch* onto *game* (a .ffpfsc / folder / archive) and
        repacks. Archives are resolved when the job reaches the front of the queue."""
        obj              = cls.__new__(cls)
        obj.path         = game
        obj.archive_path = None
        obj.operation    = "patch"
        obj.name         = (game.stem if game.suffix.lower() == ".ffpfsc" else game.name)
        try:
            obj.title_id = parse_title_id(game) or "🩹"
        except Exception:
            obj.title_id = "🩹"
        obj.size         = 0
        obj.files        = 0
        obj.artwork      = None
        obj.status       = "Queued"
        obj.source_kind    = "inplace"
        obj.extracted_size = 0
        obj.patch_source    = patch
        obj.output_path     = output_path
        obj.patch_overwrite = bool(overwrite)
        obj.patch_inplace   = bool(inplace)
        return obj

    @classmethod
    def from_fpkg_extract(cls, pkg_file: Path, *, output_path=None) -> "GameItem":
        """Job that extracts a PS5 fake package (.pkg) into a /app0-style folder."""
        obj              = cls.__new__(cls)
        obj.path         = pkg_file
        obj.archive_path = None
        obj.operation    = "fpkg-extract"
        obj.name         = pkg_file.stem
        # A PS5 package is named after its content id (UP9000-PPSA12345_00-…): lift the
        # title id straight out of the file name; fall back to the generic parser.
        _m = re.search(r"[A-Z]{4}[0-9]{5}", pkg_file.stem.upper())
        if _m:
            obj.title_id = _m.group(0)
        else:
            try:
                _t = parse_title_id(pkg_file)
                obj.title_id = _t if (_t and _t != "Unknown") else "📦"
            except Exception:
                obj.title_id = "📦"
        obj.size         = pkg_file.stat().st_size if pkg_file.exists() else 0
        obj.files        = 1
        obj.artwork      = None
        obj.status       = "Queued"
        obj.source_kind    = "inplace"
        obj.extracted_size = obj.size
        obj.output_path    = output_path
        return obj

    @classmethod
    def from_chain(cls, source: Path, *, to: str, output_path=None,
                   sign: bool = False, patch_source=None,
                   backport_target=None, backport_libs_root=None) -> "GameItem":
        """The one job the job dialog produces: source → [patch → backport → sign] → *to*."""
        src = Path(source)
        suf = src.suffix.lower() if src.is_file() else ""
        if src.is_dir():
            obj = cls(src)                                   # FolderStats, title id, artwork
        elif suf in ARCHIVE_SUFFIXES or re.match(r"^\.r\d{2,}$", suf):
            obj = cls.from_archive(src)                      # placeholder until extracted
        else:
            obj              = cls.__new__(cls)
            obj.path         = src
            obj.archive_path = None
            obj.name         = src.stem
            _m = re.search(r"[A-Z]{4}[0-9]{5}", src.stem.upper()) if suf == ".pkg" else None
            obj.title_id     = _m.group(0) if _m else (parse_title_id(src) or "📦")
            obj.size         = src.stat().st_size if src.exists() else 0
            obj.files        = 1
            obj.artwork      = None
            obj.status       = "Queued"
            obj.source_kind  = "inplace"
            obj.extracted_size = obj.size                    # the container is a floor for the unpacked size
            obj._is_disk_image = suf in DISK_IMAGE_SUFFIXES
        obj.operation          = "chain"
        obj.status             = "Pending Extract" if getattr(obj, "archive_path", None) else "Queued"
        obj.chain_to           = to if to in CHAIN_TARGETS else "ffpfsc"
        obj.chain_sign         = bool(sign)
        obj.patch_source       = Path(patch_source) if patch_source else None
        obj.backport_target    = backport_target if is_backport_target(backport_target) else None
        obj.backport_libs_root = str(backport_libs_root) if backport_libs_root else None
        obj.output_path        = Path(output_path) if output_path else None
        obj.output_compressed  = (obj.chain_to != "ffpfs")
        return obj

    @classmethod
    def from_fpkg_build(cls, source: Path, *, output_path=None,
                        content_id: str = "",
                        title_id: str = "",
                        title: str = "",
                        version: str = "01.000.000",
                        inner_mode: str = "none",
                        kraken_backend: str = "builtin",
                        pubtools_dll: str = "",
                        level: int = 7) -> "GameItem":
        """Job that builds a PS5 fake package (.pkg) from a prepared /app0 folder OR a
        packed image (.ffpfsc/.ffpfs/.exfat/.ffpkg - the backend unwraps it first)."""
        obj              = cls.__new__(cls)
        obj.path         = source
        obj.archive_path = None
        obj.operation    = "fpkg-build"
        obj.name         = source.stem if source.is_file() else source.name
        obj.title_id     = title_id or (parse_title_id(source) or "📦")
        try:
            obj.size = source.stat().st_size if source.is_file() else get_folder_size(source)
        except Exception:
            obj.size = 0
        obj.files        = 0
        obj.artwork      = None
        obj.status       = "Queued"
        obj.source_kind    = "inplace"
        obj.extracted_size = obj.size
        obj.output_path    = output_path
        obj.fpkg_level     = int(level)
        # fPKG parameters (persisted with the queue item so re-adds keep them)
        obj.fpkg_content_id     = content_id
        obj.fpkg_title_id       = title_id
        obj.fpkg_title          = title
        obj.fpkg_version        = version
        obj.fpkg_inner_mode     = inner_mode
        obj.fpkg_kraken_backend = kraken_backend
        obj.fpkg_pubtools_dll   = pubtools_dll
        return obj



# Everything above is re-exported by the GUI module (underscore names included).
__all__ = [
    "APP_DIR",
    "strip_written_clutter",
    "prune_empty_scratch",
    "SCRATCH_DIR_NAMES",
    "ident_from_folder_name",
    "_ENV_APP_DIR",
    "_LEGACY_APP_DIRS",
    "RAW_LOG_FILE",
    "FINAL_REPORT_FILE",
    "HISTORY_FILE",
    "SETTINGS_FILE",
    "TITLE_RE",
    "PROGRESS_RE",
    "PFS_IMAGE_SUFFIXES",
    "DISK_IMAGE_SUFFIXES",
    "ensure_app_dir",
    "open_path",
    "now_time",
    "now_datetime",
    "format_size",
    "format_duration",
    "eta_seconds",
    "job_time_left",
    "LONG_STEPS",
    "humanize_eta",
    "get_free_space",
    "get_total_space",
    "same_drive",
    "space_safety_factor",
    "ARCHIVE_PEAK_FACTOR",
    "INPLACE_PEAK_FACTOR",
    "PATCH_PEAK_FACTOR",
    "PKG_UNPACK_PEAK_FACTOR",
    "IMAGE_PEAK_FACTOR",
    "archive_set_ondisk_size",
    "archive_set_parts",
    "_item_is_single_pass",
    "_build_size_of",
    "shows_extracted_size",
    "display_size",
    "_APP_TMP_RE",
    "_is_app_tmp_dir",
    "_peak_factor_for",
    "estimate_peak_space_needed",
    "estimate_image_space_needed",
    "COMPRESSED_OUTPUT_RATIO",
    "estimate_output_space_needed",
    "_space_requirements",
    "_space_preflight_ok",
    "_fs_status",
    "_space_report",
    "get_folder_size",
    "find_newest_ffpfsc_after",
    "compression_rating",
    "_DRIVE_TYPE_CACHE",
    "_drive_cache_key",
    "get_drive_type",
    "drive_type_cached",
    "temp_drive_label",
    "KEEP_AWAKE_FILENAME",
    "poke_drive_keepalive",
    "_name_looks_ssd",
    "_probe_drive_speed",
    "_probe_drive_type",
    "get_filesystem_type",
    "is_game_folder",
    "find_game_folders",
    "find_job_sources",
    "is_archive_file",
    "JOB_CONTAINER_SUFFIXES",
    "find_files_by_suffix",
    "has_any_files",
    "validate_game_structure",
    "_ERROR_PATTERNS",
    "smart_error_from_log",
    "get_backend_python_command",
    "backend_base_dir",
    "folder_size",
    "file_count",
    "FolderStats",
    "parse_title_id",
    "guess_game_name",
    "guess_game_version",
    "AMPR_SPRX_FILES",
    "is_apr_game",
    "MAX_FILENAME_BYTES",
    "SHADOWMOUNT_NAME_LIMIT",
    "_truncate_to_bytes",
    "short_version",
    "sanitize_filename",
    "_EDITION_FLUFF_RE",
    "_strip_edition_fluff",
    "descriptive_ffpfsc_name",
    "canonical_game_title",
    "ident_from_param_bytes",
    "organized_names",
    "find_artwork",
    "ART_DIR",
    "art_source_key",
    "art_cache_file",
    "store_art",
    "prune_art_cache",
    "load_history",
    "save_history",
    "_SETTINGS_UNREADABLE",
    "_SETTINGS_CORRUPT_COPY",
    "load_settings",
    "save_settings",
    "CHAIN_TARGETS",
    "CHAIN_TARGET_LABEL",
    "ORGANIZE_TARGET",
    "LibItem",
    "library_layout",
    "ARCHIVE_SUFFIXES",
    "chain_source_kind",
    "chain_changes",
    "is_backport_target",
    "chain_needs_unpack",
    "chain_summary",
    "is_first_run",
    "get_last_log_lines",
    "ArchiveExtractionCancelled",
    "ArchivePasswordError",
    "ArchiveToolError",
    "ArchiveExtractor",
    "EXTRA_JUNK_EXTS",
    "FS_JUNK_NAMES",
    "is_fs_junk_name",
    "strip_fs_junk",
    "source_label",
    "archive_label",
    "shown_title_id",
    "_COPYTREE_JUNK_GLOBS",
    "detect_game_bundle",
    "scan_parent_for_bundles",
    "GameItem",
]
