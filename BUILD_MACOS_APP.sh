#!/usr/bin/env bash
# build the macOS app, release zip and SHA-256; run from the repo root in a venv
set -euo pipefail

# pinned build deps go into a venv, never the system Python
if [[ -z "${VIRTUAL_ENV:-}" ]] \
   && ! python3 -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)'; then
    echo "BUILD_MACOS_APP.sh: not inside a virtual environment. Create and activate one first:" >&2
    echo "  python3 -m venv .venv && source .venv/bin/activate" >&2
    exit 1
fi

# fPKG helper always built from source, never taken from git
./BUILD_PKG_TOOL.sh
# 7-Zip, pinned and checked, from 7-zip.org
./FETCH_7ZIP.sh
# OpenSSL for the fPKG helper, a Mac without Homebrew has none it can load
./BUILD_OPENSSL.sh
# ampr_emu runtime for LZ4 packs (GPL-3); the packer lives in backend/ampr_pack
./FETCH_AMPR_EMU.sh

python3 -m pip install -r requirements-build.txt
python3 -m pip install ./backend/unrar
(cd backend/unrar && python3 setup.py build_ext --inplace)
python3 -m PyInstaller --clean --noconfirm PS5_UltraPack_macos.spec

APP="dist/PS5 UltraPack.app"

# APP_VERSION from the main script; quotes are hex escapes so it fits in single quotes
VERSION="$(python3 -c 'import re; m = re.search(r"^APP_VERSION\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]", open("PS5_UltraPack.py", encoding="utf-8").read(), re.M); print(m.group(1) if m else "1.0")')"

ZIP="PS5-UltraPack-${VERSION}-macos-arm64.zip"
rm -f "dist/${ZIP}" "dist/${ZIP}.sha256"
ditto -c -k --keepParent "${APP}" "dist/${ZIP}"
(cd dist && shasum -a 256 "${ZIP}" > "${ZIP}.sha256")

echo "Built: ${APP}"
echo "Release: dist/${ZIP}"
cat "dist/${ZIP}.sha256"
