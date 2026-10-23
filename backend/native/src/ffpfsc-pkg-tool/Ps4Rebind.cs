using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Orbis = LibOrbisPkg;

namespace PkgTool;

/// <summary>
/// ps4-rebind: points a PS4 update package at its base package again. An update stores the base's
/// content digest and game digest in its general digests (slots 10 and 12); a base whose image
/// changed (a new keystone) has new ones, and the console then refuses the update as incompatible.
/// The two slots are rewritten in place and the entry digest, the four header hashes, the header
/// digest and the fake header signature are recomputed. --undo FILE keeps the overwritten bytes
/// in the format 'ps4-keystone --restore' reads.
/// </summary>
internal static partial class Program
{
    const int BaseContentSlot = 10, BaseGameSlot = 12;

    static int CmdPs4Rebind(string[] args)
    {
        if (args.Length < 3) return Bad("ps4-rebind needs <update.pkg> <base.pkg> [--undo FILE]");
        string updPath = args[1], basePath = args[2], undoPath = null;
        for (int i = 3; i < args.Length; i++)
        {
            if (args[i] == "--undo") undoPath = Need(args, ref i, "--undo");
            else return Bad("unknown ps4-rebind flag: " + args[i]);
        }
        foreach (var p in new[] { updPath, basePath })
            if (!File.Exists(p)) throw new FileNotFoundException("package not found", p);

        // the base: its own content and game digest, checked against its header first
        Orbis.PKG.Pkg bpkg;
        byte[] bhead;
        using (var fs = File.OpenRead(basePath)) { bpkg = new Orbis.PKG.PkgReader(fs).ReadPkg(); bhead = new byte[0x1000]; fs.Position = 0; fs.ReadExactly(bhead); }
        var bgd = bpkg.GeneralDigests;
        var bContent = bgd.Digests[Orbis.PKG.GeneralDigest.ContentDigest];
        var bGame = bgd.Digests[Orbis.PKG.GeneralDigest.GameDigest];
        var bContentCalc = Orbis.Util.Crypto.Sha256(bhead.AsSpan(0x40, 36).ToArray().Concat(new byte[12]).Concat(bhead.AsSpan(0x70, 8).ToArray())
            .Concat(bpkg.Header.pfs_image_digest).Concat(bgd.Digests[Orbis.PKG.GeneralDigest.MajorParamDigest]).ToArray());
        if (!bContent.SequenceEqual(bContentCalc) || !bGame.SequenceEqual(bpkg.Header.pfs_image_digest))
        {
            Console.Error.WriteLine("[ERROR] the base package's own digests do not agree with its header; it is not a base to bind to");
            return 1;
        }

        Orbis.PKG.Pkg pkg;
        using (var fs = File.OpenRead(updPath)) pkg = new Orbis.PKG.PkgReader(fs).ReadPkg();
        if (pkg.Header.content_id != bpkg.Header.content_id)
        {
            Console.Error.WriteLine($"[ERROR] the update is for {pkg.Header.content_id}, the base is {bpkg.Header.content_id}");
            return 1;
        }
        var gd = pkg.GeneralDigests;
        uint set = (uint)gd.set_digests;
        if ((set & (1u << BaseContentSlot)) == 0 || (set & (1u << BaseGameSlot)) == 0 || gd.meta.DataSize < 32 * (BaseGameSlot + 1))
        {
            Console.Error.WriteLine("[ERROR] the package carries no base digests (not an update)");
            return 1;
        }
        long gdOff = gd.meta.DataOffset;

        using var f = new FileStream(updPath, FileMode.Open, FileAccess.ReadWrite, FileShare.Read);
        byte[] Read(long off, int len) { var b = new byte[len]; f.Position = off; f.ReadExactly(b); return b; }
        var oldContent = Read(gdOff + 32 * BaseContentSlot, 32);
        var oldGame = Read(gdOff + 32 * BaseGameSlot, 32);
        Console.WriteLine($"[rebind] base content {Convert.ToHexString(oldContent)[..16]} -> {Convert.ToHexString(bContent)[..16]}");
        Console.WriteLine($"[rebind] base game    {Convert.ToHexString(oldGame)[..16]} -> {Convert.ToHexString(bGame)[..16]}");
        if (oldContent.SequenceEqual(bContent) && oldGame.SequenceEqual(bGame)) { Console.WriteLine("[ok] the update already points at this base"); return 0; }

        // what was right before stays right after
        var metas = pkg.Metas.Metas;
        var digMeta = metas.First(m => m.id == Orbis.PKG.EntryId.DIGESTS);
        int gdIndex = metas.FindIndex(m => m.id == Orbis.PKG.EntryId.GENERAL_DIGESTS);
        bool gdEntryOk = Read(digMeta.DataOffset + 32 * gdIndex, 32).SequenceEqual(Orbis.Util.Crypto.Sha256(f, gd.meta.DataOffset, gd.meta.DataSize));
        bool bodyOk = pkg.Header.body_digest.SequenceEqual(Orbis.Util.Crypto.Sha256(f, (long)pkg.Header.body_offset, (long)pkg.Header.body_size));

        var saved = new List<(long off, byte[] data)>();
        using var undo = undoPath == null ? null : new BinaryWriter(File.Create(undoPath));
        if (undo != null)
        {
            undo.Write(UndoMagic);
            undo.Write(System.Text.Encoding.ASCII.GetBytes(pkg.Header.content_id.PadRight(36, '\0')[..36]));
            undo.Write(f.Length);
            undo.Flush();
        }
        void Put(long off, byte[] b)
        {
            var old = Read(off, b.Length);
            saved.Add((off, old));
            if (undo != null) { undo.Write(off); undo.Write(old.Length); undo.Write(old); undo.Flush(); }
            f.Position = off; f.Write(b);
        }
        try
        {
            Put(gdOff + 32 * BaseContentSlot, bContent);
            Put(gdOff + 32 * BaseGameSlot, bGame);
            f.Flush();
            if (gdEntryOk) Put(digMeta.DataOffset + 32 * gdIndex, Orbis.Util.Crypto.Sha256(f, gd.meta.DataOffset, gd.meta.DataSize));
            f.Flush();
            f.Position = 0;
            pkg = new Orbis.PKG.PkgReader(f).ReadPkg();
            byte[] Cat(bool sc2)
            {
                var ms = new MemoryStream();
                var parts = new List<Orbis.PKG.Entry> { pkg.EntryKeys, pkg.ImageKey, pkg.GeneralDigests, pkg.Metas };
                if (!sc2) parts.Add(pkg.Digests);
                foreach (var e in parts)
                {
                    long size = sc2 && e.Id == Orbis.PKG.EntryId.METAS ? pkg.Header.sc_entry_count * 0x20 : e.meta.DataSize;
                    new Orbis.Util.SubStream(f, e.meta.DataOffset, size).CopyTo(ms);
                }
                return Orbis.Util.Crypto.Sha256(ms);
            }
            Put(0x100, Cat(false));
            Put(0x120, Cat(true));
            Put(0x140, Orbis.Util.Crypto.Sha256(pkg.Digests.FileData));
            if (bodyOk) Put(0x160, Orbis.Util.Crypto.Sha256(f, (long)pkg.Header.body_offset, (long)pkg.Header.body_size));
            f.Flush();
            Put(0xFE0, Orbis.Util.Crypto.Sha256(f, 0, 0xFE0));
            f.Flush();
            Put(0x1000, Orbis.Util.Crypto.RSA2048EncryptKey(Orbis.Util.Keys.PkgSignKey, Orbis.Util.Crypto.Sha256(f, 0, 0x1000)));
            f.Flush();
        }
        catch
        {
            for (int i = saved.Count - 1; i >= 0; i--) { f.Position = saved[i].off; f.Write(saved[i].data); }
            f.Flush();
            Console.Error.WriteLine("[ERROR] the package was restored to its state before the run");
            throw;
        }
        Console.WriteLine("[ok] update bound to the base");
        return 0;
    }
}
