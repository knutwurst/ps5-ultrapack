using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Orbis = LibOrbisPkg;

namespace PkgTool;

// ps4-rebind: re-point an update at a base whose digests changed (slots 10 and 12)
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

        // base digests, checked against its header first
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

        byte[] oldContent = new byte[32], oldGame = new byte[32];
        using (var f = File.OpenRead(updPath))
        {
            f.Position = gdOff + 32 * BaseContentSlot; f.ReadExactly(oldContent);
            f.Position = gdOff + 32 * BaseGameSlot; f.ReadExactly(oldGame);
        }
        Console.WriteLine($"[rebind] base content {Convert.ToHexString(oldContent)[..16]} -> {Convert.ToHexString(bContent)[..16]}");
        Console.WriteLine($"[rebind] base game    {Convert.ToHexString(oldGame)[..16]} -> {Convert.ToHexString(bGame)[..16]}");
        if (oldContent.SequenceEqual(bContent) && oldGame.SequenceEqual(bGame)) { Console.WriteLine("[ok] the update already points at this base"); return 0; }

        using (var e = new CntEdit(updPath, undoPath, UndoMagic, new[] { Orbis.PKG.EntryId.GENERAL_DIGESTS }))
        {
            try
            {
                e.Put(gdOff + 32 * BaseContentSlot, bContent);
                e.Put(gdOff + 32 * BaseGameSlot, bGame);
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
        Console.WriteLine("[ok] update bound to the base");
        return 0;
    }
}
