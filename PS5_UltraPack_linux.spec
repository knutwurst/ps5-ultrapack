# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later AND MIT
# Copyright (C) 2026 Giorgi Kakabadze (trongio), contributed under the MIT license, see NOTICE
# Copyright (C) 2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply to Knutwurst's parts, see the NOTICE file.

import re as _re
from PyInstaller.utils.hooks import collect_all, collect_data_files

# APP_VERSION lives in the app script only; bump it there
_m = _re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                open("PS5_UltraPack.py", encoding="utf-8").read(), _re.M)
APP_VERSION = _m.group(1) if _m else "1.0"


import os as _os

# backend/ ships as data, minus what never runs inside the app
_BACKEND_SKIP_DIRS = {
    _os.path.join("native", "src"),  # fPKG tool sources and .NET build trees (~160 MB)
    "tests",
    _os.path.join("unrar", "src"),     # UnRAR C++ sources (compiled into unrar/_unrar*.so)
    _os.path.join("unrar", "build"),
    _os.path.join("unrar", "build-win"),
    ".github",
}
_BACKEND_SKIP_FILES = {".gitignore", ".DS_Store", "setup.py", "pyproject.toml", "_unrar.cpp", "build.log",
                       "LICENSE.7-Zip.windows", "LICENSE.7-Zip.macos", "LICENSE.OpenSSL"}
# the macOS and Windows builds share backend/ with these
_BACKEND_SKIP_SUFFIXES = (".pyc", ".pyo", ".md", ".exe", ".dll", ".pyd", ".dylib")
# fail early: a bundle missing any of these is broken
_BACKEND_REQUIRED = [
    "cli.py", "fpkg.py", "copy_job.py", "fake_sign.py", "make_fself.py",
    "backport.py", "self_file.py", "backport_libs.py", "bps_patch.py",
    "organize_lib.py", "ps4pkg.py", "ps4_sort.py", "after_job.py", "title_db.py", "patch_backup.py",
    "lz4_packs.py", _os.path.join("ampr_pack", "ampr_pack.py"), _os.path.join("ampr_pack", "ampr_pack_format.py"),
    _os.path.join("ampr_pack", "build_ampr_index.py"), _os.path.join("ampr_pack", "LICENSE"),
    _os.path.join("native", "ampr", "libSceAmpr.sprx"), _os.path.join("native", "ampr", "LICENSE.ampr_emu"),   # FETCH_AMPR_EMU.sh
    _os.path.join("mkpfs", "cli.py"), _os.path.join("mkpfs", "pfs.py"),
    _os.path.join("unrar", "__init__.py"), _os.path.join("unrar", "rarfile.py"),
    _os.path.join("native", "ffpfsc-pkg-tool"),
    _os.path.join("native", "Magick.Native-Q8-x64.dll.so"),   # loaded from the tool's folder
    _os.path.join("native", "7zz"), _os.path.join("native", "LICENSE.7-Zip.linux"),   # BUILD_LINUX_APP.sh
    _os.path.join("native", "LICENSE.LibProsperoPkg"),
    _os.path.join("native", "LICENSE.LibOrbisPkg"),
]


def _backend_datas():
    out = []
    for root, dirs, files in _os.walk("backend"):
        rel = _os.path.relpath(root, "backend")
        if rel == ".":
            rel = ""
        dirs[:] = sorted(
            d for d in dirs
            if _os.path.join(rel, d) not in _BACKEND_SKIP_DIRS
            and d != "__pycache__" and not d.endswith(".egg-info")
        )
        for f in sorted(files):
            if f in _BACKEND_SKIP_FILES or f.endswith(_BACKEND_SKIP_SUFFIXES):
                continue
            out.append((_os.path.join(root, f), root))
    bundled = {_os.path.relpath(src, "backend") for src, _ in out}
    missing = [p for p in _BACKEND_REQUIRED if p not in bundled]
    if not any(p.startswith(_os.path.join("unrar", "_unrar")) and p.endswith(".so") for p in bundled):
        missing.append("unrar/_unrar*.so (build it: cd backend/unrar && python3 setup.py build_ext --inplace)")
    if missing:
        raise SystemExit("PS5_UltraPack_linux.spec: runtime files missing from backend/: " + ", ".join(missing))
    return out


datas = _backend_datas()
# window icon the app sets at start; the bundle icon comes further down
datas += [(_os.path.join("assets", "icon", n), _os.path.join("assets", "icon"))
          for n in ("app-icon-256.png", "app-icon-32.png")]
# license, NOTICE and, when GitHub Actions builds, the repo and commit for the About page
datas += [("LICENSE", "."), ("NOTICE", ".")]
_build_repo = _re.sub(r"[^A-Za-z0-9._/-]", "", _os.environ.get("GITHUB_REPOSITORY", ""))
_build_sha = _re.sub(r"[^0-9a-f]", "", _os.environ.get("GITHUB_SHA", ""))[:7]
if _build_repo and _build_sha:
    import json as _json
    _os.makedirs("build", exist_ok=True)
    with open(_os.path.join("build", "build_info.json"), "w", encoding="utf-8") as _f:
        _json.dump({"repo": _build_repo, "commit": _build_sha}, _f)
    datas += [(_os.path.join("build", "build_info.json"), ".")]
datas += collect_data_files("customtkinter")
datas += collect_data_files("tkinterdnd2")
cryptography_datas, cryptography_binaries, cryptography_hiddenimports = collect_all("cryptography")
datas += cryptography_datas


a = Analysis(
    ["PS5_UltraPack.py"],
    pathex=["backend", "backend/unrar"],
    binaries=cryptography_binaries,
    datas=datas,
    hiddenimports=cryptography_hiddenimports + [
        "ultra_core",
        "ui_kit",
        "i18n",
        "i18n_de",  # imported lazily when German is chosen
        "py7zr",
        "py7zr.helpers",
        "py7zr.compressor",
        "rarfile",
        "unrar",
        "unrar.rarfile",
        "unrar._unrar",
        "tkinterdnd2",
        "psutil",
        "PIL._tkinter_finder",
        "cryptography",
        "cryptography.hazmat.primitives.ciphers",
        # ampr_pack (LZ4 asset packs) imports these only at run time
        "lz4", "lz4.block", "tomllib", "ftplib", "statistics", "fnmatch", "bisect", "heapq",
        "concurrent.futures", "urllib.parse",
        "mkpfs",
        "mkpfs.cli",
        "mkpfs.pfs",
        "mkpfs.utils",
        "mkpfs.logging",
        "mkpfs.pbar",
        # pfs.py and cli.py hard-import these siblings
        "mkpfs.consts",
        "mkpfs.compression",
        "mkpfs.exfat",
        "mkpfs.exfat_writer",
        "mkpfs._exfat_upcase",
        "mkpfs.ampr",
        "mkpfs.gather",
        "mkpfs.batch",
        "mkpfs.game_metadata",
        "make_fself",
        "fake_sign",
        "fpkg",
        "argparse",
        "contextlib",
        "dataclasses",
        "enum",
        "hashlib",
        "hmac",
        "json",
        "multiprocessing",
        "queue",
        "shutil",
        "struct",
        "tempfile",
        "uuid",
        "zlib",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PS5 UltraPack",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PS5 UltraPack",
)
