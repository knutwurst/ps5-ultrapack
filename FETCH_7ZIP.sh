#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
# fetch pinned, hash-checked 7zz (mac arm64 or x86_64) and 7za.exe (win x64) into backend/native
set -euo pipefail
cd "$(dirname "$0")"
# Git Bash on Windows has sha256sum, not shasum
sha256() { if command -v shasum >/dev/null; then shasum -a 256 "$@"; else sha256sum "$@"; fi; }

VER=2604
MAC_SHA=bee04358cbcbc7106273cee0e8d72916db2696c48067a3538c34d8cd6fd16578
EXTRA_SHA=dc4b11d3399db18b063630137145f5585d8f7ac847bf3639bd1185d7d1f7cee0
MAC_ARCH="${MACOS_ARCH:-arm64}"
# the x86_64 slice of 26.04 needs macOS 26, the one of 25.01 runs from macOS 12
if [[ "${MAC_ARCH}" == "x86_64" ]]; then
    MAC_VER=2501
    MAC_SHA=26aa75bc262bb10bf0805617b95569c3035c2c590a99f7db55c7e9607b2685e0
else
    MAC_VER="${VER}"
fi
DL="build/7zip-${VER}"
mkdir -p "${DL}"

fetch() {   # file sha256
    if [[ ! -f "${DL}/$1" ]] || ! echo "$2  ${DL}/$1" | sha256 -c --status; then
        curl -fsSL -o "${DL}/$1" "https://www.7-zip.org/a/$1"
    fi
    echo "$2  ${DL}/$1" | sha256 -c --quiet
}
fetch "7z${MAC_VER}-mac.tar.xz" "${MAC_SHA}"
fetch "7z${VER}-extra.7z" "${EXTRA_SHA}"

rm -rf "${DL}/mac" "${DL}/extra"
mkdir -p "${DL}/mac" "${DL}/extra"
tar -xJf "${DL}/7z${MAC_VER}-mac.tar.xz" -C "${DL}/mac" 7zz License.txt
# 7zz is Mac only, elsewhere an installed 7z unpacks the Windows tool
if [[ "$(uname)" == "Darwin" ]]; then
    "${DL}/mac/7zz" x -y -bso0 -bsp0 "-o${DL}/extra" "${DL}/7z${VER}-extra.7z" x64/7za.exe License.txt
else
    7z x -y -bso0 -bsp0 "-o${DL}/extra" "${DL}/7z${VER}-extra.7z" x64/7za.exe License.txt
fi

# one slice, the app's architecture
if command -v lipo >/dev/null; then
    lipo "${DL}/mac/7zz" -thin "${MAC_ARCH}" -output "${DL}/mac/7zz-${MAC_ARCH}"
    install -m 755 "${DL}/mac/7zz-${MAC_ARCH}" backend/native/7zz
else
    install -m 755 "${DL}/mac/7zz" backend/native/7zz
fi
install -m 644 "${DL}/mac/License.txt" backend/native/LICENSE.7-Zip.macos
install -m 755 "${DL}/extra/x64/7za.exe" backend/native/7za.exe
install -m 644 "${DL}/extra/License.txt" backend/native/LICENSE.7-Zip.windows
ls -l backend/native/7zz backend/native/7za.exe
