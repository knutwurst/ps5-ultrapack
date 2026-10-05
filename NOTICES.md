# NOTICES

## Trademark

**Not affiliated with, endorsed by, or sponsored by Sony Interactive
Entertainment Inc.** "PlayStation", "PS4", "PS5", "Prospero", and related
marks are trademarks or registered trademarks of Sony Interactive
Entertainment Inc. Use of these names in this repository is nominative fair
use to identify the format and console this tool interoperates with.

## User responsibility

This tool builds and processes PS5 package formats (`.ffpfsc`, `.ffpfs`,
`.pkg`) from files that **you supply**. It does not decrypt, decode,
distribute, or download any Sony-owned content, firmware, executables, or
cryptographic keys. The console-side jailbreak stack this tool's output is
compatible with (kstuff-lite, etaHEN, and similar) is developed and hosted
by third parties and is **not** included with this software.

You are responsible for having the legal right to any content you process
with this tool, in your jurisdiction:

  * dumps of games you own on consoles you own;
  * homebrew you have written yourself or that has been licensed for such
    use by its author;
  * fake packages you build for those two categories.

Circumventing technological protection measures may be regulated
differently where you live. Nothing in this repository is legal advice.

## Bundled third-party components

Each component below is bundled as source or as a compiled artifact.
Their upstream authors and licenses:

| Component | Upstream | License | Location in this repo |
|---|---|---|---|
| **MkPFS** 1.0.0 | [PSBrew/MkPFS](https://github.com/PSBrew/MkPFS) | **GPL-3.0-or-later** | `backend/mkpfs/` (LICENSE: `backend/mkpfs/LICENSE`) |
| **LibProsperoPkg** 1.2.0, build `d7090eb6` | [drakmor/LibProsperoPkg](https://github.com/drakmor/LibProsperoPkg), a fork of [SvenGDK/LibProsperoPKG](https://github.com/SvenGDK/LibProsperoPKG); assembly taken from the author's fpkg-gui 0.6.11 release (2026-10-04). The source of this build is not in a public repository we know of; `lib/README.md` records what was checked | **GPL-3.0-or-later** | `backend/native/src/ffpfsc-pkg-tool/lib/` holds the pristine assembly and a **modified** one (three IL patches, 2026-10-05 — see `lib/README.md` and `patches/README.md` there); the modified assembly is embedded into `backend/native/ffpfsc-pkg-tool` (LICENSE: `backend/native/LICENSE.LibProsperoPkg`, NOTICE: `backend/native/NOTICE.LibProsperoPkg`, bundling notice: `backend/native/NOTICE.fpkg`) |
| **BCnEncoder.NET** 2.3.0 | [Nominom/BCnEncoder.NET](https://github.com/Nominom/BCnEncoder.NET) (NuGet, pinned in `PkgTool.csproj`; dependency of LibProsperoPkg) | **MIT OR Unlicense** | embedded into `backend/native/ffpfsc-pkg-tool` |
| **CommunityToolkit.HighPerformance** 8.4.0 | [CommunityToolkit/dotnet](https://github.com/CommunityToolkit/dotnet) (NuGet, pinned) | **MIT** | embedded into `backend/native/ffpfsc-pkg-tool` |
| **Magick.NET** 14.15.0 (Q8-AnyCPU + Core, with the native ImageMagick library) | [dlemstra/Magick.NET](https://github.com/dlemstra/Magick.NET) (NuGet, pinned; PNG→DDS for the package icon) | **Apache-2.0** (native ImageMagick: ImageMagick License) | managed part embedded into `backend/native/ffpfsc-pkg-tool`; the native library ships beside it as `backend/native/Magick.Native-Q8-arm64.dll.dylib` |
| **.NET 9 runtime** | [dotnet/runtime](https://github.com/dotnet/runtime) | **MIT** | self-contained inside `backend/native/ffpfsc-pkg-tool` |
| **UnRAR** sources | [rarlab.com](https://www.rarlab.com/) by Alexander Roshal / RARLAB | **UnRAR license** (free for extraction; **may not** be used to reverse-engineer the RAR compression algorithm or to build a RAR-compatible compressor) | `backend/unrar/src/` (LICENSE: `backend/unrar/license.txt`) |
| **make_fself** | Alex Free's [ps5-make-fself-recursive](https://github.com/alex-free/ps5-make-fself-recursive), which redistributes `make_fself.py` from the [ps5-payload-dev SDK](https://github.com/ps5-payload-dev/sdk), originally by flatz | **BSD-3-Clause** | `backend/make_fself.py` (attribution in the file header) |
| **BackPork BPS patches** | [BestPig/BackPork](https://github.com/BestPig/BackPork) — small BPS deltas that turn a PS5 10.01 system library into the version an older firmware loads | **GPL-3.0-or-later** (same as BackPork) | **Downloaded on demand** from the upstream repo by Settings → "Prepare 7.61 / 6.02 libraries"; cached under `~/Library/Application Support/PS5_UltraPack/backport-patches/`. The patches are applied to the user's own 10.01 libraries, which are never bundled with this app. |
| **BPS format** (byuu, 2014) | [byuu.org — BPS specification](https://www.romhacking.net/utilities/1040/) | Public domain | `backend/bps_patch.py` — an independent Python implementation of the format; no external code copied. |
| **Bizkut's ps5-ffpfs-cli** (the backend wrapper `backend/cli.py` grew out of) | [bizkut/ps5-ffpfs-cli](https://github.com/bizkut/ps5-ffpfs-cli) | **No license file upstream.** No license is claimed here for whatever remains of the upstream code; this project's own contributions are MIT (see the scope block in `LICENSE`). If you are Bizkut and want a specific licensing statement or attribution change, please open an issue. | `backend/cli.py` started as that tool's backend wrapper (imported with this repository's first commit) and has been rewritten extensively since. The UnRAR Python binding under `backend/unrar/` (`rarfile.py`, which mirrors the subset of the `rarfile` API that tool used, `_unrar.cpp`, `setup.py`) came in with the same import. |

## Bundled runtime libraries (inside the .app)

The compiled `.app` carries its own Python runtime and every Python library
the program imports (macOS system frameworks and optional command-line tools
such as `7z` are used from the system). Licenses are as declared by each
project in its package metadata or license file, checked 2026-09-27 against
the build environment and the contents of the built bundle. The LGPL
components are used unmodified; their compiled extension modules ship as
separate shared libraries inside the bundle.

* **Python 3.13** runtime and standard library — PSF-2.0. With its native
  helpers: **OpenSSL 3** (`libcrypto`, `libssl`) — Apache-2.0; **mpdecimal**
  (`libmpdec`) — BSD-2-Clause.
* **Tcl/Tk 9** (`libtcl9`, `libtcl9tk9`, the `_tkinter` module) — Tcl/Tk
  license (BSD-style); **libtommath** (bundled with Tcl) — public domain
  (Unlicense).
* **customtkinter** — MIT. **darkdetect** (its dependency) — BSD-3-Clause.
* **tkinterdnd2** — MIT, with the **TkDnD** Tcl extension it ships —
  BSD-style (Tcl license terms).
* **Pillow** — MIT-CMU (the license formerly listed as HPND). With the native
  libraries its wheel ships: **libjpeg-turbo** — IJG license, BSD-3-Clause
  and zlib; **libtiff** — libtiff license (BSD-style); **libwebp** (incl.
  `libsharpyuv`, `libwebpmux`, `libwebpdemux`) — BSD-3-Clause; **OpenJPEG**
  — BSD-2-Clause; **Little-CMS 2** — MIT; **libavif** — BSD-2-Clause;
  **liblzma** (xz) — 0BSD (older parts public domain); **zlib-ng** — zlib
  license; **libxcb** and **libXau** — MIT (X11 style).
* **psutil** — BSD-3-Clause.
* **py7zr** — LGPL-2.1-or-later. With its helper packages: **pybcj**,
  **inflate64**, **pyppmd**, **multivolumefile** — LGPL-2.1-or-later;
  **backports.zstd** — PSF-2.0; **brotli** — MIT; **pycryptodomex** —
  BSD-2-Clause and public domain.
* **rarfile** — ISC.
* **cryptography** — Apache-2.0 OR BSD-3-Clause. With **cffi** — MIT-0 (as
  declared by cffi 2.x) and **pycparser** — BSD-3-Clause.
* **pyobjc** (`objc`, `Foundation`, `CoreFoundation`, `AppKit` wrappers) —
  MIT.
* **setuptools** — MIT. **packaging** — Apache-2.0 OR BSD-2-Clause.

Inside `ffpfsc-pkg-tool` (see `backend/native/NOTICE.fpkg`):

* **.NET 9 runtime** (self-contained) — MIT.
* **Magick.NET-Q8-AnyCPU** and **Magick.NET.Core** (PNG→DDS) — Apache-2.0;
  the embedded native ImageMagick library — ImageMagick License.
* **BCnEncoder.NET** (dependency of LibProsperoPkg) — MIT OR Unlicense.
* **CommunityToolkit.HighPerformance** — MIT.

All of the above are compatible with the GPL-3-or-later that governs the
combined compiled binary.

## What the compiled binary is, legally

Because the compiled `.app` bundle statically embeds MkPFS (GPL-3.0-or-later)
and LibProsperoPkg (GPL-3.0-or-later), the compiled binary as a whole is
distributed as a work under the terms of **GNU General Public License
version 3, or (at your option) any later version**, per GPL-3 section 5.
Source code required to reproduce it is present in this repository (see the
scope block at the end of `LICENSE`). The original Python and C# code
authored for this project is separately licensed under MIT so downstream
projects can reuse it under either compatible license.
