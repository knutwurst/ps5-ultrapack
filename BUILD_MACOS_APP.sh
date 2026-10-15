#!/usr/bin/env bash
# Build "dist/PS5 UltraPack.app" (macOS, Apple Silicon) plus the release zip and its
# SHA-256. Run from the repository root inside an activated virtual environment.
set -euo pipefail

# The pinned build inputs go into a virtual environment, never into the system Python.
if [[ -z "${VIRTUAL_ENV:-}" ]] \
   && ! python3 -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)'; then
    echo "BUILD_MACOS_APP.sh: not inside a virtual environment. Create and activate one first:" >&2
    echo "  python3 -m venv .venv && source .venv/bin/activate" >&2
    exit 1
fi

# The fPKG helper is built from source (backend/native/src), never taken from git.
./BUILD_PKG_TOOL.sh
# 7-Zip, pinned and checked, from 7-zip.org
./FETCH_7ZIP.sh

python3 -m pip install -r requirements-build.txt
python3 -m pip install ./backend/unrar
(cd backend/unrar && python3 setup.py build_ext --inplace)
python3 -m PyInstaller --clean --noconfirm PS5_UltraPack_macos.spec

APP="dist/PS5 UltraPack.app"

# APP_VERSION in the main script is the single source of truth: the spec's regex, with the
# quote characters written as \x22 / \x27 so the expression can sit inside single quotes.
VERSION="$(python3 -c 'import re; m = re.search(r"^APP_VERSION\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]", open("PS5_UltraPack.py", encoding="utf-8").read(), re.M); print(m.group(1) if m else "1.0")')"

ZIP="PS5-UltraPack-${VERSION}-macos-arm64.zip"
rm -f "dist/${ZIP}" "dist/${ZIP}.sha256"
ditto -c -k --keepParent "${APP}" "dist/${ZIP}"
(cd dist && shasum -a 256 "${ZIP}" > "${ZIP}.sha256")

echo "Built: ${APP}"
echo "Release: dist/${ZIP}"
cat "dist/${ZIP}.sha256"
