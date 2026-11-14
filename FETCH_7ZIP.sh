#!/usr/bin/env bash
# fetch pinned, hash-checked 7zz (mac x64/arm64) and 7za.exe (win x64) into backend/native
set -euo pipefail
cd "$(dirname "$0")"
# Git Bash on Windows has sha256sum, not shasum
sha256() { if command -v shasum >/dev/null; then shasum -a 256 "$@"; else sha256sum "$@"; fi; }

MAC_VER=2301
MAC_SHA=343eae9ccbbd8f68320adaaa3c87e0244cf39fad0fbec6b9d2cd3e5b0f8a5fbf
EXTRA_VER=2604
EXTRA_SHA=dc4b11d3399db18b063630137145f5585d8f7ac847bf3639bd1185d7d1f7cee0
DL="build/7zip-${EXTRA_VER}"
mkdir -p "${DL}"

fetch() {   # file sha256
    if [[ ! -f "${DL}/$1" ]] || ! echo "$2  ${DL}/$1" | sha256 -c --status; then
        curl -fsSL -o "${DL}/$1" "https://www.7-zip.org/a/$1"
    fi
    echo "$2  ${DL}/$1" | sha256 -c --quiet
}
fetch "7z${MAC_VER}-mac.tar.xz" "${MAC_SHA}"
fetch "7z${EXTRA_VER}-extra.7z" "${EXTRA_SHA}"

rm -rf "${DL}/mac" "${DL}/extra"
mkdir -p "${DL}/mac" "${DL}/extra"
tar -xJf "${DL}/7z${MAC_VER}-mac.tar.xz" -C "${DL}/mac" 7zz License.txt
# 7zz is Mac only, elsewhere an installed 7z unpacks the Windows tool
if [[ "$(uname)" == "Darwin" ]]; then
    "${DL}/mac/7zz" x -y -bso0 -bsp0 "-o${DL}/extra" "${DL}/7z${EXTRA_VER}-extra.7z" x64/7za.exe License.txt
else
    7z x -y -bso0 -bsp0 "-o${DL}/extra" "${DL}/7z${EXTRA_VER}-extra.7z" x64/7za.exe License.txt
fi

if [[ "$(uname)" == "Darwin" ]]; then
    case "${MACOS_ARCH:-$(uname -m)}" in
        x86_64|x64) ARCH=x86_64 ;;
        arm64|aarch64) ARCH=arm64 ;;
        *)
            echo "FETCH_7ZIP.sh: unsupported macOS architecture: ${MACOS_ARCH:-$(uname -m)}" >&2
            exit 1
            ;;
    esac
    if ! command -v lipo >/dev/null; then
        echo "FETCH_7ZIP.sh: lipo is required; install the Xcode Command Line Tools" >&2
        exit 1
    fi
    lipo "${DL}/mac/7zz" -thin "${ARCH}" -output "${DL}/mac/7zz-${ARCH}"
    install -m 755 "${DL}/mac/7zz-${ARCH}" backend/native/7zz
else
    install -m 755 "${DL}/mac/7zz" backend/native/7zz
fi
install -m 644 "${DL}/mac/License.txt" backend/native/LICENSE.7-Zip.macos
install -m 755 "${DL}/extra/x64/7za.exe" backend/native/7za.exe
install -m 644 "${DL}/extra/License.txt" backend/native/LICENSE.7-Zip.windows
ls -l backend/native/7zz backend/native/7za.exe
