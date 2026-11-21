// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
// Additional terms under section 7 of the GPL apply, see the NOTICE file.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using LibProsperoPkg;
using LibProsperoPkg.PFS;
using LibProsperoPkg.PKG;
using LibProsperoPkg.PlayGo;

namespace PkgTool;

// a dump's files against the file list of the original package: Sony's playgo-hash-table.dat holds
// one hash per path of the retail image. Names of missing files are not in it, only their count
internal static partial class Program
{
    internal sealed class CompletenessDoc
    {
        public bool checkable { get; set; }
        public string reason { get; set; } = "";
        public int listed { get; set; }            // files in the original package's list
        public int files { get; set; }             // files of the dump that the list could hold
        public int missing { get; set; }           // listed, not in the dump
        public int extra_count { get; set; }       // in the dump, not listed
        public long extra_bytes { get; set; }
        public long leftover_bytes { get; set; }   // of those, under a dump tool's decrypted/
        public List<string> extra { get; set; } = new();   // the first ones, by path
    }

    // the right.sprx the builder adds itself when a dump lacks it
    const string AddedByBuilder = "/sce_sys/about/right.sprx";

    internal static CompletenessDoc Completeness(IEnumerable<(string Rel, long Size)> files, byte[]? table)
    {
        var doc = new CompletenessDoc();
        if (table == null) { doc.reason = "no playgo-hash-table.dat from the original package"; return doc; }
        if (!LooksLikePlayGoHashTable(table)) { doc.reason = "playgo-hash-table.dat has an unknown format"; return doc; }
        uint count = U32(table, 36);
        if (56L + count * 8L != table.Length) { doc.reason = "playgo-hash-table.dat is damaged"; return doc; }
        var listed = new HashSet<ulong>();
        for (int i = 0; i < count; i++)
            listed.Add(System.Buffers.Binary.BinaryPrimitives.ReadUInt64LittleEndian(table.AsSpan(56 + i * 8, 8)));
        var present = new HashSet<ulong>();
        var extra = new List<(string Rel, long Size)>();
        foreach (var (raw, size) in files)
        {
            var rel = "/" + raw.Replace('\\', '/').Trim('/');
            if (FsJunk.PathHasJunk(rel)) continue;                       // OS junk is never read
            if (rel.StartsWith("/sce_sys/", StringComparison.Ordinal))
            {
                var t = rel["/sce_sys/".Length..];
                if (t == "param.json" || EntryNames.NameToId.ContainsKey(t)) continue;   // package entries, not image files
            }
            var h = ProsperoPs5FlatPathTable.HashPath(rel);
            present.Add(h);
            doc.files++;
            if (!listed.Contains(h)) extra.Add((rel.TrimStart('/'), size));
        }
        var added = ProsperoPs5FlatPathTable.HashPath(AddedByBuilder);
        doc.checkable = true;
        doc.listed = (int)count;
        doc.missing = listed.Count(h => !present.Contains(h) && h != added);
        doc.extra_count = extra.Count;
        doc.extra_bytes = extra.Sum(e => e.Size);
        doc.leftover_bytes = extra.Where(e => e.Rel.StartsWith("decrypted/", StringComparison.OrdinalIgnoreCase)).Sum(e => e.Size);
        doc.extra = extra.OrderBy(e => e.Rel, StringComparer.Ordinal).Take(50).Select(e => e.Rel).ToList();
        return doc;
    }

    // completeness <folder|.ffpfs|.ffpfsc|.pkg> [--passcode X]: JSON on stdout
    static int CmdCompleteness(string[] args)
    {
        if (args.Length < 2) return Bad("completeness needs a game folder, .ffpfs, .ffpfsc or .pkg");
        var src = args[1];
        string passcode = new string('0', 32);
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i] == "--passcode") passcode = Need(args, ref i, "--passcode");
            else return Bad("unknown completeness flag: " + args[i]);
        }
        CompletenessDoc doc;
        if (Directory.Exists(src))
        {
            var root = Path.GetFullPath(src);
            var files = Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories)
                .Select(f => (Path.GetRelativePath(root, f), new FileInfo(f).Length)).ToList();
            var t = Path.Combine(root, "sce_sys", "playgo-hash-table.dat");
            doc = Completeness(files, File.Exists(t) ? File.ReadAllBytes(t) : null);
        }
        else if (SourceImage.IsImage(src))
        {
            var (files, table) = SourceImage.FilesAndHashTable(src);
            doc = Completeness(files, table);
        }
        else if (File.Exists(src) && src.EndsWith(".pkg", StringComparison.OrdinalIgnoreCase))
        {
            var errors = new List<string>();
            var tmpCnt = Path.Combine(RealPath(Path.GetTempPath()), "fpkg-complete-cnt-" + Guid.NewGuid().ToString("N"));
            try
            {
                using var img = new InnerImage(src, passcode);
                var cnt = LiftCntSceSys(src, passcode, tmpCnt, errors);
                var tree = BuildInnerTree(img, cnt);
                var files = tree.Values.Where(e => e.type == "file" && e.source == "pfs")
                                .Select(e => (e.path, e.size ?? 0L)).ToList();
                var table = cnt.TryGetValue("sce_sys/playgo-hash-table.dat", out var tp) && File.Exists(tp) ? File.ReadAllBytes(tp) : null;
                doc = Completeness(files, table);
            }
            finally
            {
                try { if (Directory.Exists(tmpCnt)) Directory.Delete(tmpCnt, recursive: true); } catch { }
            }
        }
        else return Bad("not a game folder, .ffpfs, .ffpfsc or .pkg: " + src);
        Console.WriteLine(JsonSerializer.Serialize(doc, PkgToolJsonContext.Default.CompletenessDoc));
        return 0;
    }
}
