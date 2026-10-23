using System;
using System.Collections.Generic;
using System.IO;
using System.IO.MemoryMappedFiles;
using System.Linq;
using System.Security.Cryptography;
using Orbis = LibOrbisPkg;

namespace PkgTool;

/// <summary>
/// ps4-keystone: writes another sce_sys/keystone into a PS4 fake package, in place.
///
/// A save holds the keystone of the game that wrote it; a package built with another keystone
/// (a builder's default from the all-zero passcode) shows retail saves as corrupted. The keystone
/// lives in the inner PFSC image. Only its sector changes: the stored bytes keep their length
/// (a compressed sector is re-encoded to the same size), the outer blocks around it are re-signed
/// and re-encrypted, and playgo-chunk.sha, the PFS digests, the general and entry digests, the
/// header hashes and the fake header signature are recomputed. Every byte range is saved before
/// it is written: to --undo FILE when given, and in memory to roll back a failed run.
/// </summary>
internal static partial class Program
{
    const int KsSize = 0x60;
    const int PfsSigEntry = 36;
    const int SignedDinodeSize = 0x2C8;
    // undo file: this magic, the package's content id (36 bytes), its size, then the ranges
    static readonly byte[] UndoMagic = "PS4KSUNDO1"u8.ToArray();

    static int CmdPs4Keystone(string[] args)
    {
        if (args.Length < 2) return Bad("ps4-keystone needs <ps4.pkg> [<keystone>] [--undo FILE] | --restore FILE");
        var pkgPath = args[1];
        string? ksPath = null, undoPath = null, restore = null;
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i] == "--undo") undoPath = Need(args, ref i, "--undo");
            else if (args[i] == "--restore") restore = Need(args, ref i, "--restore");
            else if (!args[i].StartsWith("--") && ksPath == null) ksPath = args[i];
            else return Bad("unknown ps4-keystone flag: " + args[i]);
        }
        if (!File.Exists(pkgPath)) throw new FileNotFoundException("package not found", pkgPath);
        if (restore != null) return KsRestore(pkgPath, restore);

        byte[]? newKs = null;
        if (ksPath != null)
        {
            if (!File.Exists(ksPath)) throw new FileNotFoundException("keystone not found", ksPath);
            newKs = File.ReadAllBytes(ksPath);
            var why = KeystoneProblem(newKs);
            if (why != null) { Console.Error.WriteLine("[ERROR] " + Path.GetFileName(ksPath) + ": " + why); return 1; }
        }
        var site = KsLocate(pkgPath);
        Console.WriteLine($"[keystone] current fingerprint={Convert.ToHexString(site.OldKs, 0x20, 0x20)} zero_passcode={(IsZeroPasscodeKeystone(site.OldKs) ? "true" : "false")} header_hash={(site.HeaderHashOk ? "ok" : "none")} valid={(KeystoneProblem(site.OldKs) == null ? "true" : "false")}");
        if (newKs == null) return 0;
        if (newKs.SequenceEqual(site.OldKs)) { Console.WriteLine("[ok] the package already has this keystone"); return 0; }
        KsApply(pkgPath, site, newKs, undoPath);
        var after = KsLocate(pkgPath);
        if (!after.OldKs.SequenceEqual(newKs)) throw new InvalidDataException("the keystone read back differs from the one written");
        if (site.HeaderHashOk && !after.HeaderHashOk) throw new InvalidDataException("the content image's header hash does not match after the change");
        Console.WriteLine($"[keystone] new fingerprint={Convert.ToHexString(newKs, 0x20, 0x20)}");
        Console.WriteLine("[ok] keystone replaced");
        return 0;
    }

    /// <summary>Null when <paramref name="ks"/> is a well-formed keystone (v2 header, valid MAC).</summary>
    internal static string? KeystoneProblem(byte[] ks)
    {
        if (ks.Length != KsSize) return $"a keystone is {KsSize} bytes, this file has {ks.Length}";
        var header = Orbis.Util.Crypto.CreateKeystone(new string('0', 32)).Take(0x20);
        if (!ks.Take(0x20).SequenceEqual(header)) return "no keystone header";
        var mac = Orbis.Util.Crypto.HmacSha256(Orbis.Util.Keys.keystone_mac_data, ks.Take(0x40).ToArray());
        if (!mac.SequenceEqual(ks.Skip(0x40))) return "the keystone's checksum does not match (damaged file?)";
        return null;
    }

    static bool IsZeroPasscodeKeystone(byte[] ks) => ks.SequenceEqual(Orbis.Util.Crypto.CreateKeystone(new string('0', 32)));

    /// <summary>Where the keystone sits: the PFSC sector holding it and the outer blocks that
    /// carry that sector, with the signature slot of every block on the way to the header.</summary>
    sealed class KsSite
    {
        public byte[] OldKs = [];
        public long PfsOff, PfsSize;
        public uint BlockSize;
        public bool Encrypted;
        public byte[] DataKey = [], TweakKey = [], SignKey = [];
        public long SectorOff, SectorEnd;          // stored sector, logical offsets in pfs_image.dat
        public int PfscBlock, InSector;
        public bool Raw;
        public byte[] Stored = [], Plain = [];
        public Func<long, long> Phys = k => k;     // logical block of pfs_image.dat -> outer block
        public Dictionary<long, (long block, int off)> Parent = [];
        public long DinodeBlock;
        public int HeaderSlot;
        public bool HeaderHashOk;                  // the superblock hash at 0x380 matches (Sony-built images carry one)
    }

    static byte[] KsReadPlain(FileStream f, KsSite s, long block)
    {
        var b = new byte[s.BlockSize];
        f.Position = s.PfsOff + block * s.BlockSize;
        f.ReadExactly(b);
        if (s.Encrypted && block > 0) KsCrypt(s, b, block, false);
        return b;
    }

    static void KsCrypt(KsSite s, byte[] b, long block, bool encrypt)
    {
        const int sector = 0x1000;
        var xts = new Orbis.Util.XtsBlockTransform(s.DataKey, s.TweakKey);
        var buf = new byte[sector];
        for (int i = 0; i < s.BlockSize / sector; i++)
        {
            Buffer.BlockCopy(b, i * sector, buf, 0, sector);
            var n = (ulong)(block * (s.BlockSize / sector) + i);
            if (encrypt) xts.EncryptSector(buf, n); else xts.DecryptSector(buf, n);
            Buffer.BlockCopy(buf, 0, b, i * sector, sector);
        }
    }

    static KsSite KsLocate(string pkgPath)
    {
        var s = new KsSite();
        Orbis.PKG.Pkg pkg;
        using (var fs = File.OpenRead(pkgPath)) pkg = new Orbis.PKG.PkgReader(fs).ReadPkg();
        if (pkg.Header.content_id.Length < 16 || pkg.Header.content_id.Substring(7, 4) != "CUSA")
            throw new InvalidDataException("not a PS4 package (no CUSA title id in the content id)");
        if (pkg.Header.pfs_image_size == 0) throw new InvalidDataException("the package has no content image");
        s.PfsOff = (long)pkg.Header.pfs_image_offset;
        s.PfsSize = (long)pkg.Header.pfs_image_size;
        bool newCrypt = (pkg.Header.pfs_flags & 0x2000000000000000UL) != 0;

        byte[]? ekpfs = null;
        uint imageIno = 0; int[]? imageBlocks = null; long imageStart = 0; long innerOff = 0, ksSize = -1; bool ksPacked = false;
        using (var mmf = MemoryMappedFile.CreateFromFile(pkgPath, FileMode.Open, null, 0, MemoryMappedFileAccess.Read))
        using (var view = mmf.CreateViewAccessor(s.PfsOff, s.PfsSize, MemoryMappedFileAccess.Read))
        {
            // the same two kinds of fake package as Ps4Image
            foreach (var key in new Func<byte[]>[] { () => pkg.GetEkpfs(),
                         () => Orbis.Util.Crypto.ComputeKeys(pkg.Header.content_id, new string('0', 32), 1) })
            {
                try
                {
                    var k = key();
                    var outer = new Orbis.PFS.PfsReader(view, pkg.Header.pfs_flags, k);
                    var image = outer.GetFile("pfs_image.dat");
                    if (image == null) continue;
                    var inner = new Orbis.PFS.PfsReader(new Orbis.PFS.PFSCReader(image.GetView()));
                    ekpfs = k;
                    var ks = inner.GetFile("sce_sys/keystone");
                    if (ks != null) { imageIno = image.ino; imageBlocks = image.blocks; imageStart = image.offset; innerOff = ks.offset; ksSize = ks.size; ksPacked = ks.compressed_size != ks.size; }
                    break;
                }
                catch { }
            }
        }
        if (ekpfs == null) throw new InvalidDataException("not a fake package; its contents cannot be read");
        if (ksSize < 0) throw new InvalidDataException("the package has no keystone (updates and add-ons carry none)");
        if (ksSize != KsSize || ksPacked) throw new InvalidDataException("unexpected keystone entry in the package");

        using var f = File.OpenRead(pkgPath);
        var h0 = new byte[0x400];
        f.Position = s.PfsOff; f.ReadExactly(h0);
        var hdr = Orbis.PFS.PfsHeader.ReadFromStream(new MemoryStream(h0));
        if (!hdr.Mode.HasFlag(Orbis.PFS.PfsMode.Signed)) throw new InvalidDataException("the content image is not signed");
        s.BlockSize = hdr.BlockSize;
        s.Encrypted = hdr.Mode.HasFlag(Orbis.PFS.PfsMode.Encrypted);
        var (tweak, data) = Orbis.Util.Crypto.PfsGenEncKey(ekpfs, hdr.Seed, newCrypt);
        s.TweakKey = tweak; s.DataKey = data;
        var bs = s.BlockSize;
        var blocks = imageBlocks; var start = imageStart;
        s.Phys = k => blocks == null ? start / bs + k : blocks[k];

        byte[] Logical(long off, int count)
        {
            var r = new byte[count]; int done = 0;
            while (done < count)
            {
                long k = (off + done) / bs; int at = (int)((off + done) % bs);
                var b = KsReadPlain(f, s, s.Phys(k));
                int n = Math.Min(count - done, (int)bs - at);
                Buffer.BlockCopy(b, at, r, done, n); done += n;
            }
            return r;
        }
        var ph = Logical(0, 0x30);
        if (BitConverter.ToInt32(ph, 0) != 0x43534650) throw new InvalidDataException("the inner image is not PFSC");
        s.PfscBlock = BitConverter.ToInt32(ph, 12);
        long sector = innerOff / s.PfscBlock;
        if ((innerOff + KsSize - 1) / s.PfscBlock != sector) throw new InvalidDataException("the keystone spans two sectors");
        var map = Logical(BitConverter.ToInt64(ph, 0x18) + 8 * sector, 16);
        s.SectorOff = BitConverter.ToInt64(map, 0); s.SectorEnd = BitConverter.ToInt64(map, 8);
        s.Stored = Logical(s.SectorOff, (int)(s.SectorEnd - s.SectorOff));
        s.Raw = s.Stored.Length == s.PfscBlock;
        s.Plain = s.Raw ? s.Stored : Inflate(s.Stored, s.PfscBlock);
        s.InSector = (int)(innerOff - sector * s.PfscBlock);
        s.OldKs = s.Plain.Skip(s.InSector).Take(KsSize).ToArray();

        // signature slots: data block -> dinode / indirect block -> ... -> dinode block -> header
        int perBlock = (int)(bs / SignedDinodeSize), spb = (int)(bs / PfsSigEntry);
        s.DinodeBlock = 1 + imageIno / perBlock;
        int dinOff = (int)(imageIno % perBlock) * SignedDinodeSize;
        var dinPlain = KsReadPlain(f, s, s.DinodeBlock);
        var ino = Orbis.PFS.DinodeS32.ReadFromStream(new MemoryStream(dinPlain, dinOff, SignedDinodeSize));
        (long, int) DinSlot(int idx) => (s.DinodeBlock, dinOff + 0x64 + PfsSigEntry * idx);
        for (long k = s.SectorOff / bs; k <= (s.SectorEnd - 1) / bs; k++)
        {
            long phys = s.Phys(k);
            if (k < 12) s.Parent[phys] = DinSlot((int)k);
            else if (k < 12 + spb)
            {
                long ib0 = ino.ib[0].block;
                s.Parent[phys] = (ib0, (int)(k - 12) * PfsSigEntry);
                s.Parent[ib0] = DinSlot(12);
            }
            else
            {
                long r = k - 12 - spb, j = r / spb, i = r % spb, ib1 = ino.ib[1].block;
                long mid = BitConverter.ToInt32(KsReadPlain(f, s, ib1), (int)j * PfsSigEntry + 32);
                s.Parent[phys] = (mid, (int)i * PfsSigEntry);
                s.Parent[mid] = (ib1, (int)j * PfsSigEntry);
                s.Parent[ib1] = DinSlot(13);
            }
        }
        // the signing key is the one whose signature of the dinode block is in the header
        var hdrBlock = new byte[bs];
        f.Position = s.PfsOff; f.ReadExactly(hdrBlock);
        foreach (var cand in new[] { Orbis.Util.Crypto.PfsGenSignKey(ekpfs, hdr.Seed),
                     Orbis.Util.Crypto.PfsGenSignKey(new HMACSHA256(ekpfs).ComputeHash(hdr.Seed), hdr.Seed) })
        {
            int at = IndexOfBytes(hdrBlock, Orbis.Util.Crypto.HmacSha256(cand, dinPlain));
            if (at >= 0 && BitConverter.ToInt32(hdrBlock, at + 32) == s.DinodeBlock) { s.SignKey = cand; s.HeaderSlot = at; break; }
        }
        if (s.SignKey.Length == 0) throw new InvalidDataException("the content image's signatures do not verify");
        var hc = new byte[0x5A0];
        Buffer.BlockCopy(hdrBlock, 0, hc, 0, 0x380);
        s.HeaderHashOk = Orbis.Util.Crypto.HmacSha256(s.SignKey, hc).AsSpan().SequenceEqual(hdrBlock.AsSpan(0x380, 32));
        var cache = new Dictionary<long, byte[]> { [s.DinodeBlock] = dinPlain };
        foreach (var (child, (pb, po)) in s.Parent)
        {
            var parent = cache.TryGetValue(pb, out var p) ? p : cache[pb] = KsReadPlain(f, s, pb);
            var c = cache.TryGetValue(child, out var cp) ? cp : cache[child] = KsReadPlain(f, s, child);
            if (BitConverter.ToInt32(parent, po + 32) != child
                || !Orbis.Util.Crypto.HmacSha256(s.SignKey, c).SequenceEqual(parent.Skip(po).Take(32)))
                throw new InvalidDataException($"the signature of content block {child} does not verify");
        }
        return s;
    }

    static byte[] Inflate(byte[] zlibStream, int size)
    {
        var o = new byte[size];
        using var ds = new System.IO.Compression.DeflateStream(new MemoryStream(zlibStream, 2, zlibStream.Length - 2),
                                                                System.IO.Compression.CompressionMode.Decompress);
        ds.ReadExactly(o);
        return o;
    }

    static int IndexOfBytes(byte[] hay, byte[] needle)
    {
        for (int i = 0; i + needle.Length <= hay.Length; i++)
            if (hay.AsSpan(i, needle.Length).SequenceEqual(needle)) return i;
        return -1;
    }

    static void KsApply(string pkgPath, KsSite s, byte[] newKs, string? undoPath)
    {
        var plain = (byte[])s.Plain.Clone();
        Buffer.BlockCopy(newKs, 0, plain, s.InSector, KsSize);
        byte[] stored;
        if (s.Raw) stored = plain;
        else
        {
            var deflate = DeflateFit.Encode(plain, s.Stored.Length - 6)
                ?? throw new InvalidDataException("the new keystone does not fit into the stored sector");
            var adler = Adler32(plain);
            stored = [s.Stored[0], s.Stored[1], .. deflate, (byte)(adler >> 24), (byte)(adler >> 16), (byte)(adler >> 8), (byte)adler];
            if (!Inflate(stored, s.PfscBlock).SequenceEqual(plain)) throw new InvalidDataException("re-encoded sector does not round-trip");
        }

        Orbis.PKG.Pkg pkg;
        using (var fs = File.OpenRead(pkgPath)) pkg = new Orbis.PKG.PkgReader(fs).ReadPkg();
        var saved = new List<(long off, byte[] data)>();
        using var f = new FileStream(pkgPath, FileMode.Open, FileAccess.ReadWrite, FileShare.Read);
        var undo = undoPath == null ? null : new BinaryWriter(File.Create(undoPath));   // once the package opens
        if (undo != null)
        {
            undo.Write(UndoMagic);
            undo.Write(System.Text.Encoding.ASCII.GetBytes(pkg.Header.content_id.PadRight(36, '\0')[..36]));
            undo.Write(f.Length);
            undo.Flush();
        }

        // what was right before stays right after; what was wrong stays as it was
        byte[] Read(long off, int len) { var b = new byte[len]; f.Position = off; f.ReadExactly(b); return b; }
        byte[] HeaderDigest() => Orbis.Util.Crypto.Sha256(Read(0, 64).Concat(Read(0x400, 128)).ToArray());
        // content id padded to 48 bytes, drm type, content type, PFS image digest and the stored major
        // param digest (LibOrbisPkg builds the major param string differently, so its own result never matches)
        byte[] ContentDigest(Orbis.PKG.Pkg p) => Orbis.Util.Crypto.Sha256(
            Read(0x40, 36).Concat(new byte[12]).Concat(Read(0x70, 8)).Concat(p.Header.pfs_image_digest)
                .Concat(p.GeneralDigests.Digests[Orbis.PKG.GeneralDigest.MajorParamDigest]).ToArray());
        var gd = pkg.GeneralDigests;
        bool gameOk = gd.set_digests.HasFlag(Orbis.PKG.GeneralDigest.GameDigest)
                      && gd.Digests[Orbis.PKG.GeneralDigest.GameDigest].SequenceEqual(pkg.Header.pfs_image_digest);
        bool headerOk = gd.set_digests.HasFlag(Orbis.PKG.GeneralDigest.HeaderDigest)
                        && gd.Digests[Orbis.PKG.GeneralDigest.HeaderDigest].SequenceEqual(HeaderDigest());
        bool contentOk = gd.set_digests.HasFlag(Orbis.PKG.GeneralDigest.ContentDigest)
                         && gd.Digests[Orbis.PKG.GeneralDigest.ContentDigest].SequenceEqual(ContentDigest(pkg));
        var metas = pkg.Metas.Metas;
        // a second chunk hash table whose layout is unknown here: leave such a package alone
        if (metas.Any(m => (uint)m.id == 0x1009))
            throw new InvalidDataException("the package has app/playgo-chunk.sha, which this tool does not update yet");
        // SHA-256 over the SHA-256 of every PlayGo entry (playgo-chunk.dat/.sha, playgo-manifest.xml, app/…)
        byte[] PlaygoDigest() => Orbis.Util.Crypto.Sha256(metas
            .Where(m => (uint)m.id is (>= 0x1001 and <= 0x1003) or (>= 0x1008 and <= 0x100A))
            .OrderBy(m => (uint)m.id).SelectMany(m => Orbis.Util.Crypto.Sha256(f, m.DataOffset, m.DataSize)).ToArray());
        bool playgoOk = gd.set_digests.HasFlag(Orbis.PKG.GeneralDigest.PlaygoDigest)
                        && gd.Digests[Orbis.PKG.GeneralDigest.PlaygoDigest].SequenceEqual(PlaygoDigest());
        var digMeta = metas.First(m => m.id == Orbis.PKG.EntryId.DIGESTS);
        bool EntryOk(int i) => Read(digMeta.DataOffset + 32 * i, 32).SequenceEqual(Orbis.Util.Crypto.Sha256(f, metas[i].DataOffset, metas[i].DataSize));
        var entryIds = new[] { Orbis.PKG.EntryId.GENERAL_DIGESTS, Orbis.PKG.EntryId.PLAYGO_CHUNK_SHA };
        var entryOk = entryIds.Select(id => metas.FindIndex(m => m.id == id)).Where(i => i >= 1).ToDictionary(i => i, EntryOk);
        bool bodyOk = pkg.Header.body_digest.SequenceEqual(Orbis.Util.Crypto.Sha256(f, (long)pkg.Header.body_offset, (long)pkg.Header.body_size));
        var chunkMeta = metas.FirstOrDefault(m => m.id == Orbis.PKG.EntryId.PLAYGO_CHUNK_SHA);

        void Put(long off, byte[] b)
        {
            var old = Read(off, b.Length);
            saved.Add((off, old));
            if (undo != null) { undo.Write(off); undo.Write(old.Length); undo.Write(old); undo.Flush(); }
            f.Position = off; f.Write(b);
        }
        try
        {
            var bs = s.BlockSize;
            var cache = new Dictionary<long, byte[]>();
            byte[] Plain(long b) => cache.TryGetValue(b, out var p) ? p : cache[b] = KsReadPlain(f, s, b);
            for (long k = s.SectorOff / bs; k <= (s.SectorEnd - 1) / bs; k++)
            {
                var blk = Plain(s.Phys(k));
                long lo = Math.Max(s.SectorOff, k * bs), hi = Math.Min(s.SectorEnd, (k + 1) * bs);
                Buffer.BlockCopy(stored, (int)(lo - s.SectorOff), blk, (int)(lo - k * bs), (int)(hi - lo));
            }
            // re-sign bottom up; a few passes settle every level of the chain
            for (int pass = 0; pass < 4; pass++)
                foreach (var (child, (pb, po)) in s.Parent)
                    Buffer.BlockCopy(Orbis.Util.Crypto.HmacSha256(s.SignKey, Plain(child)), 0, Plain(pb), po, 32);
            var hdrBlock = Read(s.PfsOff, (int)bs);
            Buffer.BlockCopy(Orbis.Util.Crypto.HmacSha256(s.SignKey, Plain(s.DinodeBlock)), 0, hdrBlock, s.HeaderSlot, 32);
            // the superblock's own hash at 0x380: HMAC over its first 0x380 bytes, zero-padded to 0x5A0
            byte[] HeaderHash(byte[] h) { var c = new byte[0x5A0]; Buffer.BlockCopy(h, 0, c, 0, 0x380); return Orbis.Util.Crypto.HmacSha256(s.SignKey, c); }
            if (HeaderHash(Read(s.PfsOff, 0x380)).SequenceEqual(Read(s.PfsOff + 0x380, 32)))
                Buffer.BlockCopy(HeaderHash(hdrBlock), 0, hdrBlock, 0x380, 32);
            var touched = new SortedSet<long>(cache.Keys) { 0 };
            foreach (var b in cache.Keys)
            {
                var enc = (byte[])cache[b].Clone();
                if (s.Encrypted) KsCrypt(s, enc, b, true);
                Put(s.PfsOff + b * bs, enc);
            }
            Put(s.PfsOff, hdrBlock);
            // playgo-chunk.sha: the first 4 bytes of SHA-256 of every 64 KiB chunk of the file
            if (chunkMeta != null)
                foreach (var b in touched)
                    for (long c = (s.PfsOff + b * bs) / 0x10000; c < (s.PfsOff + (b + 1) * bs + 0xFFFF) / 0x10000; c++)
                        if (c * 4 + 4 <= chunkMeta.DataSize)
                            Put(chunkMeta.DataOffset + c * 4, Orbis.Util.Crypto.Sha256(Read(c * 0x10000, 0x10000)).Take(4).ToArray());
            f.Flush();

            Put(0x460, Orbis.Util.Crypto.Sha256(f, s.PfsOff, 0x10000));
            Put(0x440, HashWithProgress(f, s.PfsOff, s.PfsSize));
            f.Flush();
            f.Position = 0;
            pkg = new Orbis.PKG.PkgReader(f).ReadPkg();
            long gdOff = pkg.GeneralDigests.meta.DataOffset;
            if (gameOk) Put(gdOff + 32 * 2, pkg.Header.pfs_image_digest);
            if (headerOk) Put(gdOff + 32 * 3, HeaderDigest());
            if (contentOk) { f.Flush(); f.Position = 0; pkg = new Orbis.PKG.PkgReader(f).ReadPkg(); Put(gdOff + 32, ContentDigest(pkg)); }
            if (playgoOk) Put(gdOff + 32 * 7, PlaygoDigest());
            f.Flush();
            foreach (var (i, ok) in entryOk)
                if (ok) Put(digMeta.DataOffset + 32 * i, Orbis.Util.Crypto.Sha256(f, metas[i].DataOffset, metas[i].DataSize));
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
            // later writes may cover earlier ones: restore newest first
            for (int i = saved.Count - 1; i >= 0; i--) { f.Position = saved[i].off; f.Write(saved[i].data); }
            f.Flush();
            Console.Error.WriteLine("[ERROR] the package was restored to its state before the run");
            if (undo != null) { undo.Dispose(); undo = null; File.Delete(undoPath!); }   // nothing to undo
            throw;
        }
        finally
        {
            undo?.Dispose();
        }
    }

    static byte[] HashWithProgress(FileStream f, long off, long len)
    {
        using var sha = SHA256.Create();
        var buf = new byte[1 << 22];
        long done = 0; int last = -1;
        f.Position = off;
        while (done < len)
        {
            int n = (int)Math.Min(buf.Length, len - done);
            f.ReadExactly(buf, 0, n);
            sha.TransformBlock(buf, 0, n, null, 0);
            done += n;
            int pct = (int)(done * 100 / len);
            if (pct != last && pct < 100) { last = pct; Console.WriteLine($"[####] {pct}% keystone"); }
        }
        sha.TransformFinalBlock([], 0, 0);
        return sha.Hash!;
    }

    static uint Adler32(byte[] d)
    {
        uint a = 1, b = 0;
        foreach (var x in d) { a = (a + x) % 65521; b = (b + a) % 65521; }
        return (b << 16) | a;
    }

    static int KsRestore(string pkgPath, string undoPath)
    {
        var entries = new List<(long, byte[])>();
        string cid; long size;
        using (var r = new BinaryReader(File.OpenRead(undoPath)))
        {
            if (!r.ReadBytes(UndoMagic.Length).SequenceEqual(UndoMagic))
                return Bad("not an undo file of ps4-keystone: " + undoPath);
            cid = System.Text.Encoding.ASCII.GetString(r.ReadBytes(36)).TrimEnd('\0');
            size = r.ReadInt64();
            while (r.BaseStream.Length - r.BaseStream.Position >= 12)
            {
                var off = r.ReadInt64(); var len = r.ReadInt32();
                var old = r.ReadBytes(len);
                if (old.Length < len) break;   // a run stopped while saving this range: not written yet
                entries.Add((off, old));
            }
        }
        // only the package the undo file was made from
        Orbis.PKG.Pkg pkg;
        using (var fs = File.OpenRead(pkgPath)) pkg = new Orbis.PKG.PkgReader(fs).ReadPkg();
        if (pkg.Header.content_id != cid || new FileInfo(pkgPath).Length != size)
        {
            Console.Error.WriteLine($"[ERROR] the undo file belongs to {cid} ({size} bytes), not to this package");
            return 1;
        }
        using var f = new FileStream(pkgPath, FileMode.Open, FileAccess.Write);
        for (int i = entries.Count - 1; i >= 0; i--) { f.Position = entries[i].Item1; f.Write(entries[i].Item2); }
        Console.WriteLine($"[ok] restored {entries.Count} range(s)");
        return 0;
    }
}

/// <summary>
/// A raw deflate stream of an exact length. zlib cannot be told a target size, and a sector in a
/// PFSC image cannot grow or shrink in place. The data is coded as two blocks (the head with the
/// literals, then the long run), each fixed or dynamic, runs as distance-1 matches; zeros coded
/// as literals add bits until the length is reached.
/// </summary>
internal static class DeflateFit
{
    static readonly int[] LenBase = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258];
    static readonly int[] LenExtra = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0];
    static readonly int[] ClOrder = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15];

    public static byte[]? Encode(byte[] data, int target) => Encode(data, target, TimeSpan.FromSeconds(30));

    /// <summary>Null when no encoding of exactly <paramref name="target"/> bytes turns up within
    /// <paramref name="limit"/>: the search space is large, and a sector that cannot take the
    /// keystone must say so instead of running for hours.</summary>
    public static byte[]? Encode(byte[] data, int target, TimeSpan limit)
    {
        var clock = System.Diagnostics.Stopwatch.StartNew();
        var splits = new List<int> { 0 };
        for (int i = 1; i < Math.Min(data.Length, 1024); i++) splits.Add(i);
        foreach (var split in splits)
            foreach (var dynA in new[] { false, true })
                foreach (var dynB in new[] { false, true })
                {
                    if (split == 0 && dynA) continue;
                    if (clock.Elapsed > limit) return null;
                    if (Build(data, split, dynA, dynB, 0, 0).Length > target) continue;
                    for (int stored = 0; stored < 3; stored++)
                        for (int lit = 0; lit < 256; lit++)
                        {
                            var d = Build(data, split, dynA, dynB, lit, stored);
                            if (d.Length == target) return d;
                            if (d.Length > target) break;
                        }
                }
        return null;
    }

    sealed class Bits
    {
        readonly List<byte> o = [];
        ulong acc; int n;
        public void Put(uint v, int bits)
        {
            acc |= (ulong)v << n; n += bits;
            while (n >= 8) { o.Add((byte)acc); acc >>= 8; n -= 8; }
        }
        public void Huff(int code, int len)
        {
            uint r = 0;
            for (int i = 0; i < len; i++) r |= (uint)((code >> (len - 1 - i)) & 1) << i;   // MSB first
            Put(r, len);
        }
        public void Align() { if (n > 0) Put(0, 8 - n); }
        public byte[] Finish() { if (n > 0) o.Add((byte)acc); return o.ToArray(); }
    }

    // (literal or match length, isMatch)
    static List<(int v, bool m)> Tokens(byte[] d, int start, int end, int padLiterals)
    {
        var t = new List<(int, bool)>();
        int i = start, left = padLiterals;
        while (i < end)
        {
            int j = i;
            while (j < end && d[j] == d[i]) j++;
            int run = j - i;
            bool prevSame = i > 0 && d[i - 1] == d[i];
            if (run + (prevSame ? 0 : -1) >= 3)
            {
                if (!prevSame) { t.Add((d[i], false)); i++; run--; }
                int take = Math.Min(left, run);
                for (int k = 0; k < take; k++) { t.Add((d[i], false)); i++; }
                left -= take; run -= take;
                while (run >= 3)
                {
                    int n = Math.Min(258, run);
                    if (run - n is 1 or 2) n = run - 3;
                    t.Add((n, true)); i += n; run -= n;
                }
                for (int k = 0; k < run; k++) { t.Add((d[i], false)); i++; }
            }
            else
                for (int k = 0; k < run; k++) { t.Add((d[i], false)); i++; }
        }
        return t;
    }

    static (int sym, int extra, int bits) LenCode(int n)
    {
        for (int k = LenBase.Length - 1; k >= 0; k--)
            if (n >= LenBase[k]) return (257 + k, n - LenBase[k], LenExtra[k]);
        throw new ArgumentOutOfRangeException(nameof(n));
    }

    /// <summary>Huffman code lengths limited to <paramref name="limit"/> bits (package-merge).</summary>
    static int[] Lengths(long[] freq, int limit)
    {
        var res = new int[freq.Length];
        var syms = Enumerable.Range(0, freq.Length).Where(s => freq[s] > 0).ToList();
        if (syms.Count == 0) return res;
        if (syms.Count == 1) { res[syms[0]] = 1; return res; }
        var leaves = syms.Select(s => (w: freq[s], m: new List<int> { s })).OrderBy(x => x.w).ToList();
        var cur = leaves.ToList();
        for (int l = 1; l < limit; l++)
        {
            var pk = new List<(long w, List<int> m)>();
            for (int i = 0; i + 1 < cur.Count; i += 2) pk.Add((cur[i].w + cur[i + 1].w, cur[i].m.Concat(cur[i + 1].m).ToList()));
            cur = leaves.Concat(pk).OrderBy(x => x.w).ToList();
        }
        foreach (var (_, m) in cur.Take(2 * syms.Count - 2))
            foreach (var s in m) res[s]++;
        return res;
    }

    static int[] Canon(int[] len)
    {
        int max = len.Max();
        var count = new int[max + 2];
        foreach (var l in len) if (l > 0) count[l]++;
        var next = new int[max + 2];
        int code = 0;
        for (int b = 1; b <= max; b++) { code = (code + count[b - 1]) << 1; next[b] = code; }
        var codes = new int[len.Length];
        for (int s = 0; s < len.Length; s++) if (len[s] > 0) codes[s] = next[len[s]]++;
        return codes;
    }

    static void Block(Bits w, List<(int v, bool m)> toks, bool final, bool dynamic)
    {
        var freq = new long[286];
        foreach (var (v, m) in toks) freq[m ? LenCode(v).sym : v]++;
        freq[256]++;
        var dfreq = new long[30];
        dfreq[0] = toks.Count(t => t.m);
        w.Put(final ? 1u : 0u, 1);
        int[] ll, dl;
        if (!dynamic)
        {
            w.Put(1, 2);
            ll = Enumerable.Repeat(8, 144).Concat(Enumerable.Repeat(9, 112)).Concat(Enumerable.Repeat(7, 24)).Concat(Enumerable.Repeat(8, 8)).ToArray();
            dl = Enumerable.Repeat(5, 30).ToArray();
        }
        else
        {
            w.Put(2, 2);
            ll = Lengths(freq, 15);
            dl = Lengths(dfreq, 15);
            if (dl.All(x => x == 0)) dl[0] = 1;
            int hlit = Math.Max(257, Array.FindLastIndex(ll, x => x > 0) + 1);
            int hdist = Math.Max(1, Array.FindLastIndex(dl, x => x > 0) + 1);
            var seq = ll.Take(hlit).Concat(dl.Take(hdist)).ToArray();
            var rle = new List<(int s, int e, int eb)>();
            for (int i = 0; i < seq.Length;)
            {
                int v = seq[i], j = i;
                while (j < seq.Length && seq[j] == v) j++;
                int r = j - i;
                if (v == 0)
                {
                    while (r >= 11) { int k = Math.Min(138, r); rle.Add((18, k - 11, 7)); r -= k; }
                    while (r >= 3) { int k = Math.Min(10, r); rle.Add((17, k - 3, 3)); r -= k; }
                    for (; r > 0; r--) rle.Add((0, 0, 0));
                }
                else
                {
                    rle.Add((v, 0, 0)); r--;
                    while (r >= 3) { int k = Math.Min(6, r); rle.Add((16, k - 3, 2)); r -= k; }
                    for (; r > 0; r--) rle.Add((v, 0, 0));
                }
                i = j;
            }
            var cfreq = new long[19];
            foreach (var x in rle) cfreq[x.s]++;
            var cl = Lengths(cfreq, 7);
            int hclen = 4;
            for (int k = 0; k < 19; k++) if (cl[ClOrder[k]] > 0) hclen = Math.Max(hclen, k + 1);
            w.Put((uint)(hlit - 257), 5);
            w.Put((uint)(hdist - 1), 5);
            w.Put((uint)(hclen - 4), 4);
            for (int k = 0; k < hclen; k++) w.Put((uint)cl[ClOrder[k]], 3);
            var cc = Canon(cl);
            foreach (var (s, e, eb) in rle) { w.Huff(cc[s], cl[s]); if (eb > 0) w.Put((uint)e, eb); }
        }
        var lc = Canon(ll); var dc = Canon(dl);
        foreach (var (v, m) in toks)
        {
            if (!m) { w.Huff(lc[v], ll[v]); continue; }
            var (sym, extra, eb) = LenCode(v);
            w.Huff(lc[sym], ll[sym]);
            if (eb > 0) w.Put((uint)extra, eb);
            w.Huff(dc[0], dl[0]);
        }
        w.Huff(lc[256], ll[256]);
    }

    static byte[] Build(byte[] d, int split, bool dynA, bool dynB, int padLiterals, int storedPads)
    {
        var w = new Bits();
        for (int i = 0; i < storedPads; i++) { w.Put(0, 3); w.Align(); w.Put(0, 16); w.Put(0xFFFF, 16); }
        if (split <= 0) Block(w, Tokens(d, 0, d.Length, padLiterals), true, dynB);
        else
        {
            Block(w, Tokens(d, 0, split, 0), false, dynA);
            Block(w, Tokens(d, split, d.Length, padLiterals), true, dynB);
        }
        return w.Finish();
    }
}
