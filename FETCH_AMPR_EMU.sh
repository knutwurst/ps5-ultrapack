#!/usr/bin/env bash
# fetch ampr_emu's pack-capable libSceAmpr.sprx into backend/native/ampr
# GPL-3: the matching source archive in build/ ships next to each release
set -euo pipefail
cd "$(dirname "$0")"

VER=0.4.2.1
COMMIT=e0b24bdfc344e37e8aced99a22e6f9f5c65d9152
SPRX_SHA=69e6c4d5e4f5fb83c9e01815db5861c4c75734acbf4595cafa50d4c218116d1a
SRC_SHA=b7ca6b1f13e74419d94381ff40f6c9d1e9d019a5aa5c8b08fc00d9a62b27aef0
DL="build/ampr_emu-${VER}"
mkdir -p "${DL}" backend/native/ampr

fetch() {   # file url sha256
    if [[ ! -f "${DL}/$1" ]] || ! echo "$3  ${DL}/$1" | shasum -a 256 -c --status; then
        curl -fsSL -o "${DL}/$1" "$2"
    fi
    echo "$3  ${DL}/$1" | shasum -a 256 -c --quiet
}
fetch libSceAmpr.sprx "https://github.com/drakmor/ampr_emu/releases/download/${VER}/libSceAmpr.sprx" "${SPRX_SHA}"
fetch "ampr_emu-${VER}-source.tar.gz" "https://github.com/drakmor/ampr_emu/archive/${COMMIT}.tar.gz" "${SRC_SHA}"

install -m 644 "${DL}/libSceAmpr.sprx" backend/native/ampr/libSceAmpr.sprx
install -m 644 backend/ampr_pack/LICENSE backend/native/ampr/LICENSE.ampr_emu
ls -l backend/native/ampr
