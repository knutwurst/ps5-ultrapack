#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
# builds the fPKG helper + its ImageMagick lib into backend/native (not in git)
# needs .NET SDK 9+; on a fresh clone run it before the tests
set -euo pipefail
cd "$(dirname "$0")"

DOTNET="${DOTNET:-$(command -v dotnet || true)}"
[[ -n "${DOTNET}" ]] || DOTNET="${HOME}/.dotnet/dotnet"
if [[ ! -x "${DOTNET}" ]]; then
    echo "BUILD_PKG_TOOL.sh: the .NET SDK (9 or newer) is required: https://dotnet.microsoft.com/download" >&2
    echo "  (or point DOTNET at the dotnet executable)" >&2
    exit 1
fi

SRC="backend/native/src/ffpfsc-pkg-tool"
OUT="${SRC}/out"
rm -rf "${OUT}"
"${DOTNET}" publish "${SRC}/PkgTool.csproj" -c Release -r osx-arm64 --self-contained true -o "${OUT}"

for f in ffpfsc-pkg-tool Magick.Native-Q8-arm64.dll.dylib; do
    # rm first: macOS remembers the old signature per inode and kills the new binary
    rm -f "backend/native/${f}"
    cp "${OUT}/${f}" "backend/native/${f}"
done
chmod +x backend/native/ffpfsc-pkg-tool
backend/native/ffpfsc-pkg-tool version | head -1
