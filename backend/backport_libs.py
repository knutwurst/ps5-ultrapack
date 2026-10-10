"""Prepare a folder of backport libraries from the user's own firmware libraries."""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

try:
    from . import bps_patch as bps
except ImportError:                              # for `python backend/cli.py`
    import bps_patch as bps                       # type: ignore[no-redef]


# ── target inventory ─────────────────────────────────────────────────────
# BackPork's own layout: patches/6xx/ and patches/7xx/. The library filename
# and its bundle target follow the target-firmware convention.
TARGETS: dict[str, str] = {"6.02": "6xx", "7.61": "7xx"}

_REPO_OWNER = "BestPig"
_REPO_NAME = "BackPork"
_CONTENTS_URL = "https://api.github.com/repos/{owner}/{repo}/contents/patches/{sub}"
_RAW_URL = "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/patches/{sub}/{name}"
_USER_AGENT = "ps5-ultrapack"


class BackportLibsError(RuntimeError):
    """A step failed with a message the user should see (the caller writes it to the log
    and shows it in the UI)."""


# ── HTTP (small, dependency-free) ────────────────────────────────────────
def _http_get(url: str, *, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/vnd.github.raw, application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise BackportLibsError(f"GitHub returned HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise BackportLibsError(f"could not reach GitHub for {url}: {e.reason}") from e


def _list_patches(subdir: str) -> list[dict]:
    """List the patches under BestPig/BackPork/patches/<subdir>. Returns a list of
    {"name", "sha", "size"} taken straight from the GitHub contents API."""
    url = _CONTENTS_URL.format(owner=_REPO_OWNER, repo=_REPO_NAME, sub=subdir)
    body = _http_get(url)
    data = json.loads(body)
    if not isinstance(data, list):
        raise BackportLibsError(f"GitHub response for {subdir} is not a directory listing")
    return [{"name": e["name"], "sha": e["sha"], "size": int(e["size"])}
            for e in data if isinstance(e, dict) and e.get("type") == "file"
            and e.get("name", "").endswith(".bps")]


def _cache_manifest_path(cache_dir: Path, target: str) -> Path:
    return cache_dir / f"{TARGETS[target]}.manifest.json"


def _read_cache(cache_dir: Path, target: str) -> dict[str, str]:
    """Return {filename: sha} from the target's manifest, or {} when there is none."""
    p = _cache_manifest_path(cache_dir, target)
    if not p.is_file():
        return {}
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _write_cache(cache_dir: Path, target: str, shas: dict[str, str]) -> None:
    _cache_manifest_path(cache_dir, target).write_text(
        json.dumps(shas, indent=2, sort_keys=True), encoding="utf-8")


@dataclass
class DownloadReport:
    downloaded: list[str]                           # names newly fetched or refreshed
    up_to_date: list[str]                           # names the cache already had


def download_patches(target: str, cache_dir: Path,
                     log: Callable[[str], None] | None = None) -> DownloadReport:
    """Ensure *cache_dir/<sub>/*.bps* holds the current patches for *target*. Files whose
    manifest SHA still matches the server are left alone; a new or changed file is
    downloaded from raw.githubusercontent.com. Returns a small report."""
    if target not in TARGETS:
        raise BackportLibsError(f"unknown backport target {target!r}; expected 7.61 or 6.02")
    sub = TARGETS[target]
    cache_dir = Path(cache_dir)
    dst = cache_dir / sub
    dst.mkdir(parents=True, exist_ok=True)
    listing = _list_patches(sub)
    if not listing:
        raise BackportLibsError(f"no patches found in {sub} on GitHub")

    have = _read_cache(cache_dir, target)
    downloaded, up_to_date = [], []
    fresh_shas: dict[str, str] = {}
    for entry in listing:
        name, sha, size = entry["name"], entry["sha"], entry["size"]
        fresh_shas[name] = sha
        local = dst / name
        if have.get(name) == sha and local.is_file() and local.stat().st_size == size:
            up_to_date.append(name); continue
        url = _RAW_URL.format(owner=_REPO_OWNER, repo=_REPO_NAME, sub=sub, name=name)
        body = _http_get(url)
        if len(body) != size:
            raise BackportLibsError(f"downloaded {name} is {len(body)} bytes, expected {size}")
        local.write_bytes(body)
        downloaded.append(name)
        if log:
            log(f"  [download] {sub}/{name} ({size} B)")
    _write_cache(cache_dir, target, fresh_shas)
    return DownloadReport(downloaded=downloaded, up_to_date=up_to_date)


# ── prepare ──────────────────────────────────────────────────────────────
_LIB_SUFFIXES = (".sprx", ".prx")


def _find_source_library(patch_stem: str, fw_libs_root: Path) -> Path | None:
    """The patch is <libname>.bps; look for <libname>.sprx first, then .prx, in
    *fw_libs_root* (root or one level deep) - Sony's library folder can have subdirs."""
    for suf in _LIB_SUFFIXES:
        p = fw_libs_root / (patch_stem + suf)
        if p.is_file():
            return p
    for suf in _LIB_SUFFIXES:
        for p in fw_libs_root.rglob(patch_stem + suf):
            if p.is_file():
                return p
    return None


@dataclass
class PrepareResult:
    target: str
    patched: list[tuple[str, int]]                   # (filename, size)
    missing_source: list[str]                        # patches whose source library is not here
    failed: list[tuple[str, str]]                    # (filename, error)

    def summary(self) -> str:
        parts = [f"{len(self.patched)} lib(s) patched"]
        if self.missing_source:
            parts.append(f"{len(self.missing_source)} without a source library")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)


def prepare_target(target: str, fw_libs_root: Path, out_root: Path,
                   cache_dir: Path | None = None,
                   log: Callable[[str], None] | None = None) -> PrepareResult:
    """Download the BackPork patches for *target* (using or refreshing *cache_dir*) and
    write the patched libraries into *out_root* / *target* /. *fw_libs_root* must hold
    the user's 10.01 libraries - BackPork patches only accept a 10.01 source. Missing or
    mismatching sources are reported, never guessed at."""
    if target not in TARGETS:
        raise BackportLibsError(f"unknown backport target {target!r}; expected 7.61 or 6.02")
    fw_libs_root = Path(fw_libs_root)
    if not fw_libs_root.is_dir():
        raise BackportLibsError(f"firmware libraries folder does not exist: {fw_libs_root}")
    out_dir = Path(out_root) / target
    out_dir.mkdir(parents=True, exist_ok=True)

    if cache_dir is None:
        cache_dir = Path(out_root) / ".backport-patches"
    report = download_patches(target, Path(cache_dir), log=log)
    if log:
        if report.downloaded:
            log(f"[patches] {target}: downloaded {len(report.downloaded)}, up to date {len(report.up_to_date)}")
        else:
            log(f"[patches] {target}: {len(report.up_to_date)} patch(es) up to date")

    result = PrepareResult(target=target, patched=[], missing_source=[], failed=[])
    for patch_file in sorted((Path(cache_dir) / TARGETS[target]).glob("*.bps")):
        stem = patch_file.stem
        source = _find_source_library(stem, fw_libs_root)
        if source is None:
            result.missing_source.append(stem)
            if log:
                log(f"  [skip] no {stem}.sprx or .prx under {fw_libs_root}")
            continue
        try:
            patched = bps.apply(patch_file.read_bytes(), source.read_bytes())
        except bps.BpsError as e:
            result.failed.append((stem, str(e)))
            if log:
                log(f"  [fail] {stem}: {e}")
            continue
        dst = out_dir / (stem + source.suffix)
        dst.write_bytes(patched)
        result.patched.append((dst.name, len(patched)))
        if log:
            log(f"  [patched] {dst.name} ({len(patched):,} B)")
    if log:
        log(f"[prepare] {target}: {result.summary()} → {out_dir}")
    return result


def prepare_targets(targets: Iterable[str], fw_libs_root: Path, out_root: Path,
                    cache_dir: Path | None = None,
                    log: Callable[[str], None] | None = None) -> dict[str, PrepareResult]:
    """Prepare several targets in one call. Returns a dict target → result. A failure on
    one target does not stop the others; each raises through the log."""
    out: dict[str, PrepareResult] = {}
    for t in targets:
        try:
            out[t] = prepare_target(t, fw_libs_root, out_root, cache_dir=cache_dir, log=log)
        except BackportLibsError as e:
            if log:
                log(f"[error] {t}: {e}")
            out[t] = PrepareResult(target=t, patched=[], missing_source=[], failed=[("", str(e))])
    return out
