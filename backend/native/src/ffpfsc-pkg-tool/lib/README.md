# lib/ — LibProsperoPkg 1.2.0 (build d7090eb6)

The one dependency of `ffpfsc-pkg-tool` that is not a NuGet package.

| File | What it is |
|---|---|
| `LibProsperoPkg.dll.orig` | The pristine upstream assembly, v1.2.0.0, informational version `1.2.0+d7090eb68e315e086edfd150738ffa0dcfc0cd27`, as compiled by its author. Never modified here. |
| `LibProsperoPkg.dll` | The same assembly with the three IL patches applied. This is what `PkgTool.csproj` links against and what ships inside the tool binary. |
| `SHA256SUMS` | Sums of both files: `shasum -a 256 -c SHA256SUMS`. |

## Provenance

- License: **GPL-3.0-or-later**. Text: `../../../LICENSE.LibProsperoPkg`; upstream notice:
  `../../../NOTICE.LibProsperoPkg`; how it is bundled: `../../../NOTICE.fpkg`.
- Lineage: drakmor's fork <https://github.com/drakmor/LibProsperoPkg> of
  <https://github.com/SvenGDK/LibProsperoPKG>. The binary was taken unmodified from the
  author's fpkg-gui 0.6.11 release (2026-10-04), which is where this build is published.
- The build id `d7090eb6` in the assembly's informational version is **not** in any public
  repository we know of (checked 2026-10-04: drakmor's and SvenGDK's GitHub repositories and
  git.etawen.dev). The nearest public ancestor is etawen's `drakmor/LibProsperoPKG` branch
  `feature/full-ppr-pkg-roundtrip`. The corresponding source has to come from the author;
  this project only redistributes the assembly it received, with the modifications below.
- Until 2026-10-05 this folder held build `4f71489e` (from the author's a53-fpkg 0.5 release),
  with three behavioural differences that matter here: that build stamped `drm_type = 0` for
  `"standard"` Application volumes (needed an IL patch), copied a source PlayGo prepared set
  into the package unchanged, and kept a root `ampr_emu.index`.

## Modification notice (GPL-3 section 5a)

`LibProsperoPkg.dll` was modified on 2026-10-05 by this project:

1. `ProsperoPkgBuilder.BuildInnerTree`: the local function `FilterFakeLibraryDirectory`
   is replaced by a single `ret`, so a source's `fakelib/` directory is kept intact.
2. `ProsperoPkgBuilder.BuildInnerTree`: the root-file exclusion list
   `{ "ampr_emu.index", "entitlements.txt", "entitlement_key.dat" }` has its first string
   blanked, so `/ampr_emu.index` stays in the image; the other two names stay excluded.
(A third change, `ProsperoPackageArchive.ResolveParallelism` returning 1, was applied from
2026-10-05 to 2026-10-06 and is gone: the crash it worked around was the tool's compressed
single-file bundle, see `../patches/README.md`.)

Both patches are applied by `../patches/CecilPatch` (Mono.Cecil, pattern-based, idempotent);
the reasoning is in `../patches/README.md`. To reproduce the patched file from the pristine one:

```bash
cp LibProsperoPkg.dll.orig LibProsperoPkg.dll
cd ../patches/CecilPatch && dotnet run -- ../../lib/LibProsperoPkg.dll
```
