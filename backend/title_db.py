"""Game names by title id, for a job whose game cannot be read before it is unpacked (an
image or a game folder inside a solid 7z or RAR): the public PlayStation title lists by
andshrew (https://github.com/andshrew/PlayStation-Titles, MIT), one for PS5 (PPSA) and one
for PS4 (CUSA). A list is downloaded when it is first needed, kept in the app folder and
fetched again when it is a week old; without a connection the copy on disk is used, and
without one nothing changes. The name only labels the job until the unpacked game's own
param.json names it."""
from __future__ import annotations

import os
import re
import ssl
import threading
import time
import urllib.request
from pathlib import Path

_BASE = "https://raw.githubusercontent.com/andshrew/PlayStation-Titles/main/"
LISTS = {"PPSA": "PS5_Titles.tsv", "CUSA": "PS4_Titles.tsv"}
MAX_AGE = 7 * 86400              # fetch a list again after a week
RETRY_AFTER = 600                # after a failed fetch (two tries), the next waits 10 minutes
RETRY_PAUSE = 2.0                # between the two tries of one fetch (a dropped connection)
_REGIONS = ("UP", "EP", "HP", "IP", "KP", "JP")   # English names first
_TITLE_ID = re.compile(r"\b(PPSA\d{5}|CUSA\d{5})\b", re.I)

_lock = threading.Lock()
_tables: dict = {}               # {(path, mtime): {title id: name}}
_failed: dict = {}               # {url: time of the last failed fetch}
LAST_ERROR = ""                  # why the last fetch failed ('' after a good one)


def _ssl_context() -> ssl.SSLContext:
    """The default trust store, else certifi's, else macOS's own bundle."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca"):
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    if os.path.isfile("/etc/ssl/cert.pem"):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ctx


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "PS5-UltraPack"})
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as r:
        return r.read()


FETCH = _download                # replaced by the tests


def reset() -> None:
    """Forget the lists in memory and the failed fetches (tests)."""
    with _lock:
        _tables.clear()
        _failed.clear()


def parse(text: str) -> dict:
    """{title id: name} from a list: one row per title and region; an English name wins."""
    lines = text.splitlines()
    if not lines:
        return {}
    head = {h.strip(): i for i, h in enumerate(lines[0].split("\t"))}
    ti, ni, ri = head.get("titleId"), head.get("name"), head.get("region")
    if ti is None or ni is None:
        return {}
    best: dict = {}
    for line in lines[1:]:
        f = line.split("\t")
        if len(f) <= max(ti, ni):
            continue
        tid, name = f[ti][:9].upper(), f[ni].strip()
        region = f[ri].strip().upper() if ri is not None and len(f) > ri else ""
        rank = _REGIONS.index(region) if region in _REGIONS else len(_REGIONS)
        if name and (tid not in best or rank < best[tid][0]):
            best[tid] = (rank, name)
    return {k: v[1] for k, v in best.items()}


def title_id_in(*names) -> str:
    """The first PPSA/CUSA title id in *names* (an archive's name, its folder), or ''."""
    for n in names:
        m = _TITLE_ID.search(str(n or ""))
        if m:
            return m.group(1).upper()
    return ""


def lookup(title_id: str, cache_dir, online: bool = True) -> str | None:
    """The name the title list gives *title_id* ('PPSA01234' / 'CUSA01234'), or None. With
    *online* a missing or week-old list is fetched first (tried twice; after a failure not
    again for 10 minutes); a failed fetch keeps the list on disk."""
    tid = (title_id or "").strip().upper()[:9]
    fname = LISTS.get(tid[:4])
    if fname is None or not re.fullmatch(r"[A-Z]{4}\d{5}", tid):
        return None
    path = Path(cache_dir) / fname
    url = _BASE + fname
    with _lock:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = None
        if online and (age is None or age > MAX_AGE) and time.time() - _failed.get(url, 0) > RETRY_AFTER:
            global LAST_ERROR
            try:
                try:
                    data = FETCH(url)
                except OSError:
                    time.sleep(RETRY_PAUSE)            # seen: a TLS connection cut once, fine the next time
                    data = FETCH(url)
                if b"titleId\t" not in data[:200]:
                    raise ValueError("not a title list")
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".part")
                tmp.write_bytes(data)
                os.replace(tmp, path)
                LAST_ERROR = ""
            except Exception as e:
                _failed[url] = time.time()
                LAST_ERROR = f"{type(e).__name__}: {e}"
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            return None
        table = _tables.get(key)
        if table is None:
            try:
                table = parse(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return None
            for k in [k for k in _tables if k[0] == key[0]]:
                del _tables[k]
            _tables[key] = table
    return table.get(tid)
