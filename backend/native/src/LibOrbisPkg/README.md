# LibOrbisPkg (vendored source)

The C# sources of [LibOrbisPkg](https://github.com/maxton/LibOrbisPkg) by Maxton, commit
`643477263b2644e0803e0f58b8726ea4e3f3b7d4` (2020-07-26), folder `LibOrbisPkg/` without
`Properties/` and the old-style project file; `LibOrbisPkg.csproj` here is a new SDK-style build file for .NET 9. License: GNU LGPL-3.0-or-later
(`../../LICENSE.LibOrbisPkg`, notice in `../../NOTICE.LibOrbisPkg`).

`ffpfsc-pkg-tool` compiles these files in (see `../ffpfsc-pkg-tool/PkgTool.csproj`) for the
`ps4-list` and `ps4-extract` commands: reading the header, opening a fake package's PFS
with the library's fake keyset, listing and extracting the inner files.

## Modification notice (LGPL-3 section 4 / GPL-3 section 5a)

Changed on 2026-10-06 by this project, in one place:

- `Util/Extensions.cs`: the `TupleExtension` class (a `Deconstruct` polyfill for .NET
  Framework 4.0) is removed. On .NET 9 `System.TupleExtensions` provides the same method and
  the two made every tuple deconstruction ambiguous (CS0121). No behaviour changes.

To compare with upstream: `git clone https://github.com/maxton/LibOrbisPkg && git -C
LibOrbisPkg checkout 643477263b2644e0803e0f58b8726ea4e3f3b7d4`, then diff its `LibOrbisPkg/`
folder against this one.
