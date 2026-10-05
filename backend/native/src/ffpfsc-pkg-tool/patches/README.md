# LibProsperoPkg.dll patches

`ffpfsc-pkg-tool` links against drakmor's LibProsperoPkg 1.2.0, build
`d7090eb6` (GPL-3.0-or-later). Both the pristine upstream assembly
(`lib/LibProsperoPkg.dll.orig`) and the patched one the tool links against
(`lib/LibProsperoPkg.dll`) are tracked in git, with SHA-256 sums in
`lib/SHA256SUMS`; `lib/README.md` records the provenance and the
modification notice. Two upstream behaviours keep a backported title from
launching on a jailbroken PS5, and one crashes the tool while reading a
package on macOS. All three are fixed by rewriting IL in the assembly
before the tool is compiled.

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

## Patch 4 — single-threaded package reading

`ProsperoPackageArchive.ResolveParallelism(requested)` returns
`min(ProcessorCount, 8)` when nothing is requested, and that is what
`ExtractInnerFiles` and the `Verify*` paths use: one inner-PFS session per
worker on a `Parallel.ForEach`. On macOS arm64 those sessions crash the
process (an `AccessViolationException` on a thread-pool worker, FailFast,
SIGABRT, "ffpfsc-pkg-tool quit unexpectedly") — reproduced on every full
`extract-inner` of a five-file package. It is the same family as the
encoder crash that keeps the builder at parallelism 1 (CHANGELOG 1.0.90).
The patcher makes the function return 1, so reading stays single-threaded
as it was in the previous build.

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
