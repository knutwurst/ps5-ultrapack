#!/usr/bin/env bash
# build the Linux x64 app, release tarball and SHA-256; run from the repo root in a venv
# needs .NET SDK 9+, a C++ compiler (g++), python3-dev and python3-tk from the distro
set -euo pipefail
cd "$(dirname "$0")"

if [[ -z "${VIRTUAL_ENV:-}" ]] \
   && ! python3 -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)'; then
    echo "BUILD_LINUX_APP.sh: not inside a virtual environment. Create and activate one first:" >&2
    echo "  /usr/bin/python3 -m venv .venv && source .venv/bin/activate" >&2
    exit 1
fi
python3 -c 'import tkinter' 2>/dev/null || {
    echo "BUILD_LINUX_APP.sh: this Python has no Tk; install python3-tk (apt install python3-tk)" >&2
    exit 1
}
# Tk compiled into the interpreter (uv / python-build-standalone) carries its own Xlib, and
# tkdnd then loads the system one: the two share the display and abort on the first drag-and-drop init
python3 -c 'import _tkinter, sys; sys.exit(0 if hasattr(_tkinter, "__file__") else 1)' || {
    echo "BUILD_LINUX_APP.sh: this Python has Tk built in statically, which crashes with tkdnd under X11." >&2
    echo "  Build the venv from the distro Python instead: /usr/bin/python3 -m venv .venv" >&2
    exit 1
}

DOTNET="${DOTNET:-$(command -v dotnet || true)}"
[[ -n "${DOTNET}" ]] || DOTNET="${HOME}/.dotnet/dotnet"
if [[ ! -x "${DOTNET}" ]]; then
    echo "BUILD_LINUX_APP.sh: the .NET SDK (9 or newer) is required: https://dotnet.microsoft.com/download" >&2
    exit 1
fi

# fPKG helper, from source
SRC="backend/native/src/ffpfsc-pkg-tool"
OUT="${SRC}/out-linux"
rm -rf "${OUT}"
"${DOTNET}" publish "${SRC}/PkgTool.csproj" -c Release -r linux-x64 --self-contained true -o "${OUT}"
for f in ffpfsc-pkg-tool Magick.Native-Q8-x64.dll.so; do
    install -m 755 "${OUT}/${f}" "backend/native/${f}"
done
rm -rf "${OUT}"

# 7-Zip, pinned and checked; 7zzs is the static build, so no glibc version ties it down
VER=2604
LINUX_SHA=fc0327ba27e89bd086cf426dff17d77de582953cdbbc10a6576540a06853ffcd
DL="build/7zip-${VER}"
TAR="${DL}/7z${VER}-linux-x64.tar.xz"
mkdir -p "${DL}"
if [[ ! -f "${TAR}" ]] || ! echo "${LINUX_SHA}  ${TAR}" | sha256sum -c --status; then
    curl -fsSL -o "${TAR}" "https://www.7-zip.org/a/7z${VER}-linux-x64.tar.xz"
fi
echo "${LINUX_SHA}  ${TAR}" | sha256sum -c --quiet
rm -rf "${DL}/linux"
mkdir -p "${DL}/linux"
tar -xJf "${TAR}" -C "${DL}/linux" 7zzs License.txt
install -m 755 "${DL}/linux/7zzs" backend/native/7zz
install -m 644 "${DL}/linux/License.txt" backend/native/LICENSE.7-Zip.linux

# ampr_emu runtime for LZ4 packs (GPL-3)
./FETCH_AMPR_EMU.sh

python3 -m pip install -r requirements-build.txt
python3 -m pip install setuptools
(cd backend/unrar && python3 setup.py build_ext --inplace)
python3 -m PyInstaller --clean --noconfirm PS5_UltraPack_linux.spec

APP="dist/PS5 UltraPack"
install -m 644 assets/icon/app-icon-256.png "${APP}/ps5-ultrapack.png"
install -m 755 packaging/linux/install.sh "${APP}/install.sh"

VERSION="$(python3 -c 'import re; m = re.search(r"^APP_VERSION\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]", open("PS5_UltraPack.py", encoding="utf-8").read(), re.M); print(m.group(1) if m else "1.0")')"
TGZ="PS5-UltraPack-${VERSION}-linux-x64.tar.gz"
rm -f "dist/${TGZ}" "dist/${TGZ}.sha256"
tar -C dist -czf "dist/${TGZ}" "PS5 UltraPack"
(cd dist && sha256sum "${TGZ}" > "${TGZ}.sha256")

echo "Built: ${APP}"
echo "Release: dist/${TGZ}"
cat "dist/${TGZ}.sha256"
