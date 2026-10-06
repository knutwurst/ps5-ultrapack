using System;
using System.Collections.Generic;
using System.IO;
using System.IO.MemoryMappedFiles;
using System.Linq;
using System.Text.Json;
using Orbis = LibOrbisPkg;

namespace PkgTool;

/// <summary>
/// PS4 packages (title ids CUSA…), read with LibOrbisPkg (vendored, LGPL-3): ps4-list prints
/// the inner files as the same ListInnerDoc JSON that list-inner prints for a PS5 package;
/// ps4-extract writes all or the listed files and folders with "[####] N% extract" lines.
/// Only fake packages open: their PFS key is recovered with the library's fake keyset. A
/// package that does not open that way (a retail package) fails with a plain message.
/// </summary>
internal static partial class Program
{
    sealed class Ps4Image : IDisposable
    {
        public readonly MemoryMappedFile Mmf;
        public readonly MemoryMappedViewAccessor Outer;
        public readonly Orbis.PFS.PfsReader Inner;
        /// <summary>The package-header entries shown under sce_sys/ (param.sfo, icon0.png, …):
        /// named, not encrypted, not bookkeeping ('.digests' and the like) and no license.</summary>
        public readonly SortedDictionary<string, Orbis.PKG.MetaEntry> Cnt = new(StringComparer.Ordinal);
        public Ps4Image(string path)
        {
            Mmf = MemoryMappedFile.CreateFromFile(path, FileMode.Open, null, 0, MemoryMappedFileAccess.Read);
            Orbis.PKG.Pkg pkg;
            using (var s = Mmf.CreateViewStream(0, 0, MemoryMappedFileAccess.Read))
                pkg = new Orbis.PKG.PkgReader(s).ReadPkg();
            if (pkg.Header.content_id.Length < 16 || pkg.Header.content_id.Substring(7, 4) != "CUSA")
                throw new InvalidDataException("not a PS4 package (no CUSA title id in the content id)");
            if (pkg.Header.pfs_image_size == 0)
                throw new InvalidDataException("the package has no content image");
            byte[] ekpfs;
            try { ekpfs = pkg.GetEkpfs(); }
            catch (Exception e) { throw new InvalidDataException("not a fake package; its contents cannot be read (" + e.Message + ")"); }
            foreach (var m in pkg.Metas.Metas)
            {
                if (m.Encrypted || m.DataSize == 0) continue;
                if (!Orbis.PKG.EntryNames.IdToName.TryGetValue(m.id, out var name) || string.IsNullOrEmpty(name)) continue;
                if (name.StartsWith(".", StringComparison.Ordinal) || name.StartsWith("license.", StringComparison.Ordinal)) continue;
                Cnt["sce_sys/" + name.Replace('\\', '/').TrimStart('/')] = m;
            }
            Outer = Mmf.CreateViewAccessor((long)pkg.Header.pfs_image_offset, (long)pkg.Header.pfs_image_size, MemoryMappedFileAccess.Read);
            try
            {
                var outer = new Orbis.PFS.PfsReader(Outer, pkg.Header.pfs_flags, ekpfs);
                var image = outer.GetFile("pfs_image.dat") ?? throw new InvalidDataException("no pfs_image.dat in the package");
                Inner = new Orbis.PFS.PfsReader(new Orbis.PFS.PFSCReader(image.GetView()));
            }
            catch (InvalidDataException) { throw; }
            catch (Exception e) { throw new InvalidDataException("not a fake package; its contents cannot be read (" + e.Message + ")"); }
        }
        public void Dispose() { Outer?.Dispose(); Mmf?.Dispose(); }
    }

    /// <summary>"/uroot/dir/file" → "dir/file".</summary>
    static string Ps4Rel(Orbis.PFS.PfsReader.Node n)
    {
        var full = n.FullName.Replace('\\', '/');
        const string root = "/uroot";
        if (full.StartsWith(root + "/", StringComparison.Ordinal)) full = full.Substring(root.Length + 1);
        else if (full == root) full = "";
        return full.TrimStart('/');
    }

    static long Ps4Size(Orbis.PFS.PfsReader.File f) => f.size != f.compressed_size ? f.compressed_size : f.size;

    static IEnumerable<Orbis.PFS.PfsReader.Node> Ps4Walk(Orbis.PFS.PfsReader.Dir d)
    {
        foreach (var n in d.children)
        {
            yield return n;
            if (n is Orbis.PFS.PfsReader.Dir sub)
                foreach (var x in Ps4Walk(sub)) yield return x;
        }
    }

    static int CmdPs4List(string[] args)
    {
        if (args.Length != 2) return Bad("ps4-list needs <ps4.pkg>");
        if (!File.Exists(args[1])) throw new FileNotFoundException("package not found", args[1]);
        using var img = new Ps4Image(args[1]);
        var entries = new List<InnerEntry>();
        foreach (var n in Ps4Walk(img.Inner.GetURoot()))
        {
            var rel = SafeRelative(Ps4Rel(n));
            if (n is Orbis.PFS.PfsReader.File f)
                entries.Add(new InnerEntry { path = rel, type = "file", size = Ps4Size(f), source = "pfs" });
            else
                entries.Add(new InnerEntry { path = rel, type = "dir" });
        }
        var byPath = entries.ToDictionary(e => e.path, StringComparer.Ordinal);
        foreach (var (rel, m) in img.Cnt)
        {
            for (int i = rel.IndexOf('/'); i > 0; i = rel.IndexOf('/', i + 1))
                if (!byPath.ContainsKey(rel[..i])) byPath[rel[..i]] = new InnerEntry { path = rel[..i], type = "dir" };
            byPath[rel] = new InnerEntry { path = rel, type = "file", size = m.DataSize, source = "cnt" };
        }
        entries = byPath.Values.ToList();
        entries.Sort((a, b) => string.CompareOrdinal(a.path, b.path));
        var doc = new ListInnerDoc
        {
            root = Path.GetFileName(args[1]),
            entries = entries,
            file_count = entries.Count(e => e.type == "file"),
            dir_count = entries.Count(e => e.type == "dir"),
        };
        Console.WriteLine(JsonSerializer.Serialize(doc, PkgToolJsonContext.Default.ListInnerDoc));
        return 0;
    }

    static int CmdPs4Extract(string[] args)
    {
        if (args.Length < 3) return Bad("ps4-extract needs <ps4.pkg> <out-dir>");
        var pkgPath = args[1]; var outDir = args[2];
        string? membersFile = null;
        for (int i = 3; i < args.Length; i++)
        {
            if (args[i] == "--members") membersFile = Need(args, ref i, "--members");
            else return Bad("unknown ps4-extract flag: " + args[i]);
        }
        if (!File.Exists(pkgPath)) throw new FileNotFoundException("package not found", pkgPath);
        var wanted = membersFile == null ? null
            : File.ReadAllLines(membersFile).Select(l => l.Replace('\\', '/').Trim().Trim('/')).Where(l => l.Length > 0).ToHashSet();
        if (wanted != null && wanted.Count == 0) return Bad("--members file lists no paths");
        bool Under(string rel) => wanted == null || wanted.Any(m => rel == m || rel.StartsWith(m + "/", StringComparison.Ordinal));

        using var img = new Ps4Image(pkgPath);
        var cnt = img.Cnt.Where(kv => Under(kv.Key)).ToList();
        var nodes = Ps4Walk(img.Inner.GetURoot()).Select(n => (n, rel: SafeRelative(Ps4Rel(n))))
                    .Where(x => Under(x.rel) && !img.Cnt.ContainsKey(x.rel)).ToList();
        if (nodes.Count == 0 && cnt.Count == 0) { Console.Error.WriteLine("[ERROR] None of the requested items were found in the package."); return 1; }
        Directory.CreateDirectory(outDir);
        foreach (var (n, rel) in nodes.Where(x => x.n is Orbis.PFS.PfsReader.Dir))
            Directory.CreateDirectory(SafeTarget(outDir, rel));
        var files = nodes.Where(x => x.n is Orbis.PFS.PfsReader.File).Select(x => ((Orbis.PFS.PfsReader.File)x.n, x.rel)).ToList();
        long total = files.Sum(x => Ps4Size(x.Item1)), done = 0;
        int lastPct = -1;
        var buf = new byte[1 << 20];
        foreach (var (f, rel) in files)
        {
            var dst = SafeTarget(outDir, rel);
            Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
            Orbis.Util.IMemoryReader view = f.GetView();
            if (f.size != f.compressed_size) view = new Orbis.PFS.PFSCReader(view);
            long size = Ps4Size(f), pos = 0;
            using (var o = new FileStream(dst, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20))
            {
                while (pos < size)
                {
                    int n = (int)Math.Min(buf.Length, size - pos);
                    view.Read(pos, buf, 0, n);
                    o.Write(buf, 0, n);
                    pos += n; done += n;
                    int pct = total > 0 ? (int)Math.Min(99, done * 100 / total) : 99;
                    if (pct != lastPct) { lastPct = pct; Console.WriteLine($"[####] {pct}% extract ({rel})"); }
                }
            }
        }
        using (var acc = img.Mmf.CreateViewAccessor(0, 0, MemoryMappedFileAccess.Read))
            foreach (var (rel, m) in cnt)
            {
                var dst = SafeTarget(outDir, rel);
                Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
                var data = new byte[m.DataSize];
                acc.ReadArray(m.DataOffset, data, 0, data.Length);
                File.WriteAllBytes(dst, data);
            }
        Console.WriteLine("[####] 100% extract");
        Console.WriteLine($"[ok] extracted {files.Count + cnt.Count} file(s) to {outDir}");
        return 0;
    }
}
