// usage: dotnet run -- path/to/LibProsperoPkg.dll (1.2.0, d7090eb6); patched in place, .orig kept
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

    // refuse pre-d7090eb6 builds: they stamp drm_type 0 and would ship without the fix
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

    // patch 2: keep fakelib's AMPR/PlayGo emulators, without them launch fails (CE-100022-5)
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

    // patch 3: keep /ampr_emu.index for the AMPR emulator; a blank name never matches
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
    // same names again in IsOmittedSourcePath, blank ampr_emu.index there too
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

    asm.Write();
}
Console.WriteLine($"OK: {applied} patch(es) applied to {dll}  (backup: {orig})");
return 0;
