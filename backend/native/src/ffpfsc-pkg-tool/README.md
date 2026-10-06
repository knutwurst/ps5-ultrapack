# ffpfsc-pkg-tool — source

`backend/native/ffpfsc-pkg-tool` is a self-contained, single-file .NET 9 executable
(macOS arm64), with its one native library `Magick.Native-Q8-arm64.dll.dylib` beside it,
that the app calls for everything fPKG: `build`, `extract-inner`,
`extract-outer`, `list-inner`, `inspect`, `validate`, `version`. This folder holds the part
we wrote:

- `Program.cs` — the command-line wrapper around drakmor's LibProsperoPkg 1.2.0: argument
  parsing, the `[PHASE]`/progress lines the GUI translates, the `validate` checklist, the
  staging of every build (a hard-link mirror next to the source folder, or in `--temp`;
  a copy into `--temp` only when the source volume takes no hard links, announced by a
  `[stage] ... copying N GB` line), SIGTERM/SIGINT cancellation with cleanup of the
  mirror, the library's temp files and any partial `.pkg` (exit 143/130), and the worker
  count (`--parallelism` / `-j`, default 0 = one per core) that drives both the inner-image
  Kraken workers and the outer-PFS pass. The tool ran single-threaded until 2.2.0 because
  `-j 4` crashed with AccessViolationException; that was the compressed single-file bundle
  (see `PkgTool.csproj`), not the encoder: uncompressed, 4/8/12 workers give the same bytes
  as one (22 of 22 builds, 2026-10-06).
- `InnerImage.cs` — random access into the inner PFS for `list-inner` and
  `extract-inner --members` (see below).

Every `--json` document is serialized through the source-generated `PkgToolJsonContext` at
the end of `Program.cs`. It dates from when the binary was published trimmed (which switches
reflection-based System.Text.Json off and crashed the anonymous types the commands used with
"Reflection-based serialization has been disabled"); trimming is off today, the context stays.
- `PkgTool.csproj` — the publish settings: self-contained single file without bundle
  compression (compressed, every multithreaded pass crashed; 82 MB instead of 38), no ReadyToRun,
  the native ImageMagick library next to the executable rather than bundled (a bundled
  one is unpacked into `~/.net/ffpfsc-pkg-tool/<hash>/` on first run, a new ~27 MB folder
  per build that nothing removes; the tool deletes folders left by older builds), no trimming (Magick.NET's Prospero-facing path is reflection-driven; trimming pruned
  the types the DDS conversion needs). Magick.NET is required — LibProsperoPkg calls it
  to convert `sce_sys/icon0.png` into the `sce_sys/icon0.dds` CNT entry the console needs
  to launch the app; without the DDS, an fPKG installs but never starts (CE-100096-6 /
  CE-100022-5). Adds ~30 MB (mostly the native library), unavoidable.

The fPKG logic itself is LibProsperoPkg, which is GPL-3.0-or-later; see
`../../LICENSE.LibProsperoPkg`, `../../NOTICE.LibProsperoPkg` and `../../NOTICE.fpkg`.
Because the published binary contains it, the binary is a GPL-3 work — which is why this
source is kept in the repo.

## Rebuilding

1. Install a .NET SDK (9 or newer; 10.0.400 was used).
2. Nothing to fetch. `lib/LibProsperoPkg.dll` is tracked in git and already carries the
   two IL patches; `lib/LibProsperoPkg.dll.orig` is the pristine upstream assembly and
   `lib/SHA256SUMS` proves both (`cd lib && shasum -a 256 -c SHA256SUMS`). To re-apply
   the patches to a fresh copy of the pristine assembly, see `patches/README.md` — the
   patcher is idempotent. Every other dependency is a NuGet package pinned in
   `PkgTool.csproj` (BCnEncoder.Net 2.3.0, CommunityToolkit.HighPerformance 8.4.0,
   Magick.NET-Q8-AnyCPU 14.15.0 including the native ImageMagick library); the publish
   step restores them.
3. From the repository root, build and install both files into `backend/native/`:

   ```bash
   ./BUILD_PKG_TOOL.sh
   ```

   It runs `dotnet publish PkgTool.csproj -c Release -r osx-arm64 --self-contained true -o out`
   and replaces `backend/native/ffpfsc-pkg-tool` and `backend/native/Magick.Native-Q8-arm64.dll.dylib`
   by delete + copy (copying over a signed binary in place can make macOS kill it on launch).
   Neither file is tracked in git; `BUILD_MACOS_APP.sh` runs the script first.
4. Prove it before shipping — the whole harness can run against any candidate binary:

   ```bash
   FFPFSC_PKG_TOOL=/path/to/out/ffpfsc-pkg-tool python backend/tests/test_fpkg_pipelines.py
   ```

`bin/`, `obj/` and `out/` are ignored by git, and so are the two built files in `backend/native/`; `lib/` is tracked.

## Browsing a package: `list-inner` and `extract-inner --members`

The PFS browser needs the tree of a package and a few members out of it without unpacking
100 GB first. LibProsperoPkg's `ExtractInnerFiles` cannot do that: it decrypts the outer
PFS to a temp file, NAPS-decodes the complete inner image to a second temp file, and only
then reads the directory. The two commands below chain the library's public random-access
pieces instead (`InnerImage.cs`):

    package -> plain SubStream (PlaintextNoAuth) or ProsperoOuterPfsDecryptReader (AES-XTS)
            -> PfsReader (outer: pfs_image.dat + naps_pkg_layout.dat)
            -> NapsBlockReader (NAPS plan built once, spans found by binary search and decoded
               through the library's span decoder; LRU cache of 1 MiB x 32 or 4 MiB x 8 blocks)
            -> PfsReader (inner: the /app0 tree)

Two facts shaped the code. Both outer layouts occur: the packages this tool builds since
1.1.10 use `PlaintextNoAuth` (mode 0x000D with the `PPPLAIN-NOAUTH!` seed marker, read
straight from the file), older ones a real seed (decrypted block by block); the library's
`DecodePlaintextInnerPfsRange` covers only the former. And the inner image is data-first:
the metadata (superblock, inodes, dirents) is the *last* NAPS logical file, so the
superblock is found by probing that boundary (one block decode) rather than the library's
forward 64 KiB scan, which would decode everything.

- `list-inner <pkg> [--passcode P]` prints one JSON object on stdout:
  `{"root": "<pkg file name>", "entries": [...], "file_count": N, "dir_count": N, "errors": []}`.
  Entries are sorted by path (relative to /app0, forward slashes); files carry the logical
  `size` and a `source` of `pfs` or `cnt`, directories are `{"path", "type": "dir"}` — every
  directory in the image, including empty ones. The `cnt` files are the sce_sys metadata
  that lives in the CNT table (param.json, icon0.png, pic0/pic1.png, playgo-*.dat,
  npbind.dat, changeinfo.xml), the same set `extract-inner` merges into `sce_sys/`.
  Measured on a 241.6 MB package (252 MB logical inner image, 18 files): 0.12 s warm /
  0.8 s cold, 75 MB peak RSS — of which 74 MB is the runtime floor, `version` alone shows
  it — and 2 range decodes reading 1.5 MiB of the image.
- `extract-inner <pkg> <out-dir> --members <file> [--passcode P] [--json]` extracts only the
  paths listed in `<file>` (one per line). A directory means its whole subtree, recreating
  directories including empty ones; a file name means that file; CNT files are selectable
  too. Progress goes to stdout as `[####] NN% extract (path)` (the GUI regex is
  `\[#{2,}\]\s*(\d{1,3})%`), then `[####] 100% extract` and an `OK — extracted N file(s) and
  M folder(s) to <dir>` line. Exit 0 on success, 1 when none of the members exist (each
  unknown member is a `[warn]` on stderr), 2 on usage errors. File data streams in 32 MiB
  chunks, so a single large file never has to fit in RAM.

The library's `ProsperoNapsImage.DecompressRange` rebuilds the NAPS plan on every call
(about 0.2 ms per 1000 cblocks, i.e. per ~256 MB of game) and scans all spans linearly.
`NapsBlockReader` builds the plan once, binary-searches the spans a range touches and
decodes them with the library's own span decoder (its private `DecodeSpan`, bound once by
reflection; if a future library version renames it, the code falls back to
`DecompressRange` unchanged). Byte identity with the full extract is covered by the
harness's selective-extract tests.

## Why not NativeAOT

Measured on 240 MB of mixed data: a NativeAOT build is 16–18 MB instead of 25 MB but
10–23 % slower on the Kraken/AES path (the JIT's tiered compilation wins there). For
100 GB games that is a quarter of an hour, so the JIT build ships.

## Firmware compatibility

fPKG install-and-launch works on jailbroken PS5 firmware **up to at least 11.60** when
the console runs kstuff-lite 1.13+ (Drakmor's PPR-A53 patch, released 2026-09; earlier
kstuff builds top out at 11.40). The console-side ceiling is the jailbreak stack, not
the package format — as soon as a newer kstuff exists for 11.7x/12.xx, packages this
tool produces are expected to install and launch there too.

Once-critical builder detail, kept for future readers who might hit the same wall:
LibProsperoPkg 1.2.0's default outer PFS wrap is a random-seed AES-XTS envelope which
validates structurally and round-trips through `extract-inner` but is rejected by the
PS5 debug loader with `CE-100096-6` at launch (verified 2026-09-24 on FW 11.60). Sony's
Publishing Tools DLL always writes the outer PFS with `ProsperoPublisherImageMode.
PlaintextNoAuth` (mode 0x000D + the `PPPLAIN-NOAUTH!` seed marker — no actual wrap).
`Program.cs` now forces that mode; a `pfs-dump` diff against a `libScePubTools.dll`
reference build shows layer B/L/C are byte-identical, only the outer PFS wrap remained
non-deterministic (timestamp/ICV bytes, harmless).

## AMPR emulator index

A backported source can ship drakmor's AMPR emulator as `fakelib/libSceAmpr.sprx`. The
emulator resolves APR file ids through `/app0/ampr_emu.index`, so `build` rebuilds that index
over the staged tree as its last staging step (after fake-signing, which changes sizes). The
writer (`AmprIndex.cs`) follows the reference builder in drakmor's ampr_emu repository byte for
byte: UTF-8 keys with only ASCII A–Z folded, FNV-1a-64 slots, the emulator's trace files and OS
metadata skipped. The index is written by rename, so a hard-linked index from the source is
replaced in the mirror, never written through. `--no-ampr-index` keeps the source's index as is;
`ampr-index <folder>` writes one for any folder (diagnostics).
