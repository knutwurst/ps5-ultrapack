#!/usr/bin/env bash
# Fetch the 7-Zip console tools the app bundles into backend/native/: 7zz for macOS
# (arm64) and 7za.exe for Windows x64 (from 7-Zip Extra), each with its license text.
# Downloads the pinned 7-Zip release from 7-zip.org once and checks it against the hashes below.
set -euo pipefail
cd "$(dirname "$0")"

VER=2604
MAC_SHA=bee04358cbcbc7106273cee0e8d72916db2696c48067a3538c34d8cd6fd16578
EXTRA_SHA=dc4b11d3399db18b063630137145f5585d8f7ac847bf3639bd1185d7d1f7cee0
DL="build/7zip-${VER}"
mkdir -p "${DL}"

fetch() {   # file sha256
    if [[ ! -f "${DL}/$1" ]] || ! echo "$2  ${DL}/$1" | shasum -a 256 -c --status; then
        curl -fsSL -o "${DL}/$1" "https://www.7-zip.org/a/$1"
    fi
    echo "$2  ${DL}/$1" | shasum -a 256 -c --quiet
}
fetch "7z${VER}-mac.tar.xz" "${MAC_SHA}"
fetch "7z${VER}-extra.7z" "${EXTRA_SHA}"

rm -rf "${DL}/mac" "${DL}/extra"
mkdir -p "${DL}/mac" "${DL}/extra"
tar -xJf "${DL}/7z${VER}-mac.tar.xz" -C "${DL}/mac" 7zz License.txt
# 7zz runs only on a Mac; on another system the Windows tool comes from an installed 7-Zip
if [[ "$(uname)" == "Darwin" ]]; then
    "${DL}/mac/7zz" x -y -bso0 -bsp0 "-o${DL}/extra" "${DL}/7z${VER}-extra.7z" x64/7za.exe License.txt
else
    7z x -y -bso0 -bsp0 "-o${DL}/extra" "${DL}/7z${VER}-extra.7z" x64/7za.exe License.txt
fi

# the app is arm64 only
if command -v lipo >/dev/null; then
    lipo "${DL}/mac/7zz" -thin arm64 -output "${DL}/mac/7zz-arm64"
    install -m 755 "${DL}/mac/7zz-arm64" backend/native/7zz
else
    install -m 755 "${DL}/mac/7zz" backend/native/7zz
fi
install -m 644 "${DL}/mac/License.txt" backend/native/LICENSE.7-Zip.macos
install -m 755 "${DL}/extra/x64/7za.exe" backend/native/7za.exe
install -m 644 "${DL}/extra/License.txt" backend/native/LICENSE.7-Zip.windows
ls -l backend/native/7zz backend/native/7za.exe
