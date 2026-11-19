# lib/ — LibProsperoPkg 1.2.0 (build a3ee4c1a)

The one dependency of `ffpfsc-pkg-tool` that is not a NuGet package.

| File | What it is |
|---|---|
| `LibProsperoPkg.dll.orig` | The pristine upstream assembly, v1.2.0.0, informational version `1.2.0+a3ee4c1a6c37c6689717416c4962fa544e063e2c`, as compiled by its author. Never modified here. |
| `LibProsperoPkg.dll` | The same assembly with the IL patches below applied. This is what `PkgTool.csproj` links against and what ships inside the tool binary. |
| `SHA256SUMS` | Sums of both files: `shasum -a 256 -c SHA256SUMS`. |

## Provenance

- License: **GPL-3.0-or-later**. Text: `../../../LICENSE.LibProsperoPkg`; upstream notice:
  `../../../NOTICE.LibProsperoPkg`; how it is bundled: `../../../NOTICE.fpkg`.
- Lineage: drakmor's fork <https://github.com/drakmor/LibProsperoPkg> of
  <https://github.com/SvenGDK/LibProsperoPKG>. The binary was taken unmodified from the
  author's fpkg-gui 0.6.12 release (files dated 2026-10-26), which the author hands out on
  their Discord server; that is where this build is published.
- The build id `a3ee4c1a` in the assembly's informational version is **not** in drakmor's or
  SvenGDK's GitHub repository (checked 2026-11-18). The nearest public ancestor is etawen's
  `drakmor/LibProsperoPKG` branch `feature/full-ppr-pkg-roundtrip`. The corresponding source
  has to come from the author; this project only redistributes the assembly it received, with
  the modifications below.
- Against the previous build `d7090eb6` (fpkg-gui 0.6.11, used here 2026-10-05 to 2026-11-18)
  the decompiled code differs in four ways: a source PlayGo set with a single chunk is kept
  (it was dropped before because no file sat in a chunk other than 0), the package reader
  maps CNT entry ids to corrected names (param.json, nptitle.dat, the trophy2/uds files),
  `EntryId` gained named values for ids it already wrote, and a new `ProsperoMetadataRecovery`
  class exists that this tool does not call.
  The Kraken encoder and the image builders are unchanged. A package built from the
  retail sample with both builds is byte-identical.
- Until 2026-10-05 this folder held build `4f71489e` (from the author's a53-fpkg 0.5 release),
  with three behavioural differences that matter here: that build stamped `drm_type = 0` for
  `"standard"` Application volumes (needed an IL patch), copied a source PlayGo prepared set
  into the package unchanged, and kept a root `ampr_emu.index`.

## Modification notice (GPL-3 section 5a)

`LibProsperoPkg.dll` was modified by this project (first on 2026-10-05; all patches re-applied
to build `a3ee4c1a` on 2026-11-18, patch 8 added on 2026-11-19):

1. `ProsperoPkgBuilder.BuildInnerTree`: the local function `FilterFakeLibraryDirectory`
   is replaced by a single `ret`, so a source's `fakelib/` directory is kept intact.
2. `ProsperoPkgBuilder.BuildInnerTree`: the root-file exclusion list
   `{ "ampr_emu.index", "entitlements.txt", "entitlement_key.dat" }` has its first string
   blanked, so `/ampr_emu.index` stays in the image; the other two names stay excluded.
3. `ProsperoPkgBuilder.CollectMediaEntries` (added 2026-11-08): the suffix in
   `EndsWith(".dds", StringComparison.Ordinal)` of the generic `sce_sys` loop is replaced by
   `"\0"`, so localized icons and backgrounds (`icon0_NN.dds`, `pic1_NN.dds`) go into the CNT
   instead of being dropped; `icon0`/`pic0`/`pic1`/`pic2.dds` still come from their own table.
4. New type `LibProsperoPkg.Util.SourceHooks` (added 2026-11-18) with two public static
   delegates, `Open` and `Length`, and the methods `LengthOf(FileInfo)` and `TryOpen(string)`.
   The `FileInfo.Length` reads in the source scan of `BuildInnerTree` and in
   `ProsperoPs5InnerFile` go through `LengthOf`, and `ProsperoPs5InnerFile.OpenRead` asks
   `TryOpen` first. With no delegate set, every read is the same as before. The tool sets them
   to build a package straight from a `.ffpfs`/`.ffpfsc` without unpacking its big files.
5. `SourceHooks.OpenFile` (added 2026-11-18): `BuildFileIO.Open` asks this delegate first, the
   NAPS reader of the physical inner image opens it through `BuildFileIO.Open`, and the outer
   PFS builder sizes its source files through `LengthOf`. With no delegate set, nothing changes.
   The tool uses it to write the package's inner image straight to its place in the package.
6. `ProsperoPkgBuilder.BuildInnerTree`, local function `Populate` (added 2026-11-19): the walk
   over the source folder skips OS junk: `._*` and the names in `FsJunk.cs` (`.DS_Store`,
   `Thumbs.db`, `desktop.ini`, `.Trashes` ...), checked by the new `SourceHooks.IsJunk`. macOS
   leaves a `._` file on exFAT next to every file it writes, and those used to go into the package.
(A further change, `ProsperoPackageArchive.ResolveParallelism` returning 1, was applied from
2026-10-05 to 2026-10-06 and is gone: the crash it worked around was the tool's compressed
single-file bundle, see `../patches/README.md`.)

The patches are applied by `../patches/CecilPatch` (Mono.Cecil, pattern-based, idempotent);
the reasoning is in `../patches/README.md`. To reproduce the patched file from the pristine one:

```bash
cp LibProsperoPkg.dll.orig LibProsperoPkg.dll
cd ../patches/CecilPatch && dotnet run -- ../../lib/LibProsperoPkg.dll
```
