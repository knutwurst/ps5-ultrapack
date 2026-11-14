#!/usr/bin/env bash
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

case "${MACOS_ARCH:-$(uname -m)}" in
    x86_64|x64)
        RID=osx-x64
        MAGICK=Magick.Native-Q8-x64.dll.dylib
        ;;
    arm64|aarch64)
        RID=osx-arm64
        MAGICK=Magick.Native-Q8-arm64.dll.dylib
        ;;
    *)
        echo "BUILD_PKG_TOOL.sh: unsupported macOS architecture: ${MACOS_ARCH:-$(uname -m)}" >&2
        exit 1
        ;;
esac

rm -rf "${OUT}"
"${DOTNET}" publish "${SRC}/PkgTool.csproj" -c Release -r "${RID}" --self-contained true -o "${OUT}"

for f in ffpfsc-pkg-tool "${MAGICK}"; do
    # rm first: macOS remembers the old signature per inode and kills the new binary
    rm -f "backend/native/${f}"
    cp "${OUT}/${f}" "backend/native/${f}"
done

if [[ "${RID}" == "osx-x64" ]]; then
    # The current Intel Magick.NET asset was verified on macOS 14; its package metadata defaults to macOS 15.
    native="backend/native/${MAGICK}"
    sdk_version="$(xcrun vtool -show-build "${native}" | awk '$1 == "sdk" { print $2; exit }')"
    if [[ -z "${sdk_version}" ]]; then
        echo "BUILD_PKG_TOOL.sh: could not read the Magick.Native SDK version" >&2
        exit 1
    fi
    tmp="${native}.target14"
    rm -f "${tmp}"
    xcrun vtool -set-build-version macos 14.0 "${sdk_version}" -replace -output "${tmp}" "${native}"
    mv "${tmp}" "${native}"
    min_version="$(xcrun vtool -show-build "${native}" | awk '$1 == "minos" { print $2; exit }')"
    if [[ "${min_version}" != "14.0" ]]; then
        echo "BUILD_PKG_TOOL.sh: expected Intel Magick.Native minos 14.0, got ${min_version:-unknown}" >&2
        exit 1
    fi
fi

chmod +x backend/native/ffpfsc-pkg-tool
backend/native/ffpfsc-pkg-tool version | head -1
