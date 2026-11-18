# LibProsperoPkg.dll patches

`ffpfsc-pkg-tool` links against drakmor's LibProsperoPkg 1.2.0, build
`d7090eb6` (GPL-3.0-or-later). Both the pristine upstream assembly
(`lib/LibProsperoPkg.dll.orig`) and the patched one the tool links against
(`lib/LibProsperoPkg.dll`) are tracked in git, with SHA-256 sums in
`lib/SHA256SUMS`; `lib/README.md` records the provenance and the
modification notice. Two upstream behaviours keep a backported title from
launching on a jailbroken PS5, and a third drops localized icons from the
package; all are fixed by rewriting IL in the assembly before the tool is
compiled.

## Applying

`CecilPatch/` is a small .NET console project using Mono.Cecil. It patches
the DLL in place, writes a `.orig` backup next to it, finds each site by
instruction pattern (not by file offset), and is idempotent — running it on
an already patched DLL reports "already" for each site and changes nothing.

```bash
cd backend/native/src/ffpfsc-pkg-tool/patches/CecilPatch
dotnet run -- ../../lib/LibProsperoPkg.dll
```

Then `dotnet publish` the tool as usual. If upstream changes and a pattern
is not found, the patcher exits 3 and names the patch — do not ship a tool
built from an unpatched DLL.

## Lineage check — `drm_type = 16` is upstream now

Builds before `d7090eb6` stamped the CNT header's `drm_type` with 0 for a
`"standard"`-DRM Application volume (Sony's publisher writes 16; with 0 and
real license records the homescreen shows a padlock and the launch fails
with CE-100022-5). That needed a branch retarget, "patch 1" of the
`4f71489e` era. Upstream now writes `isFree ? 0 : 16`. The patcher no
longer changes this site; it refuses an assembly that still carries the old
pattern, so an old build is never shipped without its fix.

## Patch 2 — keep `fakelib/libSceAmpr.sprx` and `libScePlayGo.sprx`

The local function `FilterFakeLibraryDirectory` in `BuildInnerTree` removes
exactly these two files from a source's `fakelib/` directory. They are the
AMPR and PlayGo emulators a backported dump ships so the title runs on
firmware older than the one it was built for; ShadowMountPlus overlays
`/app0/fakelib` into the sandbox before spawn. Without them the eboot's
module imports fail and the launch dies with CE-100022-5. Retail packages
built with Sony's own tools carry other emulators in `fakelib/` (libSceAgc,
libScePsml, …) and some ship an `ampr_emu.index` too, so keeping the
directory intact is the right shape.

The patcher replaces the function body with a single `ret`.

## Patch 3 — keep `/ampr_emu.index`

Since `d7090eb6`, `BuildInnerTree` drops three files from the image root:

```csharp
new string[3] { "ampr_emu.index", "entitlements.txt", "entitlement_key.dat" }
```

The AMPR emulator kept by patch 2 reads `/app0/ampr_emu.index` at runtime,
and the tool rebuilds that index over the packed files before the build
(`--no-ampr-index` keeps the source's copy). The patcher blanks the first
string of the array: no file has an empty name, so that `RemoveAll` never
matches, while `entitlements.txt` and `entitlement_key.dat` stay excluded.

## Former patch 4 — single-threaded package reading (removed 2026-10-06)

From 2026-10-05 to 2026-10-06 the patcher also made
`ProsperoPackageArchive.ResolveParallelism` return 1, because the parallel
inner-PFS sessions of `ExtractInnerFiles` crashed the process on macOS arm64
(`AccessViolationException` on a thread-pool worker). The crash was the
compressed single-file bundle the tool was published as, not the library:
every multithreaded pass died that way, the Kraken encoder included. With
`EnableCompressionInSingleFile` off (`../PkgTool.csproj`), 36 parallel
extractions of two test packages matched the single-threaded tree byte for
byte, so the library's own worker count is back.

## Patch 5 — keep localized `icon0_NN.dds` and `pic1_NN.dds`

`CollectMediaEntries` writes `icon0.dds`, `pic0.dds`, `pic1.dds` and
`pic2.dds` from a fixed table. Its generic loop over the other `sce_sys`
entries then skips every name ending in `.dds`, so the localized variants
(`icon0_10.dds`, `icon0_11.dds`, `pic1_10.dds`, …) never reach the CNT even
though `EntryNames` has ids for them and the PNG twins are packed. The tool's
validate step fails such a package (issue #12). The patcher replaces the
`".dds"` in that `EndsWith(…, StringComparison.Ordinal)` with `"\0"`; no file
name ends in a NUL, so the loop now packs them. The four fixed-table names
are already in `emitted` and are not written twice.

## What is deliberately not patched

- The PlayGo prepared-set handling. Upstream never copies a source set into
  the package any more: it keeps the set's file assignments, chunk labels,
  languages and scenario presentation and rebuilds the image ranges for the
  image it just made (up to 255 chunks and 5 scenarios). `Program.cs` still
  validates a source set against its on-wire format and the packed tree
  before the build and drops a corrupt or stale set from the staged mirror,
  so upstream generates one instead of throwing.
- Keystone generation. Upstream keeps a present `sce_sys/keystone` and only
  generates one when missing — which is correct: the keystone is the
  save-data key, and a regenerated one makes every existing save unreadable.

## Patch 6 — SourceHooks: build from an image without unpacking it

A `.ffpfs`/`.ffpfsc` used to be unpacked in full before a package could be built from it,
which needs the whole game in temp. The patcher adds a small public static class
`LibProsperoPkg.Util.SourceHooks` with two delegates, `Open` (path → stream or null) and
`Length` (path → size or -1). It routes the `FileInfo.Length` reads of the source scan
(`BuildInnerTree`'s `Populate`) and of `ProsperoPs5InnerFile` through `LengthOf`, and makes
`ProsperoPs5InnerFile.OpenRead` try `Open` before it opens the file. With no delegate set the
library behaves exactly as before.

The tool (`SourceImage.cs`) writes the image's small and special files to a stub folder
(`sce_sys`, `sce_module`, `fakelib*`, executables, anything under 1 MiB) and leaves the big
files as empty stand-ins whose size and bytes the hooks take from the image through the
library's own `PfsReader`/`PFSCReader`. The package is byte-identical to one built from the
unpacked folder (checked in `test_fpkg_pipelines.py`, `chain4.read-in-place-same-package`).

## Patch 7 — file hooks: the inner image straight into the package

The library writes the package's inner image (`pfs_image.dat`) to a spool file in temp and then
copies it into the package, where it starts at 0x10000. At the end both exist at once, about
twice the package. The patcher adds `SourceHooks.OpenFile` (path, mode → `FileStream` or null)
and makes `BuildFileIO.Open` ask it first; the NAPS reader of the physical inner image, which
opened the spool with `new FileStream`, goes through `BuildFileIO.Open`, and the outer PFS
builder takes its source sizes through `LengthOf`.

The tool (`InnerInPackage.cs`) points the spool at a file of its own in the output folder, at
offset 0x10000. When the library creates the package, that file is renamed into it, and the
outer PFS writes each data block back where it already is. If the layout pass re-ordered
blocks, a range the writer would overwrite before it was read is kept in a side file first.
`--no-inner-in-package` restores the library's own way; `chain4.inner-in-package` in
`test_fpkg_pipelines.py` builds both ways, with the layout pass forced to re-order, and compares
the bytes.

