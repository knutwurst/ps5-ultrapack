// Applies the ps5-ultrapack patches to a pristine LibProsperoPkg 1.2.0 (build d7090eb6) assembly.
// Usage: dotnet run -- <path/to/LibProsperoPkg.dll>   (patched in place; a .orig backup is written)
// See ../README.md for what each patch does and why it is required on a retail PS5.
using System;
using System.IO;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

if (args.Length != 1) { Console.Error.WriteLine("usage: CecilPatch <LibProsperoPkg.dll>"); return 2; }
var dll = Path.GetFullPath(args[0]);
if (!File.Exists(dll)) { Console.Error.WriteLine($"not found: {dll}"); return 2; }
var orig = dll + ".orig";
if (!File.Exists(orig)) File.Copy(dll, orig);

int applied = 0;
using (var asm = AssemblyDefinition.ReadAssembly(dll, new ReaderParameters { ReadWrite = true }))
{
    var builder = asm.MainModule.GetType("LibProsperoPkg.PKG.ProsperoPkgBuilder")
                  ?? throw new InvalidOperationException("LibProsperoPkg.PKG.ProsperoPkgBuilder not found — wrong assembly?");

    // ---- Lineage check: drm_type = 16 for Application volumes is upstream since d7090eb6 ----
    // Builds before that stamped drm_type = 0 for "standard" Application volumes and needed a
    // branch retarget here (patch 1 of the 4f71489e era). The current code is
    //   drm_type = isFree ? 0 : 16
    // whose IL ends in  ldc.i4.s 16 ; br.s STORE ; ldc.i4.0 ; STORE: stfld drm_type  with the
    // branch deciding on the "free" DRM flag only. The old pattern had a second conditional
    // (brfalse.s on the "upgradable" local) before the 16; refuse to patch an assembly that
    // still has it so an old build is never shipped without its drm_type fix.
    foreach (var m in builder.Methods.Where(m => m.HasBody))
    {
        var il = m.Body.Instructions;
        for (int i = 4; i < il.Count; i++)
        {
            if (il[i].OpCode != OpCodes.Stfld || il[i].Operand is not FieldReference fr || fr.Name != "drm_type") continue;
            var ldc16 = il[i - 3]; var brf = il[i - 4];
            if (ldc16.OpCode == OpCodes.Ldc_I4_S && (sbyte)ldc16.Operand == 16 && brf.OpCode == OpCodes.Brfalse_S && brf.Operand == il[i - 1])
            {
                Console.Error.WriteLine($"[0] FAILED: {m.Name} still stamps drm_type = 0 for \"standard\" Application volumes — this is a pre-d7090eb6 build; use the patcher from ps5-ultrapack 2.0.2");
                return 3;
            }
        }
    }
    Console.WriteLine("[0] drm_type = 16 for Application volumes is upstream in this build (no patch needed)");

    // ---- Patch 2: keep fakelib/libSceAmpr.sprx + libScePlayGo.sprx ------------------------
    // Upstream: local function FilterFakeLibraryDirectory() removes exactly these two files from
    // fakelib/. They are the AMPR/PlayGo emulators a backported title ships for older firmware;
    // without them the eboot's module imports fail at launch (CE-100022-5). Make it a no-op.
    var f = builder.Methods.FirstOrDefault(m => m.Name.Contains("FilterFakeLibraryDirectory") && m.HasBody);
    if (f == null) { Console.Error.WriteLine("[2] FAILED: FilterFakeLibraryDirectory not found (upstream changed?)"); return 3; }
    if (f.Body.Instructions.Count == 1 && f.Body.Instructions[0].OpCode == OpCodes.Ret)
        Console.WriteLine("[2] FilterFakeLibraryDirectory already a no-op");
    else
    {
        var ilp = f.Body.GetILProcessor();
        f.Body.Instructions.Clear(); f.Body.ExceptionHandlers.Clear(); f.Body.Variables.Clear();
        ilp.Append(ilp.Create(OpCodes.Ret));
        Console.WriteLine("[2] FilterFakeLibraryDirectory -> ret (fakelib emulators are kept)");
    }
    applied++;

    // ---- Patch 3: keep /ampr_emu.index ----------------------------------------------------
    // Upstream (since d7090eb6): BuildInnerTree drops three root files from the image —
    //   new string[3] { "ampr_emu.index", "entitlements.txt", "entitlement_key.dat" }
    // The AMPR emulator (fakelib/libSceAmpr.sprx, kept by patch 2) reads /app0/ampr_emu.index
    // at runtime, and the tool rebuilds that index over the packed files before the build.
    // The other two names stay excluded. Blank the first string: no file has an empty name,
    // so the RemoveAll for it never matches and the loop otherwise runs unchanged.
    bool p3 = false;
    foreach (var m in builder.Methods.Where(m => m.HasBody))
    {
        var il = m.Body.Instructions;
        if (!il.Any(x => x.OpCode == OpCodes.Ldstr && (string)x.Operand == "entitlement_key.dat")) continue;
        var hit = il.FirstOrDefault(x => x.OpCode == OpCodes.Ldstr && (string)x.Operand == "ampr_emu.index");
        if (hit != null) { hit.Operand = ""; p3 = true; Console.WriteLine($"[3] ampr_emu.index root exclusion disabled in {m.Name}"); }
        else if (il.Any(x => x.OpCode == OpCodes.Ldstr && (string)x.Operand == "")) { p3 = true; Console.WriteLine($"[3] ampr_emu.index root exclusion already disabled in {m.Name}"); }
    }
    if (!p3) { Console.Error.WriteLine("[3] FAILED: root exclusion list (ampr_emu.index / entitlement_key.dat) not found (upstream changed?)"); return 3; }
    // The same three names are also "host artifacts" for ProsperoPlayGo.IsOmittedSourcePath,
    // which BuildInnerTree's IsHostArtifact consults while populating the tree. Blank the
    // ampr_emu.index comparison there too.
    var playgo = asm.MainModule.GetType("LibProsperoPkg.PlayGo.ProsperoPlayGo")
                 ?? throw new InvalidOperationException("LibProsperoPkg.PlayGo.ProsperoPlayGo not found — wrong assembly?");
    var omit = playgo.Methods.FirstOrDefault(m => m.Name == "IsOmittedSourcePath" && m.HasBody);
    if (omit == null) { Console.Error.WriteLine("[3] FAILED: ProsperoPlayGo.IsOmittedSourcePath not found (upstream changed?)"); return 3; }
    {
        var il = omit.Body.Instructions;
        var hit = il.FirstOrDefault(x => x.OpCode == OpCodes.Ldstr && (string)x.Operand == "ampr_emu.index");
        if (hit != null) { hit.Operand = ""; Console.WriteLine("[3] ampr_emu.index host-artifact rule disabled in IsOmittedSourcePath"); }
        else if (il.Any(x => x.OpCode == OpCodes.Ldstr && (string)x.Operand == "") && il.Any(x => x.OpCode == OpCodes.Ldstr && (string)x.Operand == "entitlement_key.dat"))
            Console.WriteLine("[3] ampr_emu.index host-artifact rule already disabled in IsOmittedSourcePath");
        else { Console.Error.WriteLine("[3] FAILED: IsOmittedSourcePath does not compare against ampr_emu.index (upstream changed?)"); return 3; }
    }
    applied++;

    // (A fourth patch, ResolveParallelism -> 1, kept package reading single-threaded from
    // 2026-10-05 to 2026-10-06. The crash it worked around was the compressed single-file
    // bundle, not the library: with the compression off, 36 parallel extractions matched the
    // single-threaded tree byte for byte. See PkgTool.csproj.)

    asm.Write();
}
Console.WriteLine($"OK: {applied} patch(es) applied to {dll}  (backup: {orig})");
return 0;
