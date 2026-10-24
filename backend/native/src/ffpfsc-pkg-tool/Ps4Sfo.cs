using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using Orbis = LibOrbisPkg;

namespace PkgTool;

// undoable in-place CNT header edit; Reseal() redoes digests, hashes and fake sig
internal sealed class CntEdit : IDisposable
{
    public readonly FileStream F;
    public Orbis.PKG.Pkg Pkg;
    readonly List<(long off, byte[] data)> saved = new();
    readonly BinaryWriter undo;
    readonly Dictionary<int, bool> entryOk = new();
    readonly bool bodyOk;

    public CntEdit(string path, string? undoPath, byte[] undoMagic, IEnumerable<Orbis.PKG.EntryId> changedEntries)
    {
        F = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.Read);
        Pkg = new Orbis.PKG.PkgReader(F).ReadPkg();
        var metas = Pkg.Metas.Metas;
        var dig = metas.First(m => m.id == Orbis.PKG.EntryId.DIGESTS);
        foreach (var id in changedEntries.Distinct())
        {
            int i = metas.FindIndex(m => m.id == id);
            if (i >= 1) entryOk[i] = Read(dig.DataOffset + 32 * i, 32).SequenceEqual(EntryDigest(metas[i]));
        }
        bodyOk = Pkg.Header.body_digest.SequenceEqual(Orbis.Util.Crypto.Sha256(F, (long)Pkg.Header.body_offset, (long)Pkg.Header.body_size));
        if (undoPath != null)
        {
            undo = new BinaryWriter(File.Create(undoPath));
            undo.Write(undoMagic);
            undo.Write(Encoding.ASCII.GetBytes(Pkg.Header.content_id.PadRight(36, '\0')[..36]));
            undo.Write(F.Length);
            undo.Flush();
        }
    }

    // an encrypted entry is hashed as stored, padded to the AES block size
    byte[] EntryDigest(Orbis.PKG.MetaEntry m) =>
        Orbis.Util.Crypto.Sha256(F, m.DataOffset, m.Encrypted ? (m.DataSize + 15) / 16 * 16 : m.DataSize);

    public byte[] Read(long off, int len) { var b = new byte[len]; F.Position = off; F.ReadExactly(b); return b; }

    public void Put(long off, byte[] b)
    {
        var old = Read(off, b.Length);
        saved.Add((off, old));
        if (undo != null) { undo.Write(off); undo.Write(old.Length); undo.Write(old); undo.Flush(); }
        F.Position = off; F.Write(b); F.Flush();
    }

    public void Reseal()
    {
        var metas = Pkg.Metas.Metas;
        var dig = metas.First(m => m.id == Orbis.PKG.EntryId.DIGESTS);
        foreach (var (i, ok) in entryOk)
            if (ok) Put(dig.DataOffset + 32 * i, EntryDigest(metas[i]));
        F.Position = 0;
        Pkg = new Orbis.PKG.PkgReader(F).ReadPkg();
        byte[] Cat(bool sc2)
        {
            var ms = new MemoryStream();
            var parts = new List<Orbis.PKG.Entry> { Pkg.EntryKeys, Pkg.ImageKey, Pkg.GeneralDigests, Pkg.Metas };
            if (!sc2) parts.Add(Pkg.Digests);
            foreach (var e in parts)
            {
                long size = sc2 && e.Id == Orbis.PKG.EntryId.METAS ? Pkg.Header.sc_entry_count * 0x20 : e.meta.DataSize;
                new Orbis.Util.SubStream(F, e.meta.DataOffset, size).CopyTo(ms);
            }
            return Orbis.Util.Crypto.Sha256(ms);
        }
        Put(0x100, Cat(false));
        Put(0x120, Cat(true));
        Put(0x140, Orbis.Util.Crypto.Sha256(Pkg.Digests.FileData));
        if (bodyOk) Put(0x160, Orbis.Util.Crypto.Sha256(F, (long)Pkg.Header.body_offset, (long)Pkg.Header.body_size));
        Put(0xFE0, Orbis.Util.Crypto.Sha256(F, 0, 0xFE0));
        Put(0x1000, Orbis.Util.Crypto.RSA2048EncryptKey(Orbis.Util.Keys.PkgSignKey, Orbis.Util.Crypto.Sha256(F, 0, 0x1000)));
    }

    // put back everything this edit wrote, newest first
    public void RollBack()
    {
        for (int i = saved.Count - 1; i >= 0; i--) { F.Position = saved[i].off; F.Write(saved[i].data); }
        F.Flush();
        saved.Clear();
    }

    public void Dispose() { undo?.Dispose(); F.Dispose(); }
}

// ps4-sfo: edit + reseal in place. major digest keys stay, formula unknown
internal static partial class Program
{
    static readonly string[] MajorParamKeys = { "ATTRIBUTE", "ATTRIBUTE2", "CATEGORY", "FORMAT", "PUBTOOLVER" };

    static int CmdPs4Sfo(string[] args)
    {
        if (args.Length < 3) return Bad("ps4-sfo needs <ps4.pkg> --set KEY=VALUE [--set ...] [--undo FILE]");
        var pkgPath = args[1];
        string? undoPath = null;
        var sets = new List<(string key, string value)>();
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i] == "--undo") undoPath = Need(args, ref i, "--undo");
            else if (args[i] == "--set")
            {
                var kv = Need(args, ref i, "--set");
                int eq = kv.IndexOf('=');
                if (eq <= 0) return Bad("--set needs KEY=VALUE");
                sets.Add((kv[..eq], kv[(eq + 1)..]));
            }
            else return Bad("unknown ps4-sfo flag: " + args[i]);
        }
        if (sets.Count == 0) return Bad("ps4-sfo needs at least one --set");
        if (!File.Exists(pkgPath)) throw new FileNotFoundException("package not found", pkgPath);
        foreach (var (k, _) in sets)
            if (MajorParamKeys.Contains(k))
            {
                Console.Error.WriteLine($"[ERROR] {k} is covered by the major param digest, which cannot be recomputed");
                return 1;
            }
        using (var img = new Ps4Image(pkgPath))
            if (img.Inner.GetFile("sce_sys/param.sfo") != null)
            {
                Console.Error.WriteLine("[ERROR] the package also has a param.sfo inside its content image; it is left alone");
                return 1;
            }

        // work out the new sfo first: a refused value writes nothing, not even the undo file
        Orbis.PKG.Pkg pkg;
        using (var fs = File.OpenRead(pkgPath)) pkg = new Orbis.PKG.PkgReader(fs).ReadPkg();
        var meta = pkg.Metas.Metas.First(m => m.id == Orbis.PKG.EntryId.PARAM_SFO);
        if (meta.Encrypted) { Console.Error.WriteLine("[ERROR] the param.sfo entry is encrypted"); return 1; }
        var sfo = new byte[meta.DataSize];
        using (var fs = File.OpenRead(pkgPath)) { fs.Position = meta.DataOffset; fs.ReadExactly(sfo); }
        var gd = pkg.GeneralDigests;
        bool paramOk = gd.set_digests.HasFlag(Orbis.PKG.GeneralDigest.ParamDigest)
                       && gd.Digests[Orbis.PKG.GeneralDigest.ParamDigest].SequenceEqual(Orbis.Util.Crypto.Sha256(sfo));
        int keyTable = BitConverter.ToInt32(sfo, 8), dataTable = BitConverter.ToInt32(sfo, 12), count = BitConverter.ToInt32(sfo, 16);
        var index = new Dictionary<string, int>();
        for (int i = 0; i < count; i++)
        {
            int ko = BitConverter.ToUInt16(sfo, 20 + 16 * i);
            index[Encoding.ASCII.GetString(sfo, keyTable + ko, Array.IndexOf(sfo, (byte)0, keyTable + ko) - keyTable - ko)] = 20 + 16 * i;
        }
        foreach (var (k, v) in sets)
        {
            if (!index.TryGetValue(k, out int at)) { Console.Error.WriteLine($"[ERROR] {k} is not in the param.sfo"); return 1; }
            int fmt = BitConverter.ToUInt16(sfo, at + 2), len = BitConverter.ToInt32(sfo, at + 4), max = BitConverter.ToInt32(sfo, at + 8);
            int data = dataTable + BitConverter.ToInt32(sfo, at + 12);
            string old;
            if (fmt == 0x0404)
            {
                old = "0x" + BitConverter.ToUInt32(sfo, data).ToString("x");
                uint n = v.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? Convert.ToUInt32(v[2..], 16) : uint.Parse(v);
                BitConverter.GetBytes(n).CopyTo(sfo, data);
            }
            else if (fmt == 0x0204)
            {
                old = Encoding.UTF8.GetString(sfo, data, Math.Max(0, len - 1));
                var nb = Encoding.UTF8.GetBytes(v);
                if (nb.Length + 1 > max) { Console.Error.WriteLine($"[ERROR] {k}: '{v}' does not fit ({max - 1} bytes at most)"); return 1; }
                Array.Clear(sfo, data, max);
                nb.CopyTo(sfo, data);
                BitConverter.GetBytes(nb.Length + 1).CopyTo(sfo, at + 4);
            }
            else { Console.Error.WriteLine($"[ERROR] {k} has a format this tool does not write"); return 1; }
            Console.WriteLine($"[sfo] {k}: {SafeLineWriter.Escape(old)} -> {SafeLineWriter.Escape(v)}");
        }
        using (var e = new CntEdit(pkgPath, undoPath, UndoMagic, new[] { Orbis.PKG.EntryId.PARAM_SFO, Orbis.PKG.EntryId.GENERAL_DIGESTS }))
        {
            try
            {
                e.Put(meta.DataOffset, sfo);
                if (paramOk) e.Put(gd.meta.DataOffset + 32 * 6, Orbis.Util.Crypto.Sha256(sfo));
                e.Reseal();
            }
            catch
            {
                e.RollBack();
                e.Dispose();
                if (undoPath != null) File.Delete(undoPath);          // nothing to undo
                Console.Error.WriteLine("[ERROR] the package was restored to its state before the run");
                throw;
            }
        }
        Console.WriteLine("[ok] param.sfo changed");
        return 0;
    }
}
