// usage: dotnet run -- path/to/LibProsperoPkg.dll (1.2.0, a3ee4c1a or d7090eb6); patched in place, .orig kept
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

    // patch 5: keep icon0_NN.dds and pic1_NN.dds in the CNT; the generic sce_sys loop skipped every .dds
    // and only icon0/pic0/pic1/pic2 got theirs. A NUL never ends a file name, so the skip never matches
    var media = builder.NestedTypes.FirstOrDefault(t => t.Name.Contains("CollectMediaEntries"))
                ?.Methods.FirstOrDefault(m => m.Name == "MoveNext" && m.HasBody);
    if (media == null) { Console.Error.WriteLine("[5] FAILED: CollectMediaEntries iterator not found (upstream changed?)"); return 3; }
    {
        var il = media.Body.Instructions;
        bool EndsWithOrdinal(int i) => i + 2 < il.Count && il[i + 1].OpCode == OpCodes.Ldc_I4_4
            && il[i + 2].Operand is MethodReference mr && mr.Name == "EndsWith" && mr.Parameters.Count == 2;
        var hits = Enumerable.Range(0, il.Count).Where(i => il[i].OpCode == OpCodes.Ldstr && EndsWithOrdinal(i)).ToList();
        var dds = hits.Where(i => (string)il[i].Operand == ".dds").ToList();
        if (dds.Count == 1) { il[dds[0]].Operand = "\0"; Console.WriteLine("[5] .dds skip in CollectMediaEntries disabled (localized icon/pic DDS are kept)"); }
        else if (dds.Count == 0 && hits.Any(i => (string)il[i].Operand == "\0")) Console.WriteLine("[5] .dds skip in CollectMediaEntries already disabled");
        else { Console.Error.WriteLine($"[5] FAILED: expected one EndsWith(\".dds\", Ordinal) in CollectMediaEntries, found {dds.Count} (upstream changed?)"); return 3; }
    }
    applied++;

    // patch 6: SourceHooks, so the tool can hand over game files read straight from an image;
    // with no hook set the library reads its files exactly as before
    int p6 = PatchSourceHooks(asm.MainModule);
    if (p6 < 0) return 3;
    applied++;

    // patch 7: file hooks, so the tool can place the package's inner image straight into the package
    int p7 = PatchFileHooks(asm.MainModule);
    if (p7 < 0) return 3;
    applied++;

    asm.Write();
}
Console.WriteLine($"OK: {applied} patch(es) applied to {dll}  (backup: {orig})");
return 0;

static TypeReference FindTypeRef(ModuleDefinition module, string fullName)
    => module.GetTypeReferences().FirstOrDefault(t => t.FullName == fullName)
       ?? throw new InvalidOperationException($"{fullName} is not referenced by the assembly");

static MethodReference FindMemberRef(ModuleDefinition module, string declaringType, string name)
    => module.GetMemberReferences().OfType<MethodReference>()
           .FirstOrDefault(m => m.DeclaringType.FullName == declaringType && m.Name == name && !m.HasParameters)
       ?? throw new InvalidOperationException($"{declaringType}::{name} is not referenced by the assembly");

static int PatchSourceHooks(ModuleDefinition module)
{
    const string Ns = "LibProsperoPkg.Util", Name = "SourceHooks";
    var innerFile = module.GetType("LibProsperoPkg.PFS.ProsperoPs5InnerFile");
    var builder = module.GetType("LibProsperoPkg.PKG.ProsperoPkgBuilder");
    if (innerFile == null || builder == null) { Console.Error.WriteLine("[6] FAILED: ProsperoPs5InnerFile or ProsperoPkgBuilder not found"); return -1; }
    if (module.GetType(Ns + "." + Name) != null) { Console.WriteLine("[6] SourceHooks already in place"); return 1; }

    var ts = module.TypeSystem;
    var fileInfo = FindTypeRef(module, "System.IO.FileInfo");
    var stream = FindTypeRef(module, "System.IO.Stream");
    var func2 = FindTypeRef(module, "System.Func`2");
    var getLength = FindMemberRef(module, "System.IO.FileInfo", "get_Length");
    var getFullName = FindMemberRef(module, "System.IO.FileSystemInfo", "get_FullName");

    GenericInstanceType Func(TypeReference a, TypeReference r)
    {
        var g = new GenericInstanceType(func2); g.GenericArguments.Add(a); g.GenericArguments.Add(r); return g;
    }
    if (!func2.HasGenericParameters)
    {
        func2.GenericParameters.Add(new GenericParameter("T", func2));
        func2.GenericParameters.Add(new GenericParameter("TResult", func2));
    }
    MethodReference Invoke(GenericInstanceType f)
    {
        var m = new MethodReference("Invoke", func2.GenericParameters[1], f) { HasThis = true };
        m.Parameters.Add(new ParameterDefinition(func2.GenericParameters[0]));
        return m;
    }
    var funcOpen = Func(ts.String, stream);
    var funcLength = Func(ts.String, ts.Int64);

    var hooks = new TypeDefinition(Ns, Name,
        TypeAttributes.Public | TypeAttributes.Abstract | TypeAttributes.Sealed | TypeAttributes.BeforeFieldInit, ts.Object);
    var openField = new FieldDefinition("Open", FieldAttributes.Public | FieldAttributes.Static, funcOpen);
    var lengthField = new FieldDefinition("Length", FieldAttributes.Public | FieldAttributes.Static, funcLength);
    hooks.Fields.Add(openField); hooks.Fields.Add(lengthField);

    // public static long LengthOf(FileInfo f): the hook's length when it knows the path, else f.Length
    var lengthOf = new MethodDefinition("LengthOf", MethodAttributes.Public | MethodAttributes.Static | MethodAttributes.HideBySig, ts.Int64);
    lengthOf.Parameters.Add(new ParameterDefinition("file", ParameterAttributes.None, fileInfo));
    lengthOf.Body.Variables.Add(new VariableDefinition(ts.Int64));
    {
        var il = lengthOf.Body.GetILProcessor();
        var fallback = il.Create(OpCodes.Ldarg_0);
        il.Append(il.Create(OpCodes.Ldsfld, lengthField));
        il.Append(il.Create(OpCodes.Brfalse, fallback));
        il.Append(il.Create(OpCodes.Ldsfld, lengthField));
        il.Append(il.Create(OpCodes.Ldarg_0));
        il.Append(il.Create(OpCodes.Callvirt, getFullName));
        il.Append(il.Create(OpCodes.Callvirt, Invoke(funcLength)));
        il.Append(il.Create(OpCodes.Stloc_0));
        il.Append(il.Create(OpCodes.Ldloc_0));
        il.Append(il.Create(OpCodes.Ldc_I8, 0L));
        il.Append(il.Create(OpCodes.Blt, fallback));
        il.Append(il.Create(OpCodes.Ldloc_0));
        il.Append(il.Create(OpCodes.Ret));
        il.Append(fallback);
        il.Append(il.Create(OpCodes.Callvirt, getLength));
        il.Append(il.Create(OpCodes.Ret));
    }
    hooks.Methods.Add(lengthOf);

    // public static Stream TryOpen(string path): the hook's stream, or null for a normal open
    var tryOpen = new MethodDefinition("TryOpen", MethodAttributes.Public | MethodAttributes.Static | MethodAttributes.HideBySig, stream);
    tryOpen.Parameters.Add(new ParameterDefinition("path", ParameterAttributes.None, ts.String));
    {
        var il = tryOpen.Body.GetILProcessor();
        var none = il.Create(OpCodes.Ldnull);
        il.Append(il.Create(OpCodes.Ldsfld, openField));
        il.Append(il.Create(OpCodes.Brfalse, none));
        il.Append(il.Create(OpCodes.Ldsfld, openField));
        il.Append(il.Create(OpCodes.Ldarg_0));
        il.Append(il.Create(OpCodes.Callvirt, Invoke(funcOpen)));
        il.Append(il.Create(OpCodes.Ret));
        il.Append(none);
        il.Append(il.Create(OpCodes.Ret));
    }
    hooks.Methods.Add(tryOpen);
    module.Types.Add(hooks);

    // a) every FileInfo.Length in the source scan and in ProsperoPs5InnerFile goes through LengthOf
    int lengths = 0;
    var scan = builder.Methods.Where(m => m.HasBody && m.Name.Contains("Populate")).ToList();
    foreach (var m in scan.Concat(innerFile.Methods.Where(m => m.HasBody)))
    {
        foreach (var ins in m.Body.Instructions)
        {
            if ((ins.OpCode == OpCodes.Callvirt || ins.OpCode == OpCodes.Call) && ins.Operand is MethodReference mr
                && mr.DeclaringType.FullName == "System.IO.FileInfo" && mr.Name == "get_Length")
            {
                ins.OpCode = OpCodes.Call; ins.Operand = lengthOf; lengths++;
            }
        }
    }
    if (scan.Count == 0 || lengths < 3) { Console.Error.WriteLine($"[6] FAILED: expected FileInfo.Length in the source scan and ProsperoPs5InnerFile, found {lengths}"); return -1; }

    // b) ProsperoPs5InnerFile.OpenRead asks the hook first
    var openRead = innerFile.Methods.FirstOrDefault(m => m.Name == "OpenRead" && m.HasBody && !m.HasParameters);
    var getSource = innerFile.Methods.FirstOrDefault(m => m.Name == "get_SourcePath");
    if (openRead == null || getSource == null) { Console.Error.WriteLine("[6] FAILED: ProsperoPs5InnerFile.OpenRead or SourcePath not found"); return -1; }
    {
        var il = openRead.Body.GetILProcessor();
        var first = openRead.Body.Instructions[0];
        var cont = il.Create(OpCodes.Pop);
        il.InsertBefore(first, il.Create(OpCodes.Ldarg_0));
        il.InsertBefore(first, il.Create(OpCodes.Call, getSource));
        il.InsertBefore(first, il.Create(OpCodes.Call, tryOpen));
        il.InsertBefore(first, il.Create(OpCodes.Dup));
        il.InsertBefore(first, il.Create(OpCodes.Brfalse, cont));
        il.InsertBefore(first, il.Create(OpCodes.Ret));
        il.InsertBefore(first, cont);
    }
    Console.WriteLine($"[6] SourceHooks added: {lengths} FileInfo.Length read(s) and ProsperoPs5InnerFile.OpenRead go through it");
    return 1;
}

static int PatchFileHooks(ModuleDefinition module)
{
    var hooks = module.GetType("LibProsperoPkg.Util.SourceHooks");
    var buildIO = module.GetType("LibProsperoPkg.Util.BuildFileIO");
    var naps = module.GetType("LibProsperoPkg.PKG.ProsperoNapsMeta");
    var outer = module.GetType("LibProsperoPkg.PFS.ProsperoOuterPfsBuilder");
    if (hooks == null || buildIO == null || naps == null || outer == null)
    { Console.Error.WriteLine("[7] FAILED: SourceHooks, BuildFileIO, ProsperoNapsMeta or ProsperoOuterPfsBuilder not found"); return -1; }
    if (hooks.Fields.Any(f => f.Name == "OpenFile")) { Console.WriteLine("[7] file hooks already in place"); return 1; }

    var ts = module.TypeSystem;
    var open = buildIO.Methods.FirstOrDefault(m => m.Name == "Open" && m.Parameters.Count == 6 && m.HasBody);
    if (open == null) { Console.Error.WriteLine("[7] FAILED: BuildFileIO.Open(6) not found"); return -1; }
    var fileStream = open.ReturnType;
    var func2 = FindTypeRef(module, "System.Func`2");
    var func3 = module.GetTypeReferences().FirstOrDefault(t => t.FullName == "System.Func`3")
                ?? new TypeReference("System", "Func`3", module, func2.Scope);
    if (!func3.HasGenericParameters)
    {
        func3.GenericParameters.Add(new GenericParameter("T1", func3));
        func3.GenericParameters.Add(new GenericParameter("T2", func3));
        func3.GenericParameters.Add(new GenericParameter("TResult", func3));
    }
    var funcOpenFile = new GenericInstanceType(func3);
    funcOpenFile.GenericArguments.Add(ts.String); funcOpenFile.GenericArguments.Add(ts.Int32); funcOpenFile.GenericArguments.Add(fileStream);
    var invoke = new MethodReference("Invoke", func3.GenericParameters[2], funcOpenFile) { HasThis = true };
    invoke.Parameters.Add(new ParameterDefinition(func3.GenericParameters[0]));
    invoke.Parameters.Add(new ParameterDefinition(func3.GenericParameters[1]));

    var field = new FieldDefinition("OpenFile", FieldAttributes.Public | FieldAttributes.Static, funcOpenFile);
    hooks.Fields.Add(field);

    // public static FileStream TryOpenFile(string path, int mode): the hook's stream, or null for a normal open
    var tryOpenFile = new MethodDefinition("TryOpenFile", MethodAttributes.Public | MethodAttributes.Static | MethodAttributes.HideBySig, fileStream);
    tryOpenFile.Parameters.Add(new ParameterDefinition("path", ParameterAttributes.None, ts.String));
    tryOpenFile.Parameters.Add(new ParameterDefinition("mode", ParameterAttributes.None, ts.Int32));
    {
        var il = tryOpenFile.Body.GetILProcessor();
        var none = il.Create(OpCodes.Ldnull);
        il.Append(il.Create(OpCodes.Ldsfld, field));
        il.Append(il.Create(OpCodes.Brfalse, none));
        il.Append(il.Create(OpCodes.Ldsfld, field));
        il.Append(il.Create(OpCodes.Ldarg_0));
        il.Append(il.Create(OpCodes.Ldarg_1));
        il.Append(il.Create(OpCodes.Callvirt, invoke));
        il.Append(il.Create(OpCodes.Ret));
        il.Append(none);
        il.Append(il.Create(OpCodes.Ret));
    }
    hooks.Methods.Add(tryOpenFile);

    // a) BuildFileIO.Open asks the hook first
    {
        var il = open.Body.GetILProcessor();
        var first = open.Body.Instructions[0];
        var cont = il.Create(OpCodes.Pop);
        il.InsertBefore(first, il.Create(OpCodes.Ldarg_0));
        il.InsertBefore(first, il.Create(OpCodes.Ldarg_1));
        il.InsertBefore(first, il.Create(OpCodes.Call, tryOpenFile));
        il.InsertBefore(first, il.Create(OpCodes.Dup));
        il.InsertBefore(first, il.Create(OpCodes.Brfalse, cont));
        il.InsertBefore(first, il.Create(OpCodes.Ret));
        il.InsertBefore(first, cont);
    }

    // b) the NAPS reader of the physical inner image opens it through BuildFileIO.Open too
    int naps6 = 0;
    foreach (var m in naps.Methods.Where(m => m.HasBody))
    {
        var il = m.Body.Instructions;
        if (!il.Any(x => x.Operand is MethodReference g && g.Name == "get_PhysicalInnerImagePath")) continue;
        foreach (var ins in il.Where(x => x.OpCode == OpCodes.Newobj && x.Operand is MethodReference c
                                          && c.DeclaringType.FullName == "System.IO.FileStream" && c.Parameters.Count == 6).ToList())
        {
            ins.OpCode = OpCodes.Call; ins.Operand = open; naps6++;
        }
    }
    if (naps6 != 1) { Console.Error.WriteLine($"[7] FAILED: expected one FileStream open of the physical inner image in ProsperoNapsMeta, found {naps6}"); return -1; }

    // c) the outer builder sizes its source files through LengthOf
    var lengthOf = hooks.Methods.First(m => m.Name == "LengthOf");
    int outerLen = 0;
    foreach (var m in outer.Methods.Where(m => m.HasBody && m.Name == "BuildForPackageToFile"))
        foreach (var ins in m.Body.Instructions)
            if ((ins.OpCode == OpCodes.Callvirt || ins.OpCode == OpCodes.Call) && ins.Operand is MethodReference mr
                && mr.DeclaringType.FullName == "System.IO.FileInfo" && mr.Name == "get_Length")
            { ins.OpCode = OpCodes.Call; ins.Operand = lengthOf; outerLen++; }
    if (outerLen != 1)
    {
        var where = outer.Methods.Where(m => m.HasBody && m.Body.Instructions.Any(i => i.Operand is MethodReference r
                        && r.DeclaringType.FullName == "System.IO.FileInfo" && r.Name == "get_Length")).Select(m => m.Name);
        Console.Error.WriteLine($"[7] FAILED: expected one FileInfo.Length in BuildForPackageToFile, found {outerLen}; FileInfo.Length is in: {string.Join(", ", where)}");
        return -1;
    }

    Console.WriteLine("[7] file hooks added: BuildFileIO.Open, the NAPS inner-image reader and the outer source sizes go through SourceHooks");
    return 1;
}
