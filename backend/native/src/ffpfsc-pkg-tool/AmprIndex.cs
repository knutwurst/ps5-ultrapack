using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace PkgTool;

/// <summary>
/// Writes /app0/ampr_emu.index (AMPRIDX3), the file table the AMPR emulator
/// (fakelib/libSceAmpr.sprx) resolves APR file ids through.
///
/// Byte layout and key rules follow drakmor's reference builder
/// (ampr_emu tools/build_ampr_index.py), which matches the emulator runtime:
///   header  &lt;8sIIQQQII&gt;  magic "AMPRIDX3", version 3, record size 24, record count,
///                         path-blob length, hash-table offset, slot size 16, slot count
///   records &lt;IIQq&gt;       path-blob offset, path length, size, mtime (sorted by key)
///   blob                  "/app0/&lt;rel&gt;\0" per record
///   slots   &lt;QII&gt;        FNV-1a-64 of the key, record index + 1, duplicate flag;
///                         open addressing with linear probing, power-of-two table ≥ 2n
/// The key is the UTF-8 path with '\' → '/' and only ASCII A–Z folded to lower case.
/// Skipped: the index itself, the emulator's own trace/log files, paths containing
/// tab/CR/LF, and OS metadata (the same names MkPFS keeps out of images).
/// </summary>
internal static class AmprIndex
{
    public const string FileName = "ampr_emu.index";

    /// <summary>sce_sys files a package build must not index: the builder never packs
    /// ext_info.dat, and it (re)creates the PlayGo set and the license at package time, so their
    /// final bytes are not known when the index is written. Leaving them out makes the index
    /// the same whether the source carried them or not; the emulator serves game data, never
    /// these.</summary>
    static readonly HashSet<string> PackageTimeFiles = new(StringComparer.Ordinal)
    {
        "/app0/sce_sys/ext_info.dat", "/app0/sce_sys/license.dat", "/app0/sce_sys/license.info",
        "/app0/sce_sys/playgo-chunk.dat", "/app0/sce_sys/playgo-ficm.dat",
        "/app0/sce_sys/playgo-hash-table.dat", "/app0/sce_sys/playgo-manifest.xml",
    };

    static bool IsIgnored(string name) => FsJunk.IsJunkName(name);

    static byte[] Key(string path)
    {
        var raw = Encoding.UTF8.GetBytes(path.Replace('\\', '/'));
        for (int i = 0; i < raw.Length; i++)
            if (raw[i] >= (byte)'A' && raw[i] <= (byte)'Z') raw[i] = (byte)(raw[i] + 0x20);
        return raw;
    }

    static int CompareBytes(byte[] a, byte[] b)
    {
        int n = Math.Min(a.Length, b.Length);
        for (int i = 0; i < n; i++)
            if (a[i] != b[i]) return a[i].CompareTo(b[i]);
        return a.Length.CompareTo(b.Length);
    }

    static ulong Fnv1a64(byte[] key)
    {
        ulong h = 1469598103934665603UL;
        foreach (var b in key) { h ^= b; h *= 1099511628211UL; }
        return h == 0 ? 1UL : h;
    }

    sealed record Row(long Size, long Mtime, string Path, byte[] Key);

    /// <summary>Rebuild &lt;root&gt;/ampr_emu.index over the files under <paramref name="root"/>.
    /// Written to a temporary name and renamed into place, so an index that is a hard link
    /// into the user's source is replaced, never written through. Returns the record count
    /// (0 = nothing indexed, no file written).
    /// <paramref name="fixedMtime"/>: record this mtime for every file instead of the file's
    /// own. A package gives every file the same inode time (the build timestamp), so that is
    /// what the console reports for the installed files — and it keeps builds reproducible.
    /// <paramref name="packageView"/>: index the folder as the package will present it (skip
    /// <see cref="PackageTimeFiles"/>); off for a plain folder, which then matches the reference
    /// builder byte for byte.</summary>
    public static int Write(string root, long? fixedMtime = null, bool packageView = false)
    {
        root = Path.GetFullPath(root);
        var output = Path.Combine(root, FileName);
        var tmp = output + ".tmp";
        var rows = new List<Row>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        Walk(root, root, rows, seen, output, tmp, packageView);
        if (rows.Count == 0) return 0;
        if (fixedMtime is long fm)
            for (int i = 0; i < rows.Count; i++) rows[i] = rows[i] with { Mtime = fm };

        rows.Sort((x, y) => CompareBytes(x.Key, y.Key));

        var blob = new MemoryStream();
        var records = new MemoryStream();
        using (var rw = new BinaryWriter(records, Encoding.UTF8, leaveOpen: true))
        {
            foreach (var r in rows)
            {
                var enc = Encoding.UTF8.GetBytes(r.Path);
                if (blob.Length + enc.Length > uint.MaxValue)
                    throw new InvalidDataException("AMPR index path blob is too large");
                rw.Write((uint)blob.Length);
                rw.Write((uint)enc.Length);
                rw.Write((ulong)r.Size);
                rw.Write(r.Mtime);
                blob.Write(enc);
                blob.WriteByte(0);
            }
        }

        long slotCount = 2;
        while (slotCount < rows.Count * 2L) slotCount <<= 1;
        var slotHash = new ulong[slotCount];
        var slotIndex = new uint[slotCount];
        var slotFlags = new uint[slotCount];
        long mask = slotCount - 1;
        for (int i = 0; i < rows.Count; i++)
        {
            ulong h = Fnv1a64(rows[i].Key);
            long pos = (long)(h & (ulong)mask);
            bool duplicate = false;
            while (slotIndex[pos] != 0)
            {
                if (slotHash[pos] == h) { slotFlags[pos] |= 1; duplicate = true; }
                pos = (pos + 1) & mask;
            }
            slotHash[pos] = h;
            slotIndex[pos] = (uint)(i + 1);
            slotFlags[pos] = duplicate ? 1u : 0u;
        }

        const int HeaderSize = 8 + 4 + 4 + 8 + 8 + 8 + 4 + 4;   // 48
        long pathEnd = HeaderSize + records.Length + blob.Length;
        long hashOffset = (pathEnd + 15) & ~15L;

        using (var fs = new FileStream(tmp, FileMode.Create, FileAccess.Write, FileShare.None))
        using (var w = new BinaryWriter(fs))
        {
            w.Write(Encoding.ASCII.GetBytes("AMPRIDX3"));
            w.Write(3u);
            w.Write(24u);
            w.Write((ulong)rows.Count);
            w.Write((ulong)blob.Length);
            w.Write((ulong)hashOffset);
            w.Write(16u);
            w.Write((uint)slotCount);
            w.Write(records.ToArray());
            w.Write(blob.ToArray());
            for (long p = pathEnd; p < hashOffset; p++) w.Write((byte)0);
            for (long s = 0; s < slotCount; s++)
            {
                w.Write(slotHash[s]);
                w.Write(slotIndex[s]);
                w.Write(slotFlags[s]);
            }
        }
        File.Move(tmp, output, overwrite: true);
        return rows.Count;
    }

    // Top-down, like os.walk: this directory's files (sorted by key), then each
    // subdirectory (sorted by key) in turn. Directory symlinks are not followed.
    static void Walk(string root, string dir, List<Row> rows, HashSet<string> seen, string output, string tmp, bool packageView)
    {
        var files = new List<string>();
        var dirs = new List<string>();
        foreach (var e in new DirectoryInfo(dir).EnumerateFileSystemInfos())
        {
            if (IsIgnored(e.Name)) continue;
            if ((e.Attributes & FileAttributes.Directory) != 0)
            {
                if (e.LinkTarget == null) dirs.Add(e.FullName);
            }
            else files.Add(e.FullName);
        }
        Comparison<string> byKey = (a, b) => CompareBytes(Key(Path.GetFileName(a)), Key(Path.GetFileName(b)));
        files.Sort(byKey);
        dirs.Sort(byKey);

        foreach (var f in files)
        {
            string full = Path.GetFullPath(f);
            if (full == output || full == tmp) continue;
            string rel = Path.GetRelativePath(root, full).Replace('\\', '/');
            string indexed = "/app0/" + rel;
            string lower = indexed.ToLowerInvariant();
            if (lower == "/app0/ampr_commands.bin" || lower == "/app0/apr_emu.log") continue;
            if (packageView && PackageTimeFiles.Contains(lower)) continue;
            if (indexed.IndexOfAny(new[] { '\t', '\n', '\r' }) >= 0) continue;
            FileInfo fi;
            try
            {
                fi = new FileInfo(full);
                if (fi.LinkTarget != null)
                {
                    var target = fi.ResolveLinkTarget(returnFinalTarget: true) as FileInfo;
                    if (target == null || !target.Exists) continue;
                    fi = target;
                }
                if (!fi.Exists) continue;
            }
            catch (IOException) { continue; }
            catch (UnauthorizedAccessException) { continue; }
            var key = Key(indexed);
            var keyStr = Convert.ToHexString(key);
            if (!seen.Add(keyStr)) continue;   // case-insensitive collision: keep the first
            long mtime = new DateTimeOffset(fi.LastWriteTimeUtc).ToUnixTimeSeconds();
            rows.Add(new Row(fi.Length, mtime, indexed, key));
        }
        foreach (var d in dirs) Walk(root, d, rows, seen, output, tmp, packageView);
    }
}
