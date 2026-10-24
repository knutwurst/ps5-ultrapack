#!/usr/bin/env bash
# OpenSSL dylibs for the pkg tool; .NET needs them on a Mac without Homebrew
set -euo pipefail
cd "$(dirname "$0")"

VER=3.5.9
SHA=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
MIN_MACOS=12.0
DL="build/openssl-${VER}"
TAR="${DL}/openssl-${VER}.tar.gz"
mkdir -p "${DL}"

if [[ ! -f "${TAR}" ]] || ! echo "${SHA}  ${TAR}" | shasum -a 256 -c --status; then
    curl -fsSL -o "${TAR}" "https://github.com/openssl/openssl/releases/download/openssl-${VER}/openssl-${VER}.tar.gz"
fi
echo "${SHA}  ${TAR}" | shasum -a 256 -c --quiet

SRC="${DL}/src"
rm -rf "${SRC}"
mkdir -p "${SRC}"
tar -xzf "${TAR}" -C "${SRC}" --strip-components 1
(
    cd "${SRC}"
    MACOSX_DEPLOYMENT_TARGET="${MIN_MACOS}" ./Configure darwin64-arm64-cc shared no-tests no-docs \
        "-mmacosx-version-min=${MIN_MACOS}" >/dev/null
    make -j"$(sysctl -n hw.ncpu)" build_libs >/dev/null
)

install -m 755 "${SRC}/libcrypto.3.dylib" backend/native/libcrypto.3.dylib
install -m 755 "${SRC}/libssl.3.dylib" backend/native/libssl.3.dylib
# found next to the tool, never through the path they were built in
install_name_tool -id @loader_path/libcrypto.3.dylib backend/native/libcrypto.3.dylib
install_name_tool -id @loader_path/libssl.3.dylib \
    -change "$(otool -L backend/native/libssl.3.dylib | awk '/libcrypto/{print $1; exit}')" \
    @loader_path/libcrypto.3.dylib backend/native/libssl.3.dylib
codesign -f -s - backend/native/libcrypto.3.dylib backend/native/libssl.3.dylib
install -m 644 "${SRC}/LICENSE.txt" backend/native/LICENSE.OpenSSL
otool -L backend/native/libssl.3.dylib
ls -l backend/native/libcrypto.3.dylib backend/native/libssl.3.dylib
