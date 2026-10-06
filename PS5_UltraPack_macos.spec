# -*- mode: python ; coding: utf-8 -*-

import re as _re
from PyInstaller.utils.hooks import collect_all, collect_data_files

# Single source of truth: read APP_VERSION straight from the app script so the
# bundle version always matches what the UI shows (bump APP_VERSION only).
_m = _re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                open("PS5_UltraPack.py", encoding="utf-8").read(), _re.M)
APP_VERSION = _m.group(1) if _m else "1.0"


import os as _os

# backend/ ships as data files, minus everything that does not run inside the app.
# Directories (relative to backend/) holding no runtime material:
_BACKEND_SKIP_DIRS = {
    _os.path.join("native", "src"),    # fPKG tool sources + .NET build inputs/outputs (~160 MB)
    "tests",                           # test suites
    _os.path.join("unrar", "src"),     # UnRAR C++ sources (compiled into unrar/_unrar*.so)
    _os.path.join("unrar", "build"),   # setuptools build tree of that extension
    ".github",
}
# Build inputs, packaging metadata and documentation, not runtime files:
_BACKEND_SKIP_FILES = {".gitignore", ".DS_Store", "setup.py", "pyproject.toml", "_unrar.cpp"}
_BACKEND_SKIP_SUFFIXES = (".pyc", ".pyo", ".md")
# Files every bundle must contain; a missing one is a broken build, so fail early.
_BACKEND_REQUIRED = [
    "cli.py", "fpkg.py", "copy_job.py", "fake_sign.py", "make_fself.py",
    "backport.py", "self_file.py", "backport_libs.py", "bps_patch.py",
    _os.path.join("mkpfs", "cli.py"), _os.path.join("mkpfs", "pfs.py"),
    _os.path.join("unrar", "__init__.py"), _os.path.join("unrar", "rarfile.py"),
    _os.path.join("native", "ffpfsc-pkg-tool"),
    _os.path.join("native", "Magick.Native-Q8-arm64.dll.dylib"),   # loaded from the tool's folder
    _os.path.join("native", "LICENSE.LibProsperoPkg"),
    _os.path.join("native", "LICENSE.LibOrbisPkg"),
]


def _backend_datas():
    """backend/ as (source, dest_dir) data pairs: the runtime modules (cli.py, fpkg.py,
    copy_job.py, fake_sign.py, make_fself.py), mkpfs/, the unrar package with its compiled
    extension, the native fPKG tool and the license/notice texts. Tests, C++ and C# sources,
    build trees, packaging metadata, docs and bytecode caches stay out."""
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
        raise SystemExit("PS5_UltraPack_macos.spec: runtime files missing from backend/: " + ", ".join(missing))
    return out


datas = _backend_datas()
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
        "ultra_core",   # the Tk-free core next to the app script
        "ui_kit",       # the main window widgets next to the app script
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
        "mkpfs",
        "mkpfs.cli",
        "mkpfs.pfs",
        "mkpfs.utils",
        "mkpfs.logging",
        "mkpfs.pbar",
        # MkPFS 1.0.0 companion modules (pfs.py/cli.py hard-import these siblings).
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
        "fpkg",   # new fPKG build/extract wrapper (invokes backend/native/ffpfsc-pkg-tool)
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
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PS5 UltraPack",
)

app = BUNDLE(
    coll,
    name="PS5 UltraPack.app",
    icon=None,
    # Own identifier (distinct from the older PS5 FFPFSC PRO and ULTRA bundles) so Launch
    # Services associates .ffpfsc with THIS app, not a legacy bundle.
    bundle_identifier="com.knutwurst.ps5ultrapack",
    info_plist={
        "CFBundleDisplayName": "PS5 UltraPack",
        "CFBundleName": "PS5 UltraPack",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # Double-click a .ffpfsc / .ffpfs in Finder -> the app opens the PFS browser for
        # it (a cold launch shows ONLY the browser; see _wire_open_document in the GUI).
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "PS5 PFS image",
                "CFBundleTypeExtensions": ["ffpfsc", "ffpfs"],
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Owner",
            },
        ],
    },
)
