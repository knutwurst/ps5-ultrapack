#!/usr/bin/env bash
# Build the Windows x64 parts of the app on a Mac: the fPKG helper (ffpfsc-pkg-tool.exe and
# its ImageMagick library) into backend/native/, and the UnRAR module for the Windows Python
# that BUILD_WINDOWS_APP.ps1 bundles into backend/unrar/. Needs the .NET SDK 9+ and mingw-w64
# (brew install mingw-w64). WIN_PYTHON is a copy of that Python's folder (include/ and
# python3XX.dll are enough).
set -euo pipefail
cd "$(dirname "$0")"

WIN_PYTHON="${WIN_PYTHON:?set WIN_PYTHON to a copy of the Windows Python folder (include/, python3XX.dll)}"
DLL="$(ls "${WIN_PYTHON}"/python3*.dll | grep -E 'python3[0-9]+\.dll$' | head -1)"
TAG="$(basename "${DLL}" .dll | sed 's/python/cp/')"
DOTNET="${DOTNET:-$(command -v dotnet || true)}"
[[ -n "${DOTNET}" ]] || DOTNET="${HOME}/.dotnet/dotnet"
CXX="${CXX:-x86_64-w64-mingw32-g++}"

SRC="backend/native/src/ffpfsc-pkg-tool"
OUT="${SRC}/out-win"
rm -rf "${OUT}"
"${DOTNET}" publish "${SRC}/PkgTool.csproj" -c Release -r win-x64 --self-contained true -o "${OUT}"
for f in ffpfsc-pkg-tool.exe Magick.Native-Q8-x64.dll; do
    cp "${OUT}/${f}" "backend/native/${f}"
done
rm -rf "${OUT}"

# The same sources as backend/unrar/setup.py on Windows, linked statically (no MinGW DLLs).
SOURCES=(rar strlist strfn pathfn smallfn global file filefn filcreat archive arcread unicode
         system crypt crc rawread encname resource match timefn rdwrfn consio options errhnd
         rarvm secpassword rijndael getbits sha1 sha256 blake2s hash extinfo extract volume
         list find unpack headers threadpool rs16 cmddata ui largepage filestr scantree dll
         qopen rs motw isnt)
FILES=()
for s in "${SOURCES[@]}"; do FILES+=("backend/unrar/src/${s}.cpp"); done
"${CXX}" -O2 -std=c++17 -shared -w -DMS_WIN64 -DRARDLL -DUNRAR -DSILENT -DUNICODE -D_UNICODE \
    -Ibackend/unrar/src -I"${WIN_PYTHON}/include" backend/unrar/_unrar.cpp "${FILES[@]}" "${DLL}" \
    -o "backend/unrar/_unrar.${TAG}-win_amd64.pyd" \
    -static -static-libgcc -static-libstdc++ -ladvapi32 -lshell32 -lole32 -luuid -lpowrprof
ls -l backend/native/ffpfsc-pkg-tool.exe backend/native/Magick.Native-Q8-x64.dll "backend/unrar/_unrar.${TAG}-win_amd64.pyd"
