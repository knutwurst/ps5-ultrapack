#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
# fetch ampr_emu's libSceAmpr.sprx (LZ4 packs) into backend/native/ampr and quer3q's ampr-emu-ng
# (zstd packs) into backend/native/ampr-ng
# GPL-3: the matching source archives in build/ ship next to each release
set -euo pipefail
cd "$(dirname "$0")"
# Git Bash on Windows has sha256sum, not shasum
sha256() { if command -v shasum >/dev/null; then shasum -a 256 "$@"; else sha256sum "$@"; fi; }

VER=0.4.2.1
COMMIT=e0b24bdfc344e37e8aced99a22e6f9f5c65d9152
SPRX_SHA=69e6c4d5e4f5fb83c9e01815db5861c4c75734acbf4595cafa50d4c218116d1a
SRC_SHA=b7ca6b1f13e74419d94381ff40f6c9d1e9d019a5aa5c8b08fc00d9a62b27aef0
DL="build/ampr_emu-${VER}"
mkdir -p "${DL}" backend/native/ampr

fetch() {   # file url sha256
    if [[ ! -f "${DL}/$1" ]] || ! echo "$3  ${DL}/$1" | sha256 -c --status; then
        curl -fsSL --retry 5 --retry-all-errors -o "${DL}/$1" "$2"
    fi
    echo "$3  ${DL}/$1" | sha256 -c --quiet
}
fetch libSceAmpr.sprx "https://github.com/drakmor/ampr_emu/releases/download/${VER}/libSceAmpr.sprx" "${SPRX_SHA}"
fetch "ampr_emu-${VER}-source.tar.gz" "https://github.com/drakmor/ampr_emu/archive/${COMMIT}.tar.gz" "${SRC_SHA}"

install -m 644 "${DL}/libSceAmpr.sprx" backend/native/ampr/libSceAmpr.sprx
install -m 644 backend/ampr_pack/LICENSE backend/native/ampr/LICENSE.ampr_emu

# ampr-emu-ng, GPL-3.0-or-later, the latest release that isn't a pre-release
NG_VER=0.4.0
NG_COMMIT=920e9172b6bf99bd9210361b545e2953659d1e9b
NG_SPRX_SHA=8320eb65c16565f42031b9d88b15a447039f0fd7e5694faf5f1cb84cafdba218
NG_SRC_SHA=b2f8c3c632124c3c1e76e0b89ebf413529757c01ced82145c59fe6cd3e9d21e5
DL="build/ampr-emu-ng-${NG_VER}"
mkdir -p "${DL}" backend/native/ampr-ng
fetch libSceAmpr.sprx "https://github.com/quer3q/ampr-emu-ng/releases/download/v${NG_VER}/libSceAmpr.sprx" "${NG_SPRX_SHA}"
fetch "ampr-emu-ng-${NG_VER}-source.tar.gz" "https://github.com/quer3q/ampr-emu-ng/archive/${NG_COMMIT}.tar.gz" "${NG_SRC_SHA}"
install -m 644 "${DL}/libSceAmpr.sprx" backend/native/ampr-ng/libSceAmpr.sprx
tar -xzOf "${DL}/ampr-emu-ng-${NG_VER}-source.tar.gz" "ampr-emu-ng-${NG_COMMIT}/LICENSE" > backend/native/ampr-ng/LICENSE.ampr-emu-ng
ls -l backend/native/ampr backend/native/ampr-ng
